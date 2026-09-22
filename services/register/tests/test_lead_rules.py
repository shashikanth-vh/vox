"""The client master follows the lead — link-or-create at lead creation.

The desk's rule: a company enters the master the moment it enters the book,
not only when its lead is pushed to deals. POST /v1/leads settles the link
with the shared canonical matching (evam_backend_core.company_identity):
an existing client is LINKED (and never edited — masters outrank a lead's
free text), a genuinely new company is born as a Prospect master row, and an
explicitly supplied entity_id is always respected.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.models.registry import Entity
from app.models.system import Tenant
from app.seed.loader import ensure_tenant


async def _seed(session) -> str:
    tid = (await session.execute(
        select(Tenant.id).where(Tenant.code == "EVAM"))).scalar_one()
    ent = Entity(tenant_id=tid, code="GREENPILLREN", sector="Renewables",
                 legal_name="Greenpill Renewable Energy Limited",
                 display_name="Greenpill Renewable Energy Limited")
    session.add(ent)
    await session.commit()
    return str(ent.id)


@pytest_asyncio.fixture
async def reg() -> AsyncIterator[tuple[AsyncClient, str]]:
    clear_tenant_cache()
    s = get_settings()
    init_engine(s)
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()
        existing_id = await _seed(session)
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": get_settings().all_api_keys()[0],
                                        "X-Tenant": "EVAM"}) as c:
            yield c, existing_id
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text("TRUNCATE leads, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def _masters(client: AsyncClient) -> list[dict]:
    r = await client.get("/v1/entities", params={"limit": 100})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_a_new_company_lead_births_a_prospect_master_row(reg):
    client, _ = reg
    r = await client.post("/v1/leads", json={"company": "Unique Sun Power",
                                             "sector": "Renewables", "rm": "Shubh Dave"})
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["entity_id"], "the lead must be born linked to its client master"
    masters = {m["legal_name"]: m for m in await _masters(client)}
    born = masters["Unique Sun Power"]
    assert str(born["id"]) == str(lead["entity_id"])
    assert born["lifecycle"] == "Prospect"
    assert born["register_status"] == "Pipeline"
    assert born["sector"] == "Renewables"
    assert born["code"].startswith("UNIQUESUNPOW")


async def test_a_known_company_links_and_the_master_is_never_edited(reg):
    """Suffix/case/punctuation variants of an existing client LINK to it —
    the exact canonical rule the conversion pre-flight and VOX use — and the
    master keeps its own facts (the lead's sector does not overwrite it)."""
    client, existing_id = reg
    r = await client.post("/v1/leads", json={
        "company": "greenpill renewable energy pvt. ltd", "sector": "Biofuels"})
    assert r.status_code == 201, r.text
    assert str(r.json()["entity_id"]) == existing_id
    masters = await _masters(client)
    assert len([m for m in masters if "Greenpill" in m["legal_name"]]) == 1, \
        "no duplicate master row for a name variant"
    kept = next(m for m in masters if str(m["id"]) == existing_id)
    assert kept["sector"] == "Renewables", "the master outranks the lead's free text"


async def test_an_explicit_entity_id_is_respected(reg):
    """The Add-lead dialog's Attach row (and VOX's pre-linked leads) name the
    client outright — the hook must not second-guess them by name."""
    client, existing_id = reg
    r = await client.post("/v1/leads", json={"company": "A Different Trading Name",
                                             "entity_id": existing_id})
    assert r.status_code == 201, r.text
    assert str(r.json()["entity_id"]) == existing_id
    assert not any(m["legal_name"] == "A Different Trading Name"
                   for m in await _masters(client))


async def test_same_named_siblings_are_never_guessed_between(reg):
    """Two live masters with the same canonical name (the GREENPILLREN wound):
    linking to either would be a guess, so the lead stays unlinked and a human
    resolves it — the Attach row or the conversion dialog."""
    client, existing_id = reg
    r = await client.post("/v1/entities", json={
        "code": "GREENPILL2", "legal_name": "Greenpill Renewable Energy Limited"})
    assert r.status_code == 201, r.text
    lead = await client.post("/v1/leads", json={"company": "Greenpill Renewable Energy"})
    assert lead.status_code == 201
    assert lead.json()["entity_id"] is None
    assert len([m for m in await _masters(client)
                if "Greenpill" in m["legal_name"]]) == 2, "and no third row was minted"


async def test_two_leads_for_the_same_new_company_share_one_master(reg):
    client, _ = reg
    first = await client.post("/v1/leads", json={"company": "Amit Solar Pvt Ltd"})
    second = await client.post("/v1/leads", json={"company": "AMIT SOLAR"})
    assert first.status_code == 201 and second.status_code == 201
    assert str(first.json()["entity_id"]) == str(second.json()["entity_id"])
    assert len([m for m in await _masters(client)
                if "amit" in m["legal_name"].lower()]) == 1
