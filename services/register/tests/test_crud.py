"""CRUD, validation, auth and pagination behaviour."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_entity(client: AsyncClient, code: str = "E1", **extra) -> dict:
    body = {"code": code, "legal_name": f"{code} Pvt Ltd", **extra}
    r = await client.post("/v1/entities", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_api_key(client: AsyncClient):
    r = await client.get("/v1/entities", headers={"X-API-Key": ""})
    assert r.status_code == 401


async def test_create_get_update_delete(client: AsyncClient):
    ent = await _create_entity(client, "ACME", sector="Solar - General", lens="Mitigation")
    assert ent["version"] == 1
    eid = ent["id"]

    r = await client.get(f"/v1/entities/{eid}")
    assert r.status_code == 200
    assert r.json()["legal_name"] == "ACME Pvt Ltd"
    assert r.headers["ETag"] == '"1"'

    r = await client.patch(f"/v1/entities/{eid}", json={"state": "KA"})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["state"] == "KA"

    r = await client.delete(f"/v1/entities/{eid}")
    assert r.status_code == 204

    r = await client.get(f"/v1/entities/{eid}")
    assert r.status_code == 404

    r = await client.post(f"/v1/entities/{eid}/restore")
    assert r.status_code == 200
    assert r.json()["deleted_at"] is None


async def test_unknown_field_rejected(client: AsyncClient):
    r = await client.post("/v1/entities", json={"code": "X", "legal_name": "X", "bogus": 1})
    assert r.status_code == 422


async def test_duplicate_code_conflict(client: AsyncClient):
    await _create_entity(client, "DUP")
    r = await client.post("/v1/entities", json={"code": "DUP", "legal_name": "again"})
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "integrity_error"


async def test_missing_required_field(client: AsyncClient):
    r = await client.post("/v1/entities", json={"code": "NONAME"})
    assert r.status_code == 422


async def test_list_search_and_filter(client: AsyncClient):
    await _create_entity(client, "SOLARCO", sector="Solar - General")
    await _create_entity(client, "WATERCO", sector="Water Treatment / WASH")
    r = await client.get("/v1/entities", params={"sector": "Solar - General", "with_total": True})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "SOLARCO"

    r = await client.get("/v1/entities", params={"q": "WATERCO"})
    assert r.json()["count"] == 1


async def test_keyset_pagination(client: AsyncClient):
    for i in range(25):
        await _create_entity(client, f"P{i:03d}")
    seen: set[str] = set()
    cursor = None
    pages = 0
    while True:
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/v1/entities", params=params)
        body = r.json()
        for item in body["items"]:
            seen.add(item["id"])
        cursor = body["next_cursor"]
        pages += 1
        if not cursor:
            break
    assert len(seen) == 25
    assert pages == 3


async def test_deal_requires_valid_entity(client: AsyncClient):
    # entity_id pointing nowhere → integrity error (FK), never a silent orphan.
    r = await client.post("/v1/deals", json={"entity_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code in (409, 422)


async def test_dossier(client: AsyncClient):
    ent = await _create_entity(client, "DOSS")
    eid = ent["id"]
    await client.post("/v1/deals", json={"entity_id": eid, "is_lending": True, "code": "DOSS"})
    r = await client.get(f"/v1/entities/{eid}/dossier")
    assert r.status_code == 200
    assert r.json()["counts"]["deals"] == 1
