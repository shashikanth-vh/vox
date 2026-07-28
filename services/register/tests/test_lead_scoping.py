"""Regression: the wrong-company VOX lead bug.

/v1/leads?entity_id= used to be silently IGNORED (entity_id was not in the lead's
filter whitelist), so 'the company's active lead' degraded to 'the newest active lead
in the tenant' — a VOX capture for EcoSoch could update GH2 Solar's lead. The filter
is now whitelisted, and the core repository REFUSES unknown filters instead of
dropping them. The unrelated lead is created LAST here on purpose: under the old
behaviour it is the row that would have been returned."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_leads_entity_filter_returns_only_that_company(client: AsyncClient):
    eco = (await client.post("/v1/entities", json={
        "code": f"ECO-{uuid.uuid4().hex[:6]}", "legal_name": "EcoSoch Solar"})).json()
    gh2 = (await client.post("/v1/entities", json={
        "code": f"GH2-{uuid.uuid4().hex[:6]}", "legal_name": "GH2 Solar"})).json()
    eco_lead = (await client.post("/v1/leads", json={
        "company": "EcoSoch Solar", "entity_id": eco["id"], "status": "Active"})).json()
    # The unrelated lead is NEWER — the row the old bug would have returned.
    gh2_lead = (await client.post("/v1/leads", json={
        "company": "GH2 Solar", "entity_id": gh2["id"], "status": "Active"})).json()

    rows = (await client.get("/v1/leads", params={
        "entity_id": eco["id"], "status": "Active", "limit": 50})).json()["items"]
    ids = {r["id"] for r in rows}
    assert eco_lead["id"] in ids
    assert gh2_lead["id"] not in ids
    assert all(r["entity_id"] == eco["id"] for r in rows)
