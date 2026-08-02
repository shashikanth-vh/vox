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

OK_OR_EXISTS = ("pm.test('created or already exists', () => "
                "pm.expect(pm.response.code).to.be.oneOf([201, 409]));")

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
                    "const b = pm.response.code === 200 ? pm.response.json() : {};",
                    "if (b.id_token) {",
                    f"  pm.environment.set('{tok_var}', b.id_token);",
                    f"  pm.test('{label} signed in (id_token)', () => "
                    "pm.expect(b.id_token).to.be.a('string'));",
                    "} else {",
                    "  // NEVER wipe a previously-captured token on a failed re-sign-in — a",
                    "  // stale-but-valid token beats an empty one (an empty token turns EVERY",
                    f"  // {label}-lane request into a 401). This request only ran because",
                    "  // dexUrl is set, so a failure here is a REAL problem — fail loudly",
                    "  // instead of silently poisoning the rest of the run.",
                    f"  const prev = pm.environment.get('{tok_var}');",
                    f"  pm.test('{label} sign-in FAILED (status ' + pm.response.code + ') — ' +",
                    "          (prev ? 'previous token kept' : 'NO token available'), () =>",
                    "    pm.expect.fail('Dex returned no id_token; check ssoPassword, dexUrl "
                    "and the dex container logs.'));",
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

# --------------------------------------------------------------------------- #
# The journey — every folder is a chapter of what the Excel tracker used to hold
# --------------------------------------------------------------------------- #
MREG = "{{registerDirectUrl}}"   # machine lane: service-principal plumbing, direct to Register

_RM = [*_H, {"key": "Authorization", "value": "Bearer {{rmToken}}"},
       {"key": "X-User-Email", "value": "{{rmEmail}}"}]
# Machine lane (dev/demo): the workflow service principal's key, direct to the Register.
# In production these calls are made by the monitors/notifier/Advaya — never by a person.
_SVC = [{"key": "X-API-Key", "value": "{{svcWorkflowsKey}}"},
        {"key": "X-Tenant", "value": "{{tenant}}"},
        {"key": "X-Actor", "value": "e2e-machine"}]
# The SIMULATED Advaya peer: its own least-privilege service principal (svc_advaya) —
# exactly the credential the real integration will present for handoff outcomes and
# disbursement callbacks.
_SVC_ADVAYA = [{"key": "X-API-Key", "value": "{{svcAdvayaKey}}"},
               {"key": "X-Tenant", "value": "{{tenant}}"},
               {"key": "X-Actor", "value": "advaya-simulated"}]


def poll_kinds(name, subject_type, id_var, kind):
    """Self-repeating evidence poll: re-runs until ``kind`` appears for the subject."""
    return req(name, "GET", REG,
               f"/v1/evidence?subject_type={subject_type}&subject_id={{{{{id_var}}}}}",
               pre=_SPIN,
               tests=["pm.test('status ok', () => pm.expect(pm.response.code).to.eql(200));",
                      "const max = 15;", f"const key = 'poll_{kind}';",
                      "let n = Number(pm.environment.get(key) || 0);",
                      "const b = pm.response.json();",
                      "const kinds = (b.items || b).map(e => e.evidence_kind);",
                      "console.log('poll ' + n + ' :: kinds =', kinds.join(', '));",
                      f"if (kinds.includes('{kind}')) {{",
                      "  pm.environment.unset(key);",
                      f"  pm.test('{kind} filed', () => pm.expect(kinds).to.include('{kind}'));",
                      "} else if (n < max) {",
                      "  pm.environment.set(key, n + 1);",
                      f"  postman.setNextRequest({name!r});",
                      "} else {",
                      "  pm.environment.unset(key);",
                      f"  pm.test('{kind} filed within ' + max + ' polls', () => "
                      f"pm.expect(kinds).to.include('{kind}'));", "}"])


F = []

F.append(("00 · Health & run setup", [
    req("GET /healthz — the edge is up", "GET", REG, "/healthz",
        pre=["// Clear every DERIVED id so a stale value can never target the wrong row.",
             "// ONLY derived state belongs here. The fixed identities (rmEmail, makerEmail,",
             "// checkerEmail) are CONFIGURATION — clearing them breaks downstream requests.",
             "['entityId','leadId','dealId','lendingId','syndicationId','amId','checklistId',",
             " 'checklistId2','assignmentId','interactionId','financialId','documentId',",
             " 'synLenderId','bdrmUserId','makerUserId','checkerUserId','structWorkflowId',",
             " 'convWorkflowId','qualWorkflowId','voxWorkflowId','synWorkflowId','amWorkflowId',",
             " 'synRowId','buyerRowId','calEventId','covenantId','covObsId','ewsCaseId',",
             " 'insDocId','insDocId2','notifId','lendingVersion']",
             "  .forEach(k => pm.environment.unset(k));",
             "// Self-heal the fixed identities if a hand-edited environment lost them.",
             "if (!pm.environment.get('rmEmail')) pm.environment.set('rmEmail', 'e2e.rm@evamfinance.com');",
             "if (!pm.environment.get('makerEmail')) pm.environment.set('makerEmail', 'e2e.maker@evamfinance.com');",
             "if (!pm.environment.get('checkerEmail')) pm.environment.set('checkerEmail', 'e2e.checker@evamfinance.com');",
             "if (!pm.environment.get('registerDirectUrl')) pm.environment.set('registerDirectUrl', 'http://localhost:8000');",
             "if (!pm.environment.get('svcWorkflowsKey')) pm.environment.set('svcWorkflowsKey', 'compose-svc-workflows');",
             "pm.environment.set('runSuffix', String(Date.now()).slice(-6));",
             "// The covenant is defined due YESTERDAY, so the very first sweep generates the",
             "// period AND flags it overdue — the whole recurring loop in one demo run.",
             "pm.environment.set('covFirstDue', new Date(Date.now() - 86400000).toISOString().slice(0, 10));",
             "console.log('run suffix =', pm.environment.get('runSuffix'));"],
        tests=[OK, "pm.test('register reachable through NGINX', () => "
                   "pm.expect(pm.response.json().status).to.eql('ok'));"],
        desc="TLS terminates at NGINX; everything below enters through this one door — except "
             "the clearly-marked MACHINE LANE requests (service-principal plumbing that the "
             "monitors/notifier perform in production, shown here so the demo is deterministic)."),
    req("GET /v1/ref — dropdown vocabulary", "GET", REG, "/v1/ref",
        tests=[OK, "pm.test('reference data seeded', () => "
                   "pm.expect(Object.keys(pm.response.json()).length).to.be.above(0));"]),
]))

F.append(("00b · Sign in (Dex) — required only in the prod posture", [
    token("ADMIN", "userEmail", "adminToken"),
    token("MAKER", "makerEmail", "makerToken"),
    token("CHECKER", "checkerEmail", "checkerToken"),
    token("RM", "rmEmail", "rmToken"),
]))

F.append(("00c · Sign in (Google) — for a deployment whose issuer is Google", [
    google_token("ADMIN", "adminRefreshToken", "adminToken"),
    google_token("MAKER", "makerRefreshToken", "makerToken"),
    google_token("CHECKER", "checkerRefreshToken", "checkerToken"),
    google_token("RM", "rmRefreshToken", "rmToken"),
]))

F.append(("01 · Users, roles & people  (Access via the gateway)", [
    req("POST /access/v1/users — RM (BDRM + Syn RM + AM RM)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{rmEmail}}", "full_name": "E2E Priya Nair",
              "short_name": "Priya", "phone": "+91-9800000001", "is_active": True,
              "roles": ["BDRM", "Syn RM", "AM RM"]},
        tests=[OK_OR_EXISTS],
        desc="Reached at {{baseUrl}}/access — the gateway strips your key and injects Access's."),
    req("POST /access/v1/users — MAKER (Credit Head + Deal Analyst)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{makerEmail}}", "full_name": "E2E Arun Menon",
              "is_active": True, "roles": ["Credit Head", "Deal Analyst"]},
        tests=[OK_OR_EXISTS],
        desc="Sends the committee decision, prepares CP/CS and the handover package, owns the "
             "covenant register and the waiver."),
    req("POST /access/v1/users — CHECKER (Management)", "POST", ACC, "/v1/users",
        headers=_ADMIN,
        body={"email": "{{checkerEmail}}", "full_name": "E2E Divya Rao",
              "is_active": True, "roles": ["Management"]},
        tests=[OK_OR_EXISTS],
        desc="Every APPROVAL that needs a different senior human: the lead-conversion approve, "
             "CP/CS + handover approvals, the syndication and AM decisions, the escalated EWS "
             "closure. Self-approval is refused throughout."),
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
        desc="Conversion refuses an rm that is not a Person on record — full_name matches."),
    req("POST /v1/people — Arun Menon (Analyst on record)", "POST", REG, "/v1/people",
        body={"name": "Arun Menon", "full_name": "E2E Arun Menon", "role": "Analyst",
              "geography": "Karnataka", "inactive": False},
        tests=cap("analystPersonId")),
]))

