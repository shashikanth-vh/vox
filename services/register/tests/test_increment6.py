"""Increment 6 (Register side) — asset-monetisation governance.

The asset_monetisation decision kind is subject-bound and reserved to AM authority; the
am_closure_approval evidence is VERIFIED against it; and a mandate cannot reach 'Closed'
until that evidence is on file."""

from __future__ import annotations

import uuid

import pytest

from tests.test_decisions import _ctx, wf_client  # noqa: F401 - fixture import

pytestmark = pytest.mark.asyncio

AM_HEAD = {"X-User-Email": "ah@evamfinance.com", "X-User-Roles": "AM Head"}


async def test_am_decision_kind_is_authority_and_subject_bound(wf_client):  # noqa: F811
    mid = uuid.uuid4().hex
    wf = f"amon-{mid}"
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation"},
        headers={"X-Internal-Context": _ctx(roles=("AM Head",))})
    assert r.status_code == 422, r.text                      # subject binding required
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation",
              "subject_type": "AssetMonetisation", "subject_id": mid},
        headers={"X-Internal-Context": _ctx(roles=("Syn Head",))})
    assert r.status_code == 403, r.text                      # wrong desk's authority
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation",
              "subject_type": "AssetMonetisation", "subject_id": mid,
              "committee_reference": "am-closure/SPA-1"},
        headers={"X-Internal-Context": _ctx(roles=("AM Head",))})
    assert r.status_code == 201, r.text
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 0                             # no conversion-delivery row


async def test_am_mandate_closes_by_direct_update(client):
    """Desk review decision: the AM book is a plain update surface — no workflow, no
    approval ceremony. The transition graph still applies (forward one, back one,
    Dropped), Closed is an ordinary move from SPA / Documentation, and the terminals
    stay final. Evidence-verification infra is untouched: an invented decision_ref on
    an am_closure_approval attachment is still refused."""
    code = "AMC" + uuid.uuid4().hex[:6].upper()
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": "AM Co",
                                   "entity_type": "Company"})).json()["id"]
    mid = (await client.post("/v1/asset-monetisation",
                             json={"entity_id": eid,
                                   "status": "Teaser Prepared"})).json()["id"]

    # Jumping the pipeline is still not a move.
    r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": "Closed"})
    assert r.status_code == 422, r.text

    for st in ("Teaser Shared", "In Discussion", "NBO Received", "BO Received",
               "SPA / Documentation"):
        r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": st})
        assert r.status_code == 200, f"{st}: {r.text}"

    # From SPA / Documentation, Closed is a plain edit — no evidence demanded.
    r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": "Closed"})
    assert r.status_code == 200, r.text

    # Terminal is terminal.
    r = await client.patch(f"/v1/asset-monetisation/{mid}",
                           json={"status": "Teaser Prepared"})
    assert r.status_code == 422, r.text

    # The evidence store's decision verification is unchanged by the gate removal.
    bad = await client.post("/v1/evidence", json={
        "subject_type": "AssetMonetisation", "subject_id": mid,
        "evidence_kind": "am_closure_approval", "reference": "am/1",
        "sha256": "a" * 64, "decision_ref": "invented"}, headers=AM_HEAD)
    assert bad.status_code == 422, bad.text


async def test_convert_carries_the_am_opening_facts(client):
    """Push-to-Deals with Asset Monetisation ticked births the AM row CARRYING what
    the RM typed — value, MW, deal type, status, ownership — because the AM book is a
    plain update surface with no later ceremony to fill them in."""
    for name, full in (("Kiran Rao", "Kiran Rao"), ("Dev Mehta", "Dev Mehta")):
        await client.post("/v1/people",
                          json={"name": name, "full_name": full, "role": "RM"})
    eid = (await client.post("/v1/entities",
                             json={"code": "AMF" + uuid.uuid4().hex[:6].upper(),
                                   "legal_name": "AM Facts Co",
                                   "state": "Karnataka"})).json()["id"]
    lead = (await client.post("/v1/leads",
                              json={"company": "AM Facts Co",
                                    "entity_id": eid, "temperature": "Hot"})).json()
    r = await client.post(
        f"/v1/leads/{lead['id']}/convert",
        json={"is_asset_mon": True, "rm": "Kiran Rao", "analyst": "Dev Mehta",
              "am_value_cr": 120, "am_size_mw": 45.5,
              "am_deal_type": "Capital Market", "am_status": "Teaser Prepared"},
        headers={"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"})
    assert r.status_code == 200, r.text
    am_id = r.json()["asset_mon_id"]
    assert am_id, r.text
    row = (await client.get(f"/v1/asset-monetisation/{am_id}")).json()
    assert float(row["indicative_value_cr"]) == 120
    assert float(row["size_mw"]) == 45.5
    assert row["deal_type"] == "Capital Market"
    assert row["status"] == "Teaser Prepared"
    assert (row["rm"], row["analyst"]) == ("Kiran Rao", "Dev Mehta")
    # Defaults the desk should not retype: State from the company, Nature = Seller.
    assert row["state"] == "Karnataka"
    assert row["nature"] == "Seller"


