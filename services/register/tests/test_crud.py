"""CRUD, validation, auth and pagination behaviour."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_entity(client: AsyncClient, code: str = "E1", **extra) -> dict:
    body = {"code": code, "legal_name": f"{code} Pvt Ltd", **extra}
    r = await client.post("/v1/entities", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_api_key(client: AsyncClient):
    r = await client.get("/v1/entities", headers={"X-API-Key": ""})
    assert r.status_code == 401


async def test_create_get_update_delete(client: AsyncClient):
    ent = await _create_entity(client, "ACME", sector="Solar - General", lens="Mitigation")
    assert ent["version"] == 1
    eid = ent["id"]

    r = await client.get(f"/v1/entities/{eid}")
    assert r.status_code == 200
    assert r.json()["legal_name"] == "ACME Pvt Ltd"
    assert r.headers["ETag"] == '"1"'

    r = await client.patch(f"/v1/entities/{eid}", json={"state": "KA"})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["state"] == "KA"

    r = await client.delete(f"/v1/entities/{eid}")
    assert r.status_code == 204

    r = await client.get(f"/v1/entities/{eid}")
    assert r.status_code == 404

    r = await client.post(f"/v1/entities/{eid}/restore")
    assert r.status_code == 200
    assert r.json()["deleted_at"] is None


async def test_unknown_field_rejected(client: AsyncClient):
    r = await client.post("/v1/entities", json={"code": "X", "legal_name": "X", "bogus": 1})
    assert r.status_code == 422


async def test_duplicate_code_conflict(client: AsyncClient):
    await _create_entity(client, "DUP")
    r = await client.post("/v1/entities", json={"code": "DUP", "legal_name": "again"})
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "integrity_error"


async def test_missing_required_field(client: AsyncClient):
    r = await client.post("/v1/entities", json={"code": "NONAME"})
    assert r.status_code == 422


async def test_list_search_and_filter(client: AsyncClient):
    await _create_entity(client, "SOLARCO", sector="Solar - General")
    await _create_entity(client, "WATERCO", sector="Water Treatment / WASH")
    r = await client.get("/v1/entities", params={"sector": "Solar - General", "with_total": True})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "SOLARCO"

    r = await client.get("/v1/entities", params={"q": "WATERCO"})
    assert r.json()["count"] == 1


async def test_list_refuses_malformed_uuid_filter(client: AsyncClient):
    """The literal string "null" (an unset client variable) aimed at a UUID filter is
    the CALLER's error: 422 with the offending filter named — never a 500 from the
    driver's bind codec (which is how it used to surface, killing VOX runs)."""
    for bad in ("null", "undefined", "not-a-uuid"):
        r = await client.get("/v1/leads", params={"entity_id": bad})
        assert r.status_code == 422, f"{bad!r}: {r.status_code} {r.text}"
        assert "entity_id" in r.text


async def test_keyset_pagination(client: AsyncClient):
    for i in range(25):
        await _create_entity(client, f"P{i:03d}")
    seen: set[str] = set()
    cursor = None
    pages = 0
    while True:
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/v1/entities", params=params)
        body = r.json()
        for item in body["items"]:
            seen.add(item["id"])
        cursor = body["next_cursor"]
        pages += 1
        if not cursor:
            break
    assert len(seen) == 25
    assert pages == 3


