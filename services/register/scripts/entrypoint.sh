#!/usr/bin/env bash
# Container entrypoint. Subcommands:
#   serve      run the API (gunicorn + uvicorn workers)   [default]
#   migrate    apply database migrations then exit
#   seed       load reference data + ATLAS mock then exit
#   migrate-serve  migrate, then serve
set -euo pipefail

WORKERS="${REGISTER_WEB_CONCURRENCY:-4}"
BIND="${REGISTER_HOST:-0.0.0.0}:${REGISTER_PORT:-8000}"

run_migrate() {
  echo "[entrypoint] applying migrations..."
  alembic upgrade head
  # Converge the RLS posture every deploy (idempotent): create/refresh the register_app
  # runtime role and (re)assert FORCE to match REGISTER_ENFORCE_RLS — so flipping the flag
  # takes effect without hand-editing the database.
  echo "[entrypoint] converging RLS posture (register_app + FORCE)..."
  python -m app.db.apply_rls
}

run_seed() {
  echo "[entrypoint] seeding reference data + ATLAS mock (HTML prototype dataset)..."
  python -m app.seed
}

run_bootstrap() {
  # Provision an empty-but-usable DB: the default tenant + reference vocabularies only.
  # No business data. This is what makes tenant-scoped requests work on a fresh DB.
  echo "[entrypoint] bootstrapping tenant + reference data (no business data)..."
  python -m app.seed.bootstrap
}

run_import_mis() {
  local f="${REGISTER_MIS_XLSX:-data/Evam_ATLAS_MIS_Consolidated_v4.xlsx}"
  if [ ! -f "$f" ]; then
    # Real MIS data is not shipped in the image. If nobody mounted/copied it in,
    # do NOT inject synthetic mock data — leave the DB empty and let the operator
    # load on demand (upload API or `docker cp` + this command).
    echo "[entrypoint] MIS xlsx not found ($f) — leaving the database empty."
    echo "[entrypoint] Load on demand: POST /v1/import/atlas-xlsx, or copy the file in"
    echo "[entrypoint] and run: python -m app.seed.xlsx_cli <path>"
    return
  fi
  # Load only if the DB is empty, so restarts never wipe live edits.
  # Set REGISTER_IMPORT_FORCE=true to reload (replace) on purpose.
  local flag="--if-empty"
  [ "${REGISTER_IMPORT_FORCE:-false}" = "true" ] && flag=""
  echo "[entrypoint] importing the ATLAS MIS spreadsheet: $f ${flag:-(force replace)}..."
  python -m app.seed.xlsx_cli "$f" $flag
}

run_serve() {
  echo "[entrypoint] starting gunicorn ($WORKERS workers) on $BIND"
  exec gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind "$BIND" \
    --timeout "${REGISTER_WORKER_TIMEOUT:-60}" \
    --graceful-timeout 30 \
    --max-requests "${REGISTER_MAX_REQUESTS:-10000}" \
    --max-requests-jitter 1000 \
    --access-logfile - \
    --error-logfile -
}

case "${1:-serve}" in
  serve)          run_serve ;;
  migrate)        run_migrate ;;
  seed)           run_seed ;;
  bootstrap)      run_bootstrap ;;
  import-mis)     run_import_mis ;;
  migrate-seed)   run_migrate; run_seed ;;
  migrate-serve)  run_migrate; run_serve ;;
  migrate-bootstrap-serve) run_migrate; run_bootstrap; run_serve ;;
  migrate-seed-serve)      run_migrate; run_seed; run_serve ;;
  migrate-import-serve)    run_migrate; run_import_mis; run_serve ;;
  *)              exec "$@" ;;
esac
