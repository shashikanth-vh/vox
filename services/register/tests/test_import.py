"""ATLAS MIS xlsx import (upload endpoint)."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from openpyxl import Workbook

pytestmark = pytest.mark.asyncio


def _mini_mis() -> bytes:
    """A tiny 6-sheet workbook shaped like the real MIS."""
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Leads")
    ws.append(["Source", "Source Detail", "RM Owner", "Sector", "Mitigation / Adaptation",
               "Company Name", "Status", "Contact Person", "Designation", "Contact Phone",
               "Last Interaction Date", "Next Action", "Next Action Date", "Notes"])
    ws.append(["RM", "Shubh", "Shubh Dave", "Renewables - Solar", "Mitigation", "EcoSoch",
               "Cold", "Harsha", "", "9535240349", None, "Follow up", None, "note"])

    ws = wb.create_sheet("Deals")
    ws.append(["Company Name", "Sector", "Location", "Source", "Source Detail", "Status", "RM",
               "Lending?", "Syndication?", "Asset Mon?", "Stage", "Date Received",
               "Contact Person", "Contact Phone", "Remarks"])
    ws.append(["ANV Web Ventures Private Limited", "EV", "TG", "DSA", "Get Vantage", "Hot",
               "Shubh Dave", "Yes", "Yes", "No", "Closed Won", None, "", "", "in process"])

    ws = wb.create_sheet("Lending Tracker")
    ws.append(["Company Name", "Lending Amount (₹ Cr)", "RM", "Credit Analyst", "Stage",
               "Stage Updated", "Remarks"])
    ws.append(["ANV Web Ventures Private Limited", 1.15, "Shubh Dave", "AT", "Disbursed", None, "ok"])

    ws = wb.create_sheet("Syndication")
    ws.append(["Company Name", "Deal Status", "Bank", "Status", "Amount (₹ Cr)",
               "Accepted by Client", "Remarks"])
    ws.append(["ANV Web Ventures Private Limited", "Deal Live", "Axis Finance", "IM Circulated",
               10, "No", "shared"])
    ws.append(["ANV Web Ventures Private Limited", "Deal Live", "Bajaj Finance", "Rejected",
               10, "No", "dropped"])

    ws = wb.create_sheet("Asset Mon")
    ws.append(["Company Name", "RM", "State", "Indicative Value (₹ Cr)", "Size (MW)", "Nature",
               "Deal Type", "Investor", "Investor Type", "Status", "Date Teaser Shared",
               "Notes", "Updated Remarks 19 July 2026"])
    ws.append(["Axel Renewable Private Limited", "Shubh Dave", "MH", 270, 58, "Seller",
               "Capital Market", "Radiance", "VC", "In Discussion", None, "solar+bess", "upd"])

    ws = wb.create_sheet("Mandate Tracker")
    ws.append(["Company Name", "RM", "Mandate Sent/Not Sent", "Signed/Pending"])
    ws.append(["ANV Web Ventures Private Limited", "Shubh Dave", "Sent", "Mandate Signed"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_import_atlas_xlsx_replace(client: AsyncClient):
    files = {"file": ("mis.xlsx", _mini_mis(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await client.post("/v1/import/atlas-xlsx", params={"mode": "replace"}, files=files)
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    # 3 distinct companies across the sheets → 3 entities
    assert counts["entities"] == 3
    assert counts["leads"] == 1
    assert counts["deals"] == 1
    assert counts["lending_tracker"] == 1
    assert counts["syndication_tracker"] == 1
    assert counts["syndication_lenders"] == 2
    assert counts["asset_monetisation"] == 1
    assert counts["mandate_applied"] == 1

    # entities landed and are queryable
    r = await client.get("/v1/entities", params={"with_total": True})
    assert r.json()["total"] == 3

    # the mandate rolled onto the company's syndication tracker
    r = await client.get("/v1/syndication", params={"with_total": True})
    tr = r.json()["items"][0]
    assert tr["mandate_status"] == "Sent - Mandate Signed"

    # syndication lenders attached
    r = await client.get(f"/v1/syndication/{tr['id']}/lenders")
    assert {ln["lender_name"] for ln in r.json()} == {"Axis Finance", "Bajaj Finance"}


async def test_merge_reimport_is_upsert_not_duplicate(client: AsyncClient):
    """A second (merge) import of the same workbook must not duplicate anything or trip a
    unique constraint — the reviewer's 'merge is not a real merge' finding. People,
    counterparties, leads, deals and trackers are reused by their natural key."""
    mis = _mini_mis()
    files = {"file": ("mis.xlsx", mis,
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await client.post("/v1/import/atlas-xlsx", params={"mode": "replace"}, files=files)
    assert r.status_code == 200, r.text

    ents = (await client.get("/v1/entities", params={"with_total": True})).json()["total"]
    leads = (await client.get("/v1/leads", params={"with_total": True})).json()["total"]
    deals = (await client.get("/v1/deals", params={"with_total": True})).json()["total"]
    syn = (await client.get("/v1/syndication", params={"with_total": True})).json()["items"]
    lenders = await client.get(f"/v1/syndication/{syn[0]['id']}/lenders")
    n_lenders = len(lenders.json())

    # Re-import the SAME workbook in merge mode — must be a no-op on counts, not a
    # constraint violation.
    files = {"file": ("mis.xlsx", mis,
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await client.post("/v1/import/atlas-xlsx", params={"mode": "merge"}, files=files)
    assert r.status_code == 200, r.text
    c = r.json()["counts"]
    assert c["entities"] == 0 and c["leads"] == 0 and c["deals"] == 0
    assert c["counterparties"] == 0 and c["people"] == 0
    assert c["syndication_tracker"] == 0 and c["syndication_lenders"] == 0

    assert (await client.get("/v1/entities", params={"with_total": True})).json()["total"] == ents
    assert (await client.get("/v1/leads", params={"with_total": True})).json()["total"] == leads
    assert (await client.get("/v1/deals", params={"with_total": True})).json()["total"] == deals
    syn2 = (await client.get("/v1/syndication", params={"with_total": True})).json()["items"]
    assert len(syn2) == len(syn)
    assert len((await client.get(f"/v1/syndication/{syn2[0]['id']}/lenders")).json()) == n_lenders


async def test_company_suffix_variants_canonicalise_to_one_entity(client: AsyncClient):
    """'Pvt Ltd', 'Private Limited' and 'Ltd' variants of the same name must resolve to a
    single entity — the reviewer's canonicalization finding."""
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Leads")
    ws.append(["Company Name", "RM Owner", "Sector"])
    ws.append(["Helios Power Private Limited", "Shubh Dave", "Solar"])
    ws = wb.create_sheet("Deals")
    ws.append(["Company Name", "RM", "Lending?", "Syndication?", "Asset Mon?", "Stage"])
    ws.append(["Helios Power Pvt Ltd", "Shubh Dave", "Yes", "No", "No", "Live"])
    ws = wb.create_sheet("Lending Tracker")
    ws.append(["Company Name", "Lending Amount (₹ Cr)", "RM"])
    ws.append(["Helios Power Ltd", 5.0, "Shubh Dave"])
    buf = io.BytesIO()
    wb.save(buf)

    files = {"file": ("mis.xlsx", buf.getvalue(),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = await client.post("/v1/import/atlas-xlsx", params={"mode": "replace"}, files=files)
    assert r.status_code == 200, r.text
    # All three suffix variants collapse to ONE entity.
    assert r.json()["counts"]["entities"] == 1


async def test_import_rejects_non_xlsx(client: AsyncClient):
    files = {"file": ("data.txt", b"not a workbook", "text/plain")}
    r = await client.post("/v1/import/atlas-xlsx", files=files)
    assert r.status_code == 422


async def test_replace_import_is_tenant_scoped(client, tmp_path):
    """A replace import for tenant B must NOT delete tenant A's rows (the TRUNCATE
    data-loss fix). Seed a row on EVAM, run a replace import for a SECOND tenant, and
    confirm the EVAM row survives."""
    import uuid as _uuid

    from openpyxl import Workbook

    keep = (await client.post("/v1/entities", json={
        "code": f"KEEP-{_uuid.uuid4().hex[:6]}", "legal_name": "Keep Me Co"})).json()

    code = f"T2{_uuid.uuid4().hex[:4]}".upper()
    r = await client.post("/v1/tenants", json={"code": code, "name": "Second Tenant"})
    assert r.status_code in (201, 200), r.text

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(["Company Name", "Sector", "RM Owner"])
    ws.append(["Second Tenant Co", "Solar - EPC", "Chetan"])
    path = tmp_path / "mis.xlsx"
    wb.save(path)
    with open(path, "rb") as fh:
        r = await client.post("/v1/import/atlas-xlsx?mode=replace",
                              files={"file": ("mis.xlsx", fh.read())},
                              headers={"X-Tenant": code})
    assert r.status_code == 200, r.text

    # The EVAM row is untouched — a TRUNCATE would have wiped it.
    still = await client.get(f"/v1/entities/{keep['id']}")
    assert still.status_code == 200