F.append(("02 · Client (Entity) — the company master row", [
    req("POST /v1/entities", "POST", REG, "/v1/entities",
        body={"code": "ECOSOCH-{{runSuffix}}", "legal_name": "EcoSoch Solar Private Limited",
              "display_name": "EcoSoch Solar", "entity_type": "Company",
              "sector": "Solar - Developer", "lens": "Mitigation",
              "register_status": "Pipeline", "state": "Karnataka",
              "location": "Bengaluru, Karnataka", "cin": "U40106KA2015PTC{{runSuffix}}",
              "notes": "E2E: 150 MW C&I solar developer."},
        tests=cap("entityId"),
        desc="Excel column set 'Client Master' — one PRISM entity row, versioned + audited."),
    req("GET /v1/entities?bogus_filter=x — REFUSED", "GET", REG, "/v1/entities?bogus_filter=x",
        tests=refused(400, 422), desc="Fail-closed filtering."),
]))

F.append(("03 · VOX ▸ the field capture creates the lead + interaction", [
    req("WAIT — orchestrator ready (Temporal up)", "GET", ORC, "/readyz",
        headers=_ADMIN, pre=_SPIN,
        tests=["const max = 40;",
               "const key = 'poll_orc_ready';",
               "let n = Number(pm.environment.get(key) || 0);",
               "if (pm.response.code === 200) {",
               "  pm.environment.unset(key);",
               "  pm.test('orchestrator ready (Temporal reachable)', () => "
               "pm.expect(pm.response.code).to.eql(200));",
               "} else if (n < max) {",
               "  pm.environment.set(key, n + 1);",
               "  postman.setNextRequest('WAIT — orchestrator ready (Temporal up)');",
               "} else {",
               "  pm.environment.unset(key);",
               "  pm.test('orchestrator ready within ' + max + ' polls', () => "
               "pm.expect(pm.response.code).to.eql(200));",
               "}"],
        desc="A cold stack (after `down -v`) takes up to a minute for Temporal to "
             "initialise its store and the worker to start polling — a VOX capture "
             "fired into that window dies with a 500. This request re-runs itself "
             "until the orchestrator reports ready, so the journey never lands on a "
             "cold workflow plane. Collection Runner only."),
    req("POST /orchestrator/v1/workflows/vox-touchpoints?wait=true — RM's field capture",
        "POST", ORC, "/v1/workflows/vox-touchpoints?wait=true", headers=_RM,
        body={"capture_id": "e2e-{{runSuffix}}", "entity_id": "{{entityId}}",
              "interaction_type": "In-Person Meeting",
              "occurred_at": "2026-03-31T10:30:00Z",
              "summary": "Site walkthrough + term sheet discussion (150 MW, 45 Cr, 18m).",
              "notes": "Promoter keen on Q1 drawdown; DSCR headroom tight.",
              "transcript": "…visited the Tumkur site; availability 92%; the CFO walked us "
                            "through the FY25 provisionals; term sheet shared…",
              "location": "Tumkur site, Karnataka", "contact_name": "Ravi Kulkarni",
              "performed_by": "E2E Priya Nair", "assigned_rm": "E2E Priya Nair",
              "assigned_rm_id": "{{bdrmUserId}}",
              "next_action": "Collect FY26 provisional financials",
              "next_action_date": "2026-04-15", "next_meeting_date": "2026-04-20",
              "sector": "Solar - Developer", "lens": "Mitigation"},
        tests=[OK, "const b = pm.response.json();",
               "console.log('VOX result:', JSON.stringify(b.result || b));",
               "const r = b.result || {};",
               "pm.test('the capture resolved to THIS client', () => "
               "pm.expect(r.entity_id).to.eql(pm.environment.get('entityId')));",
               "pm.test('a lead was created', () => pm.expect(r.lead_id).to.be.a('string'));",
               "pm.test('the interaction was logged', () => "
               "pm.expect(r.interaction_id).to.be.a('string'));",
               "pm.environment.set('leadId', r.lead_id);",
               "pm.environment.set('interactionId', r.interaction_id);",
               "pm.environment.set('voxWorkflowId', b.workflow_id);"],
        desc="ONE call replaces three Excel edits: the durable VOX workflow resolves the "
             "company, creates the lead, ASSIGNS the RM as its owner (a real LineAssignment), "
             "logs the full-fidelity interaction and records the follow-up. A retried upload "
             "with the same capture_id replays the same run — exactly-once."),
    req("GET /v1/leads/{id} — the VOX-created lead", "GET", REG, "/v1/leads/{{leadId}}",
        tests=[OK, "const l = pm.response.json();",
               "pm.test('lead is Active with the RM on it', () => {",
               "  pm.expect(l.status).to.eql('Active');",
               "  pm.expect(l.rm).to.eql('E2E Priya Nair'); });"]),
    req("PATCH /v1/leads/{id} — the RM updates the lead", "PATCH", REG,
        "/v1/leads/{{leadId}}", headers=_RM,
        body={"temperature": "Hot",
              "next_action": "Take the ₹45 Cr ask to credit committee",
              "notes": "Term sheet accepted verbally; targeting credit committee this month."},
        tests=[OK, "pm.test('RM updated own lead (scoped write)', () => "
                   "pm.expect(pm.response.json().temperature).to.eql('Hot'));"],
        desc="The RM's own scoped write — allowed because the VOX run assigned ownership; "
             "another RM would be refused."),
    stage("PATCH lead — status → Converted REFUSED", "/v1/leads/{{leadId}}", "status",
          "Converted", tests=refused(400, 403, 422),
          desc="Conversion is an approval flow (folder 05), never a typed status."),
]))

F.append(("04 · Lead qualification (evidence-backed)", [
    req("POST /orchestrator/v1/workflows/lead-qualifications", "POST", ORC,
        "/v1/workflows/lead-qualifications", headers=_ADMIN,
        body={"lead_id": "{{leadId}}", "qualified_by": "{{userEmail}}",
              "qualification_reference": "QUAL/ECOSOCH/{{runSuffix}}", "passed": True},
        tests=[OK, "pm.environment.set('qualWorkflowId', pm.response.json().workflow_id);"],
        desc="Files the qualification review as immutable evidence on the lead. When the "
             "deployment configures WORKFLOWS_QUALIFICATION_CHECKLIST, per-item results are "
             "mandatory and the workflow COMPUTES the outcome."),
    poll_kinds("WAIT · qualification evidence on the lead", "Lead", "leadId",
               "lead_qualification"),
]))

F.append(("05 · Convert → deal (REQUEST + APPROVE, human-in-the-loop)", [
    req("POST /orchestrator/v1/workflows/lead-conversions — the RM REQUESTS", "POST", ORC,
        "/v1/workflows/lead-conversions", headers=_RM,
        body={"lead_id": "{{leadId}}", "requested_by": "{{rmEmail}}",
              "is_lending": True, "is_syndication": True, "is_asset_mon": True,
              "product_type": "Term Loan", "amount_cr": 45.0, "rm": "E2E Priya Nair",
              "rm_id": "{{bdrmUserId}}", "analyst": "E2E Arun Menon",
              "analyst_id": "{{makerUserId}}",
              "note": "E2E: convert with all three product lines."},
        tests=[OK, "const b = pm.response.json();",
               "pm.test('conversion is PENDING APPROVAL', () => "
               "pm.expect(b.workflow_id).to.be.a('string'));",
               "pm.environment.set('convWorkflowId', b.workflow_id);",
               "console.log('approve_url:', b.approve_url);"],
        desc="The request parks durably (days if needed) until a Head decides. SLA reminders "
             "fire while it waits; run-control (cancel/return/resubmit) is available."),
    req("GET /orchestrator/v1/workflows/pending — the APPROVER's Today list shows it", "GET",
        ORC, "/v1/workflows/pending?kind=lead-conversion", headers=_CHECKER,
        tests=[OK, "const b = pm.response.json();",
               "pm.test('the pending conversion is discoverable — no stored 202 needed', () => "
               "pm.expect(b.pending.some(p => p.subject_id === "
               "pm.environment.get('leadId'))).to.be.true);"],
        desc="DISCOVERY: an approver who just logged in finds every waiting decision here — "
             "kind, subject, requester, waiting stage and ready-made approve/reject URLs. "
             "The UI never needs the start response."),
    req("POST /orchestrator …/approve — by the RM is REFUSED", "POST", ORC,
        "/v1/workflows/{{convWorkflowId}}/approve", headers=_RM,
        body={"by": "{{rmEmail}}", "note": "self-serve attempt"},
        tests=refused(401, 403),
        desc="APPROVES ARE AUTHORITY-CHECKED: a BDRM holds no conversion authority — the "
             "orchestrator resolves the decider's roles in Access and refuses."),
    req("POST /orchestrator …/approve — MANAGEMENT approves", "POST", ORC,
        "/v1/workflows/{{convWorkflowId}}/approve", headers=_CHECKER,
        body={"by": "{{checkerEmail}}", "note": "E2E: approved — proceed to structuring."},
        tests=[OK, "console.log('decision:', JSON.stringify(pm.response.json()));"],
        desc="Persist-before-signal: the decision is DURABLY recorded (single-winner) before "
             "the run wakes; a spoofed Temporal signal can never convert a lead."),
    poll("WAIT · poll the LEAD until Converted", "/v1/leads/{{leadId}}", "LEAD",
         "status", "Converted"),
    req("GET /v1/leads/{id} — capture the created deal", "GET", REG, "/v1/leads/{{leadId}}",
        tests=[OK, "const l = pm.response.json();",
               "pm.test('conversion linked the deal', () => "
               "pm.expect(l.converted_deal_id).to.be.a('string'));",
               "pm.environment.set('dealId', l.converted_deal_id);",
               "console.log('dealId =', l.converted_deal_id);"]),
    *[req(f"GET /v1/{res}?deal_id= — resolve + VERIFY {lbl}", "GET", REG,
          f"/v1/{res}?deal_id={{{{dealId}}}}&limit=5",
          tests=[OK, "const b = pm.response.json(); const items = (b && b.items) || [];",
                 f"pm.test('conversion created the {lbl} line', () => "
                 "pm.expect(items.length, 'empty — response: ' + JSON.stringify(b))"
                 ".to.be.above(0));",
                 "if (items.length) {", "  const id = items[0].id;",
                 "  pm.expect(id).to.not.eql(pm.environment.get('leadId'));",
                 "  pm.expect(id).to.not.eql(pm.environment.get('dealId'));",
                 f"  pm.environment.set('{var}', id);", f"  console.log('{var} =', id);",
                 "} else {", f"  pm.environment.unset('{var}');", "}"])
      for res, lbl, var in (("lending", "lending", "lendingId"),
                            ("syndication", "syndication", "syndicationId"),
                            ("asset-monetisation", "asset-monetisation", "amId"))],
]))

