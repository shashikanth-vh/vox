"""The Tracxn market feed: CIN anchoring, caching, normalisation, refusals."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import app.api.tracxn as tracxn_mod
from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.seed.loader import ensure_tenant

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
CIN = "L72200KA2012PLC065294"

# One canned Tracxn per path — realistic shapes from the Postman collection's
# documentation, exercised through the SAME normaliser production runs.
CANNED = {
    "/legalentities": {"result": [
        {"id": "le-123", "entityId": CIN, "name": "Bangalore Test Company"}]},
    "/boardmembers": {"result": [
        {"people": {"id": "p1", "name": "Rohan Mehta"},
         "designation": "Managing Director", "from": "2015-04-01"},
        {"people": {"id": "p2", "name": "Priya Sharma"}, "designation": "CFO"}]},
    "/captables": {"result": [
        {"shareholders": [
            {"name": "Promoter Group", "percentage": 74.2},
            {"name": "Angel Fund I", "percentage": 12.5}]}]},
    "/companies/timeseries/revenue": {"result": [
        {"financialYear": "2023", "value": 71.2, "currency": "INR Cr",
         "companyIds": ["c-9"]},
        {"financialYear": "2024", "value": 79.8, "currency": "INR Cr"},
        {"financialYear": "2025", "value": 86.4, "currency": "INR Cr"}]},
    "/companies/timeseries/ebitda": {"result": [
        {"financialYear": "2025", "value": 9.1, "currency": "INR Cr"}]},
}


@pytest_asyncio.fixture
async def reg(monkeypatch) -> AsyncIterator[AsyncClient]:
    init_engine()
    clear_tenant_cache()
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()

    calls: list[str] = []

    async def fake_post(settings, path, body):  # noqa: ANN001
        calls.append(path)
        return CANNED.get(path, {"result": []})

    monkeypatch.setattr(tracxn_mod, "_tracxn_post", fake_post)
    app = create_app()
    app.state._tracxn_calls = calls
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": get_settings().all_api_keys()[0],
                                        "X-Tenant": "EVAM"}) as c:
            c._calls = calls  # type: ignore[attr-defined]
            yield c
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text(
                "TRUNCATE tracxn_cache, leads, prospects, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def test_unconfigured_feed_refuses_with_the_remedy(reg: AsyncClient,
                                                         monkeypatch):
    monkeypatch.setattr(get_settings(), "tracxn_access_token", "")
    r = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r.status_code == 409
    assert "TRACXN_ACCESS_TOKEN" in r.json()["error"]["detail"]


async def test_feed_normalises_caches_and_audits(reg: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "tracxn_access_token", "test-token")

    r = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["resolved"] is True
    assert p["legal_entity"]["id"] == "le-123"
    # Series normalised to year/value points, unit carried "as reported".
    rev = p["series"]["revenue"]
    assert [x["year"] for x in rev["points"]] == [2023, 2024, 2025]
    assert rev["points"][-1]["value"] == 86.4
    assert rev["unit"] == "INR Cr"
    # An endpoint Tracxn answered empty degrades to empty points, not a 500.
    assert p["series"]["valuation"]["points"] == []
    # Board and cap table.
    assert p["board"][0] == {"name": "Rohan Mehta",
                             "designation": "Managing Director",
                             "since": "2015-04-01"}
    assert p["shareholders"][0]["name"] == "Promoter Group"
    assert p["shareholders"][0]["pct"] == 74.2
    first_fetch_calls = len(reg._calls)  # type: ignore[attr-defined]
    assert p["fetched_now"] == first_fetch_calls == len(tracxn_mod._ENDPOINTS)

    # Second read: served ENTIRELY from the larder — zero external calls.
    r2 = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r2.status_code == 200
    assert r2.json()["fetched_now"] == 0
    assert len(reg._calls) == first_fetch_calls  # type: ignore[attr-defined]

    # refresh=true spends again, deliberately.
    r3 = await reg.get(f"/v1/panorama/financials?cin={CIN}&refresh=true",
                       headers=ADMIN)
    assert r3.json()["fetched_now"] == len(tracxn_mod._ENDPOINTS)


async def test_no_cin_anywhere_says_where_to_add_it(reg: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "tracxn_access_token", "test-token")
    ent = await reg.post("/v1/entities", headers=ADMIN, json={
        "code": "NOCIN", "legal_name": "No Cin Yet Pvt Ltd"})
    assert ent.status_code == 201
    r = await reg.get(f"/v1/panorama/financials?entity_id={ent.json()['id']}",
                      headers=ADMIN)
    assert r.status_code == 409
    assert "No CIN on record" in r.json()["error"]["detail"]


async def test_placeholder_cin_is_ignored_and_a_lead_cin_is_used(reg: AsyncClient,
                                                                   monkeypatch):
    monkeypatch.setattr(get_settings(), "tracxn_access_token", "test-token")
    # The placeholder older UI builds stamped: stem + the code's own six digits.
    ent = await reg.post("/v1/entities", headers=ADMIN, json={
        "code": "GENESIS-482913", "legal_name": "Genesis Poweronics Pvt Ltd",
        "cin": "U40106KA2015PTC482913"})
    assert ent.status_code == 201, ent.text
    eid = ent.json()["id"]

    # Placeholder only → treated as NO CIN; nothing is sent to Tracxn.
    r = await reg.get(f"/v1/panorama/financials?entity_id={eid}", headers=ADMIN)
    assert r.status_code == 409
    assert reg._calls == []  # type: ignore[attr-defined]
    pano = (await reg.get(f"/v1/panorama?entity_id={eid}", headers=ADMIN)).json()
    assert pano["anchor"]["cin"] is None

    # The desk types the real CIN on a lead of this (already existing) master.
    lead = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Genesis Poweronics Pvt Ltd", "source": "BDRM",
        "entity_id": eid})
    assert lead.status_code == 201, lead.text
    up = await reg.patch(f"/v1/leads/{lead.json()['id']}", headers=ADMIN,
                         json={"cin": CIN})
    assert up.status_code == 200, up.text

    r2 = await reg.get(f"/v1/panorama/financials?entity_id={eid}", headers=ADMIN)
    assert r2.status_code == 200, r2.text
    assert r2.json()["cin"] == CIN and r2.json()["resolved"] is True
    pano2 = (await reg.get(f"/v1/panorama?entity_id={eid}", headers=ADMIN)).json()
    assert pano2["anchor"]["cin"] == CIN


def test_a_genuine_cin_with_the_same_stem_is_kept():
    class E:  # noqa: D401 — a stand-in master
        code = "SUNRISE-7B54"
        cin = "U40106KA2015PTC482913"
    assert tracxn_mod.real_cin(E()) == "U40106KA2015PTC482913"
    E.code = "SUNRISE-111111"
    assert tracxn_mod.real_cin(E()) == "U40106KA2015PTC482913"
    E.code = "SUNRISE-482913"
    assert tracxn_mod.real_cin(E()) is None


async def test_an_unreachable_tracxn_is_an_answer_not_a_500_and_is_never_cached(
        reg: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "tracxn_access_token", "test-token")
    calls = reg._calls  # type: ignore[attr-defined]

    async def dead(settings, path, body):  # noqa: ANN001
        calls.append(path)
        return {"_error": "unreachable (ConnectTimeout)"}

    monkeypatch.setattr(tracxn_mod, "_tracxn_post", dead)
    r = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is False
    assert "could not be reached" in r.json()["note"]
    assert "ConnectTimeout" in r.json()["note"]
    n = len(calls)
    # Not cached: the next open asks again.
    r2 = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r2.json()["resolved"] is False
    assert len(calls) == n + 1


async def test_the_fan_out_runs_concurrently(reg: AsyncClient, monkeypatch):
    import asyncio as _aio

    monkeypatch.setattr(get_settings(), "tracxn_access_token", "test-token")
    inflight = {"now": 0, "peak": 0}

    async def slow(settings, path, body):  # noqa: ANN001
        inflight["now"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["now"])
        await _aio.sleep(0.02)
        inflight["now"] -= 1
        return CANNED.get(path, {"result": []})

    monkeypatch.setattr(tracxn_mod, "_tracxn_post", slow)
    r = await reg.get(f"/v1/panorama/financials?cin={CIN}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True
    assert inflight["peak"] > 1
