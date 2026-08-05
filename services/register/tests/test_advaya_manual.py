"""The MANUAL Advaya attestation lane: an authorised human finishes the flow in PRISM
on Advaya's behalf, citing the offline artefact — same machinery as the machine lane,
human identity + authority + provenance on top."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_handover import ADMIN, CREDIT_HEAD, _entity, _prepare_body, _ready_lending

pytestmark = pytest.mark.asyncio

RM = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM"}


async def _submitted_line(client: AsyncClient) -> str:
    """Entity → Lending at Ready for Disbursement → package prepared/approved/submitted."""
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    assert (await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                              headers=ADMIN)).status_code == 201
    assert (await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                              headers=CREDIT_HEAD)).json()["status"] == "Approved"
    sub = await client.post(f"/v1/internal/handover-packages/{lid}/submit",
                            headers=CREDIT_HEAD)
    assert sub.status_code == 200 and sub.json()["status"] == "Submitted"
    return lid


async def test_manual_attestation_full_boundary(client: AsyncClient):
    """accepted → package settles with the cited reference; disbursed → a PENDING
    BOOKING attributed to the human — nothing moves until the LMS Management approves;
    then actuals + stage 'Disbursed' land in the approval's transaction."""
    lid = await _submitted_line(client)

    acc = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "accepted", "reference": "ADV-LTR/77",
                                  "note": "Acceptance letter received by email."})
    assert acc.status_code == 201, acc.text
    body = acc.json()
    assert body["source"] == "manual-attestation"
    assert body["recorded_by"] == "ch@evamfinance.com"
    assert body["handoff"]["status"] == "Accepted"
    pkg = (await client.get(f"/v1/lending/{lid}/handover-package")).json()
    assert pkg["status"] == "Accepted" and pkg["advaya_reference"] == "ADV-LTR/77"

    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-0042",
                                  "amount_cr": 5.0, "disbursed_on": "2026-08-01"})
    assert dis.status_code == 201, dis.text
    tranche = dis.json()["tranche"]
    assert tranche["amount"] == 5.0
    # The human lane records a PENDING booking — the line has NOT moved yet.
    assert tranche["booking_status"] == "Pending"
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Ready for Disbursement"
    assert not line.get("disbursed_amount")

    # Replaying the SAME reference is idempotent — the offline artefact keys the write.
    again = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                              json={"event": "disbursed", "reference": "UTR-0042",
                                    "amount_cr": 5.0})
    assert again.status_code == 201
    assert again.json()["tranche"]["id"] == tranche["id"]

    # The LMS MANAGEMENT approves the booking — actuals, stage and account land now.
    authorizer = {"X-User-Email": "authz@evamfinance.com",
                  "X-User-Roles": "LMS Management"}
    ok = await client.post(f"/v1/lending/{lid}/tranches/{tranche['id']}/book",
                           json={"action": "approve"}, headers=authorizer)
    assert ok.status_code == 200, ok.text
    assert ok.json()["booking_status"] == "Booked"
    assert ok.json()["booked_by"] == "authz@evamfinance.com"
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Disbursed"
    assert float(line["disbursed_amount"]) == 5.0
    assert line["disbursement_date"] == "2026-08-01"
    # Provenance survives on the stage history: the approval moved the line.
    assert line["stage_history"][-1]["source"] == "lms-booking-approval"


async def test_manual_attestation_guards(client: AsyncClient):
    lid = await _submitted_line(client)

    # No authority → refused (BDRM cannot attest a money-movement outcome).
    denied = await client.post(f"/v1/lending/{lid}/advaya-events", headers=RM,
                               json={"event": "accepted", "reference": "ADV-LTR/1"})
    assert denied.status_code == 403, denied.text

    # A 'disbursed' event without the amount is refused with the field named.
    bad = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-1"})
    assert bad.status_code == 422 and "amount_cr" in bad.text

    # The reference (Advaya's artefact) is mandatory — schema-level.
    noref = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                              json={"event": "accepted"})
    assert noref.status_code == 422

    # Disbursement before acceptance stays refused — same boundary as the machine lane.
    early = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                              json={"event": "disbursed", "reference": "UTR-2",
                                    "amount_cr": 1.0})
    assert early.status_code == 409 and "accepted" in early.text.lower()

    # Settle it, then a CONTRADICTORY attestation is refused (single-winner package).
    ok = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                           json={"event": "accepted", "reference": "ADV-LTR/2"})
    assert ok.status_code == 201
    flip = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                             json={"event": "rejected", "reference": "ADV-LTR/3"})
    assert flip.status_code == 409


async def test_manual_lane_refuses_service_keys(client: AsyncClient, monkeypatch):
    """A machine presenting a service key is refused here — machines use the service
    lane; the manual lane exists to attribute the attestation to a person."""
    from httpx import ASGITransport, AsyncClient as AC

    from app.core.config import get_settings
    from app.main import create_app as _mk

    lid = await _submitted_line(client)
    monkeypatch.setattr(get_settings(), "service_api_keys", {"adv-key": "svc_advaya"})
    svc = {"X-API-Key": "adv-key", "X-Tenant": "EVAM", "X-Actor": "advaya"}
    async with AC(transport=ASGITransport(app=_mk()), base_url="http://adv",
                  headers=svc) as adv:
        r = await adv.post(f"/v1/lending/{lid}/advaya-events",
                           json={"event": "accepted", "reference": "ADV-LTR/9"})
    assert r.status_code == 403 and "human" in r.text.lower()
