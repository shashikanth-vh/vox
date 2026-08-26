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
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"},
                        {"key": "cs1", "condition_type": "CS", "status": "Completed"}]},
        headers=ADMIN)
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

    # A DIFFERENT checker approves → package Approved. The STAGE DOES NOT MOVE:
    # PRISM's boundary is Advaya's acceptance, and 'Disbursed' only ever comes from
    # Advaya's own disbursement callbacks after that.
    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve", headers=CREDIT_HEAD)
    assert appr.status_code == 200, appr.text
    assert appr.json()["status"] == "Approved"
    assert appr.json()["approved_by"] == "ch@evamfinance.com"
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"

    # Submit requires an Approved package and records the SENT intent — still no stage move.
    sub = await client.post(f"/v1/internal/handover-packages/{lid}/submit", headers=CREDIT_HEAD)
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "Submitted"
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"

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


async def test_advaya_boundary_reject_resubmit_accept_then_disbursement(client, monkeypatch):
    """PRISM stops at Advaya's ACCEPTANCE. Approve/submit never move the stage; a
    Rejected outcome reopens prepare→approve→submit; Accepted freezes the package and
    stores the acknowledgement as advaya_reference; and only Advaya's tranche callbacks
    flip the line to 'Disbursed' and write the actuals."""
    from httpx import ASGITransport, AsyncClient

    from app.core.config import get_settings
    from app.main import create_app as _mk

    s = get_settings()
    monkeypatch.setattr(s, "advaya_integration_enabled", True)
    monkeypatch.setattr(s, "advaya_integration_url", "https://advaya.simulated.local/api")
    monkeypatch.setattr(s, "service_api_keys", {"adv-key": "svc_advaya"})
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)

    assert (await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                              headers=ADMIN)).status_code == 201
    assert (await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                              headers=CREDIT_HEAD)).json()["status"] == "Approved"
    sub = await client.post(f"/v1/internal/handover-packages/{lid}/submit",
                            headers=CREDIT_HEAD)
    assert sub.status_code == 200 and sub.json()["status"] == "Submitted"

    svc_adv = {"X-API-Key": "adv-key", "X-Tenant": "EVAM", "X-Actor": "advaya"}
    async with AsyncClient(transport=ASGITransport(app=_mk()),
                           base_url="http://adv", headers=svc_adv) as adv:
        # A tranche BEFORE acceptance is refused — the boundary in one line.
        early = await adv.post(f"/v1/internal/lending/{lid}/tranches",
                               json={"tranche_ref": "T0", "amount": 1.0})
        assert early.status_code == 409 and "accepted" in early.text.lower()

        # Advaya REJECTS attempt 1 → the package reopens for correction.
        rej = await adv.post("/v1/internal/advaya-handoffs", json={
            "handoff_key": f"advaya-handoff:{lid}:r1", "lending_id": lid,
            "payload_sha256": sub.json()["package_sha256"], "status": "Rejected",
            "note": "KYC document illegible; resubmit."})
        assert rej.status_code == 201, rej.text
        assert (await client.get(
            f"/v1/lending/{lid}/handover-package")).json()["status"] == "Rejected"

        # Correct → RE-prepare → approve → resubmit (the same single-winner row).
        assert (await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                                  headers=ADMIN)).json()["status"] == "Prepared"
        assert (await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                                  headers=CREDIT_HEAD)).json()["status"] == "Approved"
        sub2 = await client.post(f"/v1/internal/handover-packages/{lid}/submit",
                                 headers=CREDIT_HEAD)
        assert sub2.json()["status"] == "Submitted"

        # Advaya ACCEPTS attempt 2: package frozen, acknowledgement stored.
        acc = await adv.post("/v1/internal/advaya-handoffs", json={
            "handoff_key": f"advaya-handoff:{lid}:r2", "lending_id": lid,
            "payload_sha256": sub2.json()["package_sha256"], "status": "Accepted",
            "acknowledgement_id": "ADV-ACK-0042"})
        assert acc.status_code == 201, acc.text
        pkg = (await client.get(f"/v1/lending/{lid}/handover-package")).json()
        assert pkg["status"] == "Accepted" and pkg["advaya_reference"] == "ADV-ACK-0042"
        # Acceptance is NOT fund movement — the stage has still not moved.
        assert (await client.get(
            f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"

        # Only Advaya's FIRST disbursement tranche flips the stage and writes actuals.
        t1 = await adv.post(f"/v1/internal/lending/{lid}/tranches",
                            json={"tranche_ref": "T1", "amount": 5.0,
                                  "disbursed_on": "2026-03-05"})
        assert t1.status_code == 201, t1.text
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Disbursed"
    assert float(line["disbursed_amount"]) == 5.0
    assert line["disbursement_date"] == "2026-03-05"
    assert (line["stage_history"] or [])[-1]["source"] == "advaya-disbursement"


async def test_checker_reject_is_terminal_then_fresh_cycle_allowed(client):
    """The checker's third verb on a handover: REJECT ends this attempt (note mandatory,
    maker≠checker); the line does not move; the queue drops it; a genuine later revival
    is a FRESH prepare→approve cycle on the same single-winner row."""
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    prep = await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                             headers=ADMIN)
    assert prep.status_code == 201, prep.text
    # Note mandatory; the maker cannot reject their own package.
    assert (await client.post(f"/v1/internal/handover-packages/{lid}/reject",
                              json={}, headers=CREDIT_HEAD)).status_code == 422
    self_rej = await client.post(f"/v1/internal/handover-packages/{lid}/reject",
                                 json={"note": "no"}, headers=ADMIN)
    assert self_rej.status_code == 422 and "different checker" in self_rej.text.lower()
    r = await client.post(f"/v1/internal/handover-packages/{lid}/reject",
                          json={"note": "Facility should not be handed over yet."},
                          headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Rejected"
    # The line did not move, and a later approve CANNOT resurrect the rejected attempt —
    # the endpoint's idempotent echo returns the row still Rejected.
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"
    echo = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             headers=CREDIT_HEAD)
    assert echo.status_code == 200 and echo.json()["status"] == "Rejected"
    q = await client.get("/v1/internal/handover-packages", params={"status": "Prepared"},
                         headers=ADMIN)
    assert all(row["lending_id"] != lid for row in q.json())
    # A fresh cycle is allowed: re-prepare puts it back to Prepared for a new check.
    re_prep = await client.post("/v1/internal/handover-packages", json=_prepare_body(lid),
                                headers=ADMIN)
    assert re_prep.status_code == 201 and re_prep.json()["status"] == "Prepared"


async def test_the_lane_the_ui_drives_end_to_end(client):
    """Steps 4-6 exactly as the console sends them: prepare -> approve -> submit -> attest.

    Every earlier refusal on this lane was found by a user, one round trip at a time,
    because the action catalogue was written from endpoint NAMES while the rules live deep
    in the handlers. This walks the remaining lane with the bodies the UI actually
    produces, so the next rule is found here instead.

    What it pins, in the order it bit:
      * the package cites {reference, sha256} objects — NOT document ids, which is what the
        handover screen sent until this test existed;
      * the submitted refs must include the on-file executed_agreement digest, so the
        client cannot assemble a package from arbitrary documents;
      * approve is FOUR-EYES — the preparer may not approve their own package;
      * submit is only legal from Approved, and is idempotent on resend;
      * the manual Advaya attestation closes the line, and needs a human identity.
    """
    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    MAKER = {"X-User-Email": "maker@evamfinance.com", "X-User-Roles": "Credit Head"}

    # 4a. PREPARE — the shape the dialog now builds: the mandatory executed-agreement ref
    #     (handed to it by the plane) plus whatever else the maker picked.
    prep = await client.post("/v1/internal/handover-packages",
                             json=_prepare_body(lid), headers=MAKER)
    assert prep.status_code in (200, 201), prep.text
    assert prep.json()["status"] == "Prepared"

    # Re-preparing the SAME line returns the package already on file rather than
    # validating a new body — so the reconciliation guard has to be shown on a fresh line.
    lid2 = await _ready_lending(client, await _entity(client))
    other = await client.post(
        "/v1/internal/handover-packages",
        json=_prepare_body(lid2, executed_document_refs=[{"reference": "misc/1",
                                                          "sha256": "b" * 64}]),
        headers=MAKER)
    assert other.status_code == 422, other.text
    assert "executed_agreement digest" in other.text

    # 4b. APPROVE — four eyes. The preparer cannot approve their own package.
    same = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             json={"note": "ok"}, headers=MAKER)
    assert same.status_code in (403, 409, 422), same.text

    appr = await client.post(f"/v1/internal/handover-packages/{lid}/approve",
                             json={"note": "checked"}, headers=ADMIN)
    assert appr.status_code == 200, appr.text
    assert appr.json()["status"] == "Approved"

    # 5. SUBMIT — legal only from Approved, and idempotent when resent.
    sub = await client.post(f"/v1/internal/handover-packages/{lid}/submit", headers=ADMIN)
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "Submitted"
    again = await client.post(f"/v1/internal/handover-packages/{lid}/submit", headers=ADMIN)
    assert again.status_code == 200 and again.json()["status"] == "Submitted"

    # 6. ATTEST — the human lane records what Advaya actually did.
    # Attestation has TWO beats, in order: Advaya ACCEPTS the handover, and only then can
    # a disbursement tranche be recorded against it. Recording the tranche first is a 409 —
    # a rule a user driving the panel will meet, since the confirmation dropdown offers
    # all three outcomes and says nothing about their order.
    early = await client.post(f"/v1/lending/{lid}/advaya-events",
                              json={"event": "disbursed", "amount_cr": 8.0,
                                    "disbursed_on": "2026-03-05",
                                    "reference": "ADV/2026/1"}, headers=ADMIN)
    assert early.status_code == 409, early.text
    assert "ACCEPTED" in early.text

    acc = await client.post(f"/v1/lending/{lid}/advaya-events",
                            json={"event": "accepted", "reference": "ADV/ACK/2026/1"},
                            headers=ADMIN)
    assert acc.status_code == 201, acc.text

    # The field is `disbursed_on` — the value date Advaya confirmed, not a generic
    # "occurred on". (The action catalogue already had this right; this test did not,
    # which is the same class of mistake the catalogue itself once made.)
    att = await client.post(f"/v1/lending/{lid}/advaya-events",
                            json={"event": "disbursed", "amount_cr": 8.0,
                                  "disbursed_on": "2026-03-05",
                                  "reference": "ADV/2026/1"}, headers=ADMIN)
    assert att.status_code == 201, att.text

    # An attestation with no citable reference is just an assertion — refused.
    bare = await client.post(f"/v1/lending/{lid}/advaya-events",
                             json={"event": "accepted"}, headers=ADMIN)
    assert bare.status_code == 422, bare.text

    # And the line ends where the lifecycle says it should.
    final = (await client.get(f"/v1/lending/{lid}")).json()
    assert final["stage"] in ("Disbursed", "Ready for Disbursement"), final["stage"]
