"""Generate the FULL end-to-end journey: every request through the NGINX edge, Temporal running.

    postman/PRISM_E2E_Full.postman_collection.json
    postman/PRISM_Full.postman_environment.json

One door — `https://<host>:8443`. The edge forwards everything to the gateway, which routes by
prefix: `/access` → Access, `/orchestrator` → the workflow plane, anything else → the Register.
Postman therefore presents NO backend api key at all; the gateway injects each upstream's own.

All three product lines reach their terminal state:

    Lending      → Disbursed   (via the committee decision + CP/CS + handover)
    Syndication  → Disbursed
    Asset Mon.   → Closed

The committee decision goes through Temporal: `POST /v1/workflows/deal-structurings` starts the
run, `POST /v1/workflows/{id}/committee-decision` is the human decision. The orchestrator persists
a subject-bound decision for the deal AND for each lending line, then the workflow files the
Lending-scoped evidence and advances the line — the only route to 'Sanctioned'.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(__file__).resolve().parents[1] / "postman"
REG = "{{baseUrl}}"
ACC = "{{accessUrl}}"          # {{baseUrl}}/access        — via the gateway
ORC = "{{orchestratorUrl}}"    # {{baseUrl}}/orchestrator  — via the gateway
SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# The client never sends a backend key: the gateway strips X-API-Key and injects the scoped
# upstream credential. Identity is the OIDC bearer in production, X-User-Email in dev.
# Identity works in BOTH postures with one header set:
#   * dev default      — no OIDC issuer, `Bearer ` resolves empty, so the gateway falls back to
#                        X-User-Email (header trust).
#   * prod posture     — REQUIRE_AUTH + issuer set: the VERIFIED bearer is the identity and
#                        X-User-Email is ignored/stripped. Folder 00b fills the token vars.
_H = [{"key": "X-Tenant", "value": "{{tenant}}"},
      {"key": "X-Actor", "value": "e2e-runner"}]
_ADMIN = [*_H, {"key": "Authorization", "value": "Bearer {{adminToken}}"},
          {"key": "X-User-Email", "value": "{{userEmail}}"},
          {"key": "X-User-Roles", "value": "Admin"}]
_USER = _ADMIN
_MAKER = [*_H, {"key": "Authorization", "value": "Bearer {{makerToken}}"},
          {"key": "X-User-Email", "value": "{{makerEmail}}"}]
_CHECKER = [*_H, {"key": "Authorization", "value": "Bearer {{checkerToken}}"},
            {"key": "X-User-Email", "value": "{{checkerEmail}}"}]

OK_OR_EXISTS = "pm.test('created or already exists', () => pm.expect(pm.response.code).to.be.oneOf([201, 409]));"

OK = "pm.test('status ok', () => pm.expect(pm.response.code).to.be.oneOf([200, 201, 202]));"


def req(name, method, host, path, *, body=None, tests=None, headers=None, pre=None, desc=None):
    hdrs = [dict(h) for h in (headers or _USER)]
    r = {"method": method, "header": hdrs,
         "url": {"raw": host + path, "host": [host],
                 "path": [s for s in path.split("?")[0].split("/") if s]}}
    if "?" in path:
        r["url"]["query"] = [{"key": k, "value": v} for k, v in
                             (kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))]
    if desc:
        r["description"] = desc
    if body is not None:
        hdrs.append({"key": "Content-Type", "value": "application/json"})
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                     "options": {"raw": {"language": "json"}}}
    it = {"name": name, "request": r, "event": []}
    if pre:
        it["event"].append({"listen": "prerequest",
                            "script": {"type": "text/javascript", "exec": pre}})
    it["event"].append({"listen": "test",
                        "script": {"type": "text/javascript", "exec": tests or [OK]}})
    return it


def cap(var, field="id"):
    return [OK, f"pm.environment.set('{var}', pm.response.json().{field});",
            f"console.log('{var} =', pm.environment.get('{var}'));"]


def refused(*c):
    return [f"pm.test('refused as designed ({'/'.join(map(str,c))})', () => "
            f"pm.expect(pm.response.code).to.be.oneOf({list(c)}));"]


def guard(var):
    return [f"const v = pm.environment.get('{var}');",
            f"if (!v) throw new Error('{var} is not set — run the collection in order from 00.');",
            "if (v === pm.environment.get('leadId') || v === pm.environment.get('dealId'))",
            f"  throw new Error('{var} = ' + v + ' is the lead/deal id — stale value.');"]


def stage(name, path, field, value, *, extra=None, tests=None, headers=None,
          pre=None, desc=None):
    b = {field: value}
    if extra:
        b.update(extra)
    return req(name, "PATCH", REG, path, body=b, headers=headers, pre=pre, desc=desc,
               tests=tests or [OK, f"pm.test('{field} = {value}', () => "
                                   f"pm.expect(pm.response.json().{field}).to.eql('{value}'));"])


_SPIN = ["// Temporal settles asynchronously — pause ~1.5s before polling.",
         "const t = Date.now(); while (Date.now() - t < 1500) { /* spin */ }"]


def poll(name, path, label, field, want):
    """A request that re-runs ITSELF (Collection Runner) until the row reaches ``want``.

    Used instead of the orchestrator's status endpoint, which requires a VERIFIED identity (an
    OIDC bearer) and answers 401 to a dev X-User-Email.
    """
    return req(name, "GET", REG, path, pre=_SPIN,
               tests=["pm.test('status ok', () => pm.expect(pm.response.code).to.eql(200));",
                      "const max = 15;",
                      f"const key = 'poll_{label}';",
                      "let n = Number(pm.environment.get(key) || 0);",
                      f"const cur = pm.response.json().{field};",
                      f"console.log('poll ' + n + ' :: {label} = ' + cur);",
                      f"if (cur === '{want}') {{",
                      "  pm.environment.unset(key);",
                      f"  pm.test('{label} reached {want}', () => "
                      f"pm.expect(cur).to.eql('{want}'));",
                      "} else if (n < max) {",
                      "  pm.environment.set(key, n + 1);",
                      f"  postman.setNextRequest({name!r});",
                      "} else {",
                      "  pm.environment.unset(key);",
                      f"  pm.test('{label} reached {want} within ' + max + ' polls (got ' + cur "
                      f"+ ')', () => pm.expect(cur).to.eql('{want}'));",
                      "}"],
               desc="Polls the REGISTER, not the orchestrator status endpoint (which needs a "
                    "verified bearer and 401s on a dev header). Re-runs itself until the "
                    "workflow settles — Collection Runner only.")



def find_user(label, mail_var, id_var):
    """Capture a user's id by e-mail — works whether the POST created them or 409'd."""
    return req(f"GET /access/v1/users — resolve {label} id", "GET", ACC,
               f"/v1/users?q={{{{{mail_var}}}}}", headers=_ADMIN,
               tests=[OK, "const rows = pm.response.json();",
                      f"const u = rows.find(r => r.email === pm.environment.get('{mail_var}'));",
                      f"pm.test('{label} exists in Access', () => "
                      "pm.expect(u, 'not found: ' + JSON.stringify(rows)).to.be.an('object'));",
                      f"if (u) {{ pm.environment.set('{id_var}', u.id);",
                      f"  console.log('{id_var} =', u.id, '| roles:', (u.roles || []).join(',')); }}"],
               desc="Provisioning is idempotent (UNIQUE(tenant_id, email) → 409 on re-run), so the "
                    "id is resolved here rather than read from the create response.")


def token(label, mail_var, tok_var):
    """Dex password grant → an ID token.

    SKIPS ITSELF when `dexUrl` is empty — which is the dev default. That guard has to be in the
    PRE-request script, not the test script: when Dex is not running the request fails at the
    TRANSPORT layer (connection refused), the test script never runs, and a runner started with
    `--bail` would abort the journey here. Empty dexUrl ⇒ no request ⇒ `Bearer ` carries no token
    and the gateway falls back to X-User-Email header trust.

    Set dexUrl (the prod-posture run does) and the sign-in happens for real; if Dex then answers
    non-200, the test script below still tolerates it and leaves the token empty.
    """
    return {"name": f"POST /dex/token — sign in as {label}",
            "request": {"method": "POST",
                "header": [{"key": "Content-Type",
                            "value": "application/x-www-form-urlencoded"}],
                "url": {"raw": "{{dexUrl}}/dex/token", "host": ["{{dexUrl}}"],
                        "path": ["dex", "token"]},
                "body": {"mode": "urlencoded", "urlencoded": [
                    {"key": "grant_type", "value": "password"},
                    {"key": "client_id", "value": "prism"},
                    {"key": "scope", "value": "openid email profile"},
                    {"key": "username", "value": f"{{{{{mail_var}}}}}"},
                    {"key": "password", "value": "{{ssoPassword}}"}]},
                "description":
                    "Only needed in the PRODUCTION POSTURE "
                    "(docker-compose.prod-posture.yml), where REQUIRE_AUTH + an OIDC issuer are "
                    "set and identity comes ONLY from a verified bearer. The gateway validates the "
                    "ID TOKEN, so that is what is captured."},
            "event": [
                {"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
                    "// Dev posture: no dexUrl ⇒ no sign-in request at all. A transport-level",
                    "// failure (Dex not running) never reaches the test script, so the guard",
                    "// must live here or a --bail run would abort before the journey starts.",
                    "if (!pm.environment.get('dexUrl')) {",
                    f"  pm.environment.set('{tok_var}', '');",
                    f"  console.log('dexUrl empty — dev posture, {label} uses header trust.');",
                    "  if (pm.execution && pm.execution.skipRequest) pm.execution.skipRequest();",
                    "}"]}},
                {"listen": "test", "script": {"type": "text/javascript", "exec": [
                    "if (pm.response.code === 200) {",
                    "  const b = pm.response.json();",
                    f"  pm.environment.set('{tok_var}', b.id_token || '');",
                    f"  pm.test('{label} signed in (id_token)', () => "
                    "pm.expect(b.id_token).to.be.a('string'));",
                    "} else {",
                    f"  pm.environment.set('{tok_var}', '');",
                    f"  console.log('Dex unavailable ({label}) — dev posture, header trust. status=' "
                    "+ pm.response.code);",
                    "}"]}}]}


def google_token(label, refresh_var, tok_var):
    """Exchange a stored Google refresh token for a fresh **ID token**.

    Google has no password grant, so the one-time interactive consent (Authorization Code + PKCE,
    with access_type=offline) is done by hand and only the refresh token is kept — see
    docs/POSTMAN.md. SKIPS ITSELF unless `googleClientId` is set, so a Dex/dev run makes no call
    to Google at all (the request is not merely tolerant of failure — it does not go out). The
    gateway validates the ID TOKEN; Google's access_token is opaque and would fail signature
    validation.
    """
    return {"name": f"POST oauth2.googleapis.com/token — {label} (refresh grant)",
            "request": {"method": "POST",
                "header": [{"key": "Content-Type",
                            "value": "application/x-www-form-urlencoded"}],
                "url": {"raw": "https://oauth2.googleapis.com/token",
                        "host": ["oauth2", "googleapis", "com"], "path": ["token"],
                        "protocol": "https"},
                "body": {"mode": "urlencoded", "urlencoded": [
                    {"key": "grant_type", "value": "refresh_token"},
                    {"key": "client_id", "value": "{{googleClientId}}"},
                    {"key": "client_secret", "value": "{{googleClientSecret}}"},
                    {"key": "refresh_token", "value": f"{{{{{refresh_var}}}}}"}]},
                "description":
                    "Keep googleClientSecret and the refresh tokens in Postman VAULT or a secret "
                    "environment variable — never in an exported collection."},
            "event": [
                {"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
                    "// Not configured for Google? Then don't call Google. skipRequest keeps the",
                    "// dev run free of a pointless outbound request (and of a 400 in the report).",
                    "if (!pm.environment.get('googleClientId')) {",
                    f"  console.log('Google not configured — skipping {label} sign-in "
                    "(set googleClientId to enable folder 00c).');",
                    "  if (pm.execution && pm.execution.skipRequest) pm.execution.skipRequest();",
                    "}"]}},
                {"listen": "test", "script": {"type": "text/javascript", "exec": [
                    "if (pm.response.code === 200 && pm.response.json().id_token) {",
                    f"  pm.environment.set('{tok_var}', pm.response.json().id_token);",
                    f"  pm.test('{label} signed in via Google (id_token)', () => "
                    "pm.expect(pm.response.json().id_token).to.be.a('string'));",
                    "} else {",
                    f"  console.log('Google sign-in skipped/failed for {label} — status ' "
                    "+ pm.response.code);",
                    "}"]}}]}


F = []

F.append(("00 · Health & run setup", [
    req("GET /healthz — the edge is up", "GET", REG, "/healthz",
        pre=["// Clear every DERIVED id so a stale value can never target the wrong row.",
             "// ONLY derived state belongs here. The fixed identities (rmEmail, makerEmail,",
             "// checkerEmail) are CONFIGURATION — clearing them once broke every maker/checker",
             "// request downstream: the create sent a literal '{{makerEmail}}', failed e-mail",
             "// validation, the resolve found nobody, and convert 422'd on an empty analyst_id.",
             "['entityId','leadId','dealId','lendingId','syndicationId','amId','checklistId',",
             " 'assignmentId','interactionId','financialId','documentId','synLenderId',",
             " 'bdrmUserId','makerUserId','checkerUserId','structWorkflowId','lendingVersion']",
             "  .forEach(k => pm.environment.unset(k));",
             "// Self-heal the fixed identities if a hand-edited environment lost them.",
             "if (!pm.environment.get('rmEmail')) pm.environment.set('rmEmail', 'e2e.rm@evamfinance.com');",
             "if (!pm.environment.get('makerEmail')) pm.environment.set('makerEmail', 'e2e.maker@evamfinance.com');",
             "if (!pm.environment.get('checkerEmail')) pm.environment.set('checkerEmail', 'e2e.checker@evamfinance.com');",
             "pm.environment.set('runSuffix', String(Date.now()).slice(-6));",
             "console.log('run suffix =', pm.environment.get('runSuffix'));"],
        tests=[OK, "pm.test('register reachable through NGINX', () => "
                   "pm.expect(pm.response.json().status).to.eql('ok'));"],
        desc="TLS terminates at NGINX; everything below enters through this one door."),
    req("GET /v1/ref — dropdown vocabulary", "GET", REG, "/v1/ref",
        tests=[OK, "pm.test('reference data seeded', () => "
                   "pm.expect(Object.keys(pm.response.json()).length).to.be.above(0));"]),
]))

F.append(("00b · Sign in (Dex) — required only in the prod posture", [
    token("ADMIN", "userEmail", "adminToken"),
    token("MAKER", "makerEmail", "makerToken"),
    token("CHECKER", "checkerEmail", "checkerToken"),
]))

F.append(("00c · Sign in (Google) — for a deployment whose issuer is Google", [
    google_token("ADMIN", "adminRefreshToken", "adminToken"),
    google_token("MAKER", "makerRefreshToken", "makerToken"),
    google_token("CHECKER", "checkerRefreshToken", "checkerToken"),
]))

F.append(("01 · Users, roles & people  (Access via the gateway)", [
    req("POST /access/v1/users — RM (BDRM + Syn RM + AM RM)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{rmEmail}}", "full_name": "E2E Priya Nair",
              "short_name": "Priya", "phone": "+91-9800000001", "is_active": True,
              "roles": ["BDRM", "Syn RM", "AM RM"]},
        tests=[OK_OR_EXISTS] ,
        desc="Reached at {{baseUrl}}/access — the gateway strips your key and injects Access's."),
    req("POST /access/v1/users — MAKER (Credit Head + Deal Analyst)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{makerEmail}}", "full_name": "E2E Arun Menon",
              "is_active": True, "roles": ["Credit Head", "Deal Analyst"]},
        tests=[OK_OR_EXISTS] ,
        desc="Sends the committee decision, prepares CP/CS and the handover package."),
    req("POST /access/v1/users — CHECKER (Management)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{checkerEmail}}", "full_name": "E2E Divya Rao",
              "is_active": True, "roles": ["Management"]},
        tests=[OK_OR_EXISTS] ,
        desc="Approves CP/CS and the handover — a DIFFERENT person; self-approval is refused."),
    find_user("RM", "rmEmail", "bdrmUserId"),
    find_user("MAKER", "makerEmail", "makerUserId"),
    find_user("CHECKER", "checkerEmail", "checkerUserId"),
    req("GET /access/v1/resolve — the RM's effective matrix", "GET", ACC,
        "/v1/resolve?email={{rmEmail}}", headers=_ADMIN,
        tests=[OK, "pm.test('leads view is SCOPED for a BDRM', () => "
                   "pm.expect(pm.response.json().views.leads).to.eql('SCOPED'));"]),
    req("POST /v1/people — Priya Nair (RM on record)", "POST", REG, "/v1/people",
        body={"name": "Priya Nair", "full_name": "E2E Priya Nair", "role": "RM",
              "geography": "Karnataka", "inactive": False},
        tests=cap("rmPersonId"),
        desc="/convert refuses an rm that is not a Person on record — full_name matches exactly."),
    req("POST /v1/people — Arun Menon (Analyst on record)", "POST", REG, "/v1/people",
        body={"name": "Arun Menon", "full_name": "E2E Arun Menon", "role": "Analyst",
              "geography": "Karnataka", "inactive": False},
        tests=cap("analystPersonId")),
]))

F.append(("02 · Client (Entity)", [
    req("POST /v1/entities", "POST", REG, "/v1/entities",
        body={"code": "ECOSOCH-{{runSuffix}}", "legal_name": "EcoSoch Solar Private Limited",
              "display_name": "EcoSoch Solar", "entity_type": "Company",
              "sector": "Solar - Developer", "lens": "Mitigation",
              "register_status": "Pipeline", "state": "Karnataka",
              "location": "Bengaluru, Karnataka", "cin": "U40106KA2015PTC{{runSuffix}}",
              "notes": "E2E: 150 MW C&I solar developer."},
        tests=cap("entityId")),
    req("GET /v1/entities?bogus_filter=x — REFUSED", "GET", REG, "/v1/entities?bogus_filter=x",
        tests=refused(400, 422), desc="Fail-closed filtering."),
]))

F.append(("03 · Lead, ownership & interaction", [
    req("POST /v1/leads", "POST", REG, "/v1/leads",
        body={"company": "EcoSoch Solar Private Limited", "entity_id": "{{entityId}}",
              "status": "Active", "sector": "Solar - Developer", "lens": "Mitigation",
              "source": "RM", "temperature": "Warm", "rm": "E2E Priya Nair",
              "contact": "Ravi Kulkarni", "phone": "+91-9800000010",
              "notes": "45 Cr term loan for a 150 MW pipeline."},
        tests=cap("leadId")),
    req("POST /v1/assignments — the RM owns the lead", "POST", REG, "/v1/assignments",
        body={"user_id": "{{bdrmUserId}}", "subject_type": "Lead", "subject_id": "{{leadId}}",
              "assignment_role": "BDRM", "note": "E2E: primary owner."},
        tests=cap("assignmentId")),
    req("POST /v1/leads/{id}/interactions", "POST", REG, "/v1/leads/{{leadId}}/interactions",
        body={"interaction_type": "In-Person Meeting", "occurred_at": "2026-03-31T10:30:00Z",
              "summary": "Site walkthrough + term sheet (150 MW, 45 Cr, 18m tenor).",
              "contact_name": "Ravi Kulkarni", "performed_by": "E2E Priya Nair",
              "location": "Tumkur site, Karnataka"},
        tests=cap("interactionId")),
    stage("PATCH lead — status → Converted REFUSED", "/v1/leads/{{leadId}}", "status",
          "Converted", tests=refused(400, 403, 422)),
]))

F.append(("04 · Convert → deal + 3 product lines", [
    req("POST /v1/leads/{id}/convert", "POST", REG, "/v1/leads/{{leadId}}/convert",
        body={"is_lending": True, "is_syndication": True, "is_asset_mon": True,
              "product_type": "Term Loan", "amount_cr": 45.0, "rm": "E2E Priya Nair",
              "rm_id": "{{bdrmUserId}}", "analyst": "E2E Arun Menon",
              "analyst_id": "{{makerUserId}}", "approved_by": "{{checkerEmail}}",
              "note": "E2E: convert with all three product lines."},
        tests=[OK, "const b = pm.response.json();",
               "console.log('CONVERT RESPONSE:', JSON.stringify(b));",
               "const deal = b.deal_id || (b.deal && b.deal.id);",
               "pm.test('deal created', () => pm.expect(deal).to.be.a('string'));",
               "if (deal) pm.environment.set('dealId', deal);"]),
    *[req(f"GET /v1/{res}?deal_id= — resolve + VERIFY {lbl}", "GET", REG,
          f"/v1/{res}?deal_id={{{{dealId}}}}&limit=5",
          tests=[OK, "const b = pm.response.json(); const items = (b && b.items) || [];",
                 f"pm.test('/convert created the {lbl} line', () => "
                 "pm.expect(items.length, 'empty — response: ' + JSON.stringify(b))"
                 ".to.be.above(0));",
                 "if (items.length) {", "  const id = items[0].id;",
                 "  pm.expect(id).to.not.eql(pm.environment.get('leadId'));",
                 "  pm.expect(id).to.not.eql(pm.environment.get('dealId'));",
                 f"  pm.environment.set('{var}', id);", f"  console.log('{var} =', id);",
                 "} else {", f"  pm.environment.unset('{var}');", "}"],
          desc="Filtered by deal_id — now a whitelisted filter on every product line, which is "
               "how the sanction fan-out finds them too.")
      for res, lbl, var in (("lending", "lending", "lendingId"),
                            ("syndication", "syndication", "syndicationId"),
                            ("asset-monetisation", "asset-monetisation", "amId"))],
]))

F.append(("05 · LENDING ▸ Sanctioned via Temporal (deal-structuring + committee)", [
    stage("PATCH lending — → Sanctioned by hand is REFUSED", "/v1/lending/{{lendingId}}",
          "stage", "Sanctioned", pre=guard("lendingId"), tests=refused(400, 403, 422),
          desc="Proves the milestone cannot be typed. The workflow below is the ONLY route."),
    req("POST /orchestrator/v1/workflows/deal-structurings — start the run", "POST", ORC,
        "/v1/workflows/deal-structurings", headers=_MAKER,
        body={"deal_id": "{{dealId}}", "requested_by": "{{makerEmail}}",
              "product_type": "Term Loan", "rm": "E2E Priya Nair",
              "credit_note_reference": "CN/ECOSOCH/{{runSuffix}}",
              "decision_timeout_hours": 24},
        tests=[OK, "const b = pm.response.json();",
               "console.log('workflow:', JSON.stringify(b));",
               "pm.test('workflow started', () => pm.expect(b.workflow_id).to.be.a('string'));",
               "pm.environment.set('structWorkflowId', b.workflow_id);"],
        desc="Walks the LENDING line to 'Note Circulated', files the credit note against it, "
             "then WAITS for the committee. The deal's own stage is the commercial funnel and is "
             "never touched. Workflow id is struct-{tenant}-{deal_id}."),
    req("POST /orchestrator/v1/workflows/{id}/committee-decision — the human decision", "POST",
        ORC, "/v1/workflows/{{structWorkflowId}}/committee-decision", headers=_MAKER,
        body={"approved": True, "by": "{{makerEmail}}",
              "committee_reference": "CC/2026/{{runSuffix}}",
              "sanction_letter_reference": "SL/ECOSOCH/{{runSuffix}}",
              "note": "E2E: credit committee approved 45 Cr."},
        tests=[OK, "console.log('decision:', JSON.stringify(pm.response.json()));"],
        desc="The orchestrator DURABLY records a subject-bound decision for the deal AND for each "
             "lending line (keyed {workflow_id}:lending:{line_id}) using THIS human's committee "
             "authority — which the workflow, a service principal, could never supply. The signal "
             "is only a wake-up; the run re-reads the authoritative record."),
    poll("WAIT · poll the LENDING line until Sanctioned", "/v1/lending/{{lendingId}}",
         "LENDING", "stage", "Sanctioned"),
    req("GET /v1/deals/{id} — the DEAL stays in the commercial funnel", "GET", REG,
        "/v1/deals/{{dealId}}",
        tests=[OK, "const d = pm.response.json();",
               "pm.test('deal stage is the funnel (In Pipeline), never a credit value', () => "
               "pm.expect(d.stage).to.eql('In Pipeline'));",
               "pm.test('sanction basics recorded on the deal as data', () => "
               "pm.expect(d.product_type).to.eql('Term Loan'));"],
        desc="The deal's stage is the ORIGINATION FUNNEL — sanctioning its facility does not "
             "move it. The structuring input's product_type/rm land on the deal as plain data."),
    req("GET /v1/lending/{id} — the LENDING line is Sanctioned", "GET", REG,
        "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('lending Sanctioned', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Sanctioned'));",
               "pm.environment.set('lendingVersion', pm.response.json().version);"],
        desc="The workflow filed Lending-scoped committee + sanction-letter evidence citing the "
             "per-line decision, then advanced the line. Without that the line could never leave "
             "'Note Circulated'."),
    req("GET /v1/evidence — Lending evidence is on file", "GET", REG,
        "/v1/evidence?subject_type=Lending&subject_id={{lendingId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('lending evidence:', kinds.join(', '));",
               "pm.test('committee approval filed', () => "
               "pm.expect(kinds).to.include('credit_committee_approval'));",
               "pm.test('sanction letter filed', () => "
               "pm.expect(kinds).to.include('sanction_letter'));"]),
]))

F.append(("06 · LENDING ▸ CP/CS Completed (maker → checker)", [
    req("POST /v1/evidence — executed_agreement", "POST", REG, "/v1/evidence",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "executed_agreement",
              "reference": "AGR/ECOSOCH/{{runSuffix}}", "sha256": SHA,
              # Governance evidence must carry provenance — the Register refuses it otherwise
              # ("'executed_agreement' must cite its workflow_id and run_id").
              # The cited workflow must RESOLVE to a decision recorded for THIS subject, so it
              # cites the per-LINE committee decision the orchestrator wrote, not the deal's.
              "workflow_id": "{{structWorkflowId}}:lending:{{lendingId}}",
              "run_id": "manual-{{runSuffix}}",
              "note": "E2E: executed facility agreement."},
        headers=_MAKER, tests=[OK],
        desc="No verify_source — a human may file it. The handover reconciles this digest."),
    req("POST /v1/internal/cpcs-checklists — MAKER prepares", "POST", REG,
        "/v1/internal/cpcs-checklists", headers=_MAKER,
        body={"lending_id": "{{lendingId}}", "deal_id": "{{dealId}}", "checklist_version": 1,
              "status": "Completed",
              "items": [
                  {"key": "cp_security_perfection", "label": "Security perfected & charge filed",
                   "condition_type": "CP", "required": True, "status": "Completed",
                   "evidence_ref": "SEC/{{runSuffix}}"},
                  {"key": "cp_insurance", "label": "Insurance assigned to the lender",
                   "condition_type": "CP", "required": True, "status": "Completed",
                   "evidence_ref": "INS/{{runSuffix}}"},
                  {"key": "cs_quarterly_monitoring", "label": "Quarterly monitoring reports",
                   "condition_type": "CS", "required": False, "status": "Pending"}],
              "note": "E2E: all required CPs satisfied."},
        tests=cap("checklistId"),
        desc="A 'Completed' checklist may not leave a required CP Pending; waivers and "
             "CS-deferments need senior authority plus a reason."),
    req("POST /v1/internal/cpcs-checklists/{id}/approve — CHECKER approves", "POST", REG,
        "/v1/internal/cpcs-checklists/{{checklistId}}/approve", headers=_CHECKER, tests=[OK],
        desc="A different authenticated user — the Register refuses self-approval. Approving does "
             "NOT create any evidence; it only makes cp_cs_completion FILEABLE (the next request), "
             "because evidence.py verifies that kind against an APPROVED checklist."),
    req("POST /v1/evidence — cp_cs_completion (cites the approved checklist)", "POST", REG,
        "/v1/evidence", headers=_CHECKER,
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "cp_cs_completion",
              "reference": "CPCS/ECOSOCH/{{runSuffix}}",
              "decision_ref": "{{checklistId}}",
              "note": "E2E: CP/CS conditions verified and approved."},
        tests=[OK, "pm.test('provenance generated from the checklist', () => "
                   "pm.expect(pm.response.json().workflow_id).to.include('cpcs:'));"],
        desc="THE step that unlocks 'CP/CS Completed'. decision_ref must be the CP/CS CHECKLIST "
             "ID; the Register then proves the checklist is Approved, belongs to this lending line, "
             "and was approved by a different checker than its preparer. Provenance is GENERATED "
             "from it (workflow_id = 'cpcs:{id}', run_id = the checklist version), so unlike "
             "executed_agreement no sha256 / workflow_id / run_id is sent."),
    stage("PATCH lending — → CP/CS Completed", "/v1/lending/{{lendingId}}", "stage",
          "CP/CS Completed", extra={"remarks": "E2E: CP/CS complete, agreement executed."}),
]))

F.append(("07 · LENDING ▸ Disbursed  (TERMINAL)", [
    stage("PATCH lending — → Ready for Disbursement", "/v1/lending/{{lendingId}}", "stage",
          "Ready for Disbursement",
          extra={"proposed_disbursement_amount": 45.0,
                 "proposed_disbursement_date": "2026-04-30",
                 "remarks": "E2E: drawdown proposed."},
          desc="Mandatory fields for this stage: proposed_disbursement_amount + date."),
    req("POST /v1/internal/handover-packages — MAKER prepares", "POST", REG,
        "/v1/internal/handover-packages", headers=_MAKER,
        body={"lending_id": "{{lendingId}}",
              "executed_document_refs": [
                  {"reference": "AGR/ECOSOCH/{{runSuffix}}", "sha256": SHA}],
              "cpcs_checklist_version": 1, "delivery_method": "Secure email",
              "recipient": "advaya-ops@evamfinance.com",
              "note": "E2E: handover package for disbursement."},
        tests=[OK, "pm.test('Prepared — stage NOT advanced yet', () => "
                   "pm.expect(JSON.stringify(pm.response.json())).to.include('Prepared'));"],
        desc="Two-phase. The server generates the manifest + digest and RECONCILES the document "
             "refs against the on-file executed_agreement — hence the identical sha256."),
    req("POST /v1/internal/handover-packages/{lending_id}/approve — CHECKER approves", "POST",
        REG, "/v1/internal/handover-packages/{{lendingId}}/approve", headers=_CHECKER,
        tests=[OK],
        desc="Freezes the package and advances the stage in one transaction."),
    req("GET /v1/lending/{id} — LENDING COMPLETE", "GET", REG, "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('LENDING terminal = Disbursed', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Disbursed'));",
               "console.log('LENDING =', pm.response.json().stage);"]),
]))

F.append(("08 · SYNDICATION ▸ Disbursed  (TERMINAL)", [
    req("POST /v1/syndication/{id}/lenders — invite a lender", "POST", REG,
        "/v1/syndication/{{syndicationId}}/lenders", pre=guard("syndicationId"),
        body={"lender_name": "Green Bridge Capital", "is_existing": False,
              "since": "2026-03-31", "note": "E2E: co-lender approached."},
        tests=cap("synLenderId")),
    *[stage(f"→ {v}", "/v1/syndication/{{syndicationId}}", "status", v)
      for v in ("Docs Pending", "IM in Prep", "IM Circulated", "Queries Received",
                "IP Received")],
    stage("→ Sanctioned", "/v1/syndication/{{syndicationId}}", "status", "Sanctioned",
          extra={"date_of_sanction": "2026-04-15", "sanctioned_lender": "Green Bridge Capital",
                 "amount_cr": 45.0, "remarks": "E2E: syndication sanctioned."}),
    stage("→ Disbursed", "/v1/syndication/{{syndicationId}}", "status", "Disbursed",
          extra={"remarks": "E2E: syndicated facility disbursed."}),
    req("GET /v1/syndication/{id} — SYNDICATION COMPLETE", "GET", REG,
        "/v1/syndication/{{syndicationId}}",
        tests=[OK, "pm.test('SYNDICATION terminal = Disbursed', () => "
                   "pm.expect(pm.response.json().status).to.eql('Disbursed'));"],
        desc="No evidence gate applies to Syndication — EVIDENCE_FOR_STAGE covers Deal and "
             "Lending only."),
]))

F.append(("09 · ASSET MONETISATION ▸ Closed  (TERMINAL)", [
    req("PATCH AM — → Teaser Shared", "PATCH", REG, "/v1/asset-monetisation/{{amId}}",
        pre=guard("amId"), body={"status": "Teaser Shared"},
        tests=[OK, "pm.test('status = Teaser Shared', () => "
                   "pm.expect(pm.response.json().status).to.eql('Teaser Shared'));"]),
    *[stage(f"→ {v}", "/v1/asset-monetisation/{{amId}}", "status", v)
      for v in ("In Discussion", "NBO Received", "BO Received", "SPA / Documentation")],
    stage("→ Closed", "/v1/asset-monetisation/{{amId}}", "status", "Closed",
          extra={"notes": "E2E: transaction closed.", "investor": "Green Bridge Capital",
                 "indicative_value_cr": 45.0}),
    req("GET /v1/asset-monetisation/{id} — AM COMPLETE", "GET", REG,
        "/v1/asset-monetisation/{{amId}}",
        tests=[OK, "pm.test('AM terminal = Closed', () => "
                   "pm.expect(pm.response.json().status).to.eql('Closed'));"]),
]))

F.append(("10 · Financials & documents", [
    req("POST /v1/financials — FY25 audited", "POST", REG, "/v1/financials",
        body={"entity_id": "{{entityId}}", "statement_type": "Audited",
              "period_end": "2026-03-31", "period_start": "2025-04-01",
              "fiscal_year": "FY2025-26", "currency": "INR", "is_audited": True,
              "revenue": 128.4, "ebitda": 21.7, "pat": 9.3, "net_worth": 64.2,
              "total_debt": 55.0, "dscr": 1.62, "provenance": "E2E: audited FY25 pack."},
        tests=cap("financialId")),
    req("POST /v1/documents — sanction letter reference", "POST", REG, "/v1/documents",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "doc_type": "Sanction Letter", "title": "Sanction letter — EcoSoch 45 Cr",
              "storage_uri": "s3://prism-e2e/SL-ECOSOCH-{{runSuffix}}.pdf",
              "original_filename": "SL-ECOSOCH-{{runSuffix}}.pdf",
              "content_type": "application/pdf", "uploaded_by": "E2E Arun Menon"},
        tests=[OK, "if (pm.response.json().id) "
                   "pm.environment.set('documentId', pm.response.json().id);"]),
]))

F.append(("11 · Final verification — all three lines terminal", [
    req("GET /v1/lending/{id} — Disbursed", "GET", REG, "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('LENDING complete', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Disbursed'));"]),
    req("GET /v1/syndication/{id} — Disbursed", "GET", REG, "/v1/syndication/{{syndicationId}}",
        tests=[OK, "pm.test('SYNDICATION complete', () => "
                   "pm.expect(pm.response.json().status).to.eql('Disbursed'));"]),
    req("GET /v1/asset-monetisation/{id} — Closed", "GET", REG,
        "/v1/asset-monetisation/{{amId}}",
        tests=[OK, "pm.test('ASSET MONETISATION complete', () => "
                   "pm.expect(pm.response.json().status).to.eql('Closed'));"]),
    req("GET /v1/evidence — the audit-grade trail", "GET", REG,
        "/v1/evidence?subject_type=Lending&subject_id={{lendingId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('evidence:', kinds.join(', '));",
               "pm.test('cp_cs_completion was MINTED by the approval', () => "
               "pm.expect(kinds).to.include('cp_cs_completion'));",
               "pm.test('executed agreement on file', () => "
               "pm.expect(kinds).to.include('executed_agreement'));"]),
    req("GET /v1/entities/{id}/dossier", "GET", REG, "/v1/entities/{{entityId}}/dossier",
        tests=[OK, "console.log('dossier:', Object.keys(pm.response.json()).join(', '));"]),
    req("GET /v1/audit — every write recorded", "GET", REG,
        "/v1/audit?resource_id={{lendingId}}&limit=200",
        tests=[OK, "const rows = pm.response.json();",
               "pm.test('audit trail exists for this lending line', () => "
               "pm.expect(rows.length).to.be.above(0));",
               "console.log('audited resource_type values:', "
               "[...new Set(rows.map(r => r.resource_type))].join(', '));",
               "console.log('actions:', [...new Set(rows.map(r => r.action))].join(', '));"],
        desc="Filtered by resource_id ONLY. `resource_type` in the audit log is the TABLE name "
             "(`CRUDRepository.resource = model.__tablename__`, i.e. 'lending_tracker'), not the "
             "URL segment 'lending' nor the subject_type 'Lending' — and different writers use "
             "different values, so filtering on it silently returns []. The row id is unique, so "
             "it is the reliable filter."),
]))

col = {"info": {
    "name": "PRISM · E2E FULL (via NGINX, with Temporal)",
    "description":
        "Every request enters at **one door** — `{{baseUrl}}` = `https://<host>:8443`. The edge "
        "forwards everything to the gateway, which routes by prefix: `/access` → Access, "
        "`/orchestrator` → the workflow plane, anything else → the Register. **Postman sends no "
        "backend api key** — the gateway strips it and injects each upstream's own.\\n\\n"
        "All three product lines reach their terminal state:\\n"
        "* **Lending** → `Disbursed`\\n"
        "* **Syndication** → `Disbursed`\\n"
        "* **Asset monetisation** → `Closed`\\n\\n"
        "### The sanction goes through Temporal\\n"
        "`Sanctioned` cannot be typed (folder 05 proves it). Instead: start a deal-structuring "
        "workflow, then send the committee decision. The orchestrator durably records a "
        "subject-bound decision for the deal AND for each lending line — carrying the deciding "
        "human's committee authority — and the workflow files the Lending-scoped evidence before "
        "advancing the line.\\n\\n"
        "### Requirements\\n"
        "Full stack up (`docker compose up -d --build`, including `temporal`, `workflows`, "
        "`orchestrator`), TLS certs generated (`scripts/gen_dev_certs.sh`), and SSL verification "
        "OFF in Postman for the self-signed cert.\\n\\n"
        "**Temporal is asynchronous:** if folder 05's 'lending is Sanctioned' check races the run, "
        "re-send the status request and then the two GETs.\\n\\n"
        "Four requests MUST fail (unknown filter, lead→Converted, hand-typed `Sanctioned`) — "
        "their tests pass on refusal. Request #1 clears every derived id, so the run is repeatable.",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "item": [{"name": n, "item": i} for n, i in F]}

env = {"name": "PRISM — Full (via NGINX)", "values": [
    {"key": "baseUrl", "value": "https://localhost:8443", "enabled": True},
    {"key": "accessUrl", "value": "https://localhost:8443/access", "enabled": True},
    {"key": "orchestratorUrl", "value": "https://localhost:8443/orchestrator", "enabled": True},
    # EMPTY by default = dev posture: folder 00b skips itself and identity comes from header
    # trust. For the prod posture set it to Dex as reachable from YOUR machine, normally
    # http://localhost:5556 (compose publishes dex on 5556).
    {"key": "dexUrl", "value": "", "enabled": True},
    {"key": "ssoPassword", "value": "prism", "enabled": True},
    # --- Google issuer (folder 00c). Leave blank unless the deployment's issuer is Google;
    # keep the secret and refresh tokens in Postman Vault, not in an exported environment.
    {"key": "googleClientId", "value": "", "enabled": True},
    {"key": "googleClientSecret", "value": "", "enabled": True},
    {"key": "adminRefreshToken", "value": "", "enabled": True},
    {"key": "makerRefreshToken", "value": "", "enabled": True},
    {"key": "checkerRefreshToken", "value": "", "enabled": True},
    {"key": "adminToken", "value": "", "enabled": True},
    {"key": "makerToken", "value": "", "enabled": True},
    {"key": "checkerToken", "value": "", "enabled": True},
    # FIXED identities — a bearer can only be issued for someone Dex knows.
    {"key": "rmEmail", "value": "e2e.rm@evamfinance.com", "enabled": True},
    {"key": "tenant", "value": "EVAM", "enabled": True},
    {"key": "userEmail", "value": "admin@evamfinance.com", "enabled": True},
    {"key": "bearerToken", "value": "", "enabled": True},
    {"key": "runSuffix", "value": "", "enabled": True},
    {"key": "makerEmail", "value": "e2e.maker@evamfinance.com", "enabled": True},
    {"key": "checkerEmail", "value": "e2e.checker@evamfinance.com", "enabled": True},
    {"key": "bdrmUserId", "value": "", "enabled": True},
    {"key": "makerUserId", "value": "", "enabled": True},
    {"key": "checkerUserId", "value": "", "enabled": True},
    {"key": "rmPersonId", "value": "", "enabled": True},
    {"key": "analystPersonId", "value": "", "enabled": True},
    {"key": "entityId", "value": "", "enabled": True},
    {"key": "leadId", "value": "", "enabled": True},
    {"key": "assignmentId", "value": "", "enabled": True},
    {"key": "interactionId", "value": "", "enabled": True},
    {"key": "dealId", "value": "", "enabled": True},
    {"key": "lendingId", "value": "", "enabled": True},
    {"key": "syndicationId", "value": "", "enabled": True},
    {"key": "amId", "value": "", "enabled": True},
    {"key": "checklistId", "value": "", "enabled": True},
    {"key": "structWorkflowId", "value": "", "enabled": True},
    {"key": "synLenderId", "value": "", "enabled": True},
    {"key": "financialId", "value": "", "enabled": True},
    {"key": "documentId", "value": "", "enabled": True},
    {"key": "lendingVersion", "value": "", "enabled": True},
]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with open(OUT / "PRISM_E2E_Full.postman_collection.json", "w") as fh:
        json.dump(col, fh, indent=2)
    with open(OUT / "PRISM_Full.postman_environment.json", "w") as fh:
        json.dump(env, fh, indent=2)
    # A second, ready-made environment for the Dex/prod-posture run. Identical except dexUrl is
    # FILLED, which is the one switch that turns folder 00b on. Shipping it prevents the failure
    # mode of a hand-copied environment that misses a variable (an unresolved {{var}} is sent as
    # literal text, and the run 401s in a way that looks like a platform bug).
    dex_env = {"name": "PRISM — Full (via NGINX) · Dex prod posture",
               "values": [dict(v, value="http://localhost:5556") if v["key"] == "dexUrl"
                          else dict(v) for v in env["values"]]}
    with open(OUT / "PRISM_Full_Dex.postman_environment.json", "w") as fh:
        json.dump(dex_env, fh, indent=2)
    n = sum(len(f["item"]) for f in col["item"])
    print(f"PRISM_E2E_Full: {len(col['item'])} folders · {n} requests · 2 environments")
    # Post-generation gate: statically simulate the whole run and REFUSE to emit a collection
    # where any variable is consumed before something writes it (the '{{makerUserId}} sent as
    # literal text' class). Tokens/Google credentials are legitimately empty in dev.
    import subprocess
    allow = ("adminToken,makerToken,checkerToken,bearerToken,dexUrl,ssoPassword,"
             "googleClientId,googleClientSecret,adminRefreshToken,makerRefreshToken,"
             "checkerRefreshToken")
    for envfile in ("PRISM_Full.postman_environment.json",
                    "PRISM_Full_Dex.postman_environment.json"):
        res = subprocess.run(
            [sys.executable, str(HERE / "audit_postman.py"),
             str(OUT / "PRISM_E2E_Full.postman_collection.json"), str(OUT / envfile),
             "--allow-empty", allow], capture_output=True, text=True)
        blocking = [line for line in res.stdout.splitlines()
                    if line.startswith(("HARD", "ORDER", "UNSET"))]
        if res.returncode != 0:
            raise SystemExit(f"run-order audit FAILED for {envfile}:\n" + "\n".join(blocking))
        tail = res.stdout.splitlines()[-1].strip() if res.stdout.splitlines() else ""
        print(f"run-order audit clean for {envfile} ({tail})")


if __name__ == "__main__":
    main()
