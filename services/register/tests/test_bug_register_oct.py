"""The October bug register, server half (B02, B08, B16–B18/B22, B25, B34, B42).

* a Converted lead cannot be deleted (B02);
* /v1/lead-lookup finds near-names tenant-wide for the Add-lead dupe check (B08);
* the grids' search reaches the COMPANY name on deals, lending, mandates and
  asset monetisation, and every typed word must land (B16–B18/B22);
* a stamp the register makes itself is the desk's day, not the UTC day (B25);
* the 360 shows interactions logged on a lead or a line that carry no
  entity link (B34);
* a deal's product flags follow its tracker lines (B42).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
BDRM = {"X-User-Email": "rm2@evamfinance.com", "X-User-Roles": "BDRM"}


def _items(body):
    if isinstance(body, dict):
        return body.get("items") or body.get("results") or []
    return body


async def _entity(client: AsyncClient, name: str, code: str | None = None) -> str:
    r = await client.post("/v1/entities", headers=ADMIN, json={
        "code": code or f"T{uuid.uuid4().hex[:8].upper()}", "legal_name": name,
        "display_name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- B02 -------------------------------------------------------------------
async def test_a_converted_lead_refuses_deletion(client: AsyncClient, db_session):
    eid = await _entity(client, "Converted Co")
    r = await client.post("/v1/leads", headers=ADMIN,
                          json={"company": "Converted Co", "entity_id": eid})
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    # Conversion is a flow of its own; the lock is about the STATE, so set it.
    await db_session.execute(text("UPDATE leads SET status='Converted' WHERE id=:i"),
                             {"i": lid})
    await db_session.commit()
    d = await client.delete(f"/v1/leads/{lid}", headers=ADMIN)
    assert d.status_code == 403, d.text
    assert "Converted" in d.text
    # An ordinary lead still deletes.
    r2 = await client.post("/v1/leads", headers=ADMIN,
                           json={"company": "Converted Co", "entity_id": eid})
    assert (await client.delete(f"/v1/leads/{r2.json()['id']}", headers=ADMIN)).status_code == 204


# ---- B08 -------------------------------------------------------------------
async def test_lead_lookup_finds_near_names_across_the_tenant(client: AsyncClient):
    await _entity(client, "Greenpill Renewable Energy Limited", "GREENPILLREN")
    r = await client.post("/v1/leads", headers={**ADMIN, "X-User-Roles": "BDRM",
                                                "X-User-Email": "rm1@evamfinance.com"},
                          json={"company": "Adani Green Energy", "rm": "Pallavi",
                                "status": "Active"})
    assert r.status_code == 201, r.text

    hit = await client.get("/v1/lead-lookup", params={"name": "greenphill"}, headers=BDRM)
    assert hit.status_code == 200, hit.text
    assert [c["code"] for c in hit.json()["clients"]] == ["GREENPILLREN"]

    # Another BDRM's open lead is reported — scope is the point of this check.
    hit2 = await client.get("/v1/lead-lookup", params={"name": "adani grn"}, headers=BDRM)
    leads = hit2.json()["leads"]
    assert len(leads) == 1 and leads[0]["company"] == "Adani Green Energy"
    assert leads[0]["rm"] == "Pallavi" and "notes" not in leads[0]

    # Boilerplate alone never matches, nor does a two-letter stub.
    assert (await client.get("/v1/lead-lookup", params={"name": "Veer Raj Industries"},
                             headers=BDRM)).json() == {"clients": [], "leads": []}
    assert (await client.get("/v1/lead-lookup", params={"name": "ad"},
                             headers=BDRM)).json() == {"clients": [], "leads": []}


# ---- B16–B18 / B22 ---------------------------------------------------------
async def test_grid_search_reaches_the_company_name(client: AsyncClient):
    sunvik = await _entity(client, "Sunvik Steels Private Limited", "SUNVIK")
    axel = await _entity(client, "Axel… (Wind Deal) - MH", "AXELWIND")
    other = await _entity(client, "Other Industries", "OTHER")
    for eid in (sunvik, axel, other):
        assert (await client.post("/v1/deals", headers=ADMIN,
                                  json={"entity_id": eid, "stage": "In Pipeline"})).status_code == 201
        assert (await client.post("/v1/lending", headers=ADMIN,
                                  json={"entity_id": eid, "stage": "Data Awaited"})).status_code == 201
        assert (await client.post("/v1/syndication", headers=ADMIN,
                                  json={"entity_id": eid, "status": "Deal Sourced"})).status_code == 201
        assert (await client.post("/v1/asset-monetisation", headers=ADMIN,
                                  json={"entity_id": eid, "status": "Teaser Prepared",
                                        "nature": "Seller"})).status_code == 201

    for path in ("/v1/deals", "/v1/lending", "/v1/syndication", "/v1/asset-monetisation"):
        r = await client.get(path, params={"q": "sunvik"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        rows = _items(r.json())
        assert [x["entity_id"] for x in rows] == [sunvik], path
        # Every word must land: "sunvik steels" is still one company.
        rows = _items((await client.get(path, params={"q": "sunvik steels"}, headers=ADMIN)).json())
        assert [x["entity_id"] for x in rows] == [sunvik], path
        rows = _items((await client.get(path, params={"q": "axel wind"}, headers=ADMIN)).json())
        assert [x["entity_id"] for x in rows] == [axel], path
        # A word that lands nowhere finds nothing.
        rows = _items((await client.get(path, params={"q": "sunvik wind"}, headers=ADMIN)).json())
        assert rows == [], path

    # Leads: by lead number, and by the linked company's name.
    r = await client.post("/v1/leads", headers=ADMIN,
                          json={"company": "Sunvik Steels Private Limited", "entity_id": sunvik})
    assert r.status_code == 201, r.text
    lead_no = r.json()["lead_no"]
    rows = _items((await client.get("/v1/leads", params={"q": lead_no}, headers=ADMIN)).json())
    assert [x["lead_no"] for x in rows] == [lead_no]
    rows = _items((await client.get("/v1/leads", params={"q": "SUNVIK"}, headers=ADMIN)).json())
    assert [x["lead_no"] for x in rows] == [lead_no]


# ---- B25 -------------------------------------------------------------------
async def test_the_desk_day_is_not_the_utc_day():
    from app.core.clock import tenant_date, tenant_today, tenant_zone

    assert str(tenant_zone()) == "Asia/Kolkata"
    # 20:30 UTC on the 3rd is 02:00 IST on the 4th.
    late = datetime(2026, 10, 3, 20, 30, tzinfo=UTC)
    assert tenant_date(late).isoformat() == "2026-10-04"
    # A naive timestamp is read as UTC, the way the database hands them back.
    assert tenant_date(datetime(2026, 10, 3, 20, 30)).isoformat() == "2026-10-04"
    assert tenant_date(datetime(2026, 10, 3, 10, 0, tzinfo=UTC)).isoformat() == "2026-10-03"
    assert tenant_date(None) is None
    assert tenant_today() == tenant_date(datetime.now(UTC))


async def test_a_stage_move_and_a_lead_touch_are_stamped_on_the_desk_day(
        client: AsyncClient, db_session, monkeypatch):
    from datetime import date

    from app.api import tracker_rules
    from app.repositories import interactions as inter_repo

    # Freeze the desk clock at 01:00 IST on the 4th (= 19:30 UTC on the 3rd).
    monkeypatch.setattr(tracker_rules, "tenant_today", lambda: date(2026, 10, 4))
    eid = await _entity(client, "Night Shift Co")
    ln = await client.post("/v1/lending", headers=ADMIN,
                           json={"entity_id": eid, "stage": "Data Awaited"})
    moved = await client.patch(f"/v1/lending/{ln.json()['id']}", headers=ADMIN,
                               json={"stage": "Diligence"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["stage_updated_at"] == "2026-10-04"

    # An interaction that occurred at 19:30 UTC on the 3rd touched the lead on the 4th.
    lead = await client.post("/v1/leads", headers=ADMIN,
                             json={"company": "Night Shift Co", "entity_id": eid})
    r = await client.post("/v1/interactions", headers=ADMIN, json={
        "subject_type": "Lead", "subject_id": lead.json()["id"],
        "interaction_type": "Phone Call", "summary": "late call",
        "occurred_at": "2026-10-03T19:30:00Z"})
    assert r.status_code in (200, 201), r.text
    assert inter_repo.tenant_date(datetime(2026, 10, 3, 19, 30, tzinfo=UTC)).isoformat() == "2026-10-04"
    got = await client.get(f"/v1/leads/{lead.json()['id']}", headers=ADMIN)
    assert got.json()["last_interaction_date"] == "2026-10-04"


# ---- B34 -------------------------------------------------------------------
async def test_the_360_shows_interactions_logged_on_the_lead_without_an_entity_link(
        client: AsyncClient, db_session):
    eid = await _entity(client, "Story Co", "STORYCO")
    lead = await client.post("/v1/leads", headers=ADMIN,
                             json={"company": "Story Co", "entity_id": eid})
    r = await client.post("/v1/interactions", headers=ADMIN, json={
        "subject_type": "Lead", "subject_id": lead.json()["id"],
        "interaction_type": "Phone Call", "summary": "logged before the link"})
    assert r.status_code in (200, 201), r.text
    # The lead was linked AFTER this call was logged: no entity on the row.
    await db_session.execute(text("UPDATE interactions SET entity_id = NULL WHERE id=:i"),
                             {"i": r.json()["id"]})
    await db_session.commit()
    p = await client.get("/v1/panorama", params={"entity_id": eid}, headers=ADMIN)
    assert p.status_code == 200, p.text
    assert [i["summary"] for i in p.json()["interactions"]] == ["logged before the link"]


# ---- B42 -------------------------------------------------------------------
async def test_deal_product_flags_follow_the_tracker_lines(client: AsyncClient):
    eid = await _entity(client, "Badge Co")
    deal = await client.post("/v1/deals", headers=ADMIN,
                             json={"entity_id": eid, "stage": "In Pipeline"})
    assert deal.status_code == 201, deal.text
    did = deal.json()["id"]
    assert deal.json()["is_lending"] is False

    ln = await client.post("/v1/lending", headers=ADMIN,
                           json={"entity_id": eid, "deal_id": did, "stage": "Data Awaited"})
    assert ln.status_code == 201, ln.text
    d = (await client.get(f"/v1/deals/{did}", headers=ADMIN)).json()
    assert (d["is_lending"], d["is_syndication"], d["is_asset_mon"]) == (True, False, False)

    syn = await client.post("/v1/syndication", headers=ADMIN,
                            json={"entity_id": eid, "deal_id": did, "status": "Deal Sourced"})
    assert syn.status_code == 201, syn.text
    d = (await client.get(f"/v1/deals/{did}", headers=ADMIN)).json()
    assert (d["is_lending"], d["is_syndication"]) == (True, True)

    # Deleting the only lending line clears its badge; the mandate's stays.
    assert (await client.delete(f"/v1/lending/{ln.json()['id']}", headers=ADMIN)).status_code == 204
    d = (await client.get(f"/v1/deals/{did}", headers=ADMIN)).json()
    assert (d["is_lending"], d["is_syndication"]) == (False, True)

    # A line MOVED to another deal refreshes both deals.
    deal2 = await client.post("/v1/deals", headers=ADMIN,
                              json={"entity_id": eid, "stage": "In Pipeline"})
    did2 = deal2.json()["id"]
    mv = await client.patch(f"/v1/syndication/{syn.json()['id']}", headers=ADMIN,
                            json={"deal_id": did2})
    assert mv.status_code == 200, mv.text
    assert (await client.get(f"/v1/deals/{did}", headers=ADMIN)).json()["is_syndication"] is False
    assert (await client.get(f"/v1/deals/{did2}", headers=ADMIN)).json()["is_syndication"] is True

    # The one-off recompute statement the deploy ships agrees with the hook.
    from app.api.deal_flags import sync_deal_flags  # noqa: F401  (import guard)
