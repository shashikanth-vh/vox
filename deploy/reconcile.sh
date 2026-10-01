#!/usr/bin/env bash
# PRISM register tally — Leads, Deals, Lending, Syndication, Asset Monetisation
# against the Client Master. Read-only: prints the counts and every mismatch.
#
#   sudo ./prism/deploy/reconcile.sh                 # on the box, from the deploy root
#   sudo ./prism/deploy/reconcile.sh > tally.txt     # keep a copy
#   sudo PRISM_PROJECT=prism2 ./prism/deploy/reconcile.sh
#
# Finds the running compose project the same way prism-deploy.sh does and pipes
# deploy/reconcile.sql into the register database inside the postgres container.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$HERE/.." && pwd)"                         # the prism/ tree this script lives in
SQL="$HERE/reconcile.sql"
[[ -f "$SQL" ]] || { echo "missing $SQL" >&2; exit 1; }

detect_project() {
  local cid
  cid="$(docker ps -q --filter 'label=com.docker.compose.project' | head -1)"
  if [[ -n "$cid" ]]; then
    docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid"
  else
    echo compose
  fi
}
PROJECT="${PRISM_PROJECT:-$(detect_project)}"
COMPOSE="$TREE/deploy/compose/docker-compose.yml"
[[ -f "$COMPOSE" ]] || { echo "missing $COMPOSE" >&2; exit 1; }

echo "# PRISM register tally · project $PROJECT · $(date -u +'%Y-%m-%d %H:%M UTC')"
docker compose -p "$PROJECT" -f "$COMPOSE" exec -T postgres \
  psql -U prism -d register -v ON_ERROR_STOP=1 < "$SQL"
