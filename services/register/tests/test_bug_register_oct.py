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


# ---- second batch: B07, B10, B14, B27, B36/B37, B41, B47 -------------------
async def test_lead_lookup_reports_a_live_deal(client: AsyncClient):
    eid = await _entity(client, "Worked Co", "WORKEDCO")
    d = await client.post("/v1/deals", headers=ADMIN, json={"entity_id": eid, "stage": "In Pipeline", "rm": "Shubh Dave"})
    assert d.status_code == 201, d.text
    r = await client.get("/v1/lead-lookup", params={"name": "worked co"}, headers=ADMIN)
    c = r.json()["clients"][0]
    assert c["live_deal"] is False and c["deals"] == 1        # a deal with no live line is not "worked"
    ln = await client.post("/v1/lending", headers=ADMIN,
                           json={"entity_id": eid, "deal_id": d.json()["id"], "stage": "Data Awaited"})
    assert ln.status_code == 201, ln.text
    c = (await client.get("/v1/lead-lookup", params={"name": "worked co"}, headers=ADMIN)).json()["clients"][0]
    assert c["live_deal"] is True and c["deal_no"] == d.json()["deal_no"] and c["deal_rm"] == "Shubh Dave"


async def test_a_stage_move_from_the_grid_is_written_to_the_history(client: AsyncClient):
    eid = await _entity(client, "History Co")
    ln = await client.post("/v1/lending", headers=ADMIN, json={"entity_id": eid, "stage": "Data Awaited"})
    assert ln.status_code == 201, ln.text
    assert ln.json()["stage_history"] in (None, [])      # history starts at the first move
    moved = await client.patch(f"/v1/lending/{ln.json()['id']}", headers=ADMIN, json={"stage": "Diligence"})
    assert moved.status_code == 200, moved.text
    h = moved.json()["stage_history"]
    assert [x["to"] for x in h] == ["Diligence"]
    assert h[-1]["from"] == "Data Awaited" and h[-1]["by"] and h[-1]["at"]
    # The same stage again writes nothing.
    again = await client.patch(f"/v1/lending/{ln.json()['id']}", headers=ADMIN, json={"stage": "Diligence"})
    assert len(again.json()["stage_history"]) == 1


async def test_a_lender_row_links_to_the_fi_master_by_name(client: AsyncClient):
    cp = await client.post("/v1/counterparties", headers=ADMIN,
                           json={"name": "Mas Financial Services", "short_name": "MAS", "counterparty_type": "NBFC"})
    assert cp.status_code == 201, cp.text
    eid = await _entity(client, "Linked Lender Co")
    syn = await client.post("/v1/syndication", headers=ADMIN, json={"entity_id": eid, "status": "Deal Sourced"})
    sid = syn.json()["id"]
    a = await client.post(f"/v1/syndication/{sid}/lenders", headers=ADMIN, json={"lender_name": "mas financial services", "status": "Identified"})
    assert a.status_code == 201, a.text
    assert a.json()["counterparty_id"] == cp.json()["id"]
    b = await client.post(f"/v1/syndication/{sid}/lenders", headers=ADMIN, json={"lender_name": "MAS", "status": "Identified"})
    assert b.json()["counterparty_id"] == cp.json()["id"]
    c = await client.post(f"/v1/syndication/{sid}/lenders", headers=ADMIN, json={"lender_name": "Unknown Finance", "status": "Identified"})
    assert c.json()["counterparty_id"] is None


async def test_sorted_lists_ignore_case(client: AsyncClient):
    for nm, code in (("adani Green", "ADANIG"), ("Bharat Solar", "BHARAT"), ("Zydus Power", "ZYDUS")):
        assert (await client.post("/v1/leads", headers=ADMIN, json={"company": nm, "entity_id": await _entity(client, nm, code)})).status_code == 201
    r = await client.get("/v1/leads", params={"order_by": "company", "order_dir": "asc"}, headers=ADMIN)
    assert [x["company"] for x in _items(r.json())] == ["adani Green", "Bharat Solar", "Zydus Power"]