F.append(("06 · LENDING ▸ committee APPROVES (conditional) via Temporal", [
    stage("PATCH lending — → Sanctioned by hand is REFUSED", "/v1/lending/{{lendingId}}",
          "stage", "Sanctioned", pre=guard("lendingId"), tests=refused(400, 403, 422),
          desc="The milestone cannot be typed. The workflow below is the ONLY route."),
    req("POST /orchestrator/v1/workflows/deal-structurings — start the run", "POST", ORC,
        "/v1/workflows/deal-structurings", headers=_MAKER,
        body={"deal_id": "{{dealId}}", "requested_by": "{{makerEmail}}",
              "product_type": "Term Loan", "rm": "E2E Priya Nair",
              "credit_note_reference": "CN/ECOSOCH/{{runSuffix}}",
              "decision_timeout_hours": 24},
        tests=[OK, "const b = pm.response.json();",
               "pm.test('workflow started', () => pm.expect(b.workflow_id).to.be.a('string'));",
               "pm.environment.set('structWorkflowId', b.workflow_id);"],
        desc="Walks the LENDING line to 'Note Circulated', files the credit note as versioned "
             "evidence, then WAITS for the committee."),
    req("POST /orchestrator …/committee-decision — CONDITIONAL approval", "POST",
        ORC, "/v1/workflows/{{structWorkflowId}}/committee-decision", headers=_MAKER,
        body={"approved": True, "by": "{{makerEmail}}",
              "committee_reference": "CC/2026/{{runSuffix}}",
              "sanction_letter_reference": "SL/ECOSOCH/{{runSuffix}}",
              "conditions": "Insurance assignment and DSRA top-up before first drawdown.",
              "valid_days": 90,
              "note": "E2E: credit committee approved 45 Cr with conditions."},
        tests=[OK, "const b = pm.response.json();",
               "console.log('decision:', JSON.stringify(b));",
               "pm.test('per-facility outcomes recorded', () => "
               "pm.expect(b.facilities).to.be.an('object'));"],
        desc="A GROUPED submission still records a SEPARATE per-facility decision for every "
             "lending line (a deal-wide result never implicitly sanctions lines). The "
             "conditions + validity window land on each decision, sanction_conditions "
             "evidence is filed, and a SanctionExpiryMonitor starts ticking: if the line sat "
             "at 'Sanctioned' past 90 days, the lapse would be put on the record loudly."),
    poll("WAIT · poll the LENDING line until Sanctioned", "/v1/lending/{{lendingId}}",
         "LENDING", "stage", "Sanctioned"),
    req("GET /v1/deals/{id} — the DEAL stays in the commercial funnel", "GET", REG,
        "/v1/deals/{{dealId}}",
        tests=[OK, "pm.test('deal stage is the funnel (In Pipeline)', () => "
                   "pm.expect(pm.response.json().stage).to.eql('In Pipeline'));"]),
    req("GET /v1/evidence — committee + sanction + conditions on file", "GET", REG,
        "/v1/evidence?subject_type=Lending&subject_id={{lendingId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('lending evidence:', kinds.join(', '));",
               "['credit_committee_approval', 'sanction_letter', 'sanction_conditions',",
               " 'credit_note'].forEach(k =>",
               "  pm.test(k + ' filed', () => pm.expect(kinds).to.include(k)));"]),
]))

F.append(("07 · LENDING ▸ CP/CS — maker → checker RETURNS → v2 → APPROVED", [
    req("POST /v1/evidence — executed_agreement", "POST", REG, "/v1/evidence",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "executed_agreement",
              "reference": "AGR/ECOSOCH/{{runSuffix}}", "sha256": SHA,
              "workflow_id": "{{structWorkflowId}}:lending:{{lendingId}}",
              "run_id": "manual-{{runSuffix}}",
              "note": "E2E: executed facility agreement."},
        headers=_MAKER, tests=[OK],
        desc="Cites the PER-LINE committee decision; the handover reconciles this digest."),
    req("POST /v1/internal/cpcs-checklists — MAKER prepares v1", "POST", REG,
        "/v1/internal/cpcs-checklists", headers=_MAKER,
        body={"lending_id": "{{lendingId}}", "deal_id": "{{dealId}}", "checklist_version": 1,
              "status": "Completed",
              "items": [
                  {"key": "cp_security_perfection", "label": "Security perfected & charge filed",
                   "condition_type": "CP", "required": True, "status": "Completed",
                   "evidence_ref": "SEC/{{runSuffix}}"},
                  {"key": "cp_insurance", "label": "Insurance assigned to the lender",
                   "condition_type": "CP", "required": True, "status": "Completed"}],
              "note": "E2E: v1 — insurance CP has no evidence reference yet."},
        tests=cap("checklistId")),
    req("GET /orchestrator/v1/workflows/pending — the CHECKER discovers v1", "GET", ORC,
        "/v1/workflows/pending?kind=cpcs-checklist", headers=_CHECKER,
        tests=[OK, "const b = pm.response.json();",
               "pm.test('the Completed checklist awaits its check in the Today list', () => "
               "pm.expect(b.pending.some(p => p.checklist_id === "
               "pm.environment.get('checklistId'))).to.be.true);"],
        desc="A maker-finished checklist is status 'Completed' — the Today list reads the "
             "REGISTER queue, so it appears whichever lane prepared it, and carries the "
             "checklist_id + ready-made approve URL."),
    req("POST …/cpcs-checklists/{id}/return — CHECKER RETURNS v1 (reasons mandatory)",
        "POST", REG, "/v1/internal/cpcs-checklists/{{checklistId}}/return", headers=_CHECKER,
        body={"note": "Insurance CP carries no evidence reference — attach and resubmit."},
        tests=[OK, "pm.test('v1 is Returned (and frozen at the database)', () => "
                   "pm.expect(pm.response.json().status).to.eql('Returned'));"],
        desc="THE two-way maker-checker loop: the returned version FREEZES; the amendment is "
             "the NEXT version, so every iteration stays reviewable."),
    req("POST /v1/internal/cpcs-checklists — MAKER amends as v2", "POST", REG,
        "/v1/internal/cpcs-checklists", headers=_MAKER,
        body={"lending_id": "{{lendingId}}", "deal_id": "{{dealId}}", "checklist_version": 2,
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
              "note": "E2E: v2 — insurance evidence attached as returned for."},
        tests=cap("checklistId2")),
    req("GET /v1/internal/cpcs-checklists?status=Completed — v2 back in the queue", "GET",
        REG, "/v1/internal/cpcs-checklists?status=Completed&lending_id={{lendingId}}",
        headers=_CHECKER,
        tests=[OK, "const rows = pm.response.json();",
               "pm.test('v2 awaits the check; the Returned v1 is NOT pending', () => {",
               "  pm.expect(rows.some(r => r.id === "
               "pm.environment.get('checklistId2'))).to.be.true;",
               "  pm.expect(rows.some(r => r.id === "
               "pm.environment.get('checklistId'))).to.be.false;",
               "});"],
        desc="The register-side checker queue: only maker-finished ('Completed') versions "
             "are pending — Returned/Approved versions drop out automatically."),
    req("POST …/cpcs-checklists/{id}/approve — CHECKER APPROVES v2", "POST", REG,
        "/v1/internal/cpcs-checklists/{{checklistId2}}/approve", headers=_CHECKER, tests=[OK],
        desc="A DIFFERENT authenticated user — self-approval is refused. Approving makes "
             "cp_cs_completion FILEABLE; it creates no evidence itself."),
    req("POST /v1/evidence — cp_cs_completion (cites the APPROVED v2)", "POST", REG,
        "/v1/evidence", headers=_CHECKER,
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "cp_cs_completion",
              "reference": "CPCS/ECOSOCH/{{runSuffix}}",
              "decision_ref": "{{checklistId2}}",
              "note": "E2E: CP/CS conditions verified and approved (v2)."},
        tests=[OK, "pm.test('provenance generated from the checklist', () => "
                   "pm.expect(pm.response.json().workflow_id).to.include('cpcs:'));"]),
    stage("PATCH lending — → CP/CS Completed", "/v1/lending/{{lendingId}}", "stage",
          "CP/CS Completed", extra={"remarks": "E2E: CP/CS complete (v2), agreement executed."}),
]))

