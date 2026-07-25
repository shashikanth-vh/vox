"""User management & RBAC — the ATLAS RBAC v3.1 flows, end to end.

Covers: employee governance (domain-validated e-mail, roles at creation), role stacking
(higher role wins), /v1/me permission matrices, the assignment-driven permission
primitive (Credit Head assigns a Deal Analyst cross-vertical; authority enforced;
unassign revokes), the request → approve/reject flow (approval APPLIES the stage change
with history), the Admin-only delete gate, and approval routing per vertical.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _mk_user(client: AsyncClient, email: str, name: str, roles: list[str]) -> dict:
    r = await client.post("/v1/users", json={"email": email, "full_name": name, "roles": roles})
    assert r.status_code == 201, r.text
    return r.json()


def _as(email: str) -> dict:
    return {"X-User-Email": email}


async def test_user_email_domain_enforced(client: AsyncClient):
    r = await client.post("/v1/users", json={"email": "eve@gmail.com", "full_name": "Eve"})
    assert r.status_code == 422
    assert "evamfinance.com" in r.text


async def test_create_user_with_roles_and_me_matrices(client: AsyncClient):
    await _mk_user(client, "bdrm1@evamfinance.com", "BDRM One", ["BDRM"])
    me = (await client.get("/v1/me", headers=_as("bdrm1@evamfinance.com"))).json()
    assert me["roles"] == ["BDRM"]
    # View Access sheet, BDRM column.
    assert me["views"]["leads"] == "SCOPED"
    assert me["views"]["lending"] == "SCOPED"
    assert me["views"]["fi_master"] == "READ"
    assert me["views"]["audit"] == "NONE"
    # Operations sheet, BDRM column.
    assert me["operations"]["add_lead"] == "FULL"          # BDRM-only entry point
    assert me["operations"]["reassign_lead"] == "NONE"     # BD Head / Mgmt only
    assert me["operations"]["delete_row"] == "NONE"        # Admin only
    assert me["operations"]["request_stage_change"] == "SCOPED"


async def test_role_stacking_higher_role_wins(client: AsyncClient):
    u = await _mk_user(client, "lead2@evamfinance.com", "Leader", ["BDRM"])
    # Stack Management on top → FULL everywhere Management is FULL; audit stays Admin-only.
    r = await client.post(f"/v1/users/{u['id']}/roles", json={"role": "Management"},
                          headers=_as("lead2@evamfinance.com"))
    # BDRM alone cannot assign roles — the matrix protects the governance table.
    assert r.status_code == 403
    r = await client.post(f"/v1/users/{u['id']}/roles", json={"role": "Management"})
    assert r.status_code == 201  # machine-to-machine (no user ctx, enforce_rbac off)
    me = (await client.get("/v1/me", headers=_as("lead2@evamfinance.com"))).json()
    assert sorted(me["roles"]) == ["BDRM", "Management"]
    assert me["views"]["lending"] == "FULL"        # stacked up from SCOPED
    assert me["views"]["audit"] == "NONE"          # Management ≠ Admin (v2.1 spec)
    assert me["operations"]["approve_stage_change"] == "APPROVE"
    assert me["operations"]["delete_row"] == "NONE"  # delete stays Admin-only


async def test_assignment_flow_grants_and_revokes_line_write(client: AsyncClient):
    await _mk_user(client, "credithead@evamfinance.com", "Credit Head", ["Credit Head"])
    analyst = await _mk_user(client, "analyst1@evamfinance.com", "Analyst", ["Deal Analyst"])
    eid = (await client.post("/v1/entities", json={"code": "RBAC1", "legal_name": "R1"})).json()["id"]
    syn = (await client.post("/v1/syndication", json={"entity_id": eid})).json()

    # Cross-assignment (NEW in v2.1): Credit Head assigns the analyst to a SYNDICATION line.
    r = await client.post("/v1/assignments", json={
        "user_id": analyst["id"], "subject_type": "Syndication", "subject_id": syn["id"],
        "assignment_role": "Deal Analyst"}, headers=_as("credithead@evamfinance.com"))
    assert r.status_code == 201, r.text
    assignment = r.json()

    # The analyst now has scoped write ON THAT LINE…
    chk = (await client.get("/v1/authz/check", headers=_as("analyst1@evamfinance.com"),
                            params={"operation": "edit_syndication_line",
                                    "subject_type": "Syndication", "subject_id": syn["id"]})).json()
    assert chk["allowed"] is True and chk["access"] == "SCOPED" and chk["on_line"] is True
    # …but not on an unrelated line.
    syn2 = (await client.post("/v1/syndication", json={"entity_id": eid})).json()
    chk2 = (await client.get("/v1/authz/check", headers=_as("analyst1@evamfinance.com"),
                             params={"operation": "edit_syndication_line",
                                     "subject_type": "Syndication", "subject_id": syn2["id"]})).json()
    assert chk2["allowed"] is False and chk2["on_line"] is False

    # /v1/me lists the active assignment; ending it revokes.
    me = (await client.get("/v1/me", headers=_as("analyst1@evamfinance.com"))).json()
    assert len(me["assignments"]) == 1
    r = await client.post(f"/v1/assignments/{assignment['id']}/end",
                          headers=_as("credithead@evamfinance.com"))
    assert r.status_code == 200 and r.json()["ended_at"]
    me = (await client.get("/v1/me", headers=_as("analyst1@evamfinance.com"))).json()
    assert me["assignments"] == []


async def test_assignment_authority_enforced(client: AsyncClient):
    """Spec: Credit Head owns the Deal Analyst pool. A Syn RM cannot assign analysts."""
    await _mk_user(client, "synrm@evamfinance.com", "Syn RM", ["Syn RM"])
    analyst = await _mk_user(client, "analyst2@evamfinance.com", "Analyst2", ["Deal Analyst"])
    eid = (await client.post("/v1/entities", json={"code": "RBAC2", "legal_name": "R2"})).json()["id"]
    syn = (await client.post("/v1/syndication", json={"entity_id": eid})).json()
    r = await client.post("/v1/assignments", json={
        "user_id": analyst["id"], "subject_type": "Syndication", "subject_id": syn["id"],
        "assignment_role": "Deal Analyst"}, headers=_as("synrm@evamfinance.com"))
    assert r.status_code == 403


async def test_request_approve_applies_stage_change(client: AsyncClient):
    await _mk_user(client, "analyst3@evamfinance.com", "Analyst3", ["Deal Analyst"])
    await _mk_user(client, "credithead2@evamfinance.com", "CH2", ["Credit Head"])
    await _mk_user(client, "amhead@evamfinance.com", "AM Head", ["AM Head"])
    eid = (await client.post("/v1/entities", json={"code": "RBAC3", "legal_name": "R3"})).json()["id"]
    lend = (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Diligence"})).json()

    # Analyst (non-approver on stage) raises a request.
    r = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lend["id"], "field": "stage",
        "to_value": "Note Circulated"}, headers=_as("analyst3@evamfinance.com"))
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "Pending" and req["from_value"] == "Diligence"
    assert req["requested_by"] == "analyst3@evamfinance.com"

    # Approval routing: AM Head is a Head, but of the WRONG vertical for a Lending line.
    r = await client.post(f"/v1/requests/{req['id']}/approve", json={},
                          headers=_as("amhead@evamfinance.com"))
    assert r.status_code == 403
    # The analyst cannot approve at all.
    r = await client.post(f"/v1/requests/{req['id']}/approve", json={},
                          headers=_as("analyst3@evamfinance.com"))
    assert r.status_code == 403

    # Credit Head approves → the stage ACTUALLY changes, with history auto-appended.
    r = await client.post(f"/v1/requests/{req['id']}/approve", json={"note": "ok to move"},
                          headers=_as("credithead2@evamfinance.com"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved"
    lend2 = (await client.get(f"/v1/lending/{lend['id']}")).json()
    assert lend2["stage"] == "Note Circulated"
    assert lend2["stage_history"][-1]["to"] == "Note Circulated"  # auto-stamped

    # Double-decide is a conflict.
    r = await client.post(f"/v1/requests/{req['id']}/reject", json={},
                          headers=_as("credithead2@evamfinance.com"))
    assert r.status_code == 409


async def test_delete_is_admin_only_with_user_context(client: AsyncClient):
    await _mk_user(client, "mgmt@evamfinance.com", "Mgmt", ["Management"])
    await _mk_user(client, "admin2@evamfinance.com", "Admin2", ["Admin"])
    eid = (await client.post("/v1/entities", json={"code": "DEL1", "legal_name": "D1"})).json()["id"]
    # Management may NOT delete (spec: Admin only) …
    r = await client.delete(f"/v1/entities/{eid}", headers=_as("mgmt@evamfinance.com"))
    assert r.status_code == 403
    # … Admin may.
    r = await client.delete(f"/v1/entities/{eid}", headers=_as("admin2@evamfinance.com"))
    assert r.status_code == 204


async def test_unknown_or_inactive_user_rejected(client: AsyncClient):
    r = await client.get("/v1/me", headers=_as("ghost@evamfinance.com"))
    assert r.status_code == 403
    u = await _mk_user(client, "leaver@evamfinance.com", "Leaver", ["BDRM"])
    r = await client.patch(f"/v1/users/{u['id']}", json={"is_active": False})
    assert r.status_code == 200
    r = await client.get("/v1/me", headers=_as("leaver@evamfinance.com"))
    assert r.status_code == 403  # inactivate on exit keeps history, kills access