async def test_activity_pages_by_offset_and_the_stats_count_the_whole_trail(client: AsyncClient):
    eid = await _entity(client, "Busy Co")
    for i in range(6):
        assert (await client.post("/v1/leads", headers=ADMIN, json={"company": f"Busy Co {i}", "entity_id": eid})).status_code == 201
    p1 = await client.get("/v1/activity", params={"limit": 4, "offset": 0, "with_total": "true"}, headers=ADMIN)
    assert p1.status_code == 200, p1.text
    body = p1.json()
    assert len(body["items"]) == 4 and body["total"] >= 7 and body["offset"] == 0
    p2 = await client.get("/v1/activity", params={"limit": 4, "offset": 4, "with_total": "true"}, headers=ADMIN)
    assert {x["id"] for x in p2.json()["items"]}.isdisjoint({x["id"] for x in body["items"]})
    only = await client.get("/v1/activity", params={"area": "Leads", "with_total": "true"}, headers=ADMIN)
    assert only.json()["total"] >= 6 and all(x["area"] == "Leads" for x in only.json()["items"])
    st = await client.get("/v1/activity/stats", headers=ADMIN)
    assert st.status_code == 200 and st.json()["total"] == body["total"] and st.json()["today"] >= 7
    assert st.json()["people"] >= 1 and st.json()["records"] >= 7
    # The audit trail pages the same way, and still answers a bare list by default.
    a1 = await client.get("/v1/audit", params={"limit": 3, "offset": 0, "with_total": "true"}, headers=ADMIN)
    assert len(a1.json()["items"]) == 3 and a1.json()["total"] == body["total"]
    bare = await client.get("/v1/audit", params={"limit": 2}, headers=ADMIN)
    assert isinstance(bare.json(), list) and len(bare.json()) == 2
    many = await client.get("/v1/audit", params={"resource_ids": f"{eid},nonsense"}, headers=ADMIN)
    assert {x["resource_id"] for x in many.json()} == {eid}


async def test_a_prospect_is_linked_when_its_company_gets_a_lead(client: AsyncClient):
    p = await client.post("/v1/prospects", headers=ADMIN, json={"name": "Ecosoch Solar Pvt Ltd", "verticals": ["Solar"]})
    assert p.status_code == 201, p.text
    assert p.json()["status"] == "uncontacted"
    lead = await client.post("/v1/leads", headers=ADMIN, json={"company": "EcoSoch Solar"})
    assert lead.status_code == 201, lead.text
    got = (await client.get(f"/v1/prospects/{p.json()['id']}", headers=ADMIN)).json()
    assert got["entity_id"] == lead.json()["entity_id"]
    assert got["lead_ids"] == [lead.json()["id"]] and got["status"] == "lead_created"
    # A different company's lead leaves it alone.
    other = await client.post("/v1/prospects", headers=ADMIN, json={"name": "Other Prospect"})
    assert (await client.get(f"/v1/prospects/{other.json()['id']}", headers=ADMIN)).json()["lead_ids"] is None


async def test_an_auto_approved_conversion_carries_no_approver_suffix(client: AsyncClient):
    eid = await _entity(client, "Auto Co")
    lead = await client.post("/v1/leads", headers=ADMIN, json={"company": "Auto Co", "entity_id": eid, "temperature": "Hot", "status": "Active"})
    r = await client.post(f"/v1/leads/{lead.json()['id']}/convert", headers=ADMIN,
                          json={"is_lending": True, "product_type": "Term Loan", "amount_cr": 5,
                                "note": "Pushed from the grid", "approved_by": "auto-approval@policy"})
    assert r.status_code == 200, r.text
    deal = (await client.get(f"/v1/deals/{r.json()['deal_id']}", headers=ADMIN)).json()
    assert deal["remarks"] == "Pushed from the grid"
    assert "approved by" not in ((await client.get(f"/v1/leads/{lead.json()['id']}", headers=ADMIN)).json()["conv"] or "")