F.append(("08 · LENDING ▸ handover → SUBMITTED → Advaya ACCEPTS (PRISM's boundary)", [
    stage("PATCH lending — → Ready for Disbursement", "/v1/lending/{{lendingId}}", "stage",
          "Ready for Disbursement",
          extra={"proposed_disbursement_amount": 45.0,
                 "proposed_disbursement_date": "2026-04-30",
                 "remarks": "E2E: drawdown proposed."}),
    req("POST /v1/internal/handover-packages — MAKER prepares", "POST", REG,
        "/v1/internal/handover-packages", headers=_MAKER,
        body={"lending_id": "{{lendingId}}",
              "executed_document_refs": [
                  {"reference": "AGR/ECOSOCH/{{runSuffix}}", "sha256": SHA}],
              "cpcs_checklist_version": 2, "delivery_method": "Secure email",
              "recipient": "advaya-ops@evamfinance.com",
              "note": "E2E: handover package for disbursement."},
        tests=[OK, "const p = pm.response.json();",
               "pm.test('Prepared — stage NOT advanced', () => "
               "pm.expect(p.status).to.eql('Prepared'));",
               "pm.environment.set('pkgSha', p.package_sha256);"]),
    req("GET /orchestrator/v1/workflows/pending — the CHECKER discovers the package", "GET",
        ORC, "/v1/workflows/pending?kind=advaya-handover", headers=_CHECKER,
        tests=[OK, "const b = pm.response.json();",
               "pm.test('the Prepared package awaits checker approval', () => "
               "pm.expect(b.pending.some(p => p.subject_id === "
               "pm.environment.get('lendingId'))).to.be.true);"],
        desc="Handover packages awaiting their check are 'Prepared' — same Today list, "
             "same pattern: subject, requester and the ready-made approve URL."),
    req("POST …/approve — CHECKER APPROVES (stage still does not move)", "POST",
        REG, "/v1/internal/handover-packages/{{lendingId}}/approve", headers=_CHECKER,
        tests=[OK, "pm.test('Approved — PRISM has decided, Advaya has not', () => "
                   "pm.expect(pm.response.json().status).to.eql('Approved'));"],
        desc="Internal maker-checker only. PRISM's workflow boundary is Advaya's "
             "ACCEPTANCE — approval never asserts a disbursement."),
    req("POST …/submit — the package goes TO Advaya", "POST",
        REG, "/v1/internal/handover-packages/{{lendingId}}/submit", headers=_CHECKER,
        tests=[OK, "pm.test('Submitted', () => "
                   "pm.expect(pm.response.json().status).to.eql('Submitted'));"]),
    req("MACHINE LANE · Advaya REJECTS attempt 1", "POST", MREG,
        "/v1/internal/advaya-handoffs", headers=_SVC_ADVAYA,
        body={"handoff_key": "advaya-handoff:{{lendingId}}:r1",
              "lending_id": "{{lendingId}}", "payload_sha256": "{{pkgSha}}",
              "status": "Rejected",
              "note": "Simulated: KYC document illegible — correct and resubmit."},
        tests=[OK],
        desc="MACHINE LANE (svc_advaya key): Advaya's validation answer, simulated. A "
             "rejection reopens PRISM's prepare → approve → submit loop; nothing "
             "downstream may happen off a rejected package."),
    req("GET /v1/lending/{id}/handover-package — Rejected, loop reopened", "GET", REG,
        "/v1/lending/{{lendingId}}/handover-package",
        tests=[OK, "pm.test('package Rejected', () => "
                   "pm.expect(pm.response.json().status).to.eql('Rejected'));"]),
    req("POST /v1/internal/handover-packages — MAKER re-prepares (corrected)", "POST", REG,
        "/v1/internal/handover-packages", headers=_MAKER,
        body={"lending_id": "{{lendingId}}",
              "executed_document_refs": [
                  {"reference": "AGR/ECOSOCH/{{runSuffix}}", "sha256": SHA}],
              "cpcs_checklist_version": 2, "delivery_method": "Secure email",
              "recipient": "advaya-ops@evamfinance.com",
              "note": "E2E: corrected KYC scan attached; resubmission."},
        tests=[OK, "const p = pm.response.json();",
               "pm.test('re-Prepared (same single-winner row)', () => "
               "pm.expect(p.status).to.eql('Prepared'));",
               "pm.environment.set('pkgSha', p.package_sha256);"]),
    req("POST …/approve — CHECKER approves the resubmission", "POST",
        REG, "/v1/internal/handover-packages/{{lendingId}}/approve", headers=_CHECKER,
        tests=[OK]),
    req("POST …/submit — resubmitted to Advaya", "POST",
        REG, "/v1/internal/handover-packages/{{lendingId}}/submit", headers=_CHECKER,
        tests=[OK]),
    req("MACHINE LANE · Advaya ACCEPTS attempt 2", "POST", MREG,
        "/v1/internal/advaya-handoffs", headers=_SVC_ADVAYA,
        body={"handoff_key": "advaya-handoff:{{lendingId}}:r2",
              "lending_id": "{{lendingId}}", "payload_sha256": "{{pkgSha}}",
              "status": "Accepted", "acknowledgement_id": "ADV-ACK/{{runSuffix}}"},
        tests=[OK],
        desc="Advaya validates and ACCEPTS — THIS is where PRISM's workflow stops. The "
             "acknowledgement becomes the package's one-time advaya_reference and the "
             "package freezes (database trigger)."),
    req("GET handover-package — ACCEPTED, acknowledgement stored, package frozen", "GET",
        REG, "/v1/lending/{{lendingId}}/handover-package",
        tests=[OK, "const p = pm.response.json();",
               "pm.test('Accepted + Advaya reference stored', () => {",
               "  pm.expect(p.status).to.eql('Accepted');",
               "  pm.expect(p.advaya_reference).to.eql('ADV-ACK/{{runSuffix}}'); });"]),
    req("GET /v1/lending/{id} — acceptance is NOT fund movement", "GET", REG,
        "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('stage still Ready for Disbursement', () => "
                   "pm.expect(pm.response.json().stage)"
                   ".to.eql('Ready for Disbursement'));"],
        desc="════ PRISM workflow boundary ════ Everything after this folder's end is "
             "Advaya's side: disbursement, repayment, collections, reconciliation, "
             "operational loan closure. PRISM only consumes Advaya's events."),
]))

F.append(("08b · ADVAYA SIMULATION ▸ the downstream system's events (NOT PRISM operations)", [
    req("MACHINE LANE · Advaya disburses tranche T1 (30 Cr) → stage flips", "POST", MREG,
        "/v1/internal/lending/{{lendingId}}/tranches", headers=_SVC_ADVAYA,
        body={"tranche_ref": "T1-{{runSuffix}}", "amount": 30.0,
              "disbursed_on": "2026-04-30", "advaya_reference": "ADV/{{runSuffix}}/1"},
        tests=[OK],
        desc="SIMULATED DOWNSTREAM EVENT: in production this is Advaya's callback after "
             "money actually moved. The FIRST tranche — not any PRISM approval — is what "
             "advances the line to 'Disbursed' and writes the actuals. Idempotent per "
             "ref; append-only; ceiling-bounded. Repayments, collections, penalties and "
             "operational loan closure remain wholly in Advaya and are not modelled "
             "here — PRISM would consume those as read-only status events."),
    req("GET /v1/lending/{id} — Disbursed BY ADVAYA'S EVENT", "GET", REG,
        "/v1/lending/{{lendingId}}",
        tests=[OK, "const l = pm.response.json();",
               "pm.test('Disbursed via advaya-disbursement', () => {",
               "  pm.expect(l.stage).to.eql('Disbursed');",
               "  const h = l.stage_history || [];",
               "  pm.expect(h[h.length-1].source).to.eql('advaya-disbursement'); });"]),
    req("MACHINE LANE · Advaya disburses tranche T2 (15 Cr)", "POST", MREG,
        "/v1/internal/lending/{{lendingId}}/tranches", headers=_SVC_ADVAYA,
        body={"tranche_ref": "T2-{{runSuffix}}", "amount": 15.0,
              "disbursed_on": "2026-05-15", "advaya_reference": "ADV/{{runSuffix}}/2"},
        tests=[OK]),
    req("MACHINE LANE · GET tranches — read-only reconciliation view", "GET", MREG,
        "/v1/internal/lending/{{lendingId}}/tranches", headers=_SVC_ADVAYA,
        tests=[OK, "const t = pm.response.json();",
               "pm.test('45 of 45 Cr disbursed — fully reconciled', () => {",
               "  pm.expect(t.total_disbursed).to.eql(45);",
               "  pm.expect(t.fully_disbursed).to.eql(true); });"],
        desc="PRISM's synchronized VIEW of Advaya's servicing facts — Advaya stays the "
             "system of record for fund movement."),
]))

