"""VocX → Register, end to end: capture, roll-up, exactly-once replay."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_touchpoint_lands_as_interaction(vocx: AsyncClient, register_direct: AsyncClient):
    code = f"VX{uuid.uuid4().hex[:6].upper()}"
    eid = (await register_direct.post("/v1/entities",
                                      json={"code": code, "legal_name": code})).json()["id"]
    r = await vocx.post("/v1/touchpoints", json={
        "subject_type": "Entity", "subject_id": eid,
        "interaction_type": "In-Person Meeting",
        "performed_by": "Shubh", "transcript": "Promoter bullish on Q3 pipeline",
        "language": "hinglish", "gps_lat": 12.9716, "gps_lng": 77.5946,
        "key_intel": {"sentiment": "positive"},
        "capture_id": f"rec-{code}-001"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "VocX" and body["entity_id"] == eid

    tl = (await register_direct.get(f"/v1/entities/{eid}/interactions")).json()
    assert len(tl) == 1
    assert tl[0]["source"] == "VocX" and tl[0]["transcript"].startswith("Promoter")
    assert tl[0]["gps_lat"] == 12.9716


async def test_retried_upload_is_exactly_once(vocx: AsyncClient, register_direct: AsyncClient):
    code = f"VX{uuid.uuid4().hex[:6].upper()}"
    eid = (await register_direct.post("/v1/entities",
                                      json={"code": code, "legal_name": code})).json()["id"]
    payload = {"subject_type": "Entity", "subject_id": eid,
               "interaction_type": "Phone Call", "notes": "flaky uplink",
               "capture_id": f"rec-{code}-042"}
    r1 = await vocx.post("/v1/touchpoints", json=payload)
    r2 = await vocx.post("/v1/touchpoints", json=payload)   # same capture_id → replay
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["interaction_id"] == r2.json()["interaction_id"]
    tl = (await register_direct.get(f"/v1/entities/{eid}/interactions")).json()
    assert len(tl) == 1  # no duplicate


async def test_bad_subject_type_rejected(vocx: AsyncClient):
    r = await vocx.post("/v1/touchpoints", json={
        "subject_type": "Nonsense", "subject_id": str(uuid.uuid4())})
    assert r.status_code == 422
