"""Register-side RBAC — identity via gateway-forwarded headers (three-service design).

Identity facts live in the Access service; the Gateway forwards X-User-Email /
X-User-Roles / X-User-Id (secret-verified in real deployments; dev-trusted here).
The Register enforces what sits next to the data: assignment authority, the
assignment-driven scoped write, the request → approve flow that applies the change, and
the Admin-only delete re-verification.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _as(email: str, roles: str, uid: uuid.UUID | None = None) -> dict:
    h = {"X-User-Email": email, "X-User-Roles": roles}
    if uid is not None:
        h["X-User-Id"] = str(uid)
    return h


CREDIT_HEAD = _as("credithead@evamfinance.com", "Credit Head")
MGMT = _as("mgmt@evamfinance.com", "Management")
ADMIN = _as("admin@evamfinance.com", "Admin")
AM_HEAD = _as("amhead@evamfinance.com", "AM Head")

ANALYST_ID = uuid.uuid4()
ANALYST = _as("analyst@evamfinance.com", "Deal Analyst", ANALYST_ID)


async def test_assignment_grants_and_revokes_scoped_write(client: AsyncClient):
    eid = (await client.post("/v1/entities", json={"code": "R3S1", "legal_name": "R1"})).json()["id"]
    syn = (await client.post("/v1/syndication", json={"entity_id": eid})).json()

    # Credit Head cross-assigns the analyst to a SYNDICATION line (v2.1 primitive).
    r = await client.post("/v1/assignments", json={
        "user_id": str(ANALYST_ID), "subject_type": "Syndication", "subject_id": syn["id"],
        "assignment_role": "Deal Analyst"}, headers=CREDIT_HEAD)
    assert r.status_code == 201, r.text
    assignment = r.json()

    # Scoped write on THAT line…
    chk = (await client.get("/v1/authz/check", headers=ANALYST,
                            params={"operation": "edit_syndication_line",
                                    "subject_type": "Syndication",
                                    "subject_id": syn["id"]})).json()
    assert chk["allowed"] is True and chk["access"] == "SCOPED" and chk["on_line"] is True
    # …not on another line.
    syn2 = (await client.post("/v1/syndication", json={"entity_id": eid})).json()
    chk2 = (await client.get("/v1/authz/check", headers=ANALYST,
                             params={"operation": "edit_syndication_line",
                                     "subject_type": "Syndication",
                                     "subject_id": syn2["id"]})).json()
    assert chk2["allowed"] is False

    # End the assignment → revoked.
    r = await client.post(f"/v1/assignments/{assignment['id']}/end", headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["ended_at"]
    chk3 = (await client.get("/v1/authz/check", headers=ANALYST,
                             params={"operation": "edit_syndication_line",
                                     "subject_type": "Syndication",
                                     "subject_id": syn["id"]})).json()
    assert chk3["allowed"] is False


async def test_assignment_authority_enforced(client: AsyncClient):
    """Credit Head owns the analyst pool; a Syn RM cannot assign analysts."""
    eid = (await client.post("/v1/entities", json={"code": "R3S2", "legal_name": "R2"})).json()["id"]
    syn = (await client.post("/v1/syndication", json={"entity_id": eid})).json()
    r = await client.post("/v1/assignments", json={
        "user_id": str(uuid.uuid4()), "subject_type": "Syndication", "subject_id": syn["id"],
        "assignment_role": "Deal Analyst"}, headers=_as("synrm@evamfinance.com", "Syn RM"))
    assert r.status_code == 403


async def test_request_approve_applies_change_with_vertical_routing(client: AsyncClient):
    eid = (await client.post("/v1/entities", json={"code": "R3S3", "legal_name": "R3"})).json()["id"]
    lend = (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Diligence"})).json()
    # A SCOPED requester must be ON the line (the request-scope rule): assign first.
    r = await client.post("/v1/assignments", json={
        "user_id": str(ANALYST_ID), "subject_type": "Lending", "subject_id": lend["id"],
        "assignment_role": "Deal Analyst"}, headers=CREDIT_HEAD)
    assert r.status_code == 201, r.text

    r = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lend["id"], "field": "stage",
        "to_value": "Note Circulated"}, headers=ANALYST)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "Pending" and req["from_value"] == "Diligence"
    assert req["requested_by"] == "analyst@evamfinance.com"

    # Wrong-vertical Head and the analyst are both denied.
    assert (await client.post(f"/v1/requests/{req['id']}/approve", json={},
                              headers=AM_HEAD)).status_code == 403
    assert (await client.post(f"/v1/requests/{req['id']}/approve", json={},
                              headers=ANALYST)).status_code == 403

    # Credit Head approves → the stage ACTUALLY changes, with history auto-appended.
    r = await client.post(f"/v1/requests/{req['id']}/approve", json={"note": "ok"},
                          headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Approved"
    lend2 = (await client.get(f"/v1/lending/{lend['id']}")).json()
    assert lend2["stage"] == "Note Circulated"
    assert lend2["stage_history"][-1]["to"] == "Note Circulated"

    # Double-decide → 409.
    assert (await client.post(f"/v1/requests/{req['id']}/reject", json={},
                              headers=CREDIT_HEAD)).status_code == 409


async def test_delete_reverification_admin_only(client: AsyncClient):
    eid = (await client.post("/v1/entities", json={"code": "R3S4", "legal_name": "R4"})).json()["id"]
    # Management (even via forwarded identity) may NOT delete…
    assert (await client.delete(f"/v1/entities/{eid}", headers=MGMT)).status_code == 403
    # …Admin may.
    assert (await client.delete(f"/v1/entities/{eid}", headers=ADMIN)).status_code == 204


async def test_gateway_secret_blocks_spoofed_identity(client: AsyncClient, monkeypatch):
    """With a gateway secret configured, identity headers without the secret are rejected."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    try:
        r = await client.get("/v1/entities", headers=MGMT)  # spoofed: no X-Gateway-Auth
        assert r.status_code == 403
        r = await client.get("/v1/entities", headers={**MGMT, "X-Gateway-Auth": "s3cret"})
        assert r.status_code == 200
        r = await client.get("/v1/entities")  # machine call, no identity → unaffected
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(settings, "gateway_shared_secret", "")


