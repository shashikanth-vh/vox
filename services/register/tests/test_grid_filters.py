"""The grids' facet filters, server-side (the wiring that makes checkbox filters real).

The UI's checkbox facets translate to register query params (CommonTable
meta.filterParam), multi-select joining to a comma IN-list. Pins:

* single-value equality on the newly filterable columns (lending/deals `analyst`,
  asset-monetisation `investor_type` / `deal_type`),
* the comma IN-list,
* the fail-closed guard: an unknown param still refuses the request loudly.
"""

from __future__ import annotations

import uuid

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}


async def _entity(client, code: str) -> str:
    r = await client.post("/v1/entities",
                          json={"code": code, "legal_name": f"{code} Pvt Ltd"},
                          headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_lending_filters_by_analyst_including_an_in_list(client):
    eid = await _entity(client, f"GF{uuid.uuid4().hex[:6].upper()}")
    for analyst in ("Bhavana Sridhar", "Prateek Seth", "Archana Tripathi"):
        r = await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Data Awaited",
                                    "amount_cr": 2, "analyst": analyst},
                              headers=ADMIN)
        assert r.status_code == 201, r.text

    one = await client.get("/v1/lending",
                           params={"analyst": "Bhavana Sridhar", "entity_id": eid},
                           headers=ADMIN)
    assert one.status_code == 200, one.text
    rows = one.json()["items"] if "items" in one.json() else one.json()
    rows = rows if isinstance(rows, list) else rows.get("lending", [])
    assert {r_["analyst"] for r_ in rows} == {"Bhavana Sridhar"}

    # Multi-select facet: one param, comma IN-list.
    two = await client.get("/v1/lending",
                           params={"analyst": "Bhavana Sridhar,Prateek Seth",
                                   "entity_id": eid},
                           headers=ADMIN)
    assert two.status_code == 200, two.text
    body = two.json()
    rows2 = body["items"] if isinstance(body, dict) and "items" in body else body
    rows2 = rows2 if isinstance(rows2, list) else body.get("lending", [])
    assert {r_["analyst"] for r_ in rows2} == {"Bhavana Sridhar", "Prateek Seth"}


async def test_asset_mon_filters_by_investor_type_and_deals_by_analyst(client):
    eid = await _entity(client, f"GF{uuid.uuid4().hex[:6].upper()}")
    d = await client.post("/v1/deals",
                          json={"entity_id": eid, "stage": "In Screening",
                                "analyst": "Prateek Seth"}, headers=ADMIN)
    assert d.status_code == 201, d.text
    got = await client.get("/v1/deals",
                           params={"analyst": "Prateek Seth", "entity_id": eid},
                           headers=ADMIN)
    assert got.status_code == 200, got.text

    for itype in ("Strategic", "Financial Investor"):
        r = await client.post("/v1/asset-monetisation",
                              json={"entity_id": eid, "status": "Teaser Prepared",
                                    "investor_type": itype}, headers=ADMIN)
        assert r.status_code == 201, r.text
    f = await client.get("/v1/asset-monetisation",
                         params={"investor_type": "Strategic", "entity_id": eid},
                         headers=ADMIN)
    assert f.status_code == 200, f.text
    body = f.json()
    rows = body["items"] if isinstance(body, dict) and "items" in body else body
    rows = rows if isinstance(rows, list) else body.get("asset_monetisation", [])
    assert {r_["investor_type"] for r_ in rows} == {"Strategic"}


async def test_an_unknown_filter_param_still_fails_closed(client):
    r = await client.get("/v1/lending", params={"bogus": "x"}, headers=ADMIN)
    assert r.status_code == 422
    assert "Filterable" in r.text


async def test_leads_filter_by_company_and_lead_no(client):
    """The Company facet: unique-per-row, but the desk narrows to ONE company all the
    time — the facet's Contains box finds it, the register filters it."""
    import uuid as _uuid
    tag = _uuid.uuid4().hex[:6]
    for nm in (f"Facet Co {tag}", f"Other Co {tag}"):
        r = await client.post("/v1/leads", json={"company": nm}, headers=ADMIN)
        assert r.status_code == 201, r.text
        if nm.startswith("Facet"):
            lead_no = r.json()["lead_no"]
    got = await client.get("/v1/leads", params={"company": f"Facet Co {tag}"}, headers=ADMIN)
    assert got.status_code == 200, got.text
    rows = got.json().get("items", [])
    assert {x["company"] for x in rows} == {f"Facet Co {tag}"}
    by_no = await client.get("/v1/leads", params={"lead_no": lead_no}, headers=ADMIN)
    assert by_no.status_code == 200 and len(by_no.json().get("items", [])) == 1
