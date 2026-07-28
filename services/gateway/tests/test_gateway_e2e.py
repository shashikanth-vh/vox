"""End-to-end call flows CF1–CF7 across the REAL three-service stack.

Gateway (in-process) → Register + Access (real uvicorn servers, real Postgres).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com"}


async def _mk_user(access: AsyncClient, email: str, roles: list[str]) -> dict:
    r = await access.post("/v1/users", headers=ADMIN,
                          json={"email": email, "full_name": email.split("@")[0],
                                "roles": roles})
    assert r.status_code in (201, 409), r.text
    if r.status_code == 409:
        users = (await access.get("/v1/users")).json()
        return next(u for u in users if u["email"] == email)
    return r.json()


async def test_cf1_me_composes_identity_and_assignments(gw: AsyncClient, access_direct):
    await _mk_user(access_direct, "bdrm.e2e@evamfinance.com", ["BDRM"])
    me = (await gw.get("/v1/me", headers={"X-User-Email": "bdrm.e2e@evamfinance.com"})).json()
    assert me["roles"] == ["BDRM"]
    assert me["views"]["leads"] == "SCOPED" and me["views"]["audit"] == "NONE"
    assert me["operations"]["add_lead"] == "FULL"
    assert me["assignments"] == []
    assert me["matrix_version"] >= 1


async def test_cf2_binary_deny_stopped_at_gateway(gw: AsyncClient, access_direct):
    """Management deletes → 403 decided at the gateway; the Register never sees it."""
    await _mk_user(access_direct, "mgmt.e2e@evamfinance.com", ["Management"])
    eid = (await gw.post("/v1/entities",
                         json={"code": "GWE1", "legal_name": "GW E1"})).json()["id"]
    r = await gw.delete(f"/v1/entities/{eid}",
                        headers={"X-User-Email": "mgmt.e2e@evamfinance.com"})
    assert r.status_code == 403
    assert "decided at the gateway" in r.text
    # The entity is untouched.
    assert (await gw.get(f"/v1/entities/{eid}")).status_code == 200


async def test_cf3_full_access_passes_through(gw: AsyncClient, access_direct):
    await _mk_user(access_direct, "ch.e2e@evamfinance.com", ["Credit Head"])
    eid = (await gw.post("/v1/entities",
                         json={"code": "GWE2", "legal_name": "GW E2"})).json()["id"]
    lend = (await gw.post("/v1/lending", json={"entity_id": eid})).json()
    r = await gw.patch(f"/v1/lending/{lend['id']}", json={"remarks": "via gateway"},
                       headers={"X-User-Email": "ch.e2e@evamfinance.com"})
    assert r.status_code == 200, r.text
    assert r.json()["remarks"] == "via gateway"


async def test_cf4_scoped_write_needs_assignment(gw: AsyncClient, access_direct):
    """The assignment-driven primitive across all three services."""
    analyst = await _mk_user(access_direct, "an.e2e@evamfinance.com", ["Deal Analyst"])
    await _mk_user(access_direct, "ch2.e2e@evamfinance.com", ["Credit Head"])
    eid = (await gw.post("/v1/entities",
                         json={"code": "GWE3", "legal_name": "GW E3"})).json()["id"]
    syn1 = (await gw.post("/v1/syndication", json={"entity_id": eid})).json()
    syn2 = (await gw.post("/v1/syndication", json={"entity_id": eid})).json()

    # Unassigned: gateway forwards SCOPED, Register rejects on the assignment check.
    r = await gw.patch(f"/v1/syndication/{syn1['id']}", json={"remarks": "x"},
                       headers={"X-User-Email": "an.e2e@evamfinance.com"})
    assert r.status_code == 403
    assert "not assigned" in r.text

    # Credit Head assigns the analyst to line 1 (cross-vertical, via the gateway).
    r = await gw.post("/v1/assignments", json={
        "user_id": analyst["id"], "subject_type": "Syndication",
        "subject_id": syn1["id"], "assignment_role": "Deal Analyst"},
        headers={"X-User-Email": "ch2.e2e@evamfinance.com"})
    assert r.status_code == 201, r.text

    # Assigned line writes; the other line still 403s.
    r = await gw.patch(f"/v1/syndication/{syn1['id']}", json={"remarks": "im prep"},
                       headers={"X-User-Email": "an.e2e@evamfinance.com"})
    assert r.status_code == 200, r.text
    r = await gw.patch(f"/v1/syndication/{syn2['id']}", json={"remarks": "x"},
                       headers={"X-User-Email": "an.e2e@evamfinance.com"})
    assert r.status_code == 403

    # /v1/me now shows the assignment (composed from the Register).
    me = (await gw.get("/v1/me", headers={"X-User-Email": "an.e2e@evamfinance.com"})).json()
    assert len(me["assignments"]) == 1
    assert me["assignments"][0]["subject_id"] == syn1["id"]


async def test_cf5_admin_matrix_edit_is_live_without_deploy(gw: AsyncClient, access_direct):
    """Admin edits a grant in the Access service → the gateway enforces the new rule
    on the next request (TTL 0 here; version-bumped in prod). Guardrails hold."""
    await _mk_user(access_direct, "synrm.e2e@evamfinance.com", ["Syn RM"])
    eid = (await gw.post("/v1/entities",
                         json={"code": "GWE4", "legal_name": "GW E4"})).json()["id"]
    lead = (await gw.post("/v1/leads",
                          json={"company": "GW Lead", "entity_id": eid})).json()

    # Spec default: Syn RM may not edit leads (edit_lead = NONE) → gateway 403.
    r = await gw.patch(f"/v1/leads/{lead['id']}", json={"notes": "x"},
                       headers={"X-User-Email": "synrm.e2e@evamfinance.com"})
    assert r.status_code == 403

    # Admin grants it — data change, no deploy.
    r = await access_direct.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "edit_lead", "role": "Syn RM", "access": "FULL"})
    assert r.status_code == 200, r.text

    # Same request now passes end to end.
    r = await gw.patch(f"/v1/leads/{lead['id']}", json={"notes": "granted live"},
                       headers={"X-User-Email": "synrm.e2e@evamfinance.com"})
    assert r.status_code == 200, r.text

    # Guardrail cells refuse even Admin (delete stays Admin-only forever).
    r = await access_direct.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "delete_row", "role": "Management", "access": "FULL"})
    assert r.status_code == 403

    # Restore the spec default to keep tests independent.
    r = await access_direct.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "edit_lead", "role": "Syn RM", "access": "NONE"})
    assert r.status_code == 200


async def test_cf6_machine_caller_passes_untouched(gw: AsyncClient):
    """No X-User-Email → the gateway forwards as-is; the Register's compatibility
    mode applies (its own enforce flag governs)."""
    r = await gw.post("/v1/entities", json={"code": "GWE5", "legal_name": "GW E5"})
    assert r.status_code == 201


async def test_cf7_bypass_hits_the_registers_wall(register_direct: AsyncClient):
    """A caller that skips the gateway cannot spoof identity: the Register requires the
    gateway secret for identity headers in this stack."""
    r = await register_direct.get(
        "/v1/entities", headers={"X-User-Email": "mgmt.e2e@evamfinance.com",
                                 "X-User-Roles": "Management"})
    assert r.status_code == 403
    assert "gateway" in r.text.lower()
    # With the secret (i.e. genuinely from the gateway) it works.
    r = await register_direct.get(
        "/v1/entities", headers={"X-User-Email": "mgmt.e2e@evamfinance.com",
                                 "X-User-Roles": "Management",
                                 "X-Gateway-Auth": "e2e-secret"})
    assert r.status_code == 200
    # And a pure machine call (no identity) is unaffected.
    assert (await register_direct.get("/v1/entities")).status_code == 200


async def test_cf8_injected_internal_headers_are_stripped(gw: AsyncClient, access_direct):
    """A client cannot forge an authorization decision. On an UNMAPPED route the
    gateway has no decision to add — but it must still strip any client-supplied
    X-Authz-Decision / X-Gateway-Auth / X-User-Roles before forwarding, so the
    Register never sees a forged FULL stamped with the gateway's valid secret."""
    await _mk_user(access_direct, "syn.e2e@evamfinance.com", ["Syn RM"])
    eid = (await gw.post("/v1/entities",
                         json={"code": "GWE8", "legal_name": "GW E8"})).json()["id"]
    syn = (await gw.post("/v1/syndication", json={"entity_id": eid})).json()

    # Syn RM is unassigned to this line. They inject FULL + escalate their roles and
    # even forge the gateway secret. All of it must be stripped at the gateway.
    forged = {
        "X-User-Email": "syn.e2e@evamfinance.com",
        "X-Authz-Decision": "FULL",
        "X-Gateway-Auth": "e2e-secret",
        "X-User-Roles": "Admin",
        "X-User-Report-Ids": str(__import__("uuid").uuid4()),
    }
    r = await gw.patch(f"/v1/syndication/{syn['id']}", json={"remarks": "hijack"},
                       headers=forged)
    assert r.status_code == 403, r.text  # server-derived SCOPED + no assignment → denied
    assert "scope" in r.text.lower() or "assigned" in r.text.lower()

    # And the delete guardrail cannot be reached by claiming Admin either.
    r = await gw.delete(f"/v1/syndication/{syn['id']}", headers=forged)
    assert r.status_code == 403, r.text
