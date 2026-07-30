"""R8 hardening tests: machine assignment listing, change-request vertical scoping,
approval-path transition policy, and lead-bound conversion idempotency."""

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


ADMIN = _as("admin@evamfinance.com", "Admin")
BD_HEAD = _as("bdhead.r8@evamfinance.com", "BD Head")
CREDIT_HEAD = _as("credithead.r8@evamfinance.com", "Credit Head")


async def _entity(client: AsyncClient, code: str) -> str:
    return (await client.post("/v1/entities",
                              json={"code": code, "legal_name": code})).json()["id"]


# --------------------------------------------------------------------------- #
# P0-5 — a MACHINE caller may not enumerate the tenant-wide assignment directory
# --------------------------------------------------------------------------- #
async def test_machine_assignment_list_requires_a_filter(client: AsyncClient):
    # No user context (machine) and no filter → refused; it must narrow to a user/line.
    unfiltered = await client.get("/v1/assignments")
    assert unfiltered.status_code == 403, unfiltered.text
    # A specific filter is allowed (this is how the gateway's /v1/me composes).
    filtered = await client.get("/v1/assignments", params={"user_id": str(uuid.uuid4())})
    assert filtered.status_code == 200, filtered.text
    # A human still lists (self-scoped) without an explicit filter.
    human = await client.get("/v1/assignments", headers=ADMIN)
    assert human.status_code == 200, human.text


# --------------------------------------------------------------------------- #
# P0-6 — a Head sees only their vertical's change-request queue (+ their own)
# --------------------------------------------------------------------------- #
async def test_change_request_queue_is_vertical_scoped(client: AsyncClient):
    eid = await _entity(client, "R8-CRV")
    lead = (await client.post("/v1/leads",
                              json={"company": "R8 Lead", "entity_id": eid})).json()
    lending = (await client.post("/v1/lending", json={"entity_id": eid})).json()

    # BD Head raises a Lead status request; Credit Head raises a Lending stage request.
    lead_cr = await client.post("/v1/requests", json={
        "subject_type": "Lead", "subject_id": lead["id"], "field": "status",
        "to_value": "On Hold"}, headers=BD_HEAD)
    assert lead_cr.status_code == 201, lead_cr.text
    lend_cr = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lending["id"], "field": "stage",
        "to_value": "Note Circulated"}, headers=CREDIT_HEAD)
    assert lend_cr.status_code == 201, lend_cr.text

    # The Credit Head queue includes the Lending request but NOT the unrelated Lead one.
    seen = (await client.get("/v1/requests", headers=CREDIT_HEAD)).json()
    ids = {r["id"] for r in seen}
    assert lend_cr.json()["id"] in ids
    assert lead_cr.json()["id"] not in ids
    # Admin (approves every vertical) sees both.
    all_ids = {r["id"] for r in (await client.get("/v1/requests", headers=ADMIN)).json()}
    assert {lead_cr.json()["id"], lend_cr.json()["id"]} <= all_ids


# --------------------------------------------------------------------------- #
# P0-6 — the approval path obeys the SAME transition state-machine a direct edit does
# --------------------------------------------------------------------------- #
async def test_approval_path_enforces_allowed_transitions(client: AsyncClient):
    eid = await _entity(client, "R8-TRN")
    lead = (await client.post("/v1/leads",
                              json={"company": "R8 Trn", "entity_id": eid,
                                    "status": "Dropped"})).json()
    # Dropped → On Hold is NOT an allowed Lead.status transition (Dropped → Active only).
    cr = await client.post("/v1/requests", json={
        "subject_type": "Lead", "subject_id": lead["id"], "field": "status",
        "to_value": "On Hold"}, headers=BD_HEAD)
    assert cr.status_code == 201, cr.text
    # Approving it must be refused by the transition policy (not silently applied).
    decided = await client.post(f"/v1/requests/{cr.json()['id']}/approve", json={},
                                headers=BD_HEAD)
    assert decided.status_code == 409, decided.text
    assert "may not move" in decided.text.lower()


# --------------------------------------------------------------------------- #
# P0-7 — conversion idempotency is bound to the LEAD, not just the body
# --------------------------------------------------------------------------- #
async def test_convert_idempotency_is_bound_to_lead(client: AsyncClient):
    eid = await _entity(client, "R8-IDEM")
    lead1 = (await client.post("/v1/leads",
                               json={"company": "R8 A", "entity_id": eid})).json()
    lead2 = (await client.post("/v1/leads",
                               json={"company": "R8 B", "entity_id": eid})).json()
    key = {"Idempotency-Key": f"r8-{uuid.uuid4()}"}
    body = {"is_lending": True}

    r1 = await client.post(f"/v1/leads/{lead1['id']}/convert", json=body, headers=key)
    assert r1.status_code == 200, r1.text
    assert r1.json()["lead_id"] == lead1["id"]

    # SAME key + SAME body but a DIFFERENT lead: the hash now includes the lead id, so this
    # is a key-reuse with a different request → 409, NOT a silent replay of lead1's deal.
    r2 = await client.post(f"/v1/leads/{lead2['id']}/convert", json=body, headers=key)
    assert r2.status_code == 409, r2.text