F.append(("09 · SYNDICATION ▸ mandate run: IM → lender → DECISION → allocation", [
    req("POST /v1/syndication — a LENDER row on the deal", "POST", REG, "/v1/syndication",
        pre=guard("syndicationId"),
        body={"entity_id": "{{entityId}}", "deal_id": "{{dealId}}",
              "status": "Deal Sourced", "potential": "Green Bridge Capital",
              "remarks": "E2E: co-lender approached."},
        tests=cap("synRowId"),
        desc="The mandate's lender-level tracking rows live beside it on the deal; the run "
             "whitelists exactly these rows — a signal naming any other id is ignored."),
    stage("PATCH mandate — → Sanctioned by hand is REFUSED", "/v1/syndication/{{syndicationId}}",
          "status", "Sanctioned", tests=refused(400, 403, 422),
          desc="'Sanctioned' needs VERIFIED syndication_sanction evidence — workflow-only."),
    req("POST /orchestrator/v1/workflows/syndications — start the mandate run", "POST", ORC,
        "/v1/workflows/syndications", headers=_CHECKER,
        body={"syndication_id": "{{syndicationId}}", "deal_id": "{{dealId}}",
              "requested_by": "{{checkerEmail}}",
              "im_reference": "IM/ECOSOCH/{{runSuffix}}"},
        tests=[OK, "pm.environment.set('synWorkflowId', pm.response.json().workflow_id);"],
        desc="Files the IM as versioned evidence (v1) and walks the mandate to IM Circulated."),
    req("POST /orchestrator …/lender-update — the lender responds", "POST", ORC,
        "/v1/workflows/{{synWorkflowId}}/lender-update", headers=_CHECKER, pre=_SPIN,
        body={"lender_row_id": "{{synRowId}}", "status": "Docs Pending",
              "note": "docs list shared with Green Bridge", "by": "{{rmEmail}}"},
        tests=[OK],
        desc="Whitelisted to the run's rows and policy-checked — an illegal jump becomes an "
             "ops event, never a crashed run."),
    req("POST /orchestrator …/syndication-decision — by the RM is REFUSED", "POST", ORC,
        "/v1/workflows/{{synWorkflowId}}/syndication-decision", headers=_RM,
        body={"by": "{{rmEmail}}", "approved": True},
        tests=refused(401, 403),
        desc="Sanctioning a mandate is Syn Head / Management / Admin authority."),
    req("POST /orchestrator …/syndication-decision — MANAGEMENT APPROVES", "POST", ORC,
        "/v1/workflows/{{synWorkflowId}}/syndication-decision", headers=_CHECKER,
        body={"by": "{{checkerEmail}}", "approved": True,
              "sanction_reference": "SYN-SL/ECOSOCH/{{runSuffix}}",
              "note": "E2E: syndication sanctioned at 45 Cr."},
        tests=[OK],
        desc="Persist-before-signal (kind='syndication', subject-bound); the run verifies the "
             "record fail-closed, files VERIFIED syndication_sanction evidence, and only then "
             "can the mandate reach 'Sanctioned'."),
    req("POST /orchestrator …/allocate — the post-sanction split", "POST", ORC,
        "/v1/workflows/{{synWorkflowId}}/allocate", headers=_CHECKER,
        body={"allocations": {"{{synRowId}}": 45.0}, "by": "{{checkerEmail}}"},
        tests=[OK],
        desc="Validated in-run: only the run's lender rows, sum ≤ the mandate amount — an "
             "over-allocation is refused loudly, never absorbed."),
    poll("WAIT · poll the MANDATE until Sanctioned", "/v1/syndication/{{syndicationId}}",
         "SYNDICATION", "status", "Sanctioned"),
    req("GET /v1/evidence — IM + sanction + allocation on file", "GET", REG,
        "/v1/evidence?subject_type=Syndication&subject_id={{syndicationId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('syndication evidence:', kinds.join(', '));",
               "['im_document', 'syndication_sanction', 'syndication_allocation']",
               "  .forEach(k => pm.test(k + ' filed', () => pm.expect(kinds).to.include(k)));"]),
    stage("PATCH mandate — → Disbursed (TERMINAL)", "/v1/syndication/{{syndicationId}}",
          "status", "Disbursed",
          extra={"date_of_sanction": "2026-04-15", "sanctioned_lender": "Green Bridge Capital",
                 "amount_cr": 45.0, "remarks": "E2E: syndicated facility disbursed."}),
    stage("PATCH lender row — → Withdrawn (record closed out)", "/v1/syndication/{{synRowId}}",
          "status", "Withdrawn",
          extra={"remarks": "E2E: allocation recorded on the mandate; tracking row closed."},
          desc="Deal closure (folder 14) requires every line at a terminal — the lender "
               "tracking row is settled once the allocation is on the mandate."),
]))

F.append(("10 · ASSET MONETISATION ▸ teaser → offers → CLOSURE DECISION", [
    req("POST /v1/asset-monetisation — a BUYER row on the deal", "POST", REG,
        "/v1/asset-monetisation", pre=guard("amId"),
        body={"entity_id": "{{entityId}}", "deal_id": "{{dealId}}",
              "status": "Teaser Prepared", "investor": "Green Bridge Capital",
              "notes": "E2E: buyer under NDA discussion."},
        tests=cap("buyerRowId")),
    stage("PATCH mandate — → Closed by hand is REFUSED", "/v1/asset-monetisation/{{amId}}",
          "status", "Closed", tests=refused(400, 403, 422),
          desc="'Closed' needs VERIFIED am_closure_approval evidence — workflow-only."),
    req("POST /orchestrator/v1/workflows/asset-monetisations — start the mandate run", "POST",
        ORC, "/v1/workflows/asset-monetisations", headers=_CHECKER,
        body={"asset_mon_id": "{{amId}}", "deal_id": "{{dealId}}",
              "requested_by": "{{checkerEmail}}",
              "teaser_reference": "TEASER/ECOSOCH/{{runSuffix}}"},
        tests=[OK, "pm.environment.set('amWorkflowId', pm.response.json().workflow_id);"],
        desc="Files the teaser as versioned evidence (v1); walks the mandate to Teaser Shared."),
    req("POST /orchestrator …/buyer-update — buyer engages", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/buyer-update", headers=_CHECKER, pre=_SPIN,
        body={"buyer_row_id": "{{buyerRowId}}", "status": "Teaser Shared",
              "note": "teaser shared under CA", "by": "{{rmEmail}}"}, tests=[OK]),
    req("POST /orchestrator …/record-nda — NDA + data room", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/record-nda", headers=_CHECKER,
        body={"buyer_row_id": "{{buyerRowId}}", "reference": "NDA/GBC/{{runSuffix}}",
              "data_room": True, "by": "{{rmEmail}}"}, tests=[OK],
        desc="Immutable am_nda evidence — the data-room grant is on the record."),
    req("POST /orchestrator …/record-offer — NBO 40 Cr", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/record-offer", headers=_CHECKER,
        body={"buyer_row_id": "{{buyerRowId}}", "kind": "nbo", "amount_cr": 40.0,
              "reference": "NBO/GBC/{{runSuffix}}", "by": "{{rmEmail}}"}, tests=[OK]),
    req("POST /orchestrator …/record-offer — BINDING 45 Cr", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/record-offer", headers=_CHECKER,
        body={"buyer_row_id": "{{buyerRowId}}", "kind": "binding", "amount_cr": 45.0,
              "reference": "BO/GBC/{{runSuffix}}", "by": "{{rmEmail}}"},
        tests=[OK],
        desc="Every offer is immutable evidence; the offer_comparison query returns the "
             "arrival-ordered set — it can never be quietly edited."),
    req("POST /orchestrator …/am-decision — by the RM is REFUSED", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/am-decision", headers=_RM,
        body={"by": "{{rmEmail}}", "approved": True}, tests=refused(401, 403),
        desc="Closing a sale is AM Head / Management / Admin authority."),
    req("POST /orchestrator …/am-decision — MANAGEMENT APPROVES the closure", "POST", ORC,
        "/v1/workflows/{{amWorkflowId}}/am-decision", headers=_CHECKER,
        body={"by": "{{checkerEmail}}", "approved": True,
              "closure_reference": "SPA/GBC/{{runSuffix}}",
              "note": "E2E: binding offer accepted; SPA executed."},
        tests=[OK]),
    poll("WAIT · poll the AM MANDATE until Closed", "/v1/asset-monetisation/{{amId}}",
         "ASSET-MON", "status", "Closed"),
    req("GET /v1/evidence — teaser/NDA/offers/closure on file", "GET", REG,
        "/v1/evidence?subject_type=AssetMonetisation&subject_id={{amId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('AM evidence:', kinds.join(', '));",
               "['teaser_document', 'am_nda', 'am_offer', 'am_closure_approval']",
               "  .forEach(k => pm.test(k + ' filed', () => pm.expect(kinds).to.include(k)));"]),
    stage("PATCH buyer row — → Dropped (record closed out)",
          "/v1/asset-monetisation/{{buyerRowId}}", "status", "Dropped",
          extra={"notes": "E2E: bid concluded — mandate closed with the binding offer."},
          desc="Same closure discipline as the syndication lender row."),
]))

