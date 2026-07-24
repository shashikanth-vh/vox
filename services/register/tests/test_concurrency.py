"""Concurrency guarantees — the crux of "no lost updates, no race conditions".

These tests fire many overlapping requests at the same rows and assert the Register
behaves like a correct source of truth: optimistic locking rejects lost updates,
idempotency keys dedupe retried creates, parallel inserts never deadlock, and the
versioned Financials writer serialises cleanly.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _entity(client: AsyncClient, code: str) -> dict:
    r = await client.post("/v1/entities", json={"code": code, "legal_name": code})
    assert r.status_code == 201, r.text
    return r.json()


async def test_optimistic_lock_prevents_lost_update(client: AsyncClient):
    """N writers all start from version 1 with If-Match: exactly one wins, rest 409."""
    ent = await _entity(client, "RACE")
    eid = ent["id"]
    headers = {"If-Match": '"1"'}

    async def patch(state: str):
        return await client.patch(f"/v1/entities/{eid}", json={"state": state}, headers=headers)

    results = await asyncio.gather(*[patch(f"S{i}") for i in range(12)])
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, codes
    assert codes.count(409) == 11, codes
    assert all(r.json()["error"]["type"] == "version_conflict"
               for r in results if r.status_code == 409)

    # The surviving row is at version 2 — precisely one update landed, none lost.
    r = await client.get(f"/v1/entities/{eid}")
    assert r.json()["version"] == 2


async def test_idempotency_dedupes_concurrent_creates(client: AsyncClient):
    """Ten concurrent POSTs sharing an Idempotency-Key create exactly one row."""
    headers = {"Idempotency-Key": "same-key-123"}

    async def create():
        return await client.post(
            "/v1/counterparties", json={"name": "Concurrent Bank"}, headers=headers
        )

    results = await asyncio.gather(*[create() for _ in range(10)], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception) and r.status_code in (201, 200)]
    # Every successful response points at the same id.
    ids = {r.json()["id"] for r in ok}
    assert len(ids) == 1, ids

    r = await client.get("/v1/counterparties", params={"q": "Concurrent Bank", "with_total": True})
    assert r.json()["total"] == 1


async def test_parallel_distinct_creates_no_deadlock(client: AsyncClient):
    """Fifty independent inserts run concurrently and all succeed (deadlock-free)."""
    async def create(i: int):
        return await client.post("/v1/entities", json={"code": f"C{i:03d}", "legal_name": f"c{i}"})

    results = await asyncio.gather(*[create(i) for i in range(50)])
    assert all(r.status_code == 201 for r in results)
    r = await client.get("/v1/entities", params={"limit": 1, "with_total": True})
    assert r.json()["total"] == 50


async def test_concurrent_financial_versions_serialise(client: AsyncClient):
    """Concurrent submissions for the same statement/period get distinct, sequential
    version numbers and leave exactly one row flagged current."""
    ent = await _entity(client, "FINRACE")
    eid = ent["id"]

    async def submit(rev: int):
        return await client.post("/v1/financials", json={
            "entity_id": eid, "statement_type": "Audited",
            "period_end": "2025-03-31", "revenue": rev,
        })

    results = await asyncio.gather(*[submit(i) for i in range(8)])
    assert all(r.status_code == 201 for r in results), [r.status_code for r in results]
    versions = sorted(r.json()["version_no"] for r in results)
    assert versions == list(range(1, 9)), versions  # 1..8, no duplicates, no gaps

    r = await client.get(
        "/v1/financials",
        params={"entity_id": eid, "is_current": "true", "with_total": True},
    )
    assert r.json()["total"] == 1  # the partial-unique index holds under concurrency


async def test_soft_delete_optimistic_conflict(client: AsyncClient):
    ent = await _entity(client, "DELRACE")
    eid = ent["id"]
    await client.patch(f"/v1/entities/{eid}", json={"state": "KA"})  # now version 2
    # Deleting with a stale version must be refused.
    r = await client.delete(f"/v1/entities/{eid}", headers={"If-Match": '"1"'})
    assert r.status_code == 409
