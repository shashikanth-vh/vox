"""Company 360 — the panorama endpoint: one company's whole story in one read.

The fixture builds one company end to end — client master, an open lead and a
converted one, a lending line at a real ladder stage, an interaction, a Data
Register document, a prospect-universe row — and asserts the panorama tells that
story: correct anchor, correct stats (OPEN leads, not lifetime), per-section
RBAC with restricted sections NAMED, and the name-only fallback for a company
with no client master."""
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
RM = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM"}


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
            await session.execute(text(
                "TRUNCATE interactions, documents, lending_tracker, leads, deals, "
                "prospects, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def _build_company(reg: AsyncClient) -> str:
    ent = await reg.post("/v1/entities", headers=ADMIN, json={
        "code": "SDL", "legal_name": "Siddapur Distilleries Limited",
        "sector": "Bioenergy", "state": "Karnataka"})
    assert ent.status_code == 201, ent.text
    eid = ent.json()["id"]

    open_lead = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Siddapur Distilleries Limited", "entity_id": eid,
        "sector": "Bioenergy", "source": "BDRM", "rm": "Pallavi Patel",
        "temperature": "Cold", "contact": "Jagadeesh Gudagunti",
        "designation": "Director", "phone": "9611409090",
        "next_action": "CBG capex discussion after harvest"})
    assert open_lead.status_code == 201, open_lead.text

    done_lead = await reg.post("/v1/leads", headers=ADMIN, json={
        "company": "Siddapur Distilleries Limited", "entity_id": eid,
        "sector": "Bioenergy", "source": "BDRM", "rm": "Pallavi Patel"})
    assert done_lead.status_code == 201
    deal = await reg.post("/v1/deals", headers=ADMIN, json={
        "entity_id": eid, "product_type": "Lending"})
    assert deal.status_code == 201, deal.text
    patched = await reg.patch(f"/v1/leads/{done_lead.json()['id']}", headers=ADMIN,
                              json={"converted_deal_id": deal.json()["id"]})
    assert patched.status_code == 200, patched.text

    lend = await reg.post("/v1/lending", headers=ADMIN, json={
        "entity_id": eid, "stage": "Diligence", "amount_cr": 12.0,
        "pending_with": "Credit", "rm": "Pallavi Patel"})
    assert lend.status_code == 201, lend.text
    # A line is BORN in a working state; Note Circulated is reached by transition.
    moved = await reg.patch(f"/v1/lending/{lend.json()['id']}", headers=ADMIN,
                            json={"stage": "Note Circulated"})
    assert moved.status_code == 200, moved.text

    # A DEAD line: neither a live deal nor exposure, and the brief says what
    # happened to it instead of calling it an ask (the staging screenshot bug).
    dead = await reg.post("/v1/lending", headers=ADMIN, json={
        "entity_id": eid, "stage": "Diligence", "amount_cr": 2.0})
    assert dead.status_code == 201, dead.text
    killed = await reg.patch(f"/v1/lending/{dead.json()['id']}", headers=ADMIN,
                             json={"stage": "Rejected"})
    assert killed.status_code == 200, killed.text

    # A PAUSED syndication mandate: named, its amount shown as on hold — never
    # counted into the ask (the Zeon screenshot bug: ₹25 Cr On Hold summed as ask).
    syn = await reg.post("/v1/syndication", headers=ADMIN, json={
        "entity_id": eid, "status": "Deal Sourced", "amount_cr": 25.0})
    assert syn.status_code == 201, syn.text
    held = await reg.patch(f"/v1/syndication/{syn.json()['id']}", headers=ADMIN,
                           json={"status": "On Hold"})
    assert held.status_code == 200, held.text

    inter = await reg.post("/v1/interactions", headers=ADMIN, json={
        "subject_type": "Entity", "subject_id": eid,
        "interaction_type": "Site Visit",
        "summary": "40 KLPD ethanol; CBG expansion discussed",
        "performed_by": "Pallavi Patel", "contact_name": "Jagadeesh Gudagunti"})
    assert inter.status_code in (200, 201), inter.text

    doc = await reg.post("/v1/documents", headers=ADMIN, json={
        "subject_type": "Entity", "subject_id": eid,
        "title": "KYC pack", "section": "KYC"})
    assert doc.status_code in (200, 201), doc.text

    pr = await reg.post("/v1/prospects", headers=ADMIN, json={
        "name": "Siddapur Distilleries Limited", "verticals": ["Bioenergy"],
        "revenue_cr": 86.4, "remarks": "interested"})
    assert pr.status_code == 201, pr.text
    # NOT bound to the master: the panorama finds it by canonical name, the
    # same identity the import and the lead birth share.
    return eid


