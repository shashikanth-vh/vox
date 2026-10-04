#!/usr/bin/env bash
# One-off register fixes, run inside the postgres container of the running
# compose project (found the way prism-deploy.sh / reconcile.sh find it).
#
#   sudo ./prism/deploy/fixes/run.sh recompute_deal_flags            # dry run
#   sudo ./prism/deploy/fixes/run.sh recompute_deal_flags apply      # write
#   sudo ./prism/deploy/fixes/run.sh attach_orphan_lines [apply]
#   sudo ./prism/deploy/fixes/run.sh purge_test_companies [apply] ["%test compan%"]
#
# Every script prints what it would change first; nothing is written without
# the word "apply". Deletes are the register's own soft delete (restorable).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
NAME="${1:?which fix? one of: $(ls "$HERE"/*.sql | xargs -n1 basename | sed 's/\.sql$//' | tr '\n' ' ')}"
SQL="$HERE/$NAME.sql"; [[ -f "$SQL" ]] || { echo "no such fix: $NAME" >&2; exit 1; }
MODE="${2:-dry}"; PATTERN="${3:-}"
detect_project() {
  local cid; cid="$(docker ps -q --filter 'label=com.docker.compose.project' | head -1)"
  if [[ -n "$cid" ]]; then docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid"; else echo compose; fi
}
PROJECT="${PRISM_PROJECT:-$(detect_project)}"
COMPOSE="$TREE/deploy/compose/docker-compose.yml"; [[ -f "$COMPOSE" ]] || { echo "missing $COMPOSE" >&2; exit 1; }
ARGS=(-v ON_ERROR_STOP=1)
[[ "$MODE" == "apply" ]] && ARGS+=(-v apply=1)
[[ -n "$PATTERN" ]] && ARGS+=(-v "pattern='$PATTERN'")
echo "# $NAME · project $PROJECT · $( [[ "$MODE" == apply ]] && echo APPLY || echo DRY RUN ) · $(date -u +'%Y-%m-%d %H:%M UTC')"
docker compose -p "$PROJECT" -f "$COMPOSE" exec -T postgres psql -U prism -d register "${ARGS[@]}" < "$SQL"
