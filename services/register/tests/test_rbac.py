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
