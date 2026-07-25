"""Access service — governance, matrix-as-data, guardrails, resolve."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com"}


async def test_seeded_matrix_and_admin(client: AsyncClient):
    body = (await client.get("/v1/access")).json()
    assert body["version"] >= 1
    # Spec cells present verbatim.
    assert body["operations"]["delete_row"]["Admin"] == "FULL"
    assert body["operations"]["delete_row"]["Management"] == "NONE"
    assert body["views"]["lending"]["Credit Head"] == "FULL"
    assert body["views"]["audit"]["Management"] == "NONE"
    users = (await client.get("/v1/users")).json()
    assert any(u["email"] == "admin@evamfinance.com" for u in users)


async def test_user_governance_admin_only(client: AsyncClient):
    # Admin creates a BDRM.
    r = await client.post("/v1/users", headers=ADMIN, json={
        "email": "bdrm@evamfinance.com", "full_name": "BDRM", "roles": ["BDRM"]})
    assert r.status_code == 201, r.text
    # Non-admin (the BDRM) may not create users.
    r = await client.post("/v1/users", headers={"X-User-Email": "bdrm@evamfinance.com"},
                          json={"email": "x@evamfinance.com", "full_name": "X"})
    assert r.status_code == 403
    # Domain enforced.
    r = await client.post("/v1/users", headers=ADMIN,
                          json={"email": "eve@gmail.com", "full_name": "Eve"})
    assert r.status_code == 422


async def test_matrix_edit_bumps_version_and_guardrails(client: AsyncClient):
    v0 = (await client.get("/v1/access/version")).json()["version"]
    # Admin grants BDRM the reassign_lead operation (spec default: NONE).
    r = await client.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "reassign_lead", "role": "BDRM", "access": "FULL"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == v0 + 1
    body = (await client.get("/v1/access")).json()
    assert body["operations"]["reassign_lead"]["BDRM"] == "FULL"
    # Guardrail cell refuses even Admin.
    r = await client.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "delete_row", "role": "Management", "access": "FULL"})
    assert r.status_code == 403
    assert "guardrail" in r.text.lower()
    # Non-admin cannot edit the matrix at all.
    await client.post("/v1/users", headers=ADMIN, json={
        "email": "mgmt@evamfinance.com", "full_name": "M", "roles": ["Management"]})
    r = await client.patch("/v1/access", headers={"X-User-Email": "mgmt@evamfinance.com"},
                          json={"kind": "view", "item": "leads", "role": "BDRM",
                                "access": "FULL"})
    assert r.status_code == 403


async def test_resolve_stacking_and_inactive(client: AsyncClient):
    await client.post("/v1/users", headers=ADMIN, json={
        "email": "lead@evamfinance.com", "full_name": "Leader",
        "roles": ["BDRM", "Management"]})
    res = (await client.get("/v1/resolve", params={"email": "lead@evamfinance.com"})).json()
    assert sorted(res["roles"]) == ["BDRM", "Management"]
    assert res["views"]["lending"] == "FULL"       # stacked up from SCOPED
    assert res["views"]["audit"] == "NONE"         # Management ≠ Admin
    assert res["operations"]["approve_stage_change"] == "APPROVE"
    assert res["version"] >= 1

    # Deactivate → resolve 404s (gateway drops the user).
    uid = next(u["id"] for u in (await client.get("/v1/users")).json()
               if u["email"] == "lead@evamfinance.com")
    r = await client.patch(f"/v1/users/{uid}", headers=ADMIN, json={"is_active": False})
    assert r.status_code == 200
    r = await client.get("/v1/resolve", params={"email": "lead@evamfinance.com"})
    assert r.status_code == 404


async def test_me(client: AsyncClient):
    me = (await client.get("/v1/me", headers=ADMIN)).json()
    assert me["email"] == "admin@evamfinance.com"
    assert me["operations"]["delete_row"] == "FULL"
    assert (await client.get("/v1/me")).status_code == 403  # requires user context
