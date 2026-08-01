"""Increment 4 — Documents → CP/CS → Advaya completion.

* CP/CS return-to-maker: the checker sends a Completed checklist back with reasons; the
  returned version freezes (DB trigger) and the maker amends via the NEXT version.
* Handover return + re-prepare: the checker returns a Prepared package; the maker rebuilds
  it (fresh manifest + digest) and a different checker approves the rebuilt package.
* Tranche-level disbursement callbacks: service-principal only, idempotent per ref,
  append-only, bounded by the line's disbursement ceiling, with reconciliation totals.
"""

from __future__ import annotations

import uuid

import pytest
from tests.test_handover import ADMIN, CREDIT_HEAD, _entity, _prepare_body, _ready_lending

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

MGMT = {"X-User-Email": "mg@evamfinance.com", "X-User-Roles": "Management"}
SVC = {"X-API-Key": "trn-key"}


async def _lending(client) -> str:  # noqa: ANN001
    eid = await _entity(client)
    return (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Diligence"})).json()["id"]


# --------------------------------------------------------------------------------------- #
# CP/CS return-to-maker + amendment versions
# --------------------------------------------------------------------------------------- #
async def test_cpcs_return_to_maker_and_amendment_version(client):
    lid = await _lending(client)
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"}]},
        headers=ADMIN)
    assert chk.status_code == 201, chk.text
    cid = chk.json()["id"]

    # The maker cannot return their own checklist; a return needs REASONS.
    r = await client.post(f"/v1/internal/cpcs-checklists/{cid}/return",
                          json={"note": "x"}, headers=ADMIN)
    assert r.status_code == 422 and "different checker" in r.text.lower()
    r = await client.post(f"/v1/internal/cpcs-checklists/{cid}/return", json={},
                          headers=CREDIT_HEAD)
    assert r.status_code == 422                       # note is mandatory

    r = await client.post(f"/v1/internal/cpcs-checklists/{cid}/return",
                          json={"note": "insurance CP missing"}, headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Returned"

    # A RETURNED checklist is frozen: it can no longer be approved…
    appr = await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve",
                             headers=CREDIT_HEAD)
    assert appr.status_code == 409, appr.text
    # …and the SAME version cannot be re-submitted — the amendment is the NEXT version.
    dup = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"}]},
        headers=ADMIN)
    assert dup.status_code == 409
    v2 = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed", "checklist_version": 2,
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"},
                        {"key": "insurance", "condition_type": "CP", "status": "Completed"}]},
        headers=ADMIN)
    assert v2.status_code == 201, v2.text
    appr = await client.post(f"/v1/internal/cpcs-checklists/{v2.json()['id']}/approve",
                             headers=CREDIT_HEAD)
    assert appr.status_code == 200 and appr.json()["status"] == "Approved"
    # The returned v1 is still on the record, untouched.
    v1 = await client.get(f"/v1/internal/cpcs-checklists/{cid}")
    assert v1.json()["status"] == "Returned" and v1.json()["checklist_version"] == 1


# --------------------------------------------------------------------------------------- #
# Handover return + re-prepare
# --------------------------------------------------------------------------------------- #
async def test_handover_return_reprepare_and_approve(client):
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    prep = await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                             headers=ADMIN)
    assert prep.status_code == 201, prep.text
    sha_v1 = prep.json()["package_sha256"]

    # Maker cannot return their own package; the checker's return needs reasons.
    r = await client.post(f"/v1/internal/handover-packages/{lid}/return",
                          json={"note": "x"}, headers=ADMIN)
    assert r.status_code == 422 and "different checker" in r.text.lower()
    r = await client.post(f"/v1/internal/handover-packages/{lid}/return",
                          json={"note": "recipient list incomplete"}, headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Returned"

    # A RETURNED package cannot be approved…
    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             headers=CREDIT_HEAD)
    assert appr.status_code == 200 and appr.json()["status"] == "Returned"
    # (approve on a non-Prepared package is the idempotent read-back, not an approval)
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Ready for Disbursement"    # the line never moved

    # …the maker RE-PREPARES: same row, fresh manifest + digest, back to Prepared.
    re_prep = await client.post(
        "/v1/internal/handover-packages",
        json=_prepare_body(lid, recipient="advaya-ops-corrected"), headers=ADMIN)
    assert re_prep.status_code == 201, re_prep.text
    assert re_prep.json()["status"] == "Prepared"
    assert re_prep.json()["recipient"] == "advaya-ops-corrected"
    assert re_prep.json()["package_sha256"] != sha_v1   # the manifest genuinely changed

    # A DIFFERENT checker approves the rebuilt package → the line is handed over.
    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             headers=CREDIT_HEAD)
    assert appr.status_code == 200 and appr.json()["status"] == "HandedOver"
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Disbursed"


