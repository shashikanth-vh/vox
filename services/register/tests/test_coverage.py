"""Coverage-gap features added after the ATLAS cross-check audit.

Exercises, end-to-end, every new capability so model↔migration drift is caught too:
entity tags, lending disbursement, financials basis/scale + typed line-items, external-
intel triage, covenant compliance, interaction attachments, server-side stage/status
history append, embedded syndication lenders, per-tenant settings, the lender matrix,
and the new reference vocabularies.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _entity(client: AsyncClient, code: str = "ACME") -> str:
    r = await client.post("/v1/entities", json={"code": code, "legal_name": f"{code} Ltd"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_entity_tags_roundtrip(client: AsyncClient):
    r = await client.post("/v1/entities", json={
        "code": "SUNVIK", "legal_name": "Sunvik Solar", "tags": ["Core 33", "Adaptation 10"],
    })
    assert r.status_code == 201, r.text
    assert r.json()["tags"] == ["Core 33", "Adaptation 10"]
    eid = r.json()["id"]
    # filter by tag membership
    got = await client.get(f"/v1/entities/{eid}")
    assert "Core 33" in got.json()["tags"]


async def test_lending_disbursement_fields(client: AsyncClient):
    eid = await _entity(client, "LEND1")
    r = await client.post("/v1/lending", json={
        "entity_id": eid, "amount_cr": 10.0, "disbursed_amount": 4.5,
        "disbursement_date": "2026-06-30",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert float(body["disbursed_amount"]) == 4.5
    assert body["disbursement_date"] == "2026-06-30"


async def test_financials_basis_scale_and_typed_data(client: AsyncClient):
    eid = await _entity(client, "FIN1")
    r = await client.post("/v1/financials", json={
        "entity_id": eid, "statement_type": "Audited", "period_end": "2026-03-31",
        "is_consolidated": True, "is_audited": True, "scale": "Crore", "revenue": 120.0,
        "data": {"line_items": [
            {"key": "revenue", "label": "Total Revenue", "value": 120.0, "section": "P&L", "order": 1},
            {"key": "pat", "label": "PAT", "value": 12.0, "section": "P&L", "order": 2},
        ]},
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_consolidated"] is True and body["scale"] == "Crore"
    assert body["data"]["line_items"][0]["label"] == "Total Revenue"

    # A malformed line item is rejected by the typed contract.
    bad = await client.post("/v1/financials", json={
        "entity_id": eid, "statement_type": "Provisional", "period_end": "2026-03-31",
        "data": {"line_items": [{"label": "missing key"}]},
    })
    assert bad.status_code == 422


async def test_external_intel_acknowledge_and_dismiss(client: AsyncClient):
    eid = await _entity(client, "INTEL1")
    r = await client.post("/v1/external-intelligence", json={
        "entity_id": eid, "intel_type": "News", "signal": "RED", "title": "Litigation filed",
    })
    iid = r.json()["id"]
    assert r.json()["is_dismissed"] is False

    ack = await client.post(f"/v1/external-intelligence/{iid}/acknowledge")
    assert ack.status_code == 200
    assert ack.json()["acknowledged_by"] == "pytest"
    assert ack.json()["acknowledged_at"] is not None

    # RED signal shows in the dossier until dismissed.
    dossier = await client.get(f"/v1/entities/{eid}/dossier")
    assert any(i["id"] == iid for i in dossier.json()["open_intelligence"])

    dis = await client.post(f"/v1/external-intelligence/{iid}/dismiss")
    assert dis.status_code == 200 and dis.json()["is_dismissed"] is True
    dossier2 = await client.get(f"/v1/entities/{eid}/dossier")
    assert all(i["id"] != iid for i in dossier2.json()["open_intelligence"])


async def test_monitoring_covenant_fields(client: AsyncClient):
    eid = await _entity(client, "MON1")
    r = await client.post("/v1/monitoring", json={
        "entity_id": eid, "record_type": "Covenant Compliance", "covenant_name": "DSCR",
        "target_value": 1.20, "actual_value": 1.05, "breached": True, "waiver_status": "Requested",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["breached"] is True and body["waiver_status"] == "Requested"
    assert float(body["target_value"]) == 1.20 and float(body["actual_value"]) == 1.05


async def test_interaction_attachments(client: AsyncClient):
    eid = await _entity(client, "INT1")
    r = await client.post(f"/v1/entities/{eid}/interactions", json={
        "interaction_type": "Phone Call", "summary": "Intro call",
        "attachments": [{"name": "deck.pdf", "url": "https://x/deck.pdf"}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["attachments"][0]["name"] == "deck.pdf"


async def test_stage_history_auto_append(client: AsyncClient):
    eid = await _entity(client, "STG1")
    r = await client.post("/v1/lending", json={"entity_id": eid, "stage": "Diligence"})
    lid, ver = r.json()["id"], r.json()["version"]
    assert r.json()["stage_history"] in (None, [])

    # Diligence → Note Circulated is the next ORDERED step in the credit pipeline.
    upd = await client.patch(f"/v1/lending/{lid}", headers={"If-Match": f'"{ver}"'},
                             json={"stage": "Note Circulated"})
    assert upd.status_code == 200, upd.text
    hist = upd.json()["stage_history"]
    assert hist and hist[-1]["from"] == "Diligence" and hist[-1]["to"] == "Note Circulated"
    assert hist[-1]["by"] == "pytest" and "at" in hist[-1]


async def test_syndication_embeds_lenders(client: AsyncClient):
    eid = await _entity(client, "SYN1")
    r = await client.post("/v1/syndication", json={"entity_id": eid, "status": "IM in Prep"})
    sid = r.json()["id"]
    # freshly created: empty list embedded
    assert (await client.get(f"/v1/syndication/{sid}")).json()["lenders"] == []

    await client.post(f"/v1/syndication/{sid}/lenders",
                      json={"lender_name": "Axis Finance", "status": "IM Circulated"})
    got = await client.get(f"/v1/syndication/{sid}")
    names = [ln["lender_name"] for ln in got.json()["lenders"]]
    assert names == ["Axis Finance"]


async def test_lender_row_patch_nested_only(client: AsyncClient):
    """The chase board's human lane: PATCH the lender through its PARENT syndication
    (scope-enforced); status changes append to status_history server-side; a lender
    addressed under the WRONG parent is 404; the flat update route stays disabled."""
    eid = await _entity(client, "SYNPCH")
    sid = (await client.post("/v1/syndication",
                             json={"entity_id": eid, "status": "IM in Prep"})).json()["id"]
    ln = (await client.post(f"/v1/syndication/{sid}/lenders",
                            json={"lender_name": "Kotak", "status": "Identified"})).json()

    upd = await client.patch(f"/v1/syndication/{sid}/lenders/{ln['id']}",
                             json={"status": "IM Circulated", "note": "sent v2 IM"})
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["status"] == "IM Circulated" and body["note"] == "sent v2 IM"
    hist = body["status_history"]
    assert hist and hist[-1]["from"] == "Identified" and hist[-1]["to"] == "IM Circulated"

    # Wrong parent → 404, never a cross-mandate edit.
    other = (await client.post("/v1/syndication", json={"entity_id": eid})).json()["id"]
    wrong = await client.patch(f"/v1/syndication/{other}/lenders/{ln['id']}",
                               json={"status": "Declined"})
    assert wrong.status_code == 404

    # The flat route's update stays OFF (scope-bypass surface).
    flat = await client.patch(f"/v1/syndication-lenders/{ln['id']}",
                              json={"status": "Declined"})
    assert flat.status_code == 405


async def test_tenant_settings_get_put(client: AsyncClient):
    # defaults come back even with nothing stored
    r = await client.get("/v1/settings")
    assert r.status_code == 200
    assert r.json()["settings"]["thresholds"]["staleLead"] == 21

    put = await client.put("/v1/settings", json={"settings": {"thresholds": {"staleLead": 30}}})
    assert put.status_code == 200
    # overridden value wins; unspecified defaults still merged in
    merged = put.json()["settings"]["thresholds"]
    assert merged["staleLead"] == 30 and merged["lendRed"] == 14


async def test_lender_matrix_derived(client: AsyncClient):
    eid = await _entity(client, "MTX1")
    r = await client.post("/v1/syndication", json={"entity_id": eid})
    sid = r.json()["id"]
    await client.post(f"/v1/syndication/{sid}/lenders",
                      json={"lender_name": "HDFC", "status": "Sanctioned", "chased_date": "2026-06-01"})
    m = await client.get(f"/v1/entities/{eid}/lender-matrix")
    assert m.status_code == 200
    lenders = {x["lender_name"]: x for x in m.json()["lenders"]}
    assert "HDFC" in lenders
    assert lenders["HDFC"]["latest"]["status"] == "Sanctioned"


async def test_new_reference_categories():
    # The seed/bootstrap loads REF_VALUES into ref_values (served at /v1/ref); assert the
    # newly-added categories that back real fields are present in that source of truth.
    from app.seed.refdata import REF_VALUES

    for cat in ["Syndication Type", "Mandate Status 3", "Yes/No", "Terminal (Lending)",
                "RM", "Analyst", "Financial Section", "Scale", "Waiver Status"]:
        assert cat in REF_VALUES, f"missing ref category {cat}"
    assert REF_VALUES["Yes/No"] == ["Yes", "No"]
