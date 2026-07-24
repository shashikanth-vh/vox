"""Tenant administration CRUD (the tenant boundary is not itself tenant-scoped)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_tenant_crud_lifecycle(client: AsyncClient):
    # EVAM exists (created by the fixture); it shows up in the list.
    r = await client.get("/v1/tenants")
    assert r.status_code == 200
    assert "EVAM" in {t["code"] for t in r.json()}

    # A brand-new tenant cannot be used until it's created.
    r = await client.get("/v1/leads", headers={"X-Tenant": "COLENDER"})
    assert r.status_code == 403

    # Create it via the API.
    r = await client.post("/v1/tenants", json={"code": "COLENDER", "name": "Co-lender X"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "COLENDER"
    assert body["is_active"] is True
    assert body["created_by"] == "pytest"  # from X-Actor
    new_id = body["id"]

    # ...and immediately it works (cache was invalidated).
    r = await client.get("/v1/leads", headers={"X-Tenant": "COLENDER"})
    assert r.status_code == 200

    # Duplicate code → 409.
    r = await client.post("/v1/tenants", json={"code": "COLENDER", "name": "dup"})
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "conflict"

    # Read by code and by id.
    assert (await client.get("/v1/tenants/COLENDER")).status_code == 200
    r = await client.get(f"/v1/tenants/{new_id}")
    assert r.status_code == 200 and r.json()["code"] == "COLENDER"

    # Rename.
    r = await client.patch("/v1/tenants/COLENDER", json={"name": "Co-lender X Ltd"})
    assert r.status_code == 200
    assert r.json()["name"] == "Co-lender X Ltd"
    assert r.json()["updated_by"] == "pytest"

    # Deactivate (soft) → the tenant can no longer be used.
    r = await client.delete("/v1/tenants/COLENDER")
    assert r.status_code == 200 and r.json()["is_active"] is False
    r = await client.get("/v1/leads", headers={"X-Tenant": "COLENDER"})
    assert r.status_code == 403

    # Reactivate → usable again.
    r = await client.patch("/v1/tenants/COLENDER", json={"is_active": True})
    assert r.status_code == 200 and r.json()["is_active"] is True
    r = await client.get("/v1/leads", headers={"X-Tenant": "COLENDER"})
    assert r.status_code == 200

    # Every change is audited under the affected tenant (COLENDER is active again now).
    r = await client.get("/v1/audit", headers={"X-Tenant": "COLENDER"})
    actions = [a["action"] for a in r.json() if a["resource_type"] == "tenant"]
    assert "create" in actions and "deactivate" in actions


async def test_tenant_unknown_returns_404(client: AsyncClient):
    assert (await client.get("/v1/tenants/DOES-NOT-EXIST")).status_code == 404


async def test_tenant_validation(client: AsyncClient):
    # Missing required name → 422.
    assert (await client.post("/v1/tenants", json={"code": "X"})).status_code == 422
    # Unknown field → 422 (extra="forbid").
    r = await client.post("/v1/tenants", json={"code": "Y", "name": "Y", "bogus": 1})
    assert r.status_code == 422


async def test_tenant_admin_requires_api_key(client: AsyncClient):
    r = await client.get("/v1/tenants", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401
