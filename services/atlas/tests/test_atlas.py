"""ATLAS tests — aggregation rules (pure unit) + composed views against a REAL Register."""

from __future__ import annotations

import uuid
from datetime import date

from app import aggregations as agg


# --------------------------------------------------------------------------- #
# Unit: aggregation helpers
# --------------------------------------------------------------------------- #
def test_count_by_and_sum_of():
    rows = [{"stage": "Diligence", "amount_cr": 25},
            {"stage": "Diligence", "amount_cr": 10.5},
            {"stage": None, "amount_cr": None}]
    assert agg.count_by(rows, "stage") == {"Diligence": 2, "—": 1}
    assert agg.sum_of(rows, "amount_cr") == 35.5


def test_leads_due_today_sorts_and_filters():
    today = date(2026, 7, 25)
    rows = [
        {"id": "a", "status": "Active", "next_action_date": "2026-07-30", "company": "X"},
        {"id": "b", "status": "Active", "next_action_date": "2026-07-20", "company": "Y"},
        {"id": "c", "status": "Active", "next_action_date": "2026-07-25", "company": "Z"},
        {"id": "d", "status": "Converted", "next_action_date": "2026-07-01", "company": "W"},
        {"id": "e", "status": "Active", "next_action_date": None, "company": "V"},
    ]
    due = agg.leads_due_today(rows, today)
    assert [r["lead_id"] for r in due] == ["b", "c"]  # overdue first, converted/undated out


def test_monitoring_due_horizon():
    today = date(2026, 7, 25)
    rows = [
        {"id": "1", "due_date": "2026-07-28", "submitted_date": None},
        {"id": "2", "due_date": "2026-09-01", "submitted_date": None},
        {"id": "3", "due_date": "2026-07-20", "submitted_date": "2026-07-19"},
    ]
    due = agg.monitoring_due(rows, today, horizon_days=7)
    assert [r["monitoring_id"] for r in due] == ["1"]


def test_lender_chases_excludes_answered_and_terminal():
    rows = [
        {"id": "w", "lender_name": "Kotak", "status": "IM Circulated",
         "response_date": None, "chased_date": "2026-07-10"},
        {"id": "x", "lender_name": "SBI", "status": "Sanctioned", "response_date": None},
        {"id": "y", "lender_name": "Tata", "status": "IP Received",
         "response_date": "2026-07-01"},
    ]
    chases = agg.lender_chases(rows)
    assert [r["lender_row_id"] for r in chases] == ["w"]


# --------------------------------------------------------------------------- #
# e2e: dashboard / today / pipeline composed from a real Register
# --------------------------------------------------------------------------- #
async def test_dashboard_reflects_created_rows(atlas, register_direct):
    code = f"ATLAS-{uuid.uuid4().hex[:6]}"
    ent = (await register_direct.post("/v1/entities", json={
        "code": code, "legal_name": f"Atlas Test Co {code} Pvt Ltd"})).json()
    deal = (await register_direct.post("/v1/deals", json={
        "entity_id": ent["id"], "product_type": "Term Loan", "stage": "In Pipeline",
        "is_lending": True})).json()
    lend = (await register_direct.post("/v1/lending", json={
        "entity_id": ent["id"], "deal_id": deal["id"], "amount_cr": 25,
        "stage": "Data Awaited"})).json()

    resp = await atlas.get("/v1/dashboard")
    assert resp.status_code == 200, resp.text
    dash = resp.json()
    assert dash["deals"]["total"] >= 1
    assert dash["lending"]["by_stage"].get("Data Awaited", 0) >= 1
    assert dash["lending"]["amount_cr"] >= 25
    assert "external_intelligence" in dash

    # Pipeline board for lending contains our row.
    board = (await atlas.get("/v1/pipeline/lending")).json()
    assert any(r["id"] == lend["id"] for r in board["rows"])

    # Unknown vertical → clean 404, not a proxy error.
    bad = await atlas.get("/v1/pipeline/nope")
    assert bad.status_code == 404


async def test_today_view_surfaces_due_lead_and_covenant(atlas, register_direct):
    code = f"TODAY-{uuid.uuid4().hex[:6]}"
    ent = (await register_direct.post("/v1/entities", json={
        "code": code, "legal_name": f"Today Test Co {code} Pvt Ltd"})).json()
    lead = (await register_direct.post("/v1/leads", json={
        "company": f"Today Test Co {code}", "entity_id": ent["id"], "status": "Active",
        "next_action": "Call CFO", "next_action_date": "2020-01-01"})).json()
    mon = (await register_direct.post("/v1/monitoring", json={
        "entity_id": ent["id"], "record_type": "Covenant Compliance",
        "covenant_name": "DSCR >= 1.2x", "due_date": "2020-01-02"})).json()

    resp = await atlas.get("/v1/today")
    assert resp.status_code == 200, resp.text
    today = resp.json()
    assert any(r["lead_id"] == lead["id"] for r in today["leads_due"])
    assert any(r["monitoring_id"] == mon["id"] for r in today["monitoring_due"])

    summary = await atlas.get(f"/v1/entities/{ent['id']}/summary")
    assert summary.status_code == 200
    assert summary.json()["entity"]["id"] == ent["id"]