F.append(("11 · Documents ▸ upload → maker≠checker VALIDATE → expiry → REPLACE", [
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
              "content_type": "application/pdf"},
        tests=cap("documentId")),
    req("POST /v1/documents — MAKER uploads the insurance policy (expiring)", "POST", REG,
        "/v1/documents", headers=_MAKER,
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "slot_key": "insurance", "doc_type": "Insurance Policy",
              "title": "Insurance policy — assigned to lender",
              "storage_uri": "s3://prism-e2e/INS-{{runSuffix}}.pdf",
              "expires_on": "2026-01-15"},
        tests=cap("insDocId"),
        desc="The policy's validity window is data, not a spreadsheet note — the expiry "
             "sweep watches it."),
    req("POST /v1/documents/{id}/validate — by its UPLOADER is REFUSED", "POST", REG,
        "/v1/documents/{{insDocId}}/validate", headers=_MAKER, body={},
        tests=refused(422),
        desc="Maker ≠ checker holds for documents too."),
    req("POST /v1/documents/{id}/validate — CHECKER verifies", "POST", REG,
        "/v1/documents/{{insDocId}}/validate", headers=_CHECKER,
        body={"note": "Policy sighted; lender endorsement in place."},
        tests=[OK, "pm.test('Verified by the checker', () => "
                   "pm.expect(pm.response.json().status).to.eql('Verified'));"]),
    req("MACHINE LANE · POST documents/expiry-sweep", "POST", MREG,
        "/v1/internal/documents/expiry-sweep", headers=_SVC, body={"warn_days": 7},
        tests=[OK, "const r = pm.response.json();",
               "const mine = (r.expired || []).filter(d => "
               "d.id === pm.environment.get('insDocId'));",
               "pm.test('the lapsed policy was marked Expired (exactly once)', () => "
               "pm.expect(mine.length).to.eql(1));"],
        desc="In production the per-tenant DocumentExpiryMonitorWorkflow runs this on a "
             "clock and notifies the uploader + ops. Idempotent — a re-run reports nothing."),
    req("GET /v1/documents/{id} — Expired", "GET", REG, "/v1/documents/{{insDocId}}",
        tests=[OK, "pm.test('status = Expired', () => "
                   "pm.expect(pm.response.json().status).to.eql('Expired'));"]),
    req("POST /v1/documents/{id}/replace — MAKER files the renewal", "POST", REG,
        "/v1/documents/{{insDocId}}/replace", headers=_MAKER,
        body={"title": "Insurance policy — renewed FY27",
              "storage_uri": "s3://prism-e2e/INS-{{runSuffix}}-renewed.pdf",
              "expires_on": "2027-06-30"},
        tests=cap("insDocId2"),
        desc="The replacement is a NEW row answering the same slot; the old row chains to it."),
    req("GET /v1/documents/{id} — the old policy is Superseded", "GET", REG,
        "/v1/documents/{{insDocId}}",
        tests=[OK, "const d = pm.response.json();",
               "pm.test('Superseded → successor linked', () => {",
               "  pm.expect(d.status).to.eql('Superseded');",
               "  pm.expect(d.superseded_by).to.eql(pm.environment.get('insDocId2')); });"]),
]))

F.append(("12 · Calendar ▸ schedule → reschedule → COMPLETE (frozen after)", [
    req("POST /v1/calendar-events — the RM schedules the follow-up", "POST", REG,
        "/v1/calendar-events", headers=_RM,
        body={"title": "EcoSoch — collect FY26 provisionals",
              "starts_at": "2026-04-20T09:00:00Z",
              "subject_type": "Lead", "subject_id": "{{leadId}}",
              "attendees": ["{{makerEmail}}"],
              "description": "Follow-up recorded on the VOX capture."},
        tests=cap("calEventId"),
        desc="The meeting tracker column set, as a first-class record."),
    req("PATCH /v1/calendar-events/{id} — reschedule in place", "PATCH", REG,
        "/v1/calendar-events/{{calEventId}}", headers=_RM,
        body={"starts_at": "2026-04-22T10:00:00Z", "location": "Client HQ, Bengaluru"},
        tests=[OK, "pm.test('rescheduled (same identity, audited)', () => "
                   "pm.expect(pm.response.json().location).to.eql('Client HQ, Bengaluru'));"]),
    req("POST /v1/calendar-events/{id}/complete — it happened", "POST", REG,
        "/v1/calendar-events/{{calEventId}}/complete", headers=_RM,
        body={"note": "Met the CFO; FY26 provisionals promised by Friday."},
        tests=[OK, "pm.test('Completed', () => "
                   "pm.expect(pm.response.json().status).to.eql('Completed'));"]),
    req("POST /v1/calendar-events/{id}/cancel — after completion is REFUSED", "POST", REG,
        "/v1/calendar-events/{{calEventId}}/cancel", headers=_RM,
        body={"note": "too late"}, tests=refused(409),
        desc="A completed meeting is a FACT — terminal rows are frozen by the database."),
    req("GET /v1/calendar-events — the RM's calendar", "GET", REG,
        "/v1/calendar-events?limit=50", headers=_RM,
        tests=[OK, "const items = pm.response.json().items || [];",
               "pm.test('own calendar lists the event', () => "
               "pm.expect(items.some(e => e.id === pm.environment.get('calEventId')))"
               ".to.eql(true));"]),
]))

