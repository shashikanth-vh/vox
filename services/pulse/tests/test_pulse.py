"""PULSE tests — matching rules (pure unit) + the full fetch→match→file→digest loop
against a REAL Register (uvicorn subprocess, real Postgres, real migrations)."""

from __future__ import annotations

import uuid

from app.matching import WatchEntity, classify_signal, match_entities
from app.providers import NewsItem

# --------------------------------------------------------------------------- #
# Unit: matching + signal rules
# --------------------------------------------------------------------------- #
WATCH = [
    WatchEntity(id="1", code="ECOSOCH", legal_name="EcoSoch Solar Pvt Ltd",
                display_name="EcoSoch Solar"),
    WatchEntity(id="2", code="SUNRISE", legal_name="Sunrise Green Power Private Limited"),
]


def test_matches_display_and_stripped_legal_name():
    item = NewsItem(source="t", title="EcoSoch Solar commissions 12.5 MW portfolio")
    assert [e.id for e in match_entities(item, WATCH)] == ["1"]
    # Legal-name match with the corporate suffixes stripped.
    item2 = NewsItem(source="t", title="NCLT admits plea against Sunrise Green Power")
    assert [e.id for e in match_entities(item2, WATCH)] == ["2"]


def test_no_match_on_unrelated_headline():
    item = NewsItem(source="t", title="State discom announces new open-access policy")
    assert match_entities(item, WATCH) == []


def test_signal_red_beats_green():
    red = ["insolvency", "default"]
    green = ["commissioned", "awarded"]
    assert classify_signal(NewsItem(source="t", title="Insolvency plea admitted"),
                           red, green) == "RED"
    assert classify_signal(NewsItem(source="t", title="12 MW commissioned"),
                           red, green) == "GREEN"
    assert classify_signal(NewsItem(source="t", title="Quarterly results announced"),
                           red, green) == "AMBER"
    # A headline with both words is adverse first.
    assert classify_signal(
        NewsItem(source="t", title="Commissioned park owner faces insolvency"),
        red, green) == "RED"


# --------------------------------------------------------------------------- #
# e2e: scan → intel rows in the Register → digest; re-scan files nothing new
# --------------------------------------------------------------------------- #
async def test_scan_files_intel_idempotently(pulse, register_direct):
    code = f"ECOSOCH-{uuid.uuid4().hex[:6]}"
    resp = await register_direct.post("/v1/entities", json={
        "code": code, "legal_name": "EcoSoch Solar Pvt Ltd",
        "display_name": "EcoSoch Solar", "sector": "Solar - EPC"})
    assert resp.status_code == 201, resp.text
    entity_id = resp.json()["id"]

    first = await pulse.post("/v1/scan")
    assert first.status_code == 200, first.text
    body = first.json()
    filed = [f for f in body["filed"] if f["entity_id"] == entity_id]
    assert len(filed) == 1  # the sample feed mentions EcoSoch exactly once
    assert filed[0]["signal"] == "GREEN"  # "commissions ... ahead of schedule"

    # Same scan again → idempotency replay, still exactly one intel row.
    again = await pulse.post("/v1/scan")
    assert again.status_code == 200
    rows = await register_direct.get("/v1/external-intelligence",
                                     params={"entity_id": entity_id})
    assert len(rows.json()["items"]) == 1

    digest = await pulse.get("/v1/digest", params={"hours": 1})
    assert digest.status_code == 200
    dig = digest.json()
    assert any(i["entity_id"] == entity_id for i in dig["items"]["GREEN"])


async def test_push_item_with_explicit_entity_and_signal_override(pulse, register_direct):
    code = f"PUSH-{uuid.uuid4().hex[:6]}"
    resp = await register_direct.post("/v1/entities", json={
        "code": code, "legal_name": f"Pushed Target {code} Pvt Ltd"})
    entity_id = resp.json()["id"]

    pushed = await pulse.post("/v1/items", json={
        "title": "Regulator issues penalty notice", "source": "manual",
        "url": f"https://example.com/{code}", "entity_id": entity_id,
        "signal": "RED"})
    assert pushed.status_code == 201, pushed.text
    assert pushed.json()["matched"] == 1

    rows = await register_direct.get("/v1/external-intelligence",
                                     params={"entity_id": entity_id})
    items = rows.json()["items"]
    assert len(items) == 1 and items[0]["signal"] == "RED"
    assert items[0]["source"] == "PULSE:manual"
