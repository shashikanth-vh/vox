"""R10 hardening: creation-time lifecycle enforcement, composite-read isolation, and the
unnamed→named gateway key (no own authority)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

PULSE_KEY = "svc-pulse-key-r10"
GATEWAY_KEY = "svc-gateway-key-r10"


@pytest.fixture
def _service_keys():
    s = get_settings()
    s.service_api_keys = {PULSE_KEY: "svc_pulse", GATEWAY_KEY: "svc_gateway"}
    try:
        yield
    finally:
        s.service_api_keys = {}


# --------------------------------------------------------------------------- #
# Creation-time lifecycle: a lead can't be born in a terminal state
# --------------------------------------------------------------------------- #
async def test_lead_cannot_be_created_converted(client: AsyncClient):
    ent = (await client.post("/v1/entities",
                             json={"code": "R10-LC", "legal_name": "R10 LC"})).json()
    bad = await client.post("/v1/leads", json={
        "company": "R10", "entity_id": ent["id"], "status": "Converted"})
    assert bad.status_code == 422, bad.text
    assert "cannot be created" in bad.text.lower()
    # A valid initial state is accepted.
    ok = await client.post("/v1/leads", json={
        "company": "R10", "entity_id": ent["id"], "status": "Active"})
    assert ok.status_code == 201, ok.text


# --------------------------------------------------------------------------- #
# Composite company reads need a capability NO service holds on its own key
# --------------------------------------------------------------------------- #
async def test_composite_reads_deny_entity_matching_service(client: AsyncClient,
                                                            _service_keys):
    ent = (await client.post("/v1/entities",
                             json={"code": "R10-CMP", "legal_name": "R10 Cmp"})).json()
    eid = ent["id"]
    # svc_pulse MAY match entities (its read grant covers /v1/entities)…
    assert (await client.get(f"/v1/entities/{eid}",
                             headers={"X-API-Key": PULSE_KEY})).status_code == 200
    # …but MAY NOT pull the company's full footprint on its own key.
    dossier = await client.get(f"/v1/entities/{eid}/dossier",
                               headers={"X-API-Key": PULSE_KEY})
    assert dossier.status_code == 403, dossier.text
    matrix = await client.get(f"/v1/entities/{eid}/lender-matrix",
                              headers={"X-API-Key": PULSE_KEY})
    assert matrix.status_code == 403, matrix.text


# --------------------------------------------------------------------------- #
# svc_gateway carries NO authority of its own (pure delegation transport)
# --------------------------------------------------------------------------- #
async def test_gateway_key_has_no_own_authority(client: AsyncClient, _service_keys):
    # On its own key (no user context) it can neither read the data plane…
    assert (await client.get("/v1/leads",
                             headers={"X-API-Key": GATEWAY_KEY})).status_code == 403
    assert (await client.get("/v1/entities",
                             headers={"X-API-Key": GATEWAY_KEY})).status_code == 403
    # …nor create anything.
    ent = (await client.post("/v1/entities",
                             json={"code": "R10-GW", "legal_name": "R10 GW"})).json()
    denied = await client.post("/v1/leads", json={"company": "x", "entity_id": ent["id"]},
                               headers={"X-API-Key": GATEWAY_KEY})
    assert denied.status_code == 403, denied.text