async def test_deal_requires_valid_entity(client: AsyncClient):
    # entity_id pointing nowhere → integrity error (FK), never a silent orphan.
    r = await client.post("/v1/deals", json={"entity_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code in (409, 422)


async def test_dossier(client: AsyncClient):
    ent = await _create_entity(client, "DOSS")
    eid = ent["id"]
    await client.post("/v1/deals", json={"entity_id": eid, "is_lending": True, "code": "DOSS"})
    r = await client.get(f"/v1/entities/{eid}/dossier")
    assert r.status_code == 200
    assert r.json()["counts"]["deals"] == 1


async def test_lead_no_is_auto_assigned_when_omitted(client: AsyncClient):
    """Creating leads without a lead_no never 409s: the register mints the next free
    LD-NNN per tenant — the DESK'S numbering, so the allocator can see the ledger's own
    LD-207 and continue past it. Explicit numbers pass through and the sequence skips
    them."""
    r1 = await client.post("/v1/leads", json={"company": "Auto One"})
    assert r1.status_code == 201, r1.text
    r2 = await client.post("/v1/leads", json={"company": "Auto Two"})
    assert r2.status_code == 201, r2.text
    n1, n2 = r1.json()["lead_no"], r2.json()["lead_no"]
    assert n1 == "LD-001" and n2 == "LD-002"
    # An explicit number is honoured verbatim...
    r3 = await client.post("/v1/leads", json={"company": "Manual", "lead_no": "LD-007"})
    assert r3.status_code == 201 and r3.json()["lead_no"] == "LD-007"
    # ...and the generator continues past it instead of colliding.
    r4 = await client.post("/v1/leads", json={"company": "Auto Three"})
    assert r4.status_code == 201 and r4.json()["lead_no"] == "LD-008"
    # Reusing a taken number still fails loudly — the natural key stays protected.
    r5 = await client.post("/v1/leads", json={"company": "Dup", "lead_no": "LD-007"})
    assert r5.status_code == 409
    assert r5.json()["error"]["constraint"] == "leads_tenant_lead_no"


async def test_audit_rows_carry_values_label_and_filters(client: AsyncClient):
    """The Activity screen renders from the audit row ALONE: an update records each
    changed field's before→after and the row's human label; the read side filters by
    actor/action/time; and the UI's post-login call lands a 'signin' session row."""
    ent = await _create_entity(client, "AUD1", state="KA")
    r = await client.patch(f"/v1/entities/{ent['id']}", json={"state": "TN"})
    assert r.status_code == 200
    r = await client.get("/v1/audit", params={
        "resource_type": "entities", "resource_id": ent["id"], "action": "update"})
    assert r.status_code == 200
    row = r.json()[0]
    assert row["changes"]["values"]["state"] == {"from": "KA", "to": "TN"}
    assert row["changes"]["label"] == "AUD1"
    # The create row is labelled too, and the action filter separates the two.
    r = await client.get("/v1/audit", params={
        "resource_type": "entities", "resource_id": ent["id"], "action": "create"})
    assert r.json()[0]["changes"]["label"] == "AUD1"
    # Session events: recorded as the CALLER's own audited row.
    r = await client.post("/v1/session-events", json={"event": "signin"})
    assert r.status_code == 201 and r.json()["recorded"] is True
    r = await client.get("/v1/audit", params={"resource_type": "session",
                                              "action": "signin"})
    assert r.status_code == 200 and len(r.json()) >= 1
    r = await client.post("/v1/session-events", json={"event": "reboot"})
    assert r.status_code == 422


async def test_entity_lifecycle_is_the_vistaar_journey(client: AsyncClient):
    """The client RELATIONSHIP journey (ATLAS 'Vistaar journey') is its own field —
    distinct from register_status (the origination marker) — and its vocabulary is
    served from refdata so the UI dropdown never hard-codes it."""
    import uuid as _uuid
    code = "LC" + _uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities", json={
        "code": code, "legal_name": "Lifecycle Co", "entity_type": "Company",
        "register_status": "Pipeline", "lifecycle": "Prospect"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert (row["register_status"], row["lifecycle"]) == ("Pipeline", "Prospect")
    r = await client.patch(f"/v1/entities/{row['id']}",
                           json={"lifecycle": "Vistaar — Expansion"})
    assert r.status_code == 200 and r.json()["lifecycle"] == "Vistaar — Expansion"
    # The vocabulary ships as refdata (served by /v1/ref on a bootstrapped deployment).
    from app.seed.refdata import REF_VALUES
    assert REF_VALUES["Entity Lifecycle"] == [
        "Prospect", "Onboarded", "Active", "Serviced", "Vistaar — Expansion", "Dormant"]


async def test_tracker_lines_and_deals_get_a_quotable_number(client: AsyncClient):
    """Every row a person TALKS about gets a number they can say.

    Only the seed importers used to assign these, so a deal or a tracker line born from
    the live path carried NULL: the Deals grid showed a blank Group Code and every audit
    row for it recorded "label -> null". The lines continue the seed's own series, and a
    deal takes its CLIENT's code — which is exactly what the grid labels Group Code.
    """
    ent = (await client.post("/v1/entities", json={
        "code": f"NUM{uuid.uuid4().hex[:6].upper()}", "legal_name": "Numbering Co"})).json()

    deal = (await client.post("/v1/deals", json={
        "entity_id": ent["id"], "product_type": "Term Loan"})).json()
    assert deal["deal_no"] == ent["code"], deal

    # A second facility for the SAME company cannot collide on (tenant, deal_no).
    deal2 = (await client.post("/v1/deals", json={
        "entity_id": ent["id"], "product_type": "Working Capital"})).json()
    assert deal2["deal_no"] == f"{ent['code']}-2", deal2

    for path, prefix in (("/v1/lending", "L"), ("/v1/syndication", "S")):
        r = await client.post(path, json={"entity_id": ent["id"], "deal_id": deal["id"]})
        assert r.status_code == 201, r.text
        no = r.json()["tracker_no"]
        assert no and no.startswith(prefix) and no[len(prefix):].isdigit(), no

    # A caller-supplied number always wins over the allocator.
    mine = await client.post("/v1/lending", json={
        "entity_id": ent["id"], "tracker_no": "L900"})
    assert mine.status_code == 201 and mine.json()["tracker_no"] == "L900"


async def test_conversion_numbers_the_rows_it_creates(client: AsyncClient):
    """The path that actually matters: push-to-deals must not leave unnamed rows behind."""
    ent = (await client.post("/v1/entities", json={
        "code": f"CNV{uuid.uuid4().hex[:6].upper()}", "legal_name": "Convert Numbering"})).json()
    assert (await client.post("/v1/people", json={
        "name": "Asha", "full_name": "Asha Nair", "role": "BDRM"})).status_code == 201
    lead = (await client.post("/v1/leads", json={
        "company": "Convert Numbering", "entity_id": ent["id"], "status": "Active",
              "temperature": "Hot"})).json()
    r = await client.post(f"/v1/leads/{lead['id']}/convert", json={
        "is_lending": True, "is_syndication": True, "product_type": "Term Loan",
        "amount_cr": 10, "rm": "Asha"})
    assert r.status_code == 200, r.text
    body = r.json()
    deal = (await client.get(f"/v1/deals/{body['deal_id']}")).json()
    assert deal["deal_no"] == ent["code"], deal
    lending = (await client.get(f"/v1/lending/{body['lending_id']}")).json()
    assert lending["tracker_no"], lending
    syn = (await client.get(f"/v1/syndication/{body['syndication_id']}")).json()
    assert syn["tracker_no"], syn