# --------------------------------------------------------------------------- #
# RBAC 3.1 scope scenarios — the central evaluator, exercised end to end
# (the "EcoSoch" cases: auto-ownership, direct-GET protection, connected company,
#  team scope, vertical-Head default ownership, row locks, audit guardrail)
# --------------------------------------------------------------------------- #
ARUN_ID = uuid.uuid4()
ARUN = _as("arun@evamfinance.com", "BDRM", ARUN_ID)
MEERA_ID = uuid.uuid4()
MEERA = _as("meera@evamfinance.com", "Deal Analyst", MEERA_ID)


async def test_bdrm_auto_owns_created_lead(client: AsyncClient):
    """Arun creates an EcoSoch lead → he is auto-assigned and his scoped list shows it."""
    lead = (await client.post("/v1/leads", json={"company": "EcoSoch Solar (auto)"},
                              headers=ARUN)).json()
    assigns = (await client.get("/v1/assignments", headers=ADMIN,
                                params={"subject_type": "Lead",
                                        "subject_id": lead["id"]})).json()
    assert any(str(ARUN_ID) == a["user_id"] and a["assignment_role"] == "BDRM"
               for a in assigns)
    # His scoped lead list can never hide the lead he just created…
    mine = (await client.get("/v1/leads", headers=ARUN, params={"limit": 200})).json()
    assert any(r["id"] == lead["id"] for r in mine["items"])
    # …and neither can direct GET.
    assert (await client.get(f"/v1/leads/{lead['id']}", headers=ARUN)).status_code == 200


