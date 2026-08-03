"""Generate the SEQUENTIAL end-to-end journey collection (postman/PRISM_E2E_Journey…json).

Unlike the reference collections (which mirror the OpenAPI surface one request per endpoint), this
one is a SCRIPT: hardcoded, realistic values in business order, so `Run collection` in Postman
walks a whole deal from user creation to product lines and asserts every step.

    user + roles → client → lead → interaction → convert (deal + 3 lines)
                 → lending pipeline → syndication pipeline → asset-monetisation pipeline
                 → financials → verification → governance (evidence-gated sanction)

Every value below is checked against the code that validates it: required fields from the
OpenAPI schemas, stage values from rbac.STAGE_VOCAB / ALLOWED_TRANSITIONS, dropdown values from
app.seed.refdata, and the evidence gates from policy.EVIDENCE_FOR_STAGE.

    python scripts/gen_e2e_postman.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postman"

# Register/Access hosts. The Register goes through the edge; the ACCESS service is not
# prefix-routed by the gateway, so user provisioning talks to it directly.
REG = "{{baseUrl}}"
ACC = "{{accessUrl}}"

_REG_HEADERS = [
    {"key": "X-Tenant", "value": "{{tenant}}"},
    {"key": "X-User-Email", "value": "{{userEmail}}"},
    {"key": "X-Actor", "value": "e2e-runner"},
]
# Access is reached directly, so it needs its own api key + an Admin identity (governance writes).
_ACC_HEADERS = [
    {"key": "X-API-Key", "value": "{{apiKey}}"},
    {"key": "X-Tenant", "value": "{{tenant}}"},
    {"key": "X-User-Email", "value": "{{userEmail}}"},
    {"key": "X-User-Roles", "value": "Admin"},
]

OK = "pm.test('status ok', () => pm.expect(pm.response.code).to.be.oneOf([200, 201]));"


def req(name, method, host, path, *, body=None, tests=None, headers=None,
        prerequest=None, desc=None, form=None):
    """One collection item. `tests`/`prerequest` are lists of JS lines."""
    hdrs = [dict(h) for h in (headers if headers is not None
                              else (_ACC_HEADERS if host == ACC else _REG_HEADERS))]
    request = {"method": method, "header": hdrs,
               "url": {"raw": host + path,
                       "host": [host],
                       "path": [s for s in path.split("/") if s]}}
    if desc:
        request["description"] = desc
    if body is not None:
        hdrs.append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                           "options": {"raw": {"language": "json"}}}
    if form is not None:
        request["body"] = {"mode": "formdata", "formdata": form}
    item = {"name": name, "request": request, "event": []}
    if prerequest:
        item["event"].append({"listen": "prerequest",
                              "script": {"type": "text/javascript", "exec": prerequest}})
    item["event"].append({"listen": "test",
                          "script": {"type": "text/javascript", "exec": tests or [OK]}})
    return item


def capture(var, field="id"):
    return [OK, f"pm.environment.set('{var}', pm.response.json().{field});",
            f"console.log('{var} =', pm.environment.get('{var}'));"]


def expect_refused(*codes):
    """A negative test: the platform MUST refuse. A 2xx here is a control failure."""
    lst = list(codes)
    return [f"pm.test('refused as designed ({'/'.join(map(str, lst))})', () => "
            f"pm.expect(pm.response.code).to.be.oneOf({lst}));",
            "pm.test('and explains why', () => pm.expect(pm.response.text()).to.not.be.empty);"]


def stage_patch(name, path, field, value, *, tests=None, extra=None):
    body = {field: value}
    if extra:
        body.update(extra)
    return req(name, "PATCH", REG, path, body=body,
               tests=tests or [OK, f"pm.test('{field} = {value}', () => "
                                   f"pm.expect(pm.response.json().{field}).to.eql('{value}'));"])


# --------------------------------------------------------------------------- #
# 00 — Health. Also stamps a per-run suffix so the journey is RE-RUNNABLE:
# entity `code` is UNIQUE(tenant_id, code), so a fixed code would 409 on the 2nd run.
# --------------------------------------------------------------------------- #
folders = []

folders.append(("00 · Health & run setup", [
    req("GET /healthz (through the edge)", "GET", REG, "/healthz",
        prerequest=[
            "// One suffix per run — keeps codes/emails unique so the journey can be re-run.",
            "pm.environment.set('runSuffix', String(Date.now()).slice(-6));",
            "console.log('run suffix =', pm.environment.get('runSuffix'));"],
        tests=[OK, "pm.test('register reachable via NGINX', () => "
                   "pm.expect(pm.response.json().status).to.eql('ok'));"],
        desc="Confirms TLS + edge + gateway + Register are all up before anything is written."),
    req("GET /v1/ref (dropdown vocabulary)", "GET", REG, "/v1/ref",
        tests=[OK, "pm.test('reference data seeded', () => "
                   "pm.expect(Object.keys(pm.response.json()).length).to.be.above(0));"],
        desc="Every hardcoded Sector/Stage/Type below comes from this vocabulary."),
]))

# --------------------------------------------------------------------------- #
# 01 — Users & roles (ACCESS service, Admin-only governance writes)
# --------------------------------------------------------------------------- #
folders.append(("01 · Users & roles (Access service)", [
    req("POST /v1/users — BDRM (the deal owner)", "POST", ACC, "/v1/users",
        body={"email": "e2e.bdrm.{{runSuffix}}@evamfinance.com",
              "full_name": "E2E Priya Nair", "short_name": "Priya",
              "phone": "+91-9800000001", "is_active": True,
              # Roles STACK. /convert verifies rm_id as a 'Syn RM' (syndication line) and an
              # 'AM RM' (asset-monetisation line), so BDRM alone would be refused with
              # "does not hold a role permitting a 'Syn RM' assignment".
              "roles": ["BDRM", "Syn RM", "AM RM"]},
        tests=capture("bdrmUserId") + [
            "pm.test('holds every role /convert will verify', () => "
            "['BDRM','Syn RM','AM RM'].forEach(r => "
            "pm.expect(pm.response.json().roles).to.include(r)));",
            "pm.environment.set('bdrmEmail', pm.response.json().email);"],
        desc="The RM who will own the lead. BDRM is SCOPED on leads/deals — their own book only."),
    req("POST /v1/users — Credit Head (maker)", "POST", ACC, "/v1/users",
        body={"email": "e2e.credit.maker.{{runSuffix}}@evamfinance.com",
              "full_name": "E2E Arun Menon", "is_active": True,
              # 'Deal Analyst' is required because /convert verifies analyst_id as the lending
              # line's Deal Analyst assignee; Credit Head carries the CP/CS + handover authority.
              "roles": ["Credit Head", "Deal Analyst"]},
        tests=capture("makerUserId") + [
            "pm.environment.set('makerEmail', pm.response.json().email);"],
        desc="Senior credit authority (CP/CS + handover) AND the lending line's Deal Analyst."),
    req("POST /v1/users — Management (checker)", "POST", ACC, "/v1/users",
        body={"email": "e2e.checker.{{runSuffix}}@evamfinance.com",
              "full_name": "E2E Divya Rao", "is_active": True, "roles": ["Management"]},
        tests=capture("checkerUserId") + [
            "pm.environment.set('checkerEmail', pm.response.json().email);"],
        desc="A DIFFERENT person — maker-checker requires it; self-approval is refused."),
    req("GET /v1/resolve — the BDRM's effective matrix", "GET", ACC,
        "/v1/resolve?email={{bdrmEmail}}",
        tests=[OK, "const b = pm.response.json();",
               "pm.test('leads view is SCOPED for a BDRM', () => "
               "pm.expect(b.views.leads).to.eql('SCOPED'));",
               "pm.test('may add a lead', () => pm.expect(b.operations.add_lead).to.eql('FULL'));"],
        desc="Proves the RBAC matrix the gateway will enforce for this user."),
    # ------------------------------------------------------------------ #
    # The Register keeps its OWN people directory, separate from Access's identity/roles.
    # /convert refuses an `rm`/`analyst` that is not a Person on record ("not free-text"),
    # so both names must exist here BEFORE folder 04 runs.
    # ------------------------------------------------------------------ #
    req("POST /v1/people — Priya Nair (RM on record)", "POST", REG, "/v1/people",
        body={"name": "Priya Nair", "full_name": "E2E Priya Nair", "role": "RM",
              "email": "e2e.bdrm.{{runSuffix}}@evamfinance.com",
              "phone": "+91-9800000001", "geography": "Karnataka",
              "sectors": "Solar - Developer; BESS / Energy Storage",
              "started_on": "2024-06-01", "inactive": False,
              "notes": "E2E: relationship manager."},
        tests=capture("rmPersonId") + [
            "pm.test('full_name is what /convert matches on', () => "
            "pm.expect(pm.response.json().full_name).to.eql('E2E Priya Nair'));"],
        desc="`full_name` must EXACTLY equal the `rm` string sent to /convert — the Register "
             "looks it up in the people table, tenant-scoped and not-deleted."),
    req("POST /v1/people — Arun Menon (Analyst on record)", "POST", REG, "/v1/people",
        body={"name": "Arun Menon", "full_name": "E2E Arun Menon", "role": "Analyst",
              "email": "e2e.credit.maker.{{runSuffix}}@evamfinance.com",
              "geography": "Karnataka", "sectors": "Solar - Developer",
              "started_on": "2023-04-01", "inactive": False,
              "notes": "E2E: credit analyst."},
        tests=capture("analystPersonId")),
    req("GET /v1/people?q= — both are on record", "GET", REG, "/v1/people?q=E2E&limit=10",
        tests=[OK, "const names = pm.response.json().items.map(p => p.full_name);",
               "pm.test('RM on record', () => pm.expect(names).to.include('E2E Priya Nair'));",
               "pm.test('Analyst on record', () => pm.expect(names).to.include('E2E Arun Menon'));"],
        desc="If this fails, folder 04's /convert will 422 with \"not a person on record\"."),
]))

# --------------------------------------------------------------------------- #
# 02 — Client (entity)
# --------------------------------------------------------------------------- #
folders.append(("02 · Client (Entity)", [
    req("POST /v1/entities — create the client", "POST", REG, "/v1/entities",
        body={"code": "ECOSOCH-{{runSuffix}}",
              "legal_name": "EcoSoch Solar Private Limited",
              "display_name": "EcoSoch Solar",
              "entity_type": "Company", "sector": "Solar - Developer",
              "sub_sector": "C&I Rooftop + Open Access", "lens": "Mitigation",
              "register_status": "Pipeline", "state": "Karnataka",
              "location": "Bengaluru, Karnataka",
              # CIN is capped at 21 chars: 15 literal + a 6-digit run suffix.
              "cin": "U40106KA2015PTC{{runSuffix}}",
              "pan": "AABCE1234F", "gstin": "29AABCE1234F1Z5",
              "notes": "E2E journey — 150 MW C&I solar developer."},
        tests=capture("entityId") + [
            "pm.test('code echoed', () => pm.expect(pm.response.json().code)"
            ".to.eql('ECOSOCH-' + pm.environment.get('runSuffix')));"],
        desc="UNIQUE(tenant_id, code) — the run suffix keeps re-runs from colliding."),
    req("GET /v1/entities/{id} — read it back", "GET", REG, "/v1/entities/{{entityId}}",
        tests=[OK, "pm.test('version starts at 1', () => "
                   "pm.expect(pm.response.json().version).to.eql(1));",
               "pm.environment.set('entityVersion', pm.response.json().version);"]),
    req("GET /v1/entities?q= — keyset search finds it", "GET", REG,
        "/v1/entities?q=EcoSoch&limit=10&with_total=true",
        tests=[OK, "const b = pm.response.json();",
               "pm.test('paged envelope', () => pm.expect(b).to.have.keys("
               "['items','count','next_cursor','total']));",
               "pm.test('our client is listed', () => pm.expect(b.items.map(r => r.id))"
               ".to.include(pm.environment.get('entityId')));"]),
    req("GET /v1/entities?bogus_filter=x — unknown filter REFUSED", "GET", REG,
        "/v1/entities?bogus_filter=x",
        tests=expect_refused(400, 422),
        desc="Fail-closed filtering: an unrecognised query param is rejected, never ignored."),
]))

# --------------------------------------------------------------------------- #
# 03 — Lead + assignment + interaction
# --------------------------------------------------------------------------- #
folders.append(("03 · Lead, ownership & interaction", [
    req("POST /v1/leads — open the lead (Active)", "POST", REG, "/v1/leads",
        body={"company": "EcoSoch Solar Private Limited", "entity_id": "{{entityId}}",
              "status": "Active", "sector": "Solar - Developer", "lens": "Mitigation",
              "source": "RM", "source_name": "E2E Priya Nair", "temperature": "Warm",
              "rm": "E2E Priya Nair",
              "contact": "Ravi Kulkarni", "designation": "Director — Finance",
              "phone": "+91-9800000010",
              "next_action": "Collect audited FY25 financials",
              "next_action_date": "2026-04-15",
              "notes": "Captured in the field; 45 Cr term loan for a 150 MW pipeline."},
        tests=capture("leadId") + [
            "pm.test('lifecycle starts Active', () => "
            "pm.expect(pm.response.json().status).to.eql('Active'));"],
        desc="INITIAL_STATUS allows only Active / On Hold / Dropped at creation."),
    req("POST /v1/assignments — make the BDRM the owner", "POST", REG, "/v1/assignments",
        body={"user_id": "{{bdrmUserId}}", "subject_type": "Lead", "subject_id": "{{leadId}}",
              "assignment_role": "BDRM", "note": "E2E: primary owner."},
        tests=capture("assignmentId"),
        desc="A real LineAssignment — this is what makes the RM's SCOPED reads/writes cover it."),
    req("POST /v1/leads/{id}/interactions — log the meeting", "POST", REG,
        "/v1/leads/{{leadId}}/interactions",
        body={"interaction_type": "In-Person Meeting", "occurred_at": "2026-03-31T10:30:00Z",
              "summary": "Site walkthrough + term sheet discussion (150 MW, 45 Cr, 18m tenor).",
              "notes": "Met the promoter at the Tumkur site. Sanction letter target: Q2.",
              "direction": "Outbound", "source": "PRISM-E2E",
              "contact_name": "Ravi Kulkarni",
              "attendees": ["Ravi Kulkarni", "E2E Priya Nair"],
              "location": "Tumkur site, Karnataka",
              "performed_by": "E2E Priya Nair",
              "outcome": "Positive — proceed to diligence",
              "next_action": "Collect audited financials FY25",
              "next_action_date": "2026-04-15"},
        tests=capture("interactionId")),
    req("GET /v1/leads/{id}/interactions — timeline", "GET", REG,
        "/v1/leads/{{leadId}}/interactions?limit=50",
        tests=[OK, "pm.test('timeline has our interaction', () => "
                   "pm.expect(pm.response.json().length).to.be.above(0));"],
        desc="NOTE: nested timelines take `limit` but have NO cursor — capped at 1000."),
    stage_patch("PATCH /v1/leads/{id} — Active → Converted DIRECTLY is refused",
                "/v1/leads/{{leadId}}", "status", "Converted",
                tests=expect_refused(400, 403, 422)),
]))

# --------------------------------------------------------------------------- #
# 04 — Convert: deal + all three product lines in ONE transaction
# --------------------------------------------------------------------------- #
folders.append(("04 · Convert lead → deal + product lines", [
    req("POST /v1/leads/{id}/convert — atomic conversion", "POST", REG,
        "/v1/leads/{{leadId}}/convert",
        body={"is_lending": True, "is_syndication": True, "is_asset_mon": True,
              "product_type": "Term Loan", "amount_cr": 45.0,
              "rm": "E2E Priya Nair", "rm_id": "{{bdrmUserId}}",
              "analyst": "E2E Arun Menon", "analyst_id": "{{makerUserId}}",
              "approved_by": "{{checkerEmail}}",
              "note": "E2E: convert with all three product lines."},
        tests=[OK, "const b = pm.response.json();",
               "console.log('convert result', JSON.stringify(b));",
               "// The result carries the new deal + the lines it created.",
               "const deal = b.deal_id || (b.deal && b.deal.id);",
               "pm.test('a deal was created', () => pm.expect(deal).to.be.a('string'));",
               "pm.environment.set('dealId', deal);",
               "const pick = (k) => b[k + '_id'] || (b[k] && b[k].id) ||",
               "  (b.lines && b.lines[k] && b.lines[k].id);",
               "['lending','syndication','asset_monetisation'].forEach(k => {",
               "  const v = pick(k); if (v) pm.environment.set(k + 'IdRaw', v); });",
               "if (pm.environment.get('lendingIdRaw'))",
               "  pm.environment.set('lendingId', pm.environment.get('lendingIdRaw'));",
               "if (pm.environment.get('syndicationIdRaw'))",
               "  pm.environment.set('syndicationId', pm.environment.get('syndicationIdRaw'));",
               "if (pm.environment.get('asset_monetisationIdRaw'))",
               "  pm.environment.set('amId', pm.environment.get('asset_monetisationIdRaw'));"],
        desc="Conversion is transactional ON PURPOSE: deal + product lines commit together, so a "
             "converted lead can never exist without its lines. Direct status editing is refused "
             "(see the previous request) precisely to force this path."),
    req("GET /v1/lending?entity_id= — resolve the lending line", "GET", REG,
        "/v1/lending?entity_id={{entityId}}&limit=5",
        tests=[OK, "const items = pm.response.json().items;",
               "pm.test('lending line exists', () => pm.expect(items.length).to.be.above(0));",
               "pm.environment.set('lendingId', items[0].id);",
               "pm.environment.set('lendingVersion', items[0].version);",
               "pm.test('starts pre-sanction', () => pm.expect(items[0].stage)"
               ".to.be.oneOf(['Data Awaited','Diligence']));"],
        desc="Belt-and-braces: works whether or not the convert response inlines the line ids."),
    req("GET /v1/syndication?entity_id=", "GET", REG,
        "/v1/syndication?entity_id={{entityId}}&limit=5",
        tests=[OK, "const items = pm.response.json().items;",
               "pm.test('syndication line exists', () => pm.expect(items.length).to.be.above(0));",
               "pm.environment.set('syndicationId', items[0].id);"]),
    req("GET /v1/asset-monetisation?entity_id=", "GET", REG,
        "/v1/asset-monetisation?entity_id={{entityId}}&limit=5",
        tests=[OK, "const items = pm.response.json().items;",
               "pm.test('AM line exists', () => pm.expect(items.length).to.be.above(0));",
               "pm.environment.set('amId', items[0].id);"]),
]))

# --------------------------------------------------------------------------- #
# 05 — Lending pipeline (stops at the evidence gate, by design)
# --------------------------------------------------------------------------- #
folders.append(("05 · Lending pipeline (to the evidence gate)", [
    stage_patch("PATCH lending — → Diligence", "/v1/lending/{{lendingId}}", "stage", "Diligence",
                extra={"remarks": "E2E: diligence started.", "amount_cr": 45.0,
                       "rm": "E2E Priya Nair", "analyst": "E2E Arun Menon"}),
    stage_patch("PATCH lending — → Note Circulated", "/v1/lending/{{lendingId}}",
                "stage", "Note Circulated",
                extra={"remarks": "E2E: credit note circulated to committee."}),
    stage_patch("PATCH lending — → Ready for Disbursement SKIPS a stage → refused",
                "/v1/lending/{{lendingId}}", "stage", "Ready for Disbursement",
                tests=expect_refused(400, 422)),
    stage_patch("PATCH lending — → Sanctioned WITHOUT evidence → refused",
                "/v1/lending/{{lendingId}}", "stage", "Sanctioned",
                tests=expect_refused(400, 403, 422)),
    req("GET /v1/lending/{id} — still Note Circulated", "GET", REG, "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('gate held the line', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Note Circulated'));",
               "pm.environment.set('lendingVersion', pm.response.json().version);"],
        desc="THE POINT OF THIS FOLDER: 'Sanctioned' is unreachable by typing it. It needs "
             "credit_committee_approval + sanction_letter evidence on file "
             "(policy.EVIDENCE_FOR_STAGE). Folder 09 records those properly."),
    req("PATCH lending with a STALE If-Match → 409", "PATCH", REG, "/v1/lending/{{lendingId}}",
        headers=_REG_HEADERS + [{"key": "If-Match", "value": '"1"'}],
        body={"remarks": "E2E: stale-version write must lose."},
        tests=expect_refused(409, 412),
        desc="Optimistic locking: the row is past version 1 by now, so this must conflict."),
]))

# --------------------------------------------------------------------------- #
# 06 — Syndication pipeline (full walk, no evidence gate)
# --------------------------------------------------------------------------- #
syn = [stage_patch(f"PATCH syndication — → {v}", "/v1/syndication/{{syndicationId}}", "status", v)
       for v in ("Docs Pending", "IM in Prep", "IM Circulated", "Queries Received", "IP Received")]
folders.append(("06 · Syndication pipeline", [
    req("POST /v1/syndication/{id}/lenders — invite a lender", "POST", REG,
        "/v1/syndication/{{syndicationId}}/lenders",
        body={"lender_name": "Green Bridge Capital", "is_existing": False,
              "status": "Approached", "since": "2026-03-31",
              "note": "E2E: first co-lender approached."},
        tests=capture("synLenderId")),
    *syn,
    stage_patch("PATCH syndication — → Disbursed SKIPPING Sanctioned → refused",
                "/v1/syndication/{{syndicationId}}", "status", "Disbursed",
                tests=expect_refused(400, 422)),
    req("GET /v1/syndication/{id}/lenders", "GET", REG,
        "/v1/syndication/{{syndicationId}}/lenders",
        tests=[OK, "pm.test('lender is on the mandate', () => "
                   "pm.expect(pm.response.json().length).to.be.above(0));"]),
]))

# --------------------------------------------------------------------------- #
# 07 — Asset monetisation (full walk to Closed)
# --------------------------------------------------------------------------- #
folders.append(("07 · Asset monetisation pipeline", [
    stage_patch(f"PATCH AM — → {v}", "/v1/asset-monetisation/{{amId}}", "status", v)
    for v in ("Teaser Shared", "In Discussion", "NBO Received", "BO Received",
              "SPA / Documentation", "Closed")
]))

# --------------------------------------------------------------------------- #
# 08 — Financials + a document
# --------------------------------------------------------------------------- #
folders.append(("08 · Financials & documents", [
    req("POST /v1/financials — FY25 audited", "POST", REG, "/v1/financials",
        body={"entity_id": "{{entityId}}", "statement_type": "Audited",
              "period_end": "2026-03-31", "period_start": "2025-04-01",
              "fiscal_year": "FY2025-26", "period_type": "Annual",
              "currency": "INR", "scale": "Crore", "is_audited": True,
              "is_consolidated": False,
              "revenue": 128.4, "ebitda": 21.7, "pat": 9.3,
              "net_worth": 64.2, "total_debt": 55.0, "dscr": 1.62,
              "provenance": "E2E: audited FY25 pack."},
        tests=capture("financialId")),
    req("GET /v1/financials/history", "GET", REG,
        "/v1/financials/history?entity_id={{entityId}}&statement_type=Audited",
        tests=[OK]),
    req("POST /v1/documents — register the sanction letter reference", "POST", REG,
        "/v1/documents",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "doc_type": "Sanction Letter",
              "title": "Sanction letter — EcoSoch Solar 45 Cr",
              "storage_uri": "s3://prism-e2e/SL-ECOSOCH-{{runSuffix}}.pdf",
              "original_filename": "SL-ECOSOCH-{{runSuffix}}.pdf",
              "content_type": "application/pdf", "status": "Received",
              "uploaded_by": "E2E Arun Menon",
              "notes": "E2E: reference-only registration (no binary upload)."},
        tests=[OK, "if (pm.response.code < 300 && pm.response.json().id) "
                   "pm.environment.set('documentId', pm.response.json().id);"]),
]))

# --------------------------------------------------------------------------- #
# 09 — Governance: record the committee decision, mint evidence, THEN sanction
# --------------------------------------------------------------------------- #
folders.append(("09 · Governance — decision → evidence → Sanctioned", [
    req("POST /v1/internal/decisions — durable committee decision", "POST", REG,
        "/v1/internal/decisions",
        body={"workflow_id": "e2e-committee-{{runSuffix}}", "decision": "Approved",
              "kind": "credit_committee", "subject_type": "Lending",
              "subject_id": "{{lendingId}}",
              "committee_reference": "CC/2026/{{runSuffix}}",
              "sanction_letter_reference": "SL/ECOSOCH/{{runSuffix}}",
              "note": "E2E: credit committee approved 45 Cr."},
        tests=[OK, "pm.environment.set('decisionWorkflowId', 'e2e-committee-' + "
                   "pm.environment.get('runSuffix'));",
               "if (pm.response.json().id) pm.environment.set('decisionId', "
               "pm.response.json().id);"],
        desc="Evidence of kind credit_committee_approval / sanction_letter is VERIFIED against a "
             "recorded decision (verify_source='committee') — a caller cannot simply assert it."),
    req("POST /v1/evidence — credit_committee_approval", "POST", REG, "/v1/evidence",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "credit_committee_approval",
              "reference": "CC/2026/{{runSuffix}}",
              "workflow_id": "e2e-committee-{{runSuffix}}",
              "decision_ref": "e2e-committee-{{runSuffix}}",
              "note": "E2E: committee minutes."},
        tests=[OK]),
    req("POST /v1/evidence — sanction_letter", "POST", REG, "/v1/evidence",
        body={"subject_type": "Lending", "subject_id": "{{lendingId}}",
              "evidence_kind": "sanction_letter",
              "reference": "SL/ECOSOCH/{{runSuffix}}",
              "workflow_id": "e2e-committee-{{runSuffix}}",
              "decision_ref": "e2e-committee-{{runSuffix}}",
              "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
              "note": "E2E: issued sanction letter."},
        tests=[OK]),
    req("GET /v1/evidence — both kinds on file", "GET", REG,
        "/v1/evidence?subject_type=Lending&subject_id={{lendingId}}",
        tests=[OK, "const kinds = (pm.response.json().items || pm.response.json())"
                   ".map(e => e.evidence_kind);",
               "console.log('evidence on file:', kinds.join(', '));"]),
    stage_patch("PATCH lending — → Sanctioned (now permitted)",
                "/v1/lending/{{lendingId}}", "stage", "Sanctioned",
                extra={"amount_cr": 45.0, "sanction_date": "2026-03-31",
                       "remarks": "E2E: sanctioned by committee CC/2026."}),
    req("GET /v1/lending/{id} — confirm Sanctioned", "GET", REG, "/v1/lending/{{lendingId}}",
        tests=[OK, "pm.test('sanctioned only WITH evidence', () => "
                   "pm.expect(pm.response.json().stage).to.eql('Sanctioned'));"],
        desc="Contrast with folder 05: identical request, opposite outcome — the difference is "
             "recorded evidence, not permission."),
    stage_patch("PATCH lending — → CP/CS Completed still refused (needs an APPROVED checklist)",
                "/v1/lending/{{lendingId}}", "stage", "CP/CS Completed",
                tests=expect_refused(400, 403, 422)),
]))

# --------------------------------------------------------------------------- #
# 10 — Verification
# --------------------------------------------------------------------------- #
folders.append(("10 · Verification & audit", [
    req("GET /v1/entities/{id}/dossier — the whole relationship", "GET", REG,
        "/v1/entities/{{entityId}}/dossier",
        tests=[OK, "console.log('dossier keys:', Object.keys(pm.response.json()).join(', '));"]),
    req("GET /v1/audit — the trail for this lending line", "GET", REG,
        "/v1/audit?resource_type=lending&resource_id={{lendingId}}&limit=100",
        tests=[OK, "pm.test('every write was audited', () => "
                   "pm.expect(pm.response.json().length).to.be.above(0));"]),
    req("GET /v1/export/counts — row counts per table", "GET", REG, "/v1/export/counts",
        tests=[OK, "console.log('counts:', JSON.stringify(pm.response.json()));"]),
    req("GET /v1/authz/check — can the BDRM edit this lending line?", "GET", REG,
        "/v1/authz/check?operation=edit_lending_line&subject_type=Lending"
        "&subject_id={{lendingId}}",
        headers=[{"key": "X-Tenant", "value": "{{tenant}}"},
                 {"key": "X-User-Email", "value": "{{bdrmEmail}}"},
                 {"key": "X-Actor", "value": "e2e-runner"}],
        tests=[OK, "console.log('decision:', JSON.stringify(pm.response.json()));"],
        desc="Asks the platform to explain its own decision for the RM — useful when a UI "
             "unexpectedly gets a 403."),
]))


def build() -> dict:
    return {
        "info": {
            "name": "PRISM · E2E Journey (sequential)",
            "description":
                "A HARDCODED, ORDERED walk through one full deal — run it with "
                "**Run collection** and every step asserts itself.\n\n"
                "user + roles → client → lead → interaction → convert (deal + lending + "
                "syndication + asset monetisation) → pipelines → financials → "
                "decision + evidence → Sanctioned → verification.\n\n"
                "**Re-runnable:** the first request stamps `runSuffix` from the clock, and every "
                "unique key (entity code, CIN, user e-mails, references) includes it — so a second "
                "run does not collide on `UNIQUE(tenant_id, code)`.\n\n"
                "**Negative tests are features.** Several requests MUST fail (skipping a stage, "
                "sanctioning without evidence, a stale `If-Match`, an unknown filter, editing a "
                "lead's status straight to Converted). Those tests pass when the platform refuses "
                "— a 2xx there is a control failure, not a success.\n\n"
                "Everything runs through the NGINX edge (`baseUrl`) except user provisioning, "
                "which talks to the Access service directly (`accessUrl`) because the gateway does "
                "not prefix-route it.\n\n"
                "The orchestrator/Temporal plane is deliberately NOT exercised here; CP/CS and the "
                "Advaya handover need its maker-checker pair (see the Orchestrator collection).",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [{"name": n, "item": items} for n, items in folders],
    }


# --------------------------------------------------------------------------- #
# Validation — the bodies above are HANDWRITTEN, so verify every one against the frozen
# contract BEFORE writing the file. The Register's create/update models are `extra="forbid"`,
# so a single stale field name means a 422 at run time; this turns that into a build error.
# --------------------------------------------------------------------------- #
_SPEC = ROOT / "docs" / "openapi" / "register.openapi.json"


def _resolve(schema: dict, root: dict) -> dict:
    return (root["components"]["schemas"][schema["$ref"].split("/")[-1]]
            if "$ref" in schema else schema)


def _match_path(path: str, spec_paths) -> str | None:
    """Map a concrete request path (with {{vars}}) back to its OpenAPI template."""
    want = [p for p in path.split("?")[0].split("/") if p]
    for tmpl in spec_paths:
        segs = [p for p in tmpl.split("/") if p]
        if len(segs) != len(want):
            continue
        if all(s.startswith("{") or s == w for s, w in zip(segs, want, strict=True)):
            return tmpl
    return None


def _effective_len(value: str) -> int:
    """Length once Postman substitutes: {{runSuffix}} → 6 digits, other vars → a uuid."""
    v = value.replace("{{runSuffix}}", "123456")
    return len(re.sub(r"\{\{\w+\}\}", "0" * 36, v))


def validate(col: dict) -> list[str]:
    if not _SPEC.exists():
        return [f"SKIPPED: {_SPEC} missing — run scripts/export_openapi.sh first"]
    spec = json.load(open(_SPEC))
    problems: list[str] = []
    for folder in col["item"]:
        for item in folder["item"]:
            r = item["request"]
            if r["url"]["host"][0] != REG or "body" not in r:
                continue  # Access service has its own spec; form bodies are not JSON
            if r["body"].get("mode") != "raw":
                continue
            path = r["url"]["raw"].replace(REG, "")
            tmpl = _match_path(path, spec["paths"])
            if tmpl is None:
                problems.append(f"{item['name']}: no OpenAPI path matches {path}")
                continue
            op = spec["paths"][tmpl].get(r["method"].lower())
            if not op or "requestBody" not in op:
                problems.append(f"{item['name']}: {r['method']} {tmpl} takes no JSON body")
                continue
            sch = _resolve(op["requestBody"]["content"]["application/json"]["schema"], spec)
            props = sch.get("properties", {})
            for key, value in json.loads(r["body"]["raw"]).items():
                if key not in props:
                    problems.append(
                        f"{item['name']}: '{key}' is not a field of {tmpl} "
                        f"(extra=forbid → 422). Allowed: {sorted(props)}")
                    continue
                pr = _resolve(props[key], spec)
                if "anyOf" in pr:
                    branches = [_resolve(b, spec) for b in pr["anyOf"]
                                if b.get("type") != "null"]
                    pr = branches[0] if branches else pr
                cap = pr.get("maxLength")
                if cap and isinstance(value, str) and _effective_len(value) > cap:
                    problems.append(
                        f"{item['name']}: '{key}' is {_effective_len(value)} chars "
                        f"but maxLength is {cap} → {value!r}")
    problems += _validate_person_refs(col)
    problems += _validate_assignee_roles(col)
    return problems


def _validate_person_refs(col: dict) -> list[str]:
    """A REFERENTIAL check the schema cannot express: /convert refuses an `rm`/`analyst` that is
    not a Person on record, so every name used must be created by an EARLIER POST /v1/people in
    this same collection (matched on full_name, which is what the Register looks up)."""
    on_record: set[str] = set()
    problems: list[str] = []
    for folder in col["item"]:
        for item in folder["item"]:
            r = item["request"]
            if "body" not in r or r["body"].get("mode") != "raw":
                continue
            path = r["url"]["raw"].replace(REG, "")
            body = json.loads(r["body"]["raw"])
            if path == "/v1/people" and r["method"] == "POST":
                # A person is on record under EITHER name — the short handle the platform
                # addresses them by, or the full name. /convert accepts both.
                for key in ("full_name", "name"):
                    if body.get(key):
                        on_record.add(body[key].strip().lower())
            elif path.endswith("/convert"):
                for field in ("rm", "analyst"):
                    name = body.get(field)
                    if name and name.strip().lower() not in on_record:
                        problems.append(
                            f"{item['name']}: {field}={name!r} is not created by an earlier "
                            f"POST /v1/people (would 422 'not a person on record'). "
                            f"On record so far: {sorted(on_record) or 'nothing'}")
    return problems


# /convert verifies each *_id it will auto-assign against Access: the user must hold the role
# that assignment requires (or a universal role). Mirrors register/core/access_client.py.
_ROLE_FOR_ASSIGNMENT = {"BDRM": "BDRM", "Deal Analyst": "Deal Analyst",
                        "Syn RM": "Syn RM", "AM RM": "AM RM"}
_UNIVERSAL = {"Admin", "Management"}


def _validate_assignee_roles(col: dict) -> list[str]:
    """Catch the 'does not hold a role permitting a X assignment' 422 at build time."""
    roles_of: dict[str, set[str]] = {}   # env var name -> roles granted in folder 01
    problems: list[str] = []
    for folder in col["item"]:
        for item in folder["item"]:
            r = item["request"]
            if "body" not in r or r["body"].get("mode") != "raw":
                continue
            body = json.loads(r["body"]["raw"])
            path = r["url"]["raw"].replace(REG, "").replace(ACC, "")
            if path == "/v1/users" and r["method"] == "POST":
                # Map the captured env var (…UserId) to the roles this user is created with.
                for line in json.dumps(item.get("event", [])).split("\\n"):
                    pass
                cap = [v for v in ("bdrmUserId", "makerUserId", "checkerUserId")
                       if v in json.dumps(item.get("event", []))]
                for var in cap:
                    roles_of[var] = set(body.get("roles") or [])
            elif path.endswith("/convert"):
                need = []
                if body.get("is_lending") and body.get("analyst_id"):
                    need.append((body["analyst_id"], "Deal Analyst"))
                if body.get("is_syndication") and body.get("rm_id"):
                    need.append((body["rm_id"], "Syn RM"))
                if body.get("is_asset_mon") and body.get("rm_id"):
                    need.append((body["rm_id"], "AM RM"))
                for ref, assignment_role in need:
                    var = ref.strip("{}")
                    held = roles_of.get(var)
                    if held is None:
                        continue  # not created in this collection; cannot check
                    needed = {_ROLE_FOR_ASSIGNMENT[assignment_role]} | _UNIVERSAL
                    if not (held & needed):
                        problems.append(
                            f"{item['name']}: {var} holds {sorted(held)} but a "
                            f"'{assignment_role}' assignment needs one of {sorted(needed)} "
                            f"(would 422 from verify_assignee)")
    return problems


def main() -> None:
    OUT.mkdir(exist_ok=True)
    col = build()
    problems = validate(col)
    for p in problems:
        print("  !", p)
    if any(not p.startswith("SKIPPED") for p in problems):
        raise SystemExit(f"\n{len(problems)} contract violation(s) — fix before shipping.")
    path = OUT / "PRISM_E2E_Journey.postman_collection.json"
    with open(path, "w") as fh:
        json.dump(col, fh, indent=2)
    n = sum(len(f["item"]) for f in col["item"])
    print(f"{path.name}: {len(col['item'])} folders · {n} requests in sequence")


if __name__ == "__main__":
    main()