async def test_panorama_tells_the_whole_story(reg: AsyncClient):
    eid = await _build_company(reg)

    r = await reg.get(f"/v1/panorama?entity_id={eid}", headers=ADMIN)
    assert r.status_code == 200, r.text
    p = r.json()

    assert p["anchor"]["matched_by"] == "entity"
    assert p["anchor"]["name"] == "Siddapur Distilleries Limited"
    # OPEN leads, not lifetime — the converted one is history, not an open lead.
    assert p["stats"]["open_leads"] == 1
    assert p["stats"]["leads_converted"] == 1
    # Buckets, not a blur: the rejected line vanishes; the on-hold mandate is
    # counted but NAMED as on hold, and its ₹25 Cr never joins the ask.
    assert p["stats"]["live_deals"] == 2
    assert p["stats"]["deals_in_flight"] == 1
    assert p["stats"]["deals_on_hold"] == 1
    assert p["stats"]["exposure_ask_cr"] == 12.0
    assert p["stats"]["on_hold_cr"] == 25.0
    assert "was rejected" in p["brief"]
    assert "at Rejected" not in p["brief"]
    assert "on hold" in p["brief"]
    assert p["stats"]["documents"] == 1

    lend = p["lending"][0]
    assert lend["stage"] == "Note Circulated"
    assert lend["pending_with"] == "Credit"
    assert "Note Circulated" in lend["ladder"] and lend["ladder"][0] == "Data Awaited"

    assert p["interactions"][0]["summary"].startswith("40 KLPD")
    assert p["documents"][0]["title"] == "KYC pack"
    assert p["prospect"]["revenue_cr"] == 86.4
    # The contact came off the lead; the brief is composed from the same rows.
    assert p["contacts"][0]["name"] == "Jagadeesh Gudagunti"
    assert "Note Circulated" in p["brief"]
    assert "pending with Credit" in p["brief"]
    assert p["restricted"] == []


async def test_panorama_rbac_names_what_it_hides(reg: AsyncClient):
    eid = await _build_company(reg)
    r = await reg.get(f"/v1/panorama?entity_id={eid}", headers=RM)
    assert r.status_code == 200, r.text
    p = r.json()
    # A BDRM's lending view is SCOPED — this company is not in their book, so
    # the section is empty but NOT listed as restricted (they hold the view)…
    assert p["lending"] == []
    assert "lending" not in p["restricted"]
    # …and nothing they cannot see leaks through the stats.
    assert p["stats"]["live_deals"] == 0


async def test_panorama_by_name_without_a_master(reg: AsyncClient):
    r = await reg.post("/v1/prospects", headers=ADMIN, json={
        "name": "Green Hydrogen Ventures Pvt Ltd", "verticals": ["Solar"]})
    assert r.status_code == 201
    p = (await reg.get("/v1/panorama?company=Green Hydrogen Ventures",
                       headers=ADMIN)).json()
    # No entity anywhere — the panorama still answers, anchored on the name,
    # and says so instead of pretending.
    assert p["anchor"]["entity_id"] is None
    assert p["anchor"]["matched_by"] == "name-only"
    assert "no client master yet" in p["brief"]
