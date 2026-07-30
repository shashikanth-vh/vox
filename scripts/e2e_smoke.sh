#!/usr/bin/env bash
# Deployed end-to-end smoke test — runs against a RUNNING stack (docker compose), hitting the real
# services + real PostgreSQL. Proves the deployment is wired and the new handover / CP/CS operations
# work. The exhaustive lifecycle assertions live in the register test suite (test_handover,
# test_cpcs); this verifies the *deployed* system.
#
#   REGISTER_URL=http://localhost:8000 GATEWAY_URL=http://localhost:8001 scripts/e2e_smoke.sh
#
# Identity: in the dev stack the Register trusts the API key + X-Actor (no OIDC). Distinct X-Actor
# values give the CP/CS maker-checker distinct principals.
set -euo pipefail

REGISTER_URL="${REGISTER_URL:-http://localhost:8000}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8001}"
ACCESS_URL="${ACCESS_URL:-http://localhost:8002}"
ORCH_URL="${ORCH_URL:-http://localhost:8006}"
KEY="${REGISTER_API_KEY:-dev-local-key}"
TENANT="${TENANT:-EVAM}"
pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; exit 1; }

req() {  # method path actor [json]
  local method="$1" path="$2" actor="$3" data="${4:-}"
  local args=(-sS -o /tmp/e2e_body -w '%{http_code}' -X "$method"
              -H "X-API-Key: $KEY" -H "X-Tenant: $TENANT" -H "X-Actor: $actor"
              -H "Content-Type: application/json")
  [ -n "$data" ] && args+=(--data "$data")
  curl "${args[@]}" "$REGISTER_URL$path"
}
jget() { python3 -c "import json,sys;print(json.load(open('/tmp/e2e_body')).get('$1',''))"; }

echo "== health =="
for name in "register:$REGISTER_URL" "gateway:$GATEWAY_URL" "access:$ACCESS_URL"; do
  n="${name%%:*}"; u="${name#*:}"
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$u/healthz" || true)
  [ "$code" = "200" ] && pass "$n healthy" || fail "$n unhealthy (HTTP $code)"
done
# The orchestrator has no host port in the dev stack (reached via the gateway); check via gateway.
code=$(curl -sS -o /dev/null -w '%{http_code}' "$GATEWAY_URL/orchestrator/healthz" || true)
[ "$code" = "200" ] || echo "  (orchestrator via gateway returned $code — non-fatal in minimal profile)"

echo "== register CRUD =="
CODE=$(req POST /v1/entities smoke "{\"code\":\"E2E$RANDOM\",\"legal_name\":\"E2E Co\",\"entity_type\":\"Company\"}")
[ "$CODE" = "201" ] || fail "create entity (HTTP $CODE): $(cat /tmp/e2e_body)"
EID=$(jget id); pass "entity $EID"
CODE=$(req POST /v1/lending smoke "{\"entity_id\":\"$EID\",\"stage\":\"Diligence\"}")
[ "$CODE" = "201" ] || fail "create lending (HTTP $CODE): $(cat /tmp/e2e_body)"
LID=$(jget id); pass "lending $LID"

echo "== CP/CS checklist maker-checker (real Postgres) =="
CODE=$(req POST /v1/internal/cpcs-checklists maker \
  "{\"lending_id\":\"$LID\",\"status\":\"Completed\",\"items\":[{\"key\":\"charge\",\"condition_type\":\"CP\",\"status\":\"Completed\"}]}")
[ "$CODE" = "201" ] || fail "prepare checklist (HTTP $CODE): $(cat /tmp/e2e_body)"
CID=$(jget id); pass "checklist $CID prepared (maker)"
# Same actor cannot approve their own checklist.
CODE=$(req POST "/v1/internal/cpcs-checklists/$CID/approve" maker)
[ "$CODE" = "422" ] && pass "self-approval refused (maker-checker enforced)" || fail "self-approval not refused (HTTP $CODE)"
# A different actor approves.
CODE=$(req POST "/v1/internal/cpcs-checklists/$CID/approve" checker)
[ "$CODE" = "200" ] || fail "checker approve (HTTP $CODE): $(cat /tmp/e2e_body)"
[ "$(jget status)" = "Approved" ] && pass "checklist Approved by checker" || fail "checklist not Approved"

echo "== handover endpoint is wired + gated =="
# The line is not 'Ready for Disbursement', so a handover prepare must be refused (409) — proving
# the endpoint is deployed and the precondition gate works.
CODE=$(req POST /v1/internal/handover-packages maker \
  "{\"lending_id\":\"$LID\",\"delivery_method\":\"x\",\"recipient\":\"y\",\"executed_document_refs\":[{\"reference\":\"r\",\"sha256\":\"$(printf 'a%.0s' {1..64})\"}]}")
[ "$CODE" = "409" ] && pass "handover refused before Ready for Disbursement (gate works)" \
  || fail "handover not gated (HTTP $CODE): $(cat /tmp/e2e_body)"

echo ""
echo "E2E SMOKE PASSED — deployed stack + real PostgreSQL + CP/CS maker-checker + handover gate."
