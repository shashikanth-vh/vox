#!/usr/bin/env bash
# Regenerate the frozen OpenAPI contracts (docs/openapi/*.json) from the live FastAPI apps.
# Run after any API change and commit the result in the same PR.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/openapi"
mkdir -p "$OUT"

( cd "$ROOT/services/register" && python -c "
import json, app.models
from app.main import create_app
json.dump(create_app().openapi(), open('$OUT/register.openapi.json','w'), indent=2, sort_keys=True)
print('register OK')" )

( cd "$ROOT/services/workflows" && python -c "
import json
from app.api import create_app
json.dump(create_app().openapi(), open('$OUT/orchestrator.openapi.json','w'), indent=2, sort_keys=True)
print('orchestrator OK')" )

( cd "$ROOT/services/gateway" && python -c "
import json
from app.main import create_app
json.dump(create_app().openapi(), open('$OUT/gateway.openapi.json','w'), indent=2, sort_keys=True)
print('gateway OK')" )

echo "Frozen OpenAPI written to docs/openapi/"

# Regenerate the Postman collections from the frozen specs so they never drift from the contract.
python "$ROOT/scripts/gen_postman.py"
# The sequential E2E journey is hand-authored (hardcoded values), not spec-derived — but it is
# regenerated here too so its ids/vars stay consistent with the environment.
python "$ROOT/scripts/gen_e2e_postman.py"
