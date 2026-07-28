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
    lead = (await client.post("/v1/leads",
                              json={"company": "ID", "entity_id": eid})).json()
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