F.append(("13 · Covenants & EWS ▸ breach → case → escalate → close → WAIVER", [
    req("POST /v1/covenants — Credit defines the DSCR covenant", "POST", REG,
        "/v1/covenants", headers=_MAKER,
        body={"entity_id": "{{entityId}}", "deal_id": "{{dealId}}",
              "name": "DSCR >= 1.20 (quarterly)", "covenant_type": "Financial",
              "metric": "dscr", "operator": ">=", "threshold": 1.20,
              "frequency": "Quarterly", "first_due_on": "{{covFirstDue}}",
              "breach_severity": "Red",
              "description": "Tested on certified quarterly financials."},
        tests=cap("covenantId"),
        desc="The DEFINITION (schedule + test). Observations are generated per period by the "
             "recurring sweep — exactly once, ever, per covenant+period."),
    req("MACHINE LANE · POST covenants/run-sweep — generate + flag overdue", "POST", MREG,
        "/v1/internal/covenants/run-sweep", headers=_SVC, body={},
        tests=[OK, "const r = pm.response.json();",
               "console.log('sweep:', JSON.stringify(r).slice(0, 400));",
               "const mine = (r.overdue || []).filter(o => "
               "o.deal_id === pm.environment.get('dealId'));",
               "pm.test('yesterday\\'s period generated AND flagged overdue', () => "
               "pm.expect(mine.length).to.eql(1));",
               "if (mine.length) pm.environment.set('covObsId', mine[0].id);"],
        desc="In production the per-tenant CovenantMonitorWorkflow runs this on a clock and "
             "notifies. One sweep: the period is generated from the schedule and — being past "
             "due — flagged Overdue, reported exactly once."),
    req("POST /v1/monitoring/{id}/result — the tested value FAILS", "POST", REG,
        "/v1/monitoring/{{covObsId}}/result", headers=_MAKER,
        body={"actual_value": 1.05, "note": "Q1 certified financials."},
        tests=[OK, "const b = pm.response.json();",
               "pm.test('BREACHED (1.05 < 1.20)', () => "
               "pm.expect(b.status).to.eql('Breached'));",
               "pm.test('the EWS case opened in the SAME transaction', () => "
               "pm.expect(b.ews_case_id).to.be.a('string'));",
               "pm.environment.set('ewsCaseId', b.ews_case_id);"],
        desc="A recorded breach can never silently lack its early-warning case — deduped, so "
             "a re-submission could never spawn a duplicate."),
    req("GET /v1/deals/{id}/open-items — the breach BLOCKS closure", "GET", REG,
        "/v1/deals/{{dealId}}/open-items",
        tests=[OK, "const r = pm.response.json();",
               "pm.test('closure is blocked while the case + breach are open', () => "
               "pm.expect(r.blocked).to.eql(true));",
               "console.log('open items:', JSON.stringify(r).slice(0, 400));"]),
    req("POST /v1/deals/{id}/close — attempt while blocked is REFUSED", "POST", REG,
        "/v1/deals/{{dealId}}/close", headers=_ADMIN,
        body={"outcome": "won", "note": "trying too early"}, tests=refused(422)),
    req("POST /v1/ews-cases/{id}/assign — Credit assigns the investigator", "POST", REG,
        "/v1/ews-cases/{{ewsCaseId}}/assign", headers=_MAKER,
        body={"assignee": "{{makerEmail}}", "note": "Reviewing drawdown-period DSCR bridge."},
        tests=[OK, "pm.test('UnderInvestigation', () => "
                   "pm.expect(pm.response.json().status).to.eql('UnderInvestigation'));"]),
    req("POST /v1/ews-cases/{id}/escalate — with reasons", "POST", REG,
        "/v1/ews-cases/{{ewsCaseId}}/escalate", headers=_MAKER,
        body={"note": "Headroom below policy floor two quarters running."},
        tests=[OK, "pm.test('Escalated', () => "
                   "pm.expect(pm.response.json().status).to.eql('Escalated'));"],
        desc="If nobody had acted, the case's Temporal run would AUTO-ESCALATE it when the "
             "investigation SLA lapsed — audited as system:sla."),
    req("POST /v1/ews-cases/{id}/close — by the RM is REFUSED", "POST", REG,
        "/v1/ews-cases/{{ewsCaseId}}/close", headers=_RM,
        body={"disposition": "Resolved", "note": "x"}, tests=refused(403),
        desc="An ESCALATED case closes only with senior credit authority — it can never be "
             "quietly buried below the level it escalated to."),
    req("POST /v1/ews-cases/{id}/close — MANAGEMENT closes with a disposition", "POST", REG,
        "/v1/ews-cases/{{ewsCaseId}}/close", headers=_CHECKER,
        body={"disposition": "Resolved",
              "note": "DSCR restored by the waived quarter's one-off O&M cost."},
        tests=[OK, "pm.test('Closed (frozen at the database)', () => "
                   "pm.expect(pm.response.json().status).to.eql('Closed'));"]),
    req("POST /orchestrator/v1/decisions/waiver — senior credit records the WAIVER", "POST",
        ORC, "/v1/decisions/waiver", headers=_MAKER,
        body={"reference": "waiver-{{runSuffix}}", "decision": "Approved",
              "subject_id": "{{covObsId}}", "valid_days": 90,
              "note": "One-off O&M cost; headroom restored by Q3.",
              "by": "{{makerEmail}}"},
        tests=[OK],
        desc="A waiver is a DECISION first: senior credit authority, subject-bound to the "
             "exact observation, MANDATORY validity window — recorded on the single-winner "
             "store before it can take effect anywhere. Through the ORCHESTRATOR: the "
             "verified Credit Head identity is delegated (route-bound signed context) to "
             "the decision store, so this works identically in the dev header-trust and "
             "prod bearer postures — the Register never accepts a client-asserted role."),
    req("POST /v1/monitoring/{id}/waive — the verified, time-boxed waiver", "POST", REG,
        "/v1/monitoring/{{covObsId}}/waive", headers=_MAKER,
        body={"decision_ref": "waiver-{{runSuffix}}"},
        tests=[OK, "const b = pm.response.json();",
               "pm.test('Waived — Granted until a real date', () => {",
               "  pm.expect(b.status).to.eql('Waived');",
               "  pm.expect(b.waiver_status).to.eql('Granted');",
               "  pm.expect(b.waiver_valid_until).to.be.a('string'); });"],
        desc="The endpoint VERIFIES the decision record (authority, subject, window) — a "
             "client field can never excuse a breach. When the 90 days lapse, the sweep flips "
             "it to Expired, the breach is LIVE again, and a fresh Red case opens."),
    req("MACHINE LANE · POST covenants/run-sweep — recurring re-run is a NO-OP", "POST",
        MREG, "/v1/internal/covenants/run-sweep", headers=_SVC, body={},
        tests=[OK, "const r = pm.response.json();",
               "const mine = (r.overdue || []).filter(o => "
               "o.deal_id === pm.environment.get('dealId'));",
               "pm.test('nothing re-reported for this deal (exactly-once)', () => "
               "pm.expect(mine.length).to.eql(0));"]),
]))

F.append(("14 · Deal CLOSURE — open-item validated, note mandatory", [
    req("GET /v1/deals/{id}/open-items — everything is resolved", "GET", REG,
        "/v1/deals/{{dealId}}/open-items",
        tests=[OK, "pm.test('no open EWS cases / covenants / mid-pipeline lines', () => "
                   "pm.expect(pm.response.json().blocked).to.eql(false));"]),
    req("POST /v1/deals/{id}/close — without a note is REFUSED", "POST", REG,
        "/v1/deals/{{dealId}}/close", headers=_ADMIN, body={"outcome": "won"},
        tests=refused(422)),
    req("POST /v1/deals/{id}/close — by the RM is REFUSED", "POST", REG,
        "/v1/deals/{{dealId}}/close", headers=_RM,
        body={"outcome": "won", "note": "self-serve"}, tests=refused(403),
        desc="Closure is stage-change authority (approve_stage_change), never an RM edit."),
    req("POST /v1/deals/{id}/close — Closed Won", "POST", REG,
        "/v1/deals/{{dealId}}/close", headers=_ADMIN,
        body={"outcome": "won",
              "note": "45 Cr disbursed, syndication placed, asset sale closed."},
        tests=[OK, "pm.test('Closed Won', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Closed Won'));"]),
    stage("PATCH deal — re-opening a closed deal is REFUSED", "/v1/deals/{{dealId}}",
          "stage", "In Pipeline", tests=refused(400, 403, 422),
          desc="A closed terminal is final — a revived opportunity is a NEW deal."),
]))

F.append(("15 · Notifications ▸ the in-app inbox", [
    req("MACHINE LANE · POST internal/notifications — notify the RM", "POST", MREG,
        "/v1/internal/notifications", headers=_SVC,
        body={"recipient": "{{rmEmail}}", "event": "deal_closed",
              "severity": "info", "title": "Deal closed — EcoSoch 45 Cr",
              "body": "Facility disbursed, syndication placed, asset sale closed.",
              "subject_type": "Deal", "subject_id": "{{dealId}}",
              "dedupe_key": "e2e-{{runSuffix}}-close"},
        tests=[OK, "pm.environment.set('notifId', pm.response.json().id);"],
        desc="In production the workflows raise these themselves (SLA reminders, expiries, "
             "escalations) and the notifier drives email/SMS/webhook with retry + "
             "dead-letter. Idempotent by dedupe_key — a retry never double-notifies."),
    req("GET /v1/notifications — the RM's inbox", "GET", REG, "/v1/notifications",
        headers=_RM,
        tests=[OK, "const r = pm.response.json();",
               "pm.test('inbox holds the notification, unread', () => {",
               "  pm.expect((r.items || []).some(n => "
               "n.id === pm.environment.get('notifId'))).to.eql(true);",
               "  pm.expect(r.unread).to.be.above(0); });"],
        desc="Recipient-scoped: another user's inbox needs Admin."),
    req("POST /v1/notifications/{id}/read — mark read", "POST", REG,
        "/v1/notifications/{{notifId}}/read", headers=_RM,
        tests=[OK, "pm.test('read_at stamped', () => "
                   "pm.expect(pm.response.json().read_at).to.be.a('string'));"]),
]))

F.append(("16 · FINAL ▸ verify everything, then EXPORT THE EXCEL", [
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
    req("GET /v1/deals/{id} — Closed Won", "GET", REG, "/v1/deals/{{dealId}}",
        tests=[OK, "pm.test('DEAL closed', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Closed Won'));"]),
    req("GET /v1/evidence — the audit-grade trail", "GET", REG,
        "/v1/evidence?subject_type=Lending&subject_id={{lendingId}}",
        tests=[OK, "const b = pm.response.json();",
               "const kinds = (b.items || b).map(e => e.evidence_kind);",
               "console.log('evidence:', kinds.join(', '));",
               "pm.test('cp_cs_completion was MINTED by the approval', () => "
               "pm.expect(kinds).to.include('cp_cs_completion'));"]),
    req("GET /v1/entities/{id}/dossier", "GET", REG, "/v1/entities/{{entityId}}/dossier",
        tests=[OK, "console.log('dossier:', Object.keys(pm.response.json()).join(', '));"]),
    req("GET /v1/audit — every write recorded", "GET", REG,
        "/v1/audit?resource_id={{lendingId}}&limit=200",
        tests=[OK, "pm.test('audit trail exists for this lending line', () => "
                   "pm.expect(pm.response.json().length).to.be.above(0));"]),
    req("GET /v1/export/counts — every register the journey touched", "GET", REG,
        "/v1/export/counts", headers=_ADMIN,
        tests=[OK, "const c = pm.response.json(); const m = c.counts || c;",
               "console.log('counts:', JSON.stringify(m));",
               "['entities','leads','deals','lending_tracker','syndication_tracker',",
               " 'asset_monetisation','interactions','financials','documents',",
               " 'calendar_events','covenants','ews_cases','monitoring_reporting',",
               " 'governance_evidence','workflow_decisions','cp_cs_checklists',",
               " 'advaya_handover_packages','disbursement_tranches','notifications']",
               "  .forEach(t => pm.test(t + ' has rows', () => "
               "pm.expect(m[t], t).to.be.above(0)));"],
        desc="Every activity of the run left rows behind — nineteen registers, one platform."),
    req("GET /v1/export/excel — THE WORKBOOK (use 'Send and Download')", "GET", REG,
        "/v1/export/excel", headers=_ADMIN,
        tests=["pm.test('workbook produced', () => pm.expect(pm.response.code).to.eql(200));",
               "pm.test('it is an .xlsx stream', () => "
               "pm.expect(pm.response.headers.get('Content-Type'))"
               ".to.include('spreadsheetml'));",
               "pm.test('it is not empty', () => "
               "pm.expect(pm.response.responseSize).to.be.above(10000));"],
        desc="THE POINT OF THE JOURNEY: what the Excel tracker used to hold by hand is now an "
             "EXPORT. One sheet per register — client master, leads, interactions, deals, "
             "the lending pipeline with its evidence and CP/CS record, handover + tranches, "
             "syndication, asset monetisation, documents, calendar, covenants + observations, "
             "EWS cases, decisions (committee/waiver/conversion), notifications. In Postman "
             "use **Send and Download** to save the .xlsx."),
]))

