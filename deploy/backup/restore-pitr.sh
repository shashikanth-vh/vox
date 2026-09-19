#!/bin/bash
# Point-in-time restore of the PRISM database from the live backup in S3.
#
#   sudo ./restore-pitr.sh "2026-09-19 10:30:00+00"        # UTC target time
#
# What it does, in order — each step announced, the destructive one confirmed:
#   1. picks the newest base backup taken BEFORE the target time;
#   2. downloads every WAL segment into the walarchive volume (postgres replays
#      from there with plain cp — no cloud tooling inside the DB container);
#   3. stops the stack, tars the CURRENT pgdata into the pgbackups volume as a
#      safety snapshot, then empties pgdata;
#   4. extracts the base, writes recovery settings (recovery_target_time,
#      promote on reach), starts postgres alone and waits for promotion;
#   5. starts the full stack.
#
# Uses the same .env as the stack (LIVEBACKUP_* + LIVEBACKUP_AWS_*). The MinIO
# mirror is objects-only and restores separately: `aws s3 sync s3://…/minio-live/
# <dir>` back into the miniodata volume when documents also need rewinding.
set -euo pipefail

TARGET="${1:-}"
PROJECT="${PRISM_PROJECT:-compose}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENVFILE="$HERE/../compose/.env"

[ -n "$TARGET" ] || { echo "usage: $0 'YYYY-MM-DD HH:MM:SS+00' (UTC)"; exit 2; }
[ -f "$ENVFILE" ] || { echo "no $ENVFILE — run on the deployment box"; exit 2; }

get() { grep -E "^$1=" "$ENVFILE" | tail -1 | cut -d= -f2-; }
BUCKET="$(get LIVEBACKUP_S3_BUCKET)"; PREFIX="$(get LIVEBACKUP_S3_PREFIX)"; PREFIX="${PREFIX:-prism}"
AK="$(get LIVEBACKUP_AWS_ACCESS_KEY_ID)"; SK="$(get LIVEBACKUP_AWS_SECRET_ACCESS_KEY)"
RG="$(get LIVEBACKUP_AWS_REGION)"; RG="${RG:-ap-south-1}"
[ -n "$BUCKET" ] || { echo "LIVEBACKUP_S3_BUCKET not set in .env"; exit 2; }
S3="s3://$BUCKET/$PREFIX"
AWSENV=(-e "AWS_ACCESS_KEY_ID=$AK" -e "AWS_SECRET_ACCESS_KEY=$SK" -e "AWS_DEFAULT_REGION=$RG")
IMG=prism-livebackup
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "image $IMG missing — run one deploy with the backup profile first"; exit 2; }
aws_run() { docker run --rm "${AWSENV[@]}" "$@"; }

TARGET_COMPACT="$(date -u -d "$TARGET" +%Y%m%d-%H%M%S)" || { echo "unparseable target time"; exit 2; }
echo "[pitr] target: $TARGET (compact $TARGET_COMPACT)"

echo "[pitr] choosing base backup <= target..."
BASE="$(aws_run "$IMG" aws s3 ls "$S3/base/" | awk '{print $2}' | tr -d / | grep -v partial \
        | sort | awk -v t="$TARGET_COMPACT" '$0 <= t' | tail -1)"
[ -n "$BASE" ] || { echo "no base backup exists before the target — earliest recoverable point is the oldest base"; exit 1; }
echo "[pitr] base: $BASE"

echo "[pitr] downloading WAL history into the walarchive volume (idempotent)..."
aws_run -v "${PROJECT}_walarchive:/wal_archive" "$IMG" \
  aws s3 sync "$S3/wal/" /wal_archive/ --no-progress >/dev/null

echo
echo "  *** DESTRUCTIVE STEP AHEAD ***"
echo "  The current database will be tarred to the pgbackups volume, then REPLACED"
echo "  by base $BASE replayed to $TARGET."
read -r -p "  Type RESTORE to continue: " ok
[ "$ok" = "RESTORE" ] || { echo "aborted — nothing touched"; exit 1; }

echo "[pitr] stopping the stack..."
docker compose --profile sso --profile backup -p "$PROJECT" --project-directory "$HERE/../compose" stop

TS="$(date -u +%Y%m%d-%H%M%S)"
echo "[pitr] safety snapshot of current pgdata -> pgbackups/pre-pitr-$TS.tar.gz"
docker run --rm -v "${PROJECT}_pgdata:/data:ro" -v "${PROJECT}_pgbackups:/backups" "$IMG" \
  sh -c "tar -czf /backups/pre-pitr-$TS.tar.gz -C /data . && gzip -t /backups/pre-pitr-$TS.tar.gz"

echo "[pitr] emptying pgdata and laying down base $BASE..."
docker run --rm -v "${PROJECT}_pgdata:/data" "${AWSENV[@]}" "$IMG" sh -c "
  set -e
  rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null || true
  aws s3 cp '$S3/base/$BASE/base.tar.gz' /tmp/base.tar.gz --no-progress
  tar -xzf /tmp/base.tar.gz -C /data
  mkdir -p /data/pg_wal
  {
    echo \"restore_command = 'cp /wal_archive/%f %p'\"
    echo \"recovery_target_time = '$TARGET'\"
    echo \"recovery_target_action = 'promote'\"
  } >> /data/postgresql.auto.conf
  touch /data/recovery.signal
  chown -R 70:70 /data
"

echo "[pitr] starting postgres alone for replay..."
docker compose -p "$PROJECT" --project-directory "$HERE/../compose" up -d postgres
echo "[pitr] waiting for recovery to reach the target and promote..."
for i in $(seq 1 360); do
  if docker exec "${PROJECT}-postgres-1" sh -c 'test ! -f /var/lib/postgresql/data/recovery.signal' 2>/dev/null \
     && docker exec "${PROJECT}-postgres-1" pg_isready -U prism -d register >/dev/null 2>&1; then
    echo "[pitr] promoted and accepting connections."
    break
  fi
  sleep 5
  [ "$i" = 360 ] && { echo "[pitr] TIMED OUT — inspect: docker logs ${PROJECT}-postgres-1"; exit 1; }
done

echo "[pitr] starting the full stack..."
docker compose --profile sso --profile backup -p "$PROJECT" --project-directory "$HERE/../compose" up -d
echo "[pitr] DONE. Database is at $TARGET. Safety snapshot: pgbackups/pre-pitr-$TS.tar.gz"
echo "[pitr] NOTE: MinIO objects were not rewound (usually correct — documents are"
echo "[pitr] write-once). To rewind them too, restore minio-live/ from S3 versioning."
