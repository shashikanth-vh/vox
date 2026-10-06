#!/usr/bin/env bash
# Play the website: exercise the intake door on this box without the website.
#
#   sudo ./prism/deploy/fixes/enquiry.sh send [capital|assets] [--to rm@evamfinance.com] [--no EV123456]
#       post a signed sample enquiry (status submitted) exactly as the website will;
#       prints PRISM's answer. --to names the approver (else PRISM picks: the
#       Employees-master tick, INTAKE_APPROVERS, the BD Heads).
#   sudo ./prism/deploy/fixes/enquiry.sh list              every enquiry: stage, approvers, lead
#   sudo ./prism/deploy/fixes/enquiry.sh mail              the approval mails and whether they went out
#   sudo ./prism/deploy/fixes/enquiry.sh age EV123456 4    pretend it arrived 4 days ago (then: chase → reminder)
#   sudo ./prism/deploy/fixes/enquiry.sh expire EV123456   expire its links now (then: chase → escalation)
#   sudo ./prism/deploy/fixes/enquiry.sh chase             run the reminder / escalation sweep now
#
# Reads deploy/compose/.env for INTAKE_WEBHOOK_SECRET, INTAKE_PUBLIC_BASE_URL and
# SVC_WORKFLOWS_KEY. PRISM_URL=https://host overrides the target; PRISM_INSECURE=1
# skips certificate checks (a staging box with a self-signed certificate).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
ENVF="$TREE/deploy/compose/.env"; [[ -f "$ENVF" ]] || { echo "missing $ENVF" >&2; exit 1; }
envval() { grep -E "^$1=" "$ENVF" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
SECRET="$(envval INTAKE_WEBHOOK_SECRET)"
BASE="${PRISM_URL:-$(envval INTAKE_PUBLIC_BASE_URL)}"; BASE="${BASE:-https://localhost}"; BASE="${BASE%/}"
CURL=(curl -sS); [[ "${PRISM_INSECURE:-0}" == "1" ]] && CURL+=(-k)
detect_project() {
  local cid; cid="$(docker ps -q --filter 'label=com.docker.compose.project' | head -1)"
  if [[ -n "$cid" ]]; then docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid"; else echo compose; fi
}
PROJECT="${PRISM_PROJECT:-$(detect_project)}"
COMPOSE="$TREE/deploy/compose/docker-compose.yml"
psqlc() { docker compose -p "$PROJECT" -f "$COMPOSE" exec -T postgres psql -U prism -d register -v ON_ERROR_STOP=1 "$@"; }
pretty() { if command -v python3 >/dev/null 2>&1; then python3 -m json.tool; else cat; echo; fi; }

CMD="${1:-}"; shift || true
case "$CMD" in
  send)
    [[ -n "$SECRET" ]] || { echo "INTAKE_WEBHOOK_SECRET is not set in $ENVF" >&2; exit 1; }
    KIND="capital"; TO=""; NO=""
    while [[ $# -gt 0 ]]; do case "$1" in
      capital|assets) KIND="$1";;
      --to) TO="$2"; shift;;
      --no) NO="$2"; shift;;
      *) echo "unknown argument $1" >&2; exit 1;;
    esac; shift; done
    NO="${NO:-EV$(date +%s | tail -c 7)}"
    NOW="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
    APPROVERS=""; [[ -n "$TO" ]] && APPROVERS=", \"approvers\": [\"$TO\"]"
    if [[ "$KIND" == "capital" ]]; then
      BODY="{\"enquiry_no\": \"$NO\", \"status\": \"submitted\", \"channel\": \"website\", \"intent\": \"capital\", \"submitted_at\": \"$NOW\"$APPROVERS, \"contact\": {\"name\": \"Rajesh Kumar\", \"mobile\": \"9876543210\", \"email\": \"rajesh@example.com\", \"consent\": true}, \"company\": {\"name\": \"Acme Renewables Test $NO Pvt Ltd\", \"address\": \"Plot 14, Hitec City, Hyderabad, Telangana 500081\"}, \"capital\": {\"persona\": \"advisor\", \"client_role\": \"developer\", \"sector\": \"electric-mobility\", \"sub_sectors\": [\"EV fleets\", \"Charging\"], \"ticket_size\": \"15-100\"}, \"business\": {\"years_in_operation\": \"3-10 years\", \"annual_revenue\": \"25-50 Cr\", \"need\": \"Debt facility for a 10 MW solar portfolio (staging test $NO)\"}}"
    else
      BODY="{\"enquiry_no\": \"$NO\", \"status\": \"submitted\", \"channel\": \"website\", \"intent\": \"assets\", \"submitted_at\": \"$NOW\"$APPROVERS, \"contact\": {\"name\": \"Rajesh Kumar\", \"mobile\": \"9876543210\", \"email\": \"rajesh@example.com\", \"consent\": true}, \"company\": {\"name\": \"Green Power Infra Test $NO LLP\", \"address\": \"3rd Floor, Anna Salai, Chennai, Tamil Nadu 600002\"}, \"assets\": {\"role\": \"both\", \"on_behalf_of_client\": false, \"offerings\": [\"Entire project\", \"PPA\"], \"status\": \"Operational\", \"location\": \"Tamil Nadu / Coimbatore\", \"buyer_size\": \"25-100 Cr\", \"buyer_criteria\": [\"operating solar, 10-30 MW, South India\"]}}"
    fi
    TS="$(date +%s)"
    SIG="$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
    echo "# POST $BASE/v1/intake/enquiries · $NO · $KIND${TO:+ · to $TO}"
    "${CURL[@]}" -X POST "$BASE/v1/intake/enquiries" -H "Content-Type: application/json" \
      -H "X-Intake-Timestamp: $TS" -H "X-Intake-Signature: sha256=$SIG" --data-binary "$BODY" | pretty
    ;;
  list)
    psqlc -c "SELECT e.enquiry_no, e.status, e.outcome, e.company_name AS company,
                     to_char(e.received_at AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS received,
                     (SELECT string_agg(DISTINCT t.recipient, ', ') FROM lead_enquiry_tokens t WHERE t.enquiry_id = e.id) AS sent_to,
                     to_char((SELECT max(t.expires_at) FROM lead_enquiry_tokens t WHERE t.enquiry_id = e.id) AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS links_till,
                     e.approved_by, e.rm, (SELECT lead_no FROM leads l WHERE l.id = e.lead_id) AS lead,
                     e.reminded_at IS NOT NULL AS reminded, e.escalated_at IS NOT NULL AS escalated
              FROM lead_enquiries e ORDER BY e.received_at DESC LIMIT 50"
    ;;
  mail)
    psqlc -c "SELECT to_char(n.created_at AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS at, n.recipient, n.meta->>'kind' AS kind,
                     d.status, d.attempts, left(coalesce(d.last_error,''), 60) AS last_error, left(n.title, 60) AS title
              FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id
              WHERE n.event = 'intake.approval' ORDER BY n.created_at DESC LIMIT 30"
    ;;
  age)
    NO="${1:?enquiry number}"; DAYS="${2:-4}"
    psqlc -c "UPDATE lead_enquiries SET received_at = now() - interval '$DAYS days' WHERE enquiry_no = '$NO' AND status = 'submitted'"
    echo "# $NO now looks $DAYS days old; run: enquiry.sh chase"
    ;;
  expire)
    NO="${1:?enquiry number}"
    psqlc -c "UPDATE lead_enquiry_tokens t SET expires_at = now() - interval '1 minute' FROM lead_enquiries e WHERE t.enquiry_id = e.id AND e.enquiry_no = '$NO'"
    echo "# links of $NO expired; run: enquiry.sh chase"
    ;;
  chase)
    KEY="$(envval SVC_WORKFLOWS_KEY)"; [[ -n "$KEY" ]] || { echo "SVC_WORKFLOWS_KEY is not set in $ENVF" >&2; exit 1; }
    echo "# POST $BASE/machine/v1/internal/intake/sweep"
    "${CURL[@]}" -X POST "$BASE/machine/v1/internal/intake/sweep" -H "X-API-Key: $KEY" -H "X-Tenant: EVAM" -H "Content-Length: 0" | pretty
    ;;
  *)
    sed -n '2,17p' "$0"; exit 1;;
esac