async def test_direct_get_scope_protection(client: AsyncClient):
    """Meera (assigned to EcoSoch Lending) can read EcoSoch rows but NOT an unrelated
    company's — even knowing the id (the direct-GET hole, closed)."""
    eco = (await client.post("/v1/entities", json={"code": f"ECO-{uuid.uuid4().hex[:6]}",
                                                   "legal_name": "EcoSoch Solar"})).json()
    gh2 = (await client.post("/v1/entities", json={"code": f"GH2-{uuid.uuid4().hex[:6]}",
                                                   "legal_name": "GH2 Solar"})).json()
    eco_deal = (await client.post("/v1/deals", json={"entity_id": eco["id"]})).json()
    eco_lend = (await client.post("/v1/lending", json={"entity_id": eco["id"]})).json()
    gh2_lend = (await client.post("/v1/lending", json={"entity_id": gh2["id"]})).json()
    r = await client.post("/v1/assignments", json={
        "user_id": str(MEERA_ID), "subject_type": "Lending", "subject_id": eco_lend["id"],
        "assignment_role": "Deal Analyst"}, headers=CREDIT_HEAD)
    assert r.status_code == 201, r.text

    # Her line: yes. The unrelated line: 403 despite knowing the id.
    assert (await client.get(f"/v1/lending/{eco_lend['id']}", headers=MEERA)).status_code == 200
    assert (await client.get(f"/v1/lending/{gh2_lend['id']}", headers=MEERA)).status_code == 403
    # Connected company: the EcoSoch DEAL is readable through her Lending assignment.
    assert (await client.get(f"/v1/deals/{eco_deal['id']}", headers=MEERA)).status_code == 200
    # Meera (Deal Analyst) holds READ on the clients view per the matrix — any company
    # profile opens for her. The connected-company restriction bites for roles with
    # SCOPED clients access (BDRM / Syn RM / AM RM): assigned company yes, other no.
    assert (await client.get(f"/v1/entities/{eco['id']}/dossier", headers=MEERA)).status_code == 200
    riya_id = uuid.uuid4()
    riya = _as("riya@evamfinance.com", "Syn RM", riya_id)
    eco_syn = (await client.post("/v1/syndication", json={"entity_id": eco["id"]})).json()
    r2 = await client.post("/v1/assignments", json={
        "user_id": str(riya_id), "subject_type": "Syndication", "subject_id": eco_syn["id"],
        "assignment_role": "Syn RM"}, headers=_as("synhead2@evamfinance.com", "Syn Head"))
    assert r2.status_code == 201, r2.text
    assert (await client.get(f"/v1/entities/{eco['id']}/dossier", headers=riya)).status_code == 200
    assert (await client.get(f"/v1/entities/{gh2['id']}/dossier", headers=riya)).status_code == 403
    # Scoped lending list contains her line, not the unrelated one.
    lst = (await client.get("/v1/lending", headers=MEERA, params={"limit": 200})).json()
    ids = {r["id"] for r in lst["items"]}
    assert eco_lend["id"] in ids and gh2_lend["id"] not in ids


async def test_team_scope_via_reports_headers(client: AsyncClient):
    """A senior's scope includes their reports' assignments (X-User-Report-Ids,
    resolved by Access and forwarded by the gateway)."""
    ent = (await client.post("/v1/entities", json={"code": f"TEAM-{uuid.uuid4().hex[:6]}",
                                                   "legal_name": "Team Scope Co"})).json()
    lend = (await client.post("/v1/lending", json={"entity_id": ent["id"]})).json()
    junior_id = uuid.uuid4()
    r = await client.post("/v1/assignments", json={
        "user_id": str(junior_id), "subject_type": "Lending", "subject_id": lend["id"],
        "assignment_role": "Deal Analyst"}, headers=CREDIT_HEAD)
    assert r.status_code == 201
    senior = {**_as("senior@evamfinance.com", "Deal Analyst", uuid.uuid4()),
              "X-User-Report-Ids": str(junior_id),
              "X-User-Reports": "junior@evamfinance.com"}
    stranger = _as("stranger@evamfinance.com", "Deal Analyst", uuid.uuid4())
    assert (await client.get(f"/v1/lending/{lend['id']}", headers=senior)).status_code == 200
    assert (await client.get(f"/v1/lending/{lend['id']}", headers=stranger)).status_code == 403


