"""The Chitti chatbot's read-only machine lane.

Chitti runs on its own VM and answers questions from PRISM data through
/machine/v1/internal/chitti/* with a named service key. The lane's contract:
only the svc_chitti principal gets in, everything is GET-only, tenant-scoped,
and shaped for display — search, one company's snapshot (profile, leads,
deals, lane statuses, lenders with last chase/reply, recent interactions),
and a company's interaction feed.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.models.deals import Deal, Lead
from app.models.interactions import Interaction
from app.models.registry import Entity
from app.models.system import Tenant
from app.models.trackers import (LendingTracker, SyndicationLender,
                                 SyndicationTracker)
from app.seed.loader import ensure_tenant


async def _seed(session) -> None:
    tid = (await session.execute(
        select(Tenant.id).where(Tenant.code == "EVAM"))).scalar_one()
    ent = Entity(tenant_id=tid, code="SUNGARNER", legal_name="SunGarner Energies Ltd.",
                 display_name="SunGarner Energies Ltd.", sector="Renewables",
                 sub_sector="Solar-EPC", lens="Mitigation")
    session.add(ent)
    await session.flush()
    session.add(Lead(tenant_id=tid, entity_id=ent.id, lead_no="LD-364",
                     company="SunGarner Energies Ltd.", status="Active",
                     temperature="Warm", rm="Chetan Malik",
                     next_action="Collect pending documents"))
    deal = Deal(tenant_id=tid, entity_id=ent.id, deal_no="DL-101",
                code="SUNGARNER", product_type="Syndication", stage="Mandated",
                is_syndication=True)
    session.add(deal)
    session.add(LendingTracker(tenant_id=tid, entity_id=ent.id, stage="Discussion",
                               remarks="Working capital requirement stated as ₹5 Cr."))
    syn = SyndicationTracker(tenant_id=tid, entity_id=ent.id, status="Active",
                             facility="₹5 Cr working capital",
                             remarks="₹5 Cr syndication mandate. Credit call done.")
    session.add(syn)
    await session.flush()
    session.add(SyndicationLender(tenant_id=tid, syndication_id=syn.id,
                                  lender_name="Canara Bank", status="Chased",
                                  last_chase_note="Chased; will revert by Friday."))
    session.add(Interaction(tenant_id=tid, subject_type="deal", subject_id=deal.id,
                            entity_id=ent.id, interaction_type="Call",
                            direction="Outbound", summary="Chased Canara Bank",
                            performed_by="chetan", lender_name="Canara Bank"))
    await session.commit()


@pytest_asyncio.fixture
async def chitti_client(monkeypatch) -> AsyncIterator[AsyncClient]:
    clear_tenant_cache()
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys",
                        {"chitti-key": "svc_chitti", "wf-key": "svc_workflows"})
    init_engine(s)
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()
        await _seed(session)
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": "chitti-key",
                                        "X-Tenant": "EVAM"}) as c:
            yield c
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text(
                "TRUNCATE interactions, syndication_lenders, syndication_tracker, "
                "lending_tracker, asset_monetisation, deals, leads, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def test_only_the_chitti_principal_gets_in(chitti_client):
    """A different named service key — or none — is refused; the lane is
    GET-only, so even the right principal cannot write through it."""
    for path in ("/v1/internal/chitti/search?q=sun",
                 "/v1/internal/chitti/company/SUNGARNER",
                 "/v1/internal/chitti/interactions?company=SUNGARNER"):
        wrong = await chitti_client.get(path, headers={"X-API-Key": "wf-key"})
        assert wrong.status_code == 403, path
    assert (await chitti_client.post("/v1/internal/chitti/search?q=sun",
                                     json={})).status_code == 405


async def test_search_finds_companies_leads_and_deals(chitti_client):
    r = await chitti_client.get("/v1/internal/chitti/search", params={"q": "sungarner"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["companies"][0]["code"] == "SUNGARNER"
    assert got["companies"][0]["sub_sector"] == "Solar-EPC"
    r2 = await chitti_client.get("/v1/internal/chitti/search", params={"q": "LD-364"})
    assert r2.json()["leads"][0]["rm"] == "Chetan Malik"
    r3 = await chitti_client.get("/v1/internal/chitti/search", params={"q": "DL-101"})
    assert r3.json()["deals"][0]["stage"] == "Mandated"


async def test_company_snapshot_carries_the_whole_story(chitti_client):
    r = await chitti_client.get("/v1/internal/chitti/company/SUNGARNER")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["company"]["name"] == "SunGarner Energies Ltd."
    assert got["leads"][0]["lead_no"] == "LD-364"
    assert got["deals"][0]["lanes"] == ["syndication"]
    assert "₹5 Cr" in got["lending"][0]["remarks"]
    syn = got["syndication"][0]
    assert syn["lenders"][0]["lender"] == "Canara Bank"
    assert "revert by Friday" in syn["lenders"][0]["last_chase_note"]
    assert got["recent_interactions"][0]["summary"] == "Chased Canara Bank"
    # an unknown code is a clean 404, never an empty shell
    assert (await chitti_client.get(
        "/v1/internal/chitti/company/NOPE")).status_code == 404


async def test_chitti_reads_the_whole_book_but_writes_nothing(chitti_client):
    """The bot's brief is "ask anything about any data": svc_chitti holds the
    widest own-key READ grant across the CRUD surface (proven live: /v1/entities
    was 403 "may not read ... on its own key" before the grant). Writes stay
    dead at BOTH walls — the edge window is GET-only, and even calling the
    service directly (as this test does, beneath nginx) the service has no
    write grant."""
    for path in ("/v1/entities", "/v1/leads", "/v1/deals", "/v1/interactions",
                 "/v1/syndication", "/v1/lending", "/v1/asset-monetisation",
                 "/v1/financials"):
        r = await chitti_client.get(path, params={"limit": 5})
        assert r.status_code == 200, (path, r.text)
    assert (await chitti_client.get(
        "/v1/entities", params={"limit": 2})).json()  # body parses
    # a write on the bot's own key is refused by the authz engine itself
    w = await chitti_client.post("/v1/entities",
                                 json={"code": "EVIL", "legal_name": "Evil Co"})
    assert w.status_code == 403, w.text


async def test_interaction_feed_is_scoped_and_capped(chitti_client):
    r = await chitti_client.get("/v1/internal/chitti/interactions",
                                params={"company": "SUNGARNER", "limit": 1})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["company"] == "SUNGARNER"
    assert len(got["interactions"]) == 1
    assert got["interactions"][0]["lender"] == "Canara Bank"
