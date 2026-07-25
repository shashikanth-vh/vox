#!/usr/bin/env bash
# Container entrypoint. Subcommands:
#   serve      run the API (gunicorn + uvicorn workers)   [default]
#   migrate    apply database migrations then exit
#   seed       load reference data + ATLAS mock then exit
#   migrate-serve  migrate, then serve
set -euo pipefail

WORKERS="${ACCESS_WEB_CONCURRENCY:-4}"
BIND="${ACCESS_HOST:-0.0.0.0}:${ACCESS_PORT:-8000}"

run_migrate() {
  echo "[entrypoint] applying migrations..."
  alembic upgrade head
}

run_seed() {
  echo "[entrypoint] seeding tenant + access matrix + admin user..."
  python -m app.seed
}

run_serve() {
  echo "[entrypoint] starting gunicorn ($WORKERS workers) on $BIND"
  exec gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind "$BIND" \
    --timeout "${ACCESS_WORKER_TIMEOUT:-60}" \
    --graceful-timeout 30 \
    --max-requests "${ACCESS_MAX_REQUESTS:-10000}" \
    --max-requests-jitter 1000 \
    --access-logfile - \
    --error-logfile -
}

case "${1:-serve}" in
  serve)          run_serve ;;
  migrate)        run_migrate ;;
  seed)           run_seed ;;
  migrate-serve)  run_migrate; run_serve ;;
  migrate-seed-serve) run_migrate; run_seed; run_serve ;;
  *)              exec "$@" ;;
esac