async def test_vertical_head_default_ownership(client: AsyncClient):
    """An UNASSIGNED syndication line belongs to the Syn Head (clients view is SCOPED
    for them) — operational, not descriptive: the company dossier opens for them."""
    syn_head = _as("synhead@evamfinance.com", "Syn Head", uuid.uuid4())
    with_line = (await client.post("/v1/entities", json={
        "code": f"OWN-{uuid.uuid4().hex[:6]}", "legal_name": "Unassigned Syn Co"})).json()
    await client.post("/v1/syndication", json={"entity_id": with_line["id"]})
    without = (await client.post("/v1/entities", json={
        "code": f"NON-{uuid.uuid4().hex[:6]}", "legal_name": "No Syn Co"})).json()
    assert (await client.get(f"/v1/entities/{with_line['id']}/dossier",
                             headers=syn_head)).status_code == 200
    assert (await client.get(f"/v1/entities/{without['id']}/dossier",
                             headers=syn_head)).status_code == 403


async def test_row_lock_converted_lead(client: AsyncClient):
    """Field Rules slice: a Converted lead refuses further edits except from
    Admin/Management/BD Head (the push-to-deals lock). Conversion goes through the
    proper /convert endpoint — status cannot be set to Converted by a direct PATCH."""
    ent = (await client.post("/v1/entities",
                             json={"code": "LOCKENT", "legal_name": "Locked Lead Co"})).json()
    lead = (await client.post("/v1/leads",
                              json={"company": "Locked Lead Co", "entity_id": ent["id"]},
                              headers=ARUN)).json()
    # Direct status=Converted is refused for everyone — must use /convert.
    bad = await client.patch(f"/v1/leads/{lead['id']}", json={"status": "Converted"},
                             headers=ADMIN)
    assert bad.status_code == 422, bad.text
    conv = await client.post(f"/v1/leads/{lead['id']}/convert",
                             json={"is_lending": True, "amount_cr": 1}, headers=ADMIN)
    assert conv.status_code == 200, conv.text
    locked = await client.patch(f"/v1/leads/{lead['id']}", json={"notes": "nope"},
                                headers=ARUN)
    assert locked.status_code == 403
    allowed = await client.patch(f"/v1/leads/{lead['id']}", json={"notes": "head ok"},
                                 headers=_as("bdhead@evamfinance.com", "BD Head"))
    assert allowed.status_code == 200, allowed.text


async def test_audit_and_restore_guardrails(client: AsyncClient):
    """Audit view + restore are Admin-only for any user context."""
    assert (await client.get("/v1/audit", headers=MEERA)).status_code == 403
    assert (await client.get("/v1/audit", headers=ADMIN)).status_code == 200
    ent = (await client.post("/v1/entities", json={"code": f"RES-{uuid.uuid4().hex[:6]}",
                                                   "legal_name": "Restore Co"})).json()
    assert (await client.delete(f"/v1/entities/{ent['id']}", headers=ADMIN)).status_code == 204
    denied = await client.post(f"/v1/entities/{ent['id']}/restore", headers=MEERA)
    assert denied.status_code == 403
    assert (await client.post(f"/v1/entities/{ent['id']}/restore",
                              headers=ADMIN)).status_code == 200