async def test_convert_carries_every_line_fact(client):
    """The full carry-forward audit, pinned: a three-line push lands each line's OWN
    figure and stage/status (not the combined total), the deal's temperature, and the
    syndication opening facts — while a governance-deep birth stage is refused."""
    for name in ("Asha Verma", "Rohit Jain"):
        await client.post("/v1/people",
                          json={"name": name, "full_name": name, "role": "RM"})
    eid = (await client.post("/v1/entities",
                             json={"code": "ALL" + uuid.uuid4().hex[:6].upper(),
                                   "legal_name": "Every Fact Co", "state": "Gujarat",
                                   "toi": "EPC"})).json()["id"]
    lead = (await client.post("/v1/leads",
                              json={"company": "Every Fact Co",
                                    "entity_id": eid, "temperature": "Hot"})).json()
    admin = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}

    # A birth stage deep in governance is refused outright.
    bad = await client.post(f"/v1/leads/{lead['id']}/convert",
                            json={"is_lending": True, "lending_amount_cr": 10,
                                  "lending_stage": "Sanctioned"}, headers=admin)
    assert bad.status_code == 422, bad.text

    r = await client.post(
        f"/v1/leads/{lead['id']}/convert",
        json={"is_lending": True, "is_syndication": True, "is_asset_mon": True,
              "rm": "Asha Verma", "analyst": "Rohit Jain",
              "amount_cr": 82, "temperature": "Hot",
              "lending_amount_cr": 10, "lending_stage": "Data Awaited",
              "syn_amount_cr": 52, "syn_type": "Fee will be paid by customer",
              "syn_mandate_status3": "Under discussion", "syn_status": "Deal Sourced",
              "syn_facility": "Term Loan", "syn_tenor": "24-36m",
              "syn_priority": "High", "syn_im_status": "In prep",
              "syn_potential": "Kotak, HDFC", "syn_existing": "SBI",
              "syn_price": "11-12%",
              "am_value_cr": 20, "am_deal_type": "Project Advisory"},
        headers=admin)
    assert r.status_code == 200, r.text
    ids = r.json()

    deal = (await client.get(f"/v1/deals/{ids['deal_id']}")).json()
    assert deal["temperature"] == "Hot"

    lend = (await client.get(f"/v1/lending/{ids['lending_id']}")).json()
    assert float(lend["amount_cr"]) == 10 and lend["stage"] == "Data Awaited"

    syn = (await client.get(f"/v1/syndication/{ids['syndication_id']}")).json()
    assert float(syn["amount_cr"]) == 52          # NOT the 82 combined total
    assert syn["syndication_type"] == "Fee will be paid by customer"
    assert syn["mandate_status3"] == "Under discussion"
    assert syn["status"] == "Deal Sourced" and syn["facility"] == "Term Loan"
    assert syn["tenor"] == "24-36m" and syn["priority"] == "High"
    assert syn["im_status"] == "In prep" and syn["potential"] == "Kotak, HDFC"
    assert syn["existing"] == "SBI" and syn["price"] == "11-12%"
    assert syn["toi"] == "EPC"                    # inherited from the company

    am = (await client.get(f"/v1/asset-monetisation/{ids['asset_mon_id']}")).json()
    assert float(am["indicative_value_cr"]) == 20
    assert am["deal_type"] == "Project Advisory" and am["state"] == "Gujarat"


async def test_am_tracker_carries_its_own_rm_and_analyst(client):
    """ATLAS v19 parity: the AM desk's ownership lives ON the mandate row (like the
    lending/syndication trackers) — team scoping, scorecards, and book rollups key on
    it, so it must persist through create, patch, and the read schema."""
    code = "AMO" + uuid.uuid4().hex[:6].upper()
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": "AM Owned Co",
                                   "entity_type": "Company"})).json()["id"]
    r = await client.post("/v1/asset-monetisation",
                          json={"entity_id": eid, "status": "Teaser Prepared",
                                "rm": "Kiran Rao", "analyst": "Dev Mehta"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert (row["rm"], row["analyst"]) == ("Kiran Rao", "Dev Mehta")
    r = await client.patch(f"/v1/asset-monetisation/{row['id']}",
                           json={"analyst": "Nisha Iyer"})
    assert r.status_code == 200, r.text
    assert r.json()["analyst"] == "Nisha Iyer"
