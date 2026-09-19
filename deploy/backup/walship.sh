#!/bin/sh
# Continuous WAL shipping + nightly base backup => point-in-time recovery.
#
# Postgres (archive_mode=on) copies each finished WAL segment into the shared
# /wal_archive volume; this loop pushes the archive to S3 every ~20 seconds and
# takes one pg_basebackup per UTC day. Restore-to-any-second needs exactly two
# things in S3: a base backup taken BEFORE the target time, and every WAL
# segment after it — see restore-pitr.sh.
#
# Verified-or-nothing, like pgbackup: the base backup uploads to a .partial
# prefix and is promoted only after every part landed; failures stamp
# WALSHIP_FAILED in the archive volume (cleared by the next good base) so
# offsite/status tooling can surface a broken pipeline instead of trusting it.
set -u

if [ -z "${LIVEBACKUP_S3_BUCKET:-}" ]; then
  echo "[walship] LIVEBACKUP_S3_BUCKET is empty - live backup not configured on this box; idling."
  while true; do sleep 3600; done
fi

S3="s3://${LIVEBACKUP_S3_BUCKET}/${LIVEBACKUP_S3_PREFIX:-prism}"
KEEP_BASES="${LIVEBACKUP_KEEP_BASES:-8}"
LOCAL_KEEP_DAYS="${LIVEBACKUP_WAL_LOCAL_DAYS:-2}"
FAILED=/wal_archive/WALSHIP_FAILED
last_base_day=""

fail() { echo "$(date -u +%Y%m%d-%H%M%S) $1" >> "$FAILED"; echo "[walship] FAILED: $1"; }

prune_bases() {
  list="$(aws s3 ls "$S3/base/" 2>/dev/null | awk '{print $2}' | grep -v partial | sort)"
  count="$(printf '%s\n' "$list" | grep -c . || true)"
  extra="$((count - KEEP_BASES))"
  [ "$extra" -gt 0 ] || return 0
  printf '%s\n' "$list" | head -n "$extra" | while read -r old; do
    [ -n "$old" ] && aws s3 rm "$S3/base/$old" --recursive --no-progress >/dev/null 2>&1
  done
}

base_backup() {
  day="$1"; ts="$(date -u +%Y%m%d-%H%M%S)"
  work="/tmp/base-$ts"; rm -rf "$work"; mkdir -p "$work"
  echo "[walship] $ts taking base backup..."
  if ! pg_basebackup -h postgres -U "${PGUSER:-prism}" -D "$work" -Ft -z -X none; then
    fail "pg_basebackup"; rm -rf "$work"; return 1
  fi
  if aws s3 cp "$work" "$S3/base/.partial-$ts/" --recursive --no-progress >/dev/null 2>&1 \
     && aws s3 mv "$S3/base/.partial-$ts" "$S3/base/$ts" --recursive --no-progress >/dev/null 2>&1; then
    rm -f "$FAILED"
    last_base_day="$day"
    echo "[walship] base $ts uploaded"
    prune_bases
  else
    fail "base upload $ts"
    aws s3 rm "$S3/base/.partial-$ts" --recursive --no-progress >/dev/null 2>&1
  fi
  rm -rf "$work"
}

echo "[walship] shipping /wal_archive -> $S3/wal (base cadence: daily, keep $KEEP_BASES)"
while true; do
  if ! aws s3 sync /wal_archive "$S3/wal/" --exclude "WALSHIP_FAILED" \
       --no-progress >/dev/null 2>&1; then
    fail "wal sync"
  fi
  day="$(date -u +%Y%m%d)"
  [ "$day" != "$last_base_day" ] && base_backup "$day"
  # local window only — S3 holds the history; synced segments age out locally
  find /wal_archive -maxdepth 1 -type f ! -name 'WALSHIP_FAILED' -mtime "+$LOCAL_KEEP_DAYS" -delete
  sleep 20
done
