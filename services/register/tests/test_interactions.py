"""Interaction timeline: polymorphic subjects (lead/deal/entity/counterparty/trackers),
entity roll-up, the syndication-lender response/chased behaviour, and the VOX source."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _entity(client: AsyncClient, code: str) -> str:
    r = await client.post("/v1/entities", json={"code": code, "legal_name": code})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_lead_timeline_and_rollup(client: AsyncClient):
    lead_id = (await client.post("/v1/leads", json={"company": "EcoSoch"})).json()["id"]
    for i, (typ, when) in enumerate([
        ("Phone Call", "2026-06-01T10:00:00Z"),
        ("In-Person Meeting", "2026-06-10T10:00:00Z"),
        ("Email / Written Correspondence", "2026-06-20T10:00:00Z"),
    ]):
        body = {"interaction_type": typ, "occurred_at": when, "summary": f"touch {i}"}
        if i == 2:
            body |= {"next_action": "Send term sheet", "next_action_date": "2026-06-25"}
        assert (await client.post(f"/v1/leads/{lead_id}/interactions", json=body)).status_code == 201

    items = (await client.get(f"/v1/leads/{lead_id}/interactions")).json()
    assert len(items) == 3
    assert items[0]["interaction_type"] == "Email / Written Correspondence"  # newest first
    assert items[0]["subject_type"] == "Lead"
    assert items[0]["performed_by"] == "pytest"

    lead = (await client.get(f"/v1/leads/{lead_id}")).json()
    assert lead["last_interaction_date"] == "2026-06-20"
    assert lead["next_action"] == "Send term sheet"


async def test_deal_and_tracker_rollup_to_entity(client: AsyncClient):
    eid = await _entity(client, "ACME")
    deal_id = (await client.post("/v1/deals", json={"entity_id": eid, "code": "ACME"})).json()["id"]
    lend_id = (await client.post("/v1/lending", json={"entity_id": eid, "deal_id": deal_id,
                                                      "amount_cr": 5})).json()["id"]

    r = await client.post(f"/v1/deals/{deal_id}/interactions",
                          json={"interaction_type": "Term Sheet Negotiation"})
    assert r.status_code == 201 and r.json()["entity_id"] == eid  # denormalised

    r = await client.post(f"/v1/lending/{lend_id}/interactions",
                          json={"interaction_type": "Internal Review / Credit Committee"})
    assert r.status_code == 201 and r.json()["entity_id"] == eid

    # Deal timeline has 1; entity-level timeline aggregates deal + lending = 2.
    assert len((await client.get(f"/v1/deals/{deal_id}/interactions")).json()) == 1
    assert len((await client.get(f"/v1/entities/{eid}/interactions")).json()) == 2


async def test_syndication_lender_response_and_chased(client: AsyncClient):
    eid = await _entity(client, "SYNCO")
    syn_id = (await client.post("/v1/syndication",
                                json={"entity_id": eid, "status": "IM in Prep"})).json()["id"]
    await client.post(f"/v1/syndication/{syn_id}/lenders",
                      json={"lender_name": "Kotak Mahindra", "status": "IM Circulated"})

    # Outbound (we chased) then inbound (they replied) — updates the lender's dates.
    await client.post(f"/v1/syndication/{syn_id}/interactions", json={
        "interaction_type": "Email / Written Correspondence", "direction": "Outbound",
        "lender_name": "Kotak Mahindra", "occurred_at": "2026-06-05T09:00:00Z",
        "notes": "Chased for IP"})
    await client.post(f"/v1/syndication/{syn_id}/interactions", json={
        "interaction_type": "Phone Call", "direction": "Inbound",
        "lender_name": "Kotak Mahindra", "occurred_at": "2026-06-08T09:00:00Z",
        "notes": "They confirmed interest"})

    lender = (await client.get(f"/v1/syndication/{syn_id}/lenders")).json()[0]
    assert lender["chased_date"] == "2026-06-05"
    assert lender["response_date"] == "2026-06-08"
    # The words travel with the clocks: each direction keeps ITS OWN last note.
    assert lender["last_chase_note"] == "Chased for IP"
    assert lender["last_reply_note"] == "They confirmed interest"

    # A note-less chase moves the clock and clears the snapshot text — the row
    # never shows old words against a new date.
    await client.post(f"/v1/syndication/{syn_id}/interactions", json={
        "interaction_type": "Phone Call", "direction": "Outbound",
        "lender_name": "Kotak Mahindra", "occurred_at": "2026-06-10T09:00:00Z"})
    lender = (await client.get(f"/v1/syndication/{syn_id}/lenders")).json()[0]
    assert lender["chased_date"] == "2026-06-10"
    assert lender["last_chase_note"] is None
    assert lender["last_reply_note"] == "They confirmed interest"  # untouched

    # All three interactions are on the syndication timeline, carrying the lender.
    tl = (await client.get(f"/v1/syndication/{syn_id}/interactions")).json()
    assert len(tl) == 3
    assert all(i["lender_name"] == "Kotak Mahindra" for i in tl)


async def test_vox_source_interaction(client: AsyncClient):
    eid = await _entity(client, "VOXCO")
    r = await client.post(f"/v1/entities/{eid}/interactions", json={
        "interaction_type": "Virtual Meeting / Video Call", "source": "VOX",
        "notes": "Promoter bullish on Q3 pipeline", "transcript": "…full VOX transcript…",
        "performed_by": "Shubh"})
    assert r.status_code == 201
    body = r.json()
    assert body["source"] == "VOX" and body["transcript"].startswith("…")


async def test_interaction_requires_subject(client: AsyncClient):
    r = await client.post("/v1/interactions", json={"interaction_type": "Phone Call"})
    assert r.status_code in (400, 422)


async def test_interaction_bad_subject_type(client: AsyncClient):
    r = await client.post("/v1/interactions", json={
        "subject_type": "Nonsense", "subject_id": "00000000-0000-0000-0000-000000000000",
        "interaction_type": "Phone Call"})
    assert r.status_code in (400, 422)


async def test_interaction_is_append_only(client: AsyncClient):
    """Interactions can be created and read, but never edited or deleted (ATLAS design)."""
    eid = await _entity(client, "FILT")
    iid = (await client.post(f"/v1/entities/{eid}/interactions",
                             json={"interaction_type": "Site Visit / Due Diligence",
                                   "direction": "Outbound"})).json()["id"]

    # Read + filter work.
    r = await client.get("/v1/interactions", params={"subject_type": "Entity", "with_total": True})
    assert r.json()["total"] == 1
    assert (await client.get(f"/v1/interactions/{iid}")).status_code == 200

    # Edit / delete are not exposed — the routes don't exist (405).
    assert (await client.patch(f"/v1/interactions/{iid}", json={"outcome": "x"})).status_code == 405
    assert (await client.delete(f"/v1/interactions/{iid}")).status_code == 405
