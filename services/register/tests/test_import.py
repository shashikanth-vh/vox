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


async def test_import_rejects_non_xlsx(client: AsyncClient):
    files = {"file": ("data.txt", b"not a workbook", "text/plain")}
    r = await client.post("/v1/import/atlas-xlsx", files=files)
    assert r.status_code == 422
