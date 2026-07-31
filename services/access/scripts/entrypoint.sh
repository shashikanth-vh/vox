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
  # Production posture sets ACCESS_AUTO_SEED=false: seeding then happens ONLY through the
  # explicit versioned command (python -m app.seed); the container start prints the drift
  # report instead of writing.
  if [ "${ACCESS_AUTO_SEED:-true}" = "true" ]; then
    echo "[entrypoint] seeding tenant + access matrix + admin user..."
    python -m app.seed
  else
    echo "[entrypoint] auto-seed disabled (ACCESS_AUTO_SEED=false) — drift report only:"
    python -m app.seed --check || true
  fi
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
