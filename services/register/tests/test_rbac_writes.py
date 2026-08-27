"""Adversarial write/scope/export/tenant-admin tests for the P0 authorization fixes.

These cover the bypasses the security audit called out:

* P0-1/P0-3  a READ-only viewer (or wrong role) cannot mutate financials / contracts /
             company profiles; an authorised role passes the gate (404 on a random id
             proves the gate was cleared, not the row).
* P0-2       people / counterparties gate on their write operation; the flat
             syndication-lenders route no longer allows mutation.
* P0-4       lead conversion rejects closed leads, is idempotent, and a SCOPED caller
             needs exact assignment; assignment lists are self-scoped.
* P0-5       exports are row-scoped and a scoped user cannot pull a full/deleted backup.
* P0-6       tenant administration requires a verified Admin identity.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

RANDOM = uuid.uuid4


def _as(email: str, roles: str, uid: uuid.UUID | None = None) -> dict:
    h = {"X-User-Email": email, "X-User-Roles": roles}
    if uid is not None:
        h["X-User-Id"] = str(uid)
    return h


ADMIN = _as("admin@evamfinance.com", "Admin")
MGMT = _as("mgmt@evamfinance.com", "Management")
BD_HEAD = _as("bdhead@evamfinance.com", "BD Head")
CREDIT_HEAD = _as("credithead@evamfinance.com", "Credit Head")
BDRM_ID = uuid.uuid4()
BDRM = _as("bdrm@evamfinance.com", "BDRM", BDRM_ID)


# --------------------------------------------------------------------------- #
# P0-1 / P0-3 — company-resource writes deny READ/NONE, allow the right role
# --------------------------------------------------------------------------- #
async def test_read_only_role_cannot_patch_financials(client: AsyncClient):
    # BDRM has fi_master = READ → edit_fi_record NONE → 403, before any row lookup.
    r = await client.patch(f"/v1/financials/{RANDOM()}", json={"provenance": "x"},
                           headers=BDRM)
    assert r.status_code == 403, r.text
    # Admin clears the op gate → falls through to a genuine 404 (row absent).
    r = await client.patch(f"/v1/financials/{RANDOM()}", json={"provenance": "x"},
                           headers=ADMIN)
    assert r.status_code == 404, r.text


async def test_read_only_role_cannot_patch_contracts(client: AsyncClient):
    # Credit Head has clients = READ → edit_contract NONE → 403.
    r = await client.patch(f"/v1/contracts-assets/{RANDOM()}", json={"title": "x"},
                           headers=CREDIT_HEAD)
    assert r.status_code == 403, r.text
    r = await client.patch(f"/v1/contracts-assets/{RANDOM()}", json={"title": "x"},
                           headers=ADMIN)
    assert r.status_code == 404, r.text


async def test_entity_profile_edit_is_role_gated_not_inverted(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "PW-ENT", "legal_name": "Ent"})).json()["id"]
    # Credit Head (clients READ) → edit_client NONE → 403 (previously humans were wrongly
    # denied while machines slipped through; now the human gate is correct).
    r = await client.patch(f"/v1/entities/{eid}", json={"display_name": "x"},
                           headers=CREDIT_HEAD)
    assert r.status_code == 403, r.text
    # Admin edits fine.
    r = await client.patch(f"/v1/entities/{eid}", json={"display_name": "ok"}, headers=ADMIN)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# P0-2 — orphan resources are gated; flat lender mutation is gone
# --------------------------------------------------------------------------- #
async def test_people_and_counterparty_writes_gated(client: AsyncClient):
    # BDRM cannot edit the employee directory or the counterparty (bank) directory.
    assert (await client.patch(f"/v1/people/{RANDOM()}", json={"role": "x"},
                               headers=BDRM)).status_code == 403
    assert (await client.patch(f"/v1/counterparties/{RANDOM()}", json={"short_name": "x"},
                               headers=BDRM)).status_code == 403
    # Admin clears the gate (404 = row absent, gate passed).
    assert (await client.patch(f"/v1/people/{RANDOM()}", json={"role": "x"},
                               headers=ADMIN)).status_code == 404


async def test_flat_syndication_lender_mutation_is_disabled(client: AsyncClient):
    # The flat route is read-only now — mutation goes through the secured nested routes.
    assert (await client.post("/v1/syndication-lenders",
                              json={"syndication_id": str(RANDOM()), "lender_name": "X"},
                              headers=ADMIN)).status_code == 405
    assert (await client.patch(f"/v1/syndication-lenders/{RANDOM()}",
                               json={"status": "X"}, headers=ADMIN)).status_code == 405


# --------------------------------------------------------------------------- #
# P0-4 — conversion: closed leads, idempotency, exact-assignment scope
# --------------------------------------------------------------------------- #
async def test_closed_lead_cannot_be_converted(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "PW-CL", "legal_name": "CL"})).json()["id"]
    lead = (await client.post("/v1/leads",
                              json={"company": "CL", "entity_id": eid})).json()
    await client.patch(f"/v1/leads/{lead['id']}", json={"status": "Dropped"}, headers=ADMIN)
    r = await client.post(f"/v1/leads/{lead['id']}/convert",
                          json={"is_lending": True, "amount_cr": 1}, headers=ADMIN)
    assert r.status_code == 409, r.text


async def test_conversion_is_idempotent(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "PW-ID", "legal_name": "ID"})).json()["id"]
    # Rated Hot: the register converts only a qualified lead (the desk's own gate),
    # and this test is about replay behaviour, not the temperature rule.
    lead = (await client.post("/v1/leads",
                              json={"company": "ID", "entity_id": eid,
                                    "temperature": "Hot"})).json()
    key = {"Idempotency-Key": f"conv-{uuid.uuid4()}"}
    r1 = await client.post(f"/v1/leads/{lead['id']}/convert",
                           json={"is_lending": True, "amount_cr": 2},
                           headers={**ADMIN, **key})
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"/v1/leads/{lead['id']}/convert",
                           json={"is_lending": True, "amount_cr": 2},
                           headers={**ADMIN, **key})
    assert r2.status_code == 200 and r2.json()["deal_id"] == r1.json()["deal_id"]
    # Exactly one deal for the company — the replay did not create a second.
    deals = (await client.get("/v1/deals", params={"entity_id": eid, "with_total": True})).json()
    assert deals["total"] == 1


async def test_bdrm_lists_the_lead_they_created_whoever_it_names(client: AsyncClient):
    """Own book: an RM sees a lead they created even when its `rm` field names someone
    else — the row is theirs because they created it, not because of a string match.

    ATLAS relied on the opposite: it re-filtered the register's answer client-side on
    `rm == <sign-in display name>`, so a BDRM's own new lead disappeared from her Leads
    tab whenever the field held a different spelling of her name. The register's scope
    is the wider, correct one, and this pins it.
    """
    rm = _as("priya@evamfinance.com", "BDRM", uuid.uuid4())
    eid = (await client.post("/v1/entities", json={
        "code": f"OWNB-{uuid.uuid4().hex[:6]}", "legal_name": "Own Book Co"},
        headers=rm)).json()["id"]
    mine = await client.post("/v1/leads", json={
        "company": "Own Book Co", "entity_id": eid, "rm": "Somebody Else"}, headers=rm)
    assert mine.status_code == 201, mine.text

    listed = await client.get("/v1/leads", headers=rm)
    assert listed.status_code == 200, listed.text
    assert mine.json()["id"] in {row["id"] for row in listed.json()["items"]}

    # ...and it is genuinely scoped: another BDRM's book does not contain it.
    other = _as("nikhil@evamfinance.com", "BDRM", uuid.uuid4())
    theirs = await client.get("/v1/leads", headers=other)
    assert mine.json()["id"] not in {row["id"] for row in theirs.json()["items"]}


async def test_scoped_convert_needs_exact_assignment(client: AsyncClient):
    # Lead created by a machine caller (created_by != the BDRM) and NOT assigned to them.
    eid = (await client.post("/v1/entities",
                             json={"code": "PW-SC", "legal_name": "SC"})).json()["id"]
    lead = (await client.post("/v1/leads", json={"company": "SC", "entity_id": eid})).json()
    # BDRM has push_lead_to_deals = SCOPED but no assignment / own-book / vertical default.
    r = await client.post(f"/v1/leads/{lead['id']}/convert",
                          json={"is_lending": True, "amount_cr": 1}, headers=BDRM)
    assert r.status_code == 403, r.text


async def test_assignment_list_is_self_scoped(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "PW-AS", "legal_name": "AS"})).json()["id"]
    lead_a = (await client.post("/v1/leads", json={"company": "A", "entity_id": eid})).json()
    lead_b = (await client.post("/v1/leads", json={"company": "B", "entity_id": eid})).json()
    other = uuid.uuid4()
    # BD Head assigns the BDRM to lead A and someone else to lead B.
    await client.post("/v1/assignments", json={
        "user_id": str(BDRM_ID), "subject_type": "Lead", "subject_id": lead_a["id"],
        "assignment_role": "BDRM"}, headers=BD_HEAD)
    await client.post("/v1/assignments", json={
        "user_id": str(other), "subject_type": "Lead", "subject_id": lead_b["id"],
        "assignment_role": "BDRM"}, headers=BD_HEAD)
    # The BDRM sees ONLY their own assignment; Admin sees both.
    mine = (await client.get("/v1/assignments", headers=BDRM)).json()
    assert {a["user_id"] for a in mine} == {str(BDRM_ID)}
    everyone = (await client.get("/v1/assignments", headers=ADMIN)).json()
    assert {str(BDRM_ID), str(other)} <= {a["user_id"] for a in everyone}


# --------------------------------------------------------------------------- #
# P0-5 — exports are row-scoped; full/deleted backup is Admin-only
# --------------------------------------------------------------------------- #
async def test_export_counts_are_scoped_and_backup_gated(client: AsyncClient):
    # A machine-created entity (created_by = actor 'pytest', not the BDRM's book).
    await client.post("/v1/entities", json={"code": "PW-EX", "legal_name": "EX"})
    # Scoped user: the entity is not in their scope → not counted.
    scoped = (await client.get("/v1/export/counts", headers=BDRM)).json()
    assert scoped["entities"] == 0
    # Admin sees it.
    full = (await client.get("/v1/export/counts", headers=ADMIN)).json()
    assert full["entities"] >= 1
    # include_deleted (a backup) needs backup_restore → Admin-only.
    assert (await client.get("/v1/export/counts", params={"include_deleted": True},
                             headers=BDRM)).status_code == 403
    assert (await client.get("/v1/export/counts", params={"include_deleted": True},
                             headers=ADMIN)).status_code == 200


CREDIT_HEAD_ANALYST = _as("da.e2e@evamfinance.com", "Deal Analyst")
SYN_RM = _as("synrm2@evamfinance.com", "Syn RM", uuid.uuid4())


# --------------------------------------------------------------------------- #
# R5-1 — custom financial / intelligence / lender routes gate correctly
# --------------------------------------------------------------------------- #
async def test_custom_financial_create_requires_fi_write_op(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "R5-FI", "legal_name": "FI"})).json()["id"]
    body = {"entity_id": eid, "statement_type": "P&L", "period_end": "2025-03-31"}
    # Deal Analyst has edit_fi_record = NONE → the custom versioned-create is refused
    # (it used to gate on the far-looser add_company_note).
    assert (await client.post("/v1/financials", json=body,
                              headers=CREDIT_HEAD_ANALYST)).status_code == 403
    assert (await client.post("/v1/financials", json=body, headers=ADMIN)).status_code == 201


async def test_intel_acknowledge_requires_write_op(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "R5-IN", "legal_name": "IN"})).json()["id"]
    intel = (await client.post("/v1/external-intelligence",
                               json={"entity_id": eid, "intel_type": "News"},
                               headers=ADMIN)).json()
    # Credit Head is READ-only on clients → edit_intel NONE → cannot acknowledge/dismiss.
    assert (await client.post(f"/v1/external-intelligence/{intel['id']}/acknowledge",
                              headers=CREDIT_HEAD)).status_code == 403
    assert (await client.post(f"/v1/external-intelligence/{intel['id']}/dismiss",
                              headers=ADMIN)).status_code == 200


async def test_flat_lender_list_is_company_scoped(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "R5-LN", "legal_name": "LN"})).json()["id"]
    syn = (await client.post("/v1/syndication", json={"entity_id": eid})).json()
    await client.post(f"/v1/syndication/{syn['id']}/lenders",
                      json={"lender_name": "Axis"}, headers=ADMIN)
    # A Syn RM with no assignment to this line sees no lenders via the flat route…
    scoped = (await client.get("/v1/syndication-lenders", headers=SYN_RM)).json()
    assert all(r["syndication_id"] != syn["id"] for r in scoped["items"])
    # …Admin sees it.
    full = (await client.get("/v1/syndication-lenders", headers=ADMIN)).json()
    assert any(r["syndication_id"] == syn["id"] for r in full["items"])


# --------------------------------------------------------------------------- #
# R5-2 — status transitions cannot bypass the workflow
# --------------------------------------------------------------------------- #
async def test_direct_convert_and_lock_transitions_blocked(client: AsyncClient):
    eid = (await client.post("/v1/entities",
                             json={"code": "R5-TR", "legal_name": "TR"})).json()["id"]
    lead = (await client.post("/v1/leads",
                              json={"company": "TR", "entity_id": eid})).json()
    # Direct status=Converted via PATCH is refused (must use /convert).
    assert (await client.patch(f"/v1/leads/{lead['id']}", json={"status": "Converted"},
                               headers=ADMIN)).status_code == 422
    # A change request that would convert the lead is refused too (BD Head may request
    # stage changes, but conversion specifically must go through /convert).
    cr = await client.post("/v1/requests", json={
        "subject_type": "Lead", "subject_id": lead["id"], "field": "status",
        "to_value": "Converted"}, headers=BD_HEAD)
    assert cr.status_code == 422, cr.text
    # A lending line can't be PATCHed into the locked 'Disbursed' state by a non-lock
    # role (BDRM), even assigned — that's Credit Head / Admin / Management only. Walk it (ordered
    # pipeline) to 'Ready for Disbursement' first — the stage from which handover is reachable — so
    # the row-lock, not the sequencing rule, is what refuses the BDRM.
    lend = (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Diligence"})).json()
    for stage in ("Note Circulated", "Sanctioned", "CP/CS Completed", "Ready for Disbursement"):
        if stage == "Sanctioned":
            # Sanctioning is evidence-gated: the committee approval + sanction letter must be on
            # file first, each VERIFIED against a durable committee-authority decision. Seed one.
            import uuid as _uuid

            from sqlalchemy import text

            from app.db.session import get_sessionmaker
            wf = f"committee-{_uuid.uuid4().hex[:12]}"
            sm = get_sessionmaker()
            async with sm() as s:
                await s.execute(text(
                    "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, "
                    "subject_id, run_id, decided_by, decided_by_id, roles, tenant_id) "
                    "SELECT :wf, 'Approved', 'Lending', CAST(:sid AS varchar), 'run-1', "
                    "'ch@evamfinance.com', "
                    "'u-1', CAST('[\"Credit Head\"]' AS jsonb), tenant_id "  # noqa: S608
                    "FROM lending_tracker WHERE id = CAST(:sid AS uuid)"),
                    {"wf": wf, "sid": lend["id"]})
                await s.commit()
            for ek in ("credit_committee_approval", "sanction_letter"):
                assert (await client.post(
                    "/v1/evidence",
                    json={"subject_type": "Lending", "subject_id": lend["id"],
                          "evidence_kind": ek, "reference": f"{ek}/DOC-1",
                          "sha256": "b" * 64, "decision_ref": wf},
                    headers=ADMIN)).status_code == 201
        if stage == "CP/CS Completed":
            # 'CP/CS Completed' is evidence-gated: cp_cs_completion is minted from an APPROVED
            # maker-checker checklist (ADMIN prepares, CREDIT_HEAD approves); executed_agreement is
            # a governance kind.
            chk = await client.post(
                "/v1/internal/cpcs-checklists",
                json={"lending_id": lend["id"], "status": "Completed",
                      # The milestone-truth guard demands the checklist SHOW both
                      # halves done — a settled CS rides along with the CP.
                      "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"},
                                {"key": "cs1", "condition_type": "CS", "status": "Completed"}]},
                headers=ADMIN)
            assert chk.status_code == 201, chk.text
            assert (await client.post(
                f"/v1/internal/cpcs-checklists/{chk.json()['id']}/approve",
                headers=CREDIT_HEAD)).status_code == 200
            assert (await client.post(
                "/v1/evidence",
                json={"subject_type": "Lending", "subject_id": lend["id"],
                      "evidence_kind": "cp_cs_completion", "reference": "cpcs/1",
                      "sha256": "b" * 64, "decision_ref": chk.json()["id"]},
                headers=ADMIN)).status_code == 201
            assert (await client.post(
                "/v1/evidence",
                json={"subject_type": "Lending", "subject_id": lend["id"],
                      "evidence_kind": "executed_agreement", "reference": "ea/1",
                      # Cite the committee decision seeded at the Sanctioned hop — the workflow
                      # must RESOLVE to a decision for this subject; an invented id is refused.
                      "sha256": "b" * 64, "workflow_id": wf, "run_id": "run-1"},
                headers=ADMIN)).status_code == 201
        body = {"stage": stage}
        if stage == "Ready for Disbursement":
            body.update({"proposed_disbursement_amount": 5,
                         "proposed_disbursement_date": "2026-01-01"})
        assert (await client.patch(f"/v1/lending/{lend['id']}",
                                   json=body)).status_code == 200
    r = await client.patch(f"/v1/lending/{lend['id']}",
                           json={"stage": "Disbursed"}, headers=BDRM)
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# P0-6 — tenant administration requires a verified Admin identity
# --------------------------------------------------------------------------- #
async def test_tenant_admin_requires_admin_identity(client: AsyncClient):
    code = f"PWT{uuid.uuid4().hex[:4]}".upper()
    # A non-Admin human routed through the gateway is refused even with a valid key.
    r = await client.post("/v1/tenants", json={"code": code, "name": "X"}, headers=BDRM)
    assert r.status_code == 403, r.text
    # An Admin identity succeeds.
    r = await client.post("/v1/tenants", json={"code": code, "name": "X"}, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
