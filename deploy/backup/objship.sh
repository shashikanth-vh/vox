#!/bin/sh
# Continuous object mirror + periodic aux-volume snapshots.
#
# * MinIO's data volume syncs to S3 every ~60s — objects are written by rename,
#   so a sync sees each object whole (old or new, never half); the in-flight
#   staging area (.minio.sys/tmp and multipart) is excluded. RPO ≈ one minute
#   for every document and recording. Turn VERSIONING ON on the S3 bucket so a
#   deletion (or ransomware on the box) never erases the mirrored history.
# * The small stateful volumes — vocx state (Google tokens, alias map),
#   pulse, dex — tar to S3 every 6 hours; the Whisper model cache is excluded
#   (re-downloadable, large).
set -u

if [ -z "${LIVEBACKUP_S3_BUCKET:-}" ]; then
  echo "[objship] LIVEBACKUP_S3_BUCKET is empty - live backup not configured on this box; idling."
  while true; do sleep 3600; done
fi

S3="s3://${LIVEBACKUP_S3_BUCKET}/${LIVEBACKUP_S3_PREFIX:-prism}"
VOL_EVERY_S="${LIVEBACKUP_VOLUMES_EVERY_S:-21600}"
KEEP_VOL="${LIVEBACKUP_KEEP_VOLUME_SNAPSHOTS:-12}"
FAILED=/backups/OBJSHIP_FAILED
fail() { echo "$(date -u +%Y%m%d-%H%M%S) $1" >> "$FAILED" 2>/dev/null; echo "[objship] FAILED: $1"; }

snap_volume() {  # name  mounted-path  [tar excludes...]
  name="$1"; path="$2"; shift 2
  ts="$(date -u +%Y%m%d-%H%M%S)"
  tmp="/tmp/$name-$ts.tar.gz"
  if tar -czf "$tmp" -C "$path" "$@" . 2>/dev/null && gzip -t "$tmp"; then
    if aws s3 cp "$tmp" "$S3/volumes/$name/$name-$ts.tar.gz" --no-progress >/dev/null 2>&1; then
      echo "[objship] volume $name snapshot uploaded ($ts)"
      # retention: keep the newest KEEP_VOL snapshots per volume
      list="$(aws s3 ls "$S3/volumes/$name/" 2>/dev/null | awk '{print $4}' | grep . | sort)"
      count="$(printf '%s\n' "$list" | grep -c . || true)"
      extra="$((count - KEEP_VOL))"
      if [ "$extra" -gt 0 ]; then
        printf '%s\n' "$list" | head -n "$extra" | while read -r old; do
          [ -n "$old" ] && aws s3 rm "$S3/volumes/$name/$old" --no-progress >/dev/null 2>&1
        done
      fi
    else fail "upload $name"; fi
  else fail "tar $name"; fi
  rm -f "$tmp"
}

echo "[objship] mirroring MinIO -> $S3/minio-live (60s cadence); volumes every ${VOL_EVERY_S}s"
last_vol=0
while true; do
  if aws s3 sync /data/minio "$S3/minio-live/" \
       --exclude ".minio.sys/tmp/*" --exclude ".minio.sys/multipart/*" \
       --no-progress >/dev/null 2>&1; then
    rm -f "$FAILED" 2>/dev/null
  else
    fail "minio sync"
  fi
  now="$(date +%s)"
  if [ "$((now - last_vol))" -ge "$VOL_EVERY_S" ]; then
    last_vol="$now"
    snap_volume vocx  /state/vocx  --exclude ./whisper --exclude './models*'
    snap_volume pulse /state/pulse
    snap_volume dex   /state/dex
  fi
  sleep 60
done
