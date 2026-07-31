"""The durable Advaya handover package + the disabled dormant acknowledgement path.

Covers the reviewer's three handover P1s at the Register boundary:
* the handover creates an IMMUTABLE package snapshot and advances the stage only AFTER it exists;
* authoritative amounts come from the Lending row, not the caller;
* the dormant Advaya acknowledgement path is not executable by default.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}

_DECISION_SQL = (
    "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, subject_id, "
    "run_id, decided_by, decided_by_id, roles, tenant_id) "
    "SELECT :wf, 'Approved', 'Lending', CAST(:sid AS varchar), 'run-1', "
    "'ch@evamfinance.com', 'u-1', CAST('[\"Credit Head\"]' AS jsonb), tenant_id "  # noqa: S608
    "FROM lending_tracker WHERE id = CAST(:sid AS uuid)")


async def _entity(client) -> str:  # noqa: ANN001
    code = "HND" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities",
                          json={"code": code, "legal_name": "Handover Co", "entity_type": "Company"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _ready_lending(client, eid) -> str:  # noqa: ANN001
    """Walk a Lending line to 'Ready for Disbursement' with all gates satisfied."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    assert (await client.patch(f"/v1/lending/{lid}",
                               json={"stage": "Note Circulated"})).status_code == 200
    # Sanction evidence (committee-verified).
    wf = f"committee-{uuid.uuid4().hex[:12]}"
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text(_DECISION_SQL), {"wf": wf, "sid": lid})
        await s.commit()
    for ek in ("credit_committee_approval", "sanction_letter"):
        assert (await client.post(
            "/v1/evidence",
            json={"subject_type": "Lending", "subject_id": lid, "evidence_kind": ek,
                  "reference": f"{ek}/1", "sha256": "a" * 64, "decision_ref": wf},
            headers=ADMIN)).status_code == 201
    assert (await client.patch(f"/v1/lending/{lid}", json={"stage": "Sanctioned"})).status_code == 200
    # CP/CS: maker-checker checklist mints cp_cs_completion; executed_agreement.
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"}]}, headers=ADMIN)
    assert chk.status_code == 201, chk.text
    assert (await client.post(f"/v1/internal/cpcs-checklists/{chk.json()['id']}/approve",
                              headers=CREDIT_HEAD)).status_code == 200
    for kind, ref in (("cp_cs_completion", chk.json()["id"]), ("executed_agreement", None)):
        body = {"subject_type": "Lending", "subject_id": lid, "evidence_kind": kind,
                "reference": f"{kind}/1", "sha256": "a" * 64}
        if ref:
            body["decision_ref"] = ref
        else:
            # The cited workflow must RESOLVE to a decision recorded for this subject —
            # cite the committee decision seeded above (an invented id is refused).
            body |= {"workflow_id": wf, "run_id": "run-1"}
        assert (await client.post("/v1/evidence", json=body, headers=ADMIN)).status_code == 201
    assert (await client.patch(
        f"/v1/lending/{lid}",
        json={"stage": "CP/CS Completed"})).status_code == 200
    assert (await client.patch(
        f"/v1/lending/{lid}",
        json={"stage": "Ready for Disbursement", "proposed_disbursement_amount": 8.0,
              "proposed_disbursement_date": "2026-03-01"})).status_code == 200
    return lid


def _prepare_body(lid, **over):  # noqa: ANN001
    body = {"lending_id": lid, "delivery_method": "secure-email", "recipient": "advaya-ops",
            # The executed_agreement evidence is on file with digest "a"*64, so the package must
            # reference it (integrity reconciliation).
            "executed_document_refs": [{"reference": "fa/1", "sha256": "a" * 64}]}
    body.update(over)
    return body


async def test_two_phase_maker_checker_and_package_integrity(client):
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    await client.patch(f"/v1/lending/{lid}", json={"amount_cr": 20.0})

    # MAKER prepares — Prepared, NOT yet handed over; amounts + package digest server-side.
    prep = await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                             headers=ADMIN)
    assert prep.status_code == 201, prep.text
    pkg = prep.json()
    assert pkg["status"] == "Prepared"
    assert float(pkg["facility_amount"]) == 20.0 and float(pkg["proposed_disbursement_amount"]) == 8.0
    assert pkg["package_sha256"] and pkg["package_reference"]      # server-generated
    assert pkg["initiated_by"] == "admin@evamfinance.com"
    # Stage has NOT advanced yet — approval is required.
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"

    # The SAME person cannot approve their own handover (maker-checker).
    self_appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve", headers=ADMIN)
    assert self_appr.status_code == 422 and "different checker" in self_appr.text.lower()

    # A DIFFERENT checker approves → package HandedOver + stage advances (transactionally).
    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve", headers=CREDIT_HEAD)
    assert appr.status_code == 200, appr.text
    assert appr.json()["status"] == "HandedOver"
    assert appr.json()["approved_by"] == "ch@evamfinance.com"
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Disbursed"

    # The download returns the GENERATED document; its digest self-verifies server-side.
    dl = await client.post(f"/v1/lending/{lid}/handover-package/download")
    assert dl.status_code == 200 and dl.json()["package_sha256"] == pkg["package_sha256"]
    assert dl.json()["document_base64"]


async def test_prepare_requires_a_complete_and_reconciled_package(client):
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    # Empty executed_document_refs → rejected (pydantic min_length).
    empty = await client.post("/v1/internal/handover-packages",
                              json={"lending_id": lid, "delivery_method": "x", "recipient": "y",
                                    "executed_document_refs": []}, headers=ADMIN)
    assert empty.status_code == 422, empty.text
    # Refs that do NOT include the on-file executed_agreement digest → rejected.
    bad = await client.post("/v1/internal/handover-packages",
                            json=_prepare_body(lid, executed_document_refs=[
                                {"reference": "fa/1", "sha256": "e" * 64}]), headers=ADMIN)
    assert bad.status_code == 422 and "executed_agreement" in bad.text.lower()
    # A CP/CS version that doesn't match the approved checklist → rejected.
    mism = await client.post("/v1/internal/handover-packages",
                             json=_prepare_body(lid, cpcs_checklist_version=99), headers=ADMIN)
    assert mism.status_code == 422 and "checklist" in mism.text.lower()


async def test_handover_refused_before_ready_for_disbursement(client):
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    r = await client.post("/v1/internal/handover-packages", json=_prepare_body(lid), headers=ADMIN)
    assert r.status_code == 409, r.text
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Diligence"


async def test_dormant_advaya_acknowledgement_path_is_disabled(client):
    """With no Advaya integration, the internal handoff endpoint is not registered and the
    advaya_acknowledgement evidence is refused — the dormant path is not executable."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    # The internal advaya-handoffs router is not mounted (feature off) → 404, not 201.
    rec = await client.post("/v1/internal/advaya-handoffs", json={
        "handoff_key": f"advaya-handoff:{lid}", "lending_id": lid,
        "payload_sha256": "d" * 64, "status": "Accepted"}, headers=ADMIN)
    assert rec.status_code == 404, rec.text
    # And attaching the acknowledgement evidence is refused as disabled.
    ev = await client.post("/v1/evidence", json={
        "subject_type": "Lending", "subject_id": lid, "evidence_kind": "advaya_acknowledgement",
        "reference": "ack/1", "sha256": "d" * 64, "decision_ref": f"advaya-handoff:{lid}"},
        headers=ADMIN)
    assert ev.status_code in (403, 422), ev.text
    assert "advaya" in ev.text.lower()