async def test_company_scoped_resources_and_unknown_filter(client: AsyncClient):
    """Entity-carrying resources (financials/monitoring/intel/documents/interactions)
    are now company-scoped: a Syn RM assigned to EcoSoch's syndication sees EcoSoch's
    financials but not GH2's — the ATLAS-aggregation leak the reviewer flagged. And an
    unknown query filter is refused (422), not silently ignored."""
    eco = (await client.post("/v1/entities", json={
        "code": f"ECO-{uuid.uuid4().hex[:6]}", "legal_name": "EcoSoch Solar"})).json()
    gh2 = (await client.post("/v1/entities", json={
        "code": f"GH2-{uuid.uuid4().hex[:6]}", "legal_name": "GH2 Solar"})).json()
    eco_syn = (await client.post("/v1/syndication", json={"entity_id": eco["id"]})).json()
    # Financials + monitoring on BOTH companies (created by the API key, no owner).
    eco_fin = (await client.post("/v1/financials", json={
        "entity_id": eco["id"], "statement_type": "Audited", "period_end": "2026-03-31",
        "revenue": 84.6})).json()
    gh2_fin = (await client.post("/v1/financials", json={
        "entity_id": gh2["id"], "statement_type": "Audited", "period_end": "2026-03-31",
        "revenue": 12.0})).json()

    riya_id = uuid.uuid4()
    riya = _as("riya@evamfinance.com", "Syn RM", riya_id)
    r = await client.post("/v1/assignments", json={
        "user_id": str(riya_id), "subject_type": "Syndication",
        "subject_id": eco_syn["id"], "assignment_role": "Syn RM"},
        headers=_as("synhead@evamfinance.com", "Syn Head"))
    assert r.status_code == 201, r.text

    # Financials list is scoped to her connected company.
    lst = (await client.get("/v1/financials", headers=riya, params={"limit": 200})).json()
    ids = {r["id"] for r in lst["items"]}
    assert eco_fin["id"] in ids and gh2_fin["id"] not in ids
    # Direct GET honours the same scope.
    assert (await client.get(f"/v1/financials/{eco_fin['id']}", headers=riya)).status_code == 200
    assert (await client.get(f"/v1/financials/{gh2_fin['id']}", headers=riya)).status_code == 403

    # Unknown filter param → 422 (never silently ignored).
    bad = await client.get("/v1/leads", params={"entity_idd": eco["id"]})
    assert bad.status_code == 422
    assert "Unknown query parameter" in bad.text


async def test_convert_accepts_the_short_handle_a_person_is_known_by(client: AsyncClient):
    """A person is on record under EITHER name.

    The roster stores both — the short handle the platform addresses people by ("Shubh")
    and the full name ("Shubh Dave") — and every lead, deal and tracker carries the
    handle. Validating the full name alone refused a conversion whose RM came from that
    very roster: the run sat at "Applying" re-sending `Unknown rm 'Shubh'` until it was
    killed. Both names, and either case, must be accepted; a name on NEITHER still 422s.
    """
    ent = (await client.post("/v1/entities", json={
        "code": f"HD-{uuid.uuid4().hex[:6]}", "legal_name": "Handle Co"})).json()
    assert (await client.post("/v1/people", json={
        "name": "Shubh", "full_name": "Shubh Dave", "role": "BDRM"})).status_code == 201
    assert (await client.post("/v1/people", json={
        "name": "Bhavana", "full_name": "Bhavana Sridhar",
        "role": "Deal Analyst"})).status_code == 201

    async def convert(rm: str, analyst: str | None = None) -> int:
        lead = (await client.post("/v1/leads", json={
            "company": "Handle Co", "entity_id": ent["id"], "status": "Active"})).json()
        body = {"is_lending": True, "product_type": "Term Loan", "amount_cr": 5, "rm": rm}
        if analyst:
            body["analyst"] = analyst
        return (await client.post(f"/v1/leads/{lead['id']}/convert", json=body)).status_code

    assert await convert("Shubh") == 200                       # short handle
    assert await convert("Shubh Dave") == 200                  # full name
    assert await convert("  shubh  ") == 200                   # case + surrounding space
    assert await convert("Shubh", analyst="Bhavana") == 200    # analyst, same rule
    assert await convert("Nobody At All") == 422               # genuinely not on record
    assert await convert("Shubh", analyst="Ghost") == 422