# --------------------------------------------------------------------------------------- #
# Tranche-level disbursement callbacks
# --------------------------------------------------------------------------------------- #
async def _disbursed_line(client) -> str:  # noqa: ANN001
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    assert (await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                              headers=ADMIN)).status_code == 201
    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             headers=CREDIT_HEAD)
    assert appr.status_code == 200 and appr.json()["status"] == "HandedOver"
    return lid


async def test_tranche_callbacks_idempotent_bounded_and_reconciled(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"trn-key": "svc_workflows"})
    lid = await _disbursed_line(client)      # ceiling: proposed_disbursement_amount = 8.0

    # A human key cannot record tranches — machine plumbing only.
    r = await client.post(f"/v1/internal/lending/{lid}/tranches",
                          json={"tranche_ref": "T1", "amount": 5.0}, headers=ADMIN)
    assert r.status_code == 403

    r = await client.post(f"/v1/internal/lending/{lid}/tranches",
                          json={"tranche_ref": "T1", "amount": 5.0,
                                "disbursed_on": "2026-03-05",
                                "advaya_reference": "ADV-001"}, headers=SVC)
    assert r.status_code == 201, r.text
    # Idempotent replay returns the ORIGINAL row; a different amount on the same ref is 409.
    again = await client.post(f"/v1/internal/lending/{lid}/tranches",
                              json={"tranche_ref": "T1", "amount": 5.0}, headers=SVC)
    assert again.status_code == 201 and again.json()["id"] == r.json()["id"]
    conflict = await client.post(f"/v1/internal/lending/{lid}/tranches",
                                 json={"tranche_ref": "T1", "amount": 6.0}, headers=SVC)
    assert conflict.status_code == 409

    # Over-disbursement is refused loudly (5.0 + 4.0 > 8.0 ceiling)…
    over = await client.post(f"/v1/internal/lending/{lid}/tranches",
                             json={"tranche_ref": "T2", "amount": 4.0}, headers=SVC)
    assert over.status_code == 422 and "exceed" in over.text.lower()
    # …a within-ceiling second tranche lands, and the totals reconcile.
    ok = await client.post(f"/v1/internal/lending/{lid}/tranches",
                           json={"tranche_ref": "T2", "amount": 3.0}, headers=SVC)
    assert ok.status_code == 201
    totals = (await client.get(f"/v1/internal/lending/{lid}/tranches", headers=SVC)).json()
    assert totals["total_disbursed"] == 8.0 and totals["ceiling"] == 8.0
    assert totals["fully_disbursed"] is True and totals["remaining"] == 0.0
    assert [t["tranche_ref"] for t in totals["items"]] == ["T1", "T2"]


async def test_tranches_refused_before_disbursed(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"trn-key": "svc_workflows"})
    lid = await _lending(client)             # still at 'Diligence'
    r = await client.post(f"/v1/internal/lending/{lid}/tranches",
                          json={"tranche_ref": f"T-{uuid.uuid4().hex[:6]}", "amount": 1.0},
                          headers=SVC)
    assert r.status_code == 409 and "disbursed" in r.text.lower()
