"""The authoritative CP/CS checklist: maker-checker, CP/CS distinction, and waiver / CS-deferment
controls. cp_cs_completion is minted only from an Approved checklist."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}
DEAL_ANALYST = {"X-User-Email": "da@evamfinance.com", "X-User-Roles": "Deal Analyst"}


async def _lending(client) -> str:  # noqa: ANN001
    code = "CPC" + uuid.uuid4().hex[:6].upper()
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": "CPCS Co",
                                   "entity_type": "Company"})).json()["id"]
    return (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": "Diligence"})).json()["id"]


async def test_cpcs_maker_checker_and_evidence_minting(client):
    lid = await _lending(client)
    # Maker (senior) prepares a completed checklist: a CP met, and a CS waived with a reason.
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Completed"},
                        {"key": "insurance", "condition_type": "CS", "status": "Waived",
                         "reason": "covered by group policy", "expiry_date": "2026-12-31"}]},
        headers=ADMIN)
    assert chk.status_code == 201, chk.text
    cid = chk.json()["id"]

    # The SAME person cannot approve their own checklist (maker-checker).
    self_appr = await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve", headers=ADMIN)
    assert self_appr.status_code == 422 and "different checker" in self_appr.text.lower()

    # Before approval, cp_cs_completion cannot be minted.
    early = await client.post("/v1/evidence", json={
        "subject_type": "Lending", "subject_id": lid, "evidence_kind": "cp_cs_completion",
        "reference": "cpcs/1", "sha256": "a" * 64, "decision_ref": cid}, headers=ADMIN)
    assert early.status_code == 422 and "not 'approved'" in early.text.lower()

    # A different checker approves it, then cp_cs_completion is VERIFIED against it.
    appr = await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve", headers=CREDIT_HEAD)
    assert appr.status_code == 200 and appr.json()["status"] == "Approved"
    ev = await client.post("/v1/evidence", json={
        "subject_type": "Lending", "subject_id": lid, "evidence_kind": "cp_cs_completion",
        "reference": "cpcs/1", "sha256": "a" * 64, "decision_ref": cid}, headers=ADMIN)
    assert ev.status_code == 201, ev.text
    # A cp_cs_completion with NO checklist reference is refused (no longer caller-attachable).
    bare = await client.post("/v1/evidence", json={
        "subject_type": "Lending", "subject_id": lid, "evidence_kind": "cp_cs_completion",
        "reference": "cpcs/2", "sha256": "a" * 64, "workflow_id": "wf", "run_id": "run"},
        headers=ADMIN)
    assert bare.status_code == 422 and "checklist" in bare.text.lower()


async def test_cpcs_empty_checklist_is_rejected(client):
    lid = await _lending(client)
    r = await client.post("/v1/internal/cpcs-checklists",
                          json={"lending_id": lid, "items": []}, headers=ADMIN)
    assert r.status_code == 422, r.text


async def test_cpcs_completed_requires_all_required_cp_items(client):
    lid = await _lending(client)
    r = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "charge", "condition_type": "CP", "required": True,
                         "status": "Pending"}]}, headers=ADMIN)
    assert r.status_code == 422 and "pending" in r.text.lower()


async def test_cpcs_waiver_requires_reason_and_senior_authority(client):
    lid = await _lending(client)
    # A waiver with no reason is refused.
    no_reason = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Draft",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Waived"}]},
        headers=ADMIN)
    assert no_reason.status_code == 422 and "reason" in no_reason.text.lower()
    # A non-senior maker (Deal Analyst) may prepare, but may NOT waive a condition.
    da_waive = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Draft",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Waived",
                         "reason": "x"}]}, headers=DEAL_ANALYST)
    assert da_waive.status_code == 403 and "waive" in da_waive.text.lower()


async def test_cpcs_deferral_requires_cp_reason_and_expiry(client):
    lid = await _lending(client)
    # 'Deferred as CS' on a CS item is nonsensical → refused.
    on_cs = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Draft",
              "items": [{"key": "x", "condition_type": "CS", "status": "Deferred as CS",
                         "reason": "r", "expiry_date": "2026-12-31"}]}, headers=ADMIN)
    assert on_cs.status_code == 422 and "only a cp" in on_cs.text.lower()
    # A CP deferral with no expiry → refused.
    no_expiry = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Draft",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Deferred as CS",
                         "reason": "post-close registration"}]}, headers=ADMIN)
    assert no_expiry.status_code == 422 and "expiry" in no_expiry.text.lower()
    # A well-formed CP deferral (senior, reason, expiry) is accepted.
    ok = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Deferred as CS",
                         "reason": "post-close registration", "expiry_date": "2026-12-31"}]},
        headers=ADMIN)
    assert ok.status_code == 201, ok.text


async def test_cpcs_reject_is_terminal_and_four_eyed(client):
    """The third checker verb: REJECT breaks the loop (vs RETURN, which continues it).
    Note mandatory, maker≠checker, only a maker-finished ('Completed') version can be
    rejected, and a rejected version leaves the checker queue permanently."""
    lid = await _lending(client)
    cid = (await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "charge", "condition_type": "CP", "status": "Completed"}]},
        headers=ADMIN)).json()["id"]
    # Note is mandatory; the maker cannot reject their own checklist.
    assert (await client.post(f"/v1/internal/cpcs-checklists/{cid}/reject",
                              json={}, headers=CREDIT_HEAD)).status_code == 422
    self_rej = await client.post(f"/v1/internal/cpcs-checklists/{cid}/reject",
                                 json={"note": "no"}, headers=ADMIN)
    assert self_rej.status_code == 422 and "different checker" in self_rej.text.lower()
    r = await client.post(f"/v1/internal/cpcs-checklists/{cid}/reject",
                          json={"note": "Security structure unacceptable — do not proceed."},
                          headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Rejected"
    # Terminal: it can be neither approved nor re-rejected...
    assert (await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve",
                              headers=CREDIT_HEAD)).status_code == 409
    assert (await client.post(f"/v1/internal/cpcs-checklists/{cid}/reject",
                              json={"note": "again"}, headers=CREDIT_HEAD)).status_code == 409
    # ...and it is out of the checker queue.
    q = await client.get("/v1/internal/cpcs-checklists",
                         params={"lending_id": lid, "status": "Completed"}, headers=ADMIN)
    assert all(row["id"] != cid for row in q.json())
