"""Optional company identity on leads, and how it travels at birth."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.seed.loader import ensure_tenant

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}


@pytest_asyncio.fixture
async def reg() -> AsyncIterator[AsyncClient]:
    init_engine()
    clear_tenant_cache()
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": get_settings().all_api_keys()[0],
                                        "X-Tenant": "EVAM"}) as c:
            yield c
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text("TRUNCATE leads, prospects, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def test_identity_is_optional_updates_later_and_seeds_the_newborn_master(
        reg: AsyncClient):
    # Born with nothing but a name — every identity field optional.
    r = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Sunrise Green Fuels Pvt Ltd", "source": "BDRM"})
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["cin"] is None and lead["city"] is None

    # Filled in as and when the desk learns it.
    upd = await reg.patch(f"/v1/leads/{lead['id']}", headers=ADMIN, json={
        "cin": "U40100KA2020PTC111111", "city": "Hubli", "state": "KA",
        "country": "India", "address": "Plot 12, Industrial Area, Hubli"})
    assert upd.status_code == 200, upd.text
    got = upd.json()
    assert got["cin"] == "U40100KA2020PTC111111"
    assert got["address"].startswith("Plot 12")

    # A lead born ALREADY KNOWING its identity seeds the newborn master —
    # the birth-linking copies what the desk knew, once, at creation.
    r2 = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Moonrise Hydro Private Limited", "source": "BDRM",
        "cin": "U40100MH2019PTC222222", "city": "Nashik", "state": "MH",
        "country": "India"})
    assert r2.status_code == 201, r2.text
    eid = r2.json()["entity_id"]
    assert eid
    ent = (await reg.get(f"/v1/entities/{eid}", headers=ADMIN)).json()
    assert ent["cin"] == "U40100MH2019PTC222222"
    assert ent["city"] == "Nashik" and ent["country"] == "India"

    # …but an EXISTING master is never overwritten by a later lead's fields.
    r3 = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Moonrise Hydro Private Limited", "source": "BDRM",
        "cin": "WRONG-CIN", "city": "Elsewhere"})
    assert r3.status_code == 201
    assert r3.json()["entity_id"] == eid
    ent2 = (await reg.get(f"/v1/entities/{eid}", headers=ADMIN)).json()
    assert ent2["cin"] == "U40100MH2019PTC222222"
    assert ent2["city"] == "Nashik"


async def test_prospect_promotion_seeds_registrar_identity(reg: AsyncClient):
    pr = await reg.post("/v1/prospects", headers=ADMIN, json={
        "name": "Starlight ESS Private Limited", "cin": "U31900KA2021PTC333333",
        "city": "Mysuru", "state": "Karnataka", "country": "India",
        "verticals": ["ESS"]})
    assert pr.status_code == 201, pr.text
    made = await reg.post(f"/v1/prospects/{pr.json()['id']}/create-lead",
                          headers=ADMIN, json={"rm": "Pallavi Patel"})
    assert made.status_code in (200, 201), made.text
    eid = made.json()["entity_id"]
    ent = (await reg.get(f"/v1/entities/{eid}", headers=ADMIN)).json()
    assert ent["cin"] == "U31900KA2021PTC333333"
    assert ent["city"] == "Mysuru" and ent["country"] == "India"