async def test_transactional_lead_convert(client: AsyncClient):
    """The atomic convert endpoint: deal + product lines + Converted lead in ONE
    transaction. Replaces the workflow's compensation — nothing partial survives."""
    ent = (await client.post("/v1/entities", json={
        "code": f"CV-{uuid.uuid4().hex[:6]}", "legal_name": "Convert Co"})).json()
    # rm/analyst named on a conversion must be known people on record.
    p = await client.post("/v1/people",
                          json={"name": "Chetan", "full_name": "Chetan", "role": "RM"})
    assert p.status_code == 201, p.text
    lead = (await client.post("/v1/leads", json={
        "company": "Convert Co", "entity_id": ent["id"], "status": "Active"})).json()
    r = await client.post(f"/v1/leads/{lead['id']}/convert", json={
        "is_lending": True, "is_syndication": True, "product_type": "Term Loan",
        "amount_cr": 25, "rm": "Chetan"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deal_id"] and body["lending_id"] and body["syndication_id"]
    assert body["asset_mon_id"] is None
    after = (await client.get(f"/v1/leads/{lead['id']}")).json()
    assert after["status"] == "Converted" and after["converted_deal_id"] == body["deal_id"]
    deal = (await client.get(f"/v1/deals/{body['deal_id']}")).json()
    # The deal enters the COMMERCIAL funnel; credit starts on the lending line.
    assert deal["is_lending"] and deal["is_syndication"] and deal["stage"] == "In Pipeline"
    again = await client.post(f"/v1/leads/{lead['id']}/convert", json={"is_lending": True})
    assert again.status_code == 409


async def test_convert_reports_an_access_outage_as_503_not_422(client: AsyncClient,
                                                               monkeypatch):
    """An unreachable Access service is a DEPENDENCY fault (503), never a bad request (422).

    This one bit in production. The conversion runs inside a workflow whose durable retry
    policy exists precisely to ride out a dependency outage — but that policy classifies a
    422 as deterministic and stops retrying. So a momentary Access outage killed the run
    after the approval had already been recorded: the approver was told the lead was
    approved and no deal or lending line ever appeared behind it, with nothing but a
    container log to say why. 503 keeps it retryable, and the lead stays convertible.
    """
    from app.core import access_client

    ent = (await client.post("/v1/entities", json={
        "code": f"AO-{uuid.uuid4().hex[:6]}", "legal_name": "Outage Co"})).json()
    assert (await client.post("/v1/people", json={
        "name": "Meera", "full_name": "Meera Rao", "role": "Deal Analyst"})).status_code == 201
    lead = (await client.post("/v1/leads", json={
        "company": "Outage Co", "entity_id": ent["id"], "status": "Active"})).json()

    async def _down(tenant, user_id, role, expected_name=None):  # noqa: ANN001
        raise access_client.AccessUnavailableError("connect timeout")
    monkeypatch.setattr(access_client, "verify_assignee", _down)

    r = await client.post(f"/v1/leads/{lead['id']}/convert", json={
        "is_lending": True, "product_type": "Term Loan", "amount_cr": 5,
        "analyst": "Meera", "analyst_id": str(uuid.uuid4())})
    assert r.status_code == 503, r.text
    assert "Access service is not answering" in r.text
    # Nothing was written, and the lead is still convertible once Access recovers.
    assert (await client.get(f"/v1/leads/{lead['id']}")).json()["status"] == "Active"


async def test_assignment_reports_an_access_outage_as_503_not_422(client: AsyncClient,
                                                                  monkeypatch):
    """Same rule on the assignment path — an outage is 'retry', not 'your request is wrong'."""
    from app.core import access_client

    ent = (await client.post("/v1/entities", json={
        "code": f"AO-{uuid.uuid4().hex[:6]}", "legal_name": "Outage Two"})).json()

    async def _down(tenant, user_id, role, expected_name=None):  # noqa: ANN001
        raise access_client.AccessUnavailableError("connect timeout")
    monkeypatch.setattr(access_client, "verify_assignee", _down)

    r = await client.post("/v1/assignments", json={
        "user_id": str(uuid.uuid4()), "subject_type": "Entity",
        "subject_id": ent["id"], "assignment_role": "BDRM"},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert r.status_code == 503, r.text
    assert "Access service is not answering" in r.text