col = {"info": {
    "name": "PRISM · E2E FULL (via NGINX, with Temporal)",
    "description":
        "**The complete PRISM-native journey — everything the Excel tracker used to hold, "
        "start to finish, approvals included.**\\n\\n"
        "Every request enters at **one door** — `{{baseUrl}}` = `https://<host>:8443`; the "
        "edge forwards to the gateway which routes by prefix (`/access`, `/orchestrator`, "
        "Register). Postman presents no backend key. The clearly-marked **MACHINE LANE** "
        "requests go direct to the Register with the workflow service key — in production the "
        "monitors/notifier/Advaya make those calls, shown here so the demo is deterministic.\\n\\n"
        "### The chapters\\n"
        "1. **VOX capture** creates the company touchpoint: lead + owner assignment + "
        "full-fidelity interaction + follow-up, in one durable run.\\n"
        "2. The **RM updates the lead**, qualification is filed as evidence, and conversion is "
        "a **request → APPROVE** flow (an RM's self-approval is refused).\\n"
        "3. The **committee approves conditionally** through Temporal (per-facility decisions, "
        "conditions + 90-day validity, sanction-expiry monitor armed).\\n"
        "4. **CP/CS goes both ways**: the checker RETURNS v1 with reasons, v2 is approved, and "
        "only that mints the completion evidence. The **handover approval** disburses; "
        "tranches reconcile to the rupee.\\n"
        "5. **Syndication and Asset Monetisation** run their mandate workflows: versioned "
        "IM/teaser evidence, lender/buyer tracking, authority-checked decisions, allocation, "
        "gated terminals.\\n"
        "6. **Documents** are validated maker≠checker, expire on their real dates, and are "
        "replaced with the chain kept. **Calendar** events complete and freeze.\\n"
        "7. A **covenant breach** opens its EWS case in the same transaction; the case is "
        "worked, escalated and closed with authority; the breach is **waived** only against a "
        "recorded, time-boxed senior decision.\\n"
        "8. The **deal closes** only when nothing is owed (open-item validated), and finally "
        "—\\n"
        "9. **`GET /v1/export/excel`** — the workbook. Every sheet is a register the journey "
        "filled. The spreadsheet is now an OUTPUT, not the system.\\n\\n"
        "### Requirements\\n"
        "Full stack up (`docker compose up -d --build`, including `temporal`, `workflows`, "
        "`orchestrator`), TLS certs generated (`scripts/gen_dev_certs.sh`), SSL verification "
        "OFF in Postman, and `registerDirectUrl` reachable (default `http://localhost:8000`) "
        "for the machine lane. Run **in order** with the Collection Runner; WAIT requests "
        "poll themselves until Temporal settles.\\n\\n"
        "Several requests MUST fail (self-approvals, hand-typed milestones, blocked closure, "
        "maker validating their own document) — their tests pass on refusal.",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    # Dev posture: the token variables are EMPTY, so "Bearer {{adminToken}}" resolves to a
    # bare "Bearer " — which is not an identity and (trailing space) is an illegal header
    # value for the gateway's upstream client, turning every request into a 502. Drop the
    # header client-side whenever it carries no token; with a real token it rides as-is.
    "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
        "const a = pm.request.headers.find(h => h.key.toLowerCase() === 'authorization' && !h.disabled);",
        "if (a && /^\\s*(Bearer|Basic)?\\s*$/i.test(pm.variables.replaceIn(a.value))) {",
        "    pm.request.headers.remove(a.key);",
        "}",
    ]}}],
    "item": [{"name": n, "item": i} for n, i in F]}

env = {"name": "PRISM — Full (via NGINX)", "values": [
    {"key": "baseUrl", "value": "https://localhost:8443", "enabled": True},
    {"key": "accessUrl", "value": "https://localhost:8443/access", "enabled": True},
    {"key": "orchestratorUrl", "value": "https://localhost:8443/orchestrator", "enabled": True},
    # The MACHINE LANE door (service-principal plumbing) + the workflow service key —
    # compose publishes the Register on :8000 and defaults the key to compose-svc-workflows.
    {"key": "registerDirectUrl", "value": "http://localhost:8000", "enabled": True},
    {"key": "svcWorkflowsKey", "value": "compose-svc-workflows", "enabled": True},
    {"key": "svcAdvayaKey", "value": "compose-svc-advaya", "enabled": True},
    {"key": "pkgSha", "value": "", "enabled": True},
    # EMPTY by default = dev posture: folder 00b skips itself and identity comes from header
    # trust. For the prod posture set it to Dex as reachable from YOUR machine.
    {"key": "dexUrl", "value": "", "enabled": True},
    {"key": "ssoPassword", "value": "prism", "enabled": True},
    {"key": "googleClientId", "value": "", "enabled": True},
    {"key": "googleClientSecret", "value": "", "enabled": True},
    {"key": "adminRefreshToken", "value": "", "enabled": True},
    {"key": "makerRefreshToken", "value": "", "enabled": True},
    {"key": "checkerRefreshToken", "value": "", "enabled": True},
    {"key": "rmRefreshToken", "value": "", "enabled": True},
    {"key": "adminToken", "value": "", "enabled": True},
    {"key": "makerToken", "value": "", "enabled": True},
    {"key": "checkerToken", "value": "", "enabled": True},
    {"key": "rmToken", "value": "", "enabled": True},
    # FIXED identities — a bearer can only be issued for someone Dex knows.
    {"key": "rmEmail", "value": "e2e.rm@evamfinance.com", "enabled": True},
    {"key": "tenant", "value": "EVAM", "enabled": True},
    {"key": "userEmail", "value": "admin@evamfinance.com", "enabled": True},
    {"key": "bearerToken", "value": "", "enabled": True},
    {"key": "runSuffix", "value": "", "enabled": True},
    {"key": "covFirstDue", "value": "", "enabled": True},
    {"key": "makerEmail", "value": "e2e.maker@evamfinance.com", "enabled": True},
    {"key": "checkerEmail", "value": "e2e.checker@evamfinance.com", "enabled": True},
    *[{"key": k, "value": "", "enabled": True} for k in (
        "bdrmUserId", "makerUserId", "checkerUserId", "rmPersonId", "analystPersonId",
        "entityId", "leadId", "assignmentId", "interactionId", "dealId", "lendingId",
        "syndicationId", "amId", "checklistId", "checklistId2", "structWorkflowId",
        "convWorkflowId", "qualWorkflowId", "voxWorkflowId", "synWorkflowId",
        "amWorkflowId", "synRowId", "buyerRowId", "synLenderId", "financialId",
        "documentId", "insDocId", "insDocId2", "calEventId", "covenantId", "covObsId",
        "ewsCaseId", "notifId", "lendingVersion")],
]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with open(OUT / "PRISM_E2E_Full.postman_collection.json", "w") as fh:
        json.dump(col, fh, indent=2)
    with open(OUT / "PRISM_Full.postman_environment.json", "w") as fh:
        json.dump(env, fh, indent=2)
    # A second, ready-made environment for the Dex/prod-posture run. Identical except dexUrl is
    # FILLED, which is the one switch that turns folder 00b on.
    dex_env = {"name": "PRISM — Full (via NGINX) · Dex prod posture",
               "values": [dict(v, value="http://localhost:5556") if v["key"] == "dexUrl"
                          else dict(v) for v in env["values"]]}
    with open(OUT / "PRISM_Full_Dex.postman_environment.json", "w") as fh:
        json.dump(dex_env, fh, indent=2)
    n = sum(len(f["item"]) for f in col["item"])
    print(f"PRISM_E2E_Full: {len(col['item'])} folders · {n} requests · 2 environments")
    # Post-generation gate: statically simulate the whole run and REFUSE to emit a collection
    # where any variable is consumed before something writes it.
    import subprocess
    allow = ("adminToken,makerToken,checkerToken,rmToken,bearerToken,dexUrl,ssoPassword,"
             "googleClientId,googleClientSecret,adminRefreshToken,makerRefreshToken,"
             "checkerRefreshToken,rmRefreshToken")
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
