"""Only a HOT lead converts to a deal.

Temperature is the desk's own qualification gate. A deal commits the book's attention —
an analyst, a CAM, a committee slot — and the desk rates a lead Hot before spending any
of it. A Warm or Cold lead reaching the deal register makes the pipeline count stop
meaning anything, so the refusal lives in the REGISTER rather than in a hidden button:
the API is reachable from VocX, the orchestrator and a curl, and only one of those has
a screen.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

BODY = {"is_lending": True, "product_type": "Term Loan", "amount_cr": 5}


async def _lead(client: AsyncClient, temperature: str | None) -> str:
    ent = (await client.post("/v1/entities", json={
        "code": f"HG-{uuid.uuid4().hex[:6]}", "legal_name": "Hot Gate Co"})).json()
    body: dict = {"company": "Hot Gate Co", "entity_id": ent["id"], "status": "Active"}
    if temperature is not None:
        body["temperature"] = temperature
    r = await client.post("/v1/leads", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_a_hot_lead_converts(client: AsyncClient):
    lid = await _lead(client, "Hot")
    r = await client.post(f"/v1/leads/{lid}/convert", json=BODY)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("temp", ["Warm", "Cold"])
async def test_a_lead_that_is_not_hot_is_refused(client: AsyncClient, temp: str):
    lid = await _lead(client, temp)
    r = await client.post(f"/v1/leads/{lid}/convert", json=BODY)
    assert r.status_code == 409, r.text
    # The refusal has to name the state AND the way out, or the desk just retries.
    assert temp in r.text and "HOT" in r.text
    assert "set the temperature to hot" in r.text.lower()


async def test_an_unrated_lead_is_refused_rather_than_assumed(client: AsyncClient):
    """No temperature is not a quiet yes. A lead nobody rated has not been qualified."""
    lid = await _lead(client, None)
    r = await client.post(f"/v1/leads/{lid}/convert", json=BODY)
    assert r.status_code == 409, r.text
    assert "unrated" in r.text


async def test_the_rating_is_read_case_and_space_insensitively(client: AsyncClient):
    """'hot' typed by an import or an integration is the same qualification as 'Hot' —
    refusing it would be pedantry standing in front of a real decision."""
    lid = await _lead(client, "  hot ")
    r = await client.post(f"/v1/leads/{lid}/convert", json=BODY)
    assert r.status_code == 200, r.text


async def test_warming_a_lead_up_lets_it_through(client: AsyncClient):
    """The way out the message names actually works."""
    lid = await _lead(client, "Cold")
    assert (await client.post(f"/v1/leads/{lid}/convert", json=BODY)).status_code == 409
    assert (await client.patch(f"/v1/leads/{lid}",
                               json={"temperature": "Hot"})).status_code == 200
    assert (await client.post(f"/v1/leads/{lid}/convert", json=BODY)).status_code == 200
