"""Business-lifecycle workflows: Lead Qualification → Deal Structuring (on the LENDING line) → Document Collection, and
the activities they orchestrate.

Two layers:

* ACTIVITY tests (always run) drive the evidence / stage-advance activities against the mock
  Register via ``ActivityEnvironment`` — including that a stage advance to the sanction milestone is
  REFUSED until the evidence is on file, which is the whole point of the evidence gate.
* WORKFLOW tests (run on Temporal's time-skipping test server; skip cleanly offline) prove the
  workflows do the work AND file the evidence BEFORE advancing, and are signal-driven and durable.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.types import (
    AdvayaHandoffInput,
    CpcsChecklistInput,
    DealStructuringInput,
    DocumentCollectionInput,
    LeadQualificationInput,
)
from app.workflows import (
    AdvayaHandoffWorkflow,
    CpcsChecklistWorkflow,
    DealStructuringWorkflow,
    DocumentCollectionWorkflow,
    LeadQualificationWorkflow,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Activity layer — runs everywhere (no Temporal server needed)
# --------------------------------------------------------------------------- #
async def test_attach_evidence_is_idempotent(mock_register):
    env = ActivityEnvironment()
    # A committee decision must back committee evidence — seed one for this subject.
    mock_register.state.committee["dec-1"] = {
        "workflow_id": "dec-1", "decision": "Approved", "subject_type": "Deal",
        "subject_id": "d-1", "roles": ["Credit Head"]}
    first = await env.run(activities.attach_evidence, "Deal", "d-1",
                          "credit_committee_approval", "committee/MIN-1", None, None, None, "dec-1")
    second = await env.run(activities.attach_evidence, "Deal", "d-1",
                           "credit_committee_approval", "committee/MIN-1", None, None, None, "dec-1")
    # Same (kind, reference) → the SAME record, not a duplicate (append-only store stays clean).
    assert first["id"] == second["id"]
    assert len(mock_register.state.evidence) == 1


async def test_advance_stage_is_evidence_gated_for_sanction(mock_register):
    """The activity advances through the Register's normal API, so the Register's evidence gate
    applies: a LENDING line cannot be advanced to Sanctioned until the committee + sanction
    evidence is on file for that line — and once it is, the advance succeeds. (The deal-level
    credit stage is deprecated; the gate keys on the lending line.)"""
    from evam_register_client.errors import RegisterError

    env = ActivityEnvironment()
    lid = uuid.uuid4().hex
    mock_register.state.lending[lid] = {"id": lid, "version": 1, "stage": "Note Circulated"}

    # No evidence yet → the gate refuses the sanction advance.
    with pytest.raises(RegisterError):
        await env.run(activities.advance_stage, "lending", lid, "stage", "Sanctioned",
                      {"rm": "asha"}, None)
    assert mock_register.state.lending[lid]["stage"] == "Note Circulated"

    # File both required evidence kinds (each backed by a verified committee decision), then the
    # same advance is accepted.
    mock_register.state.committee["dec-1"] = {
        "workflow_id": "dec-1", "decision": "Approved", "subject_type": "Lending",
        "subject_id": lid, "roles": ["Credit Head"]}
    for kind in ("credit_committee_approval", "sanction_letter"):
        await env.run(activities.attach_evidence, "Lending", lid, kind, f"{kind}/DOC",
                      None, None, None, "dec-1")
    out = await env.run(activities.advance_stage, "lending", lid, "stage", "Sanctioned",
                        {"rm": "asha"}, None)
    assert out["stage"] == "Sanctioned"


# --------------------------------------------------------------------------- #
# Workflow layer — Temporal time-skipping server (skips offline)
# --------------------------------------------------------------------------- #
async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


def _biz_activities():
    return [activities.attach_evidence, activities.advance_stage, activities.get_resource,
            activities.mark_lead_note, activities.verify_committee_decision,
            activities.verify_facility_decisions,
            activities.verify_control, activities.emit_operational_event,
            activities.find_lines_for_deal, activities.update_fields,
            activities.prepare_cpcs_checklist, activities.create_handover_package,
            activities.record_advaya_handoff]


async def test_cpcs_checklist_workflow_prepares_checklist(mock_register):
    """The CP/CS workflow (the maker's phase) records the authoritative checklist via the Register;
    a different checker approves it separately before cp_cs_completion can be minted."""
    env = await _env()
    from temporalio.worker import Worker

    lid = uuid.uuid4().hex
    async with env:
        tq = "cpcs-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[CpcsChecklistWorkflow], activities=_biz_activities()):
            result = await env.client.execute_workflow(
                CpcsChecklistWorkflow.run,
                CpcsChecklistInput(
                    lending_id=lid, requested_by="maker@evamfinance.com",
                    items=[{"key": "charge", "condition_type": "CP", "status": "Completed"}]),
                id=f"cpcs-{lid}", task_queue=tq)

    assert result.status == "Completed" and result.checklist_id
    assert result.checklist_id in mock_register.state.cpcs


def _seed_committee(mock_register, wf_id, did, decision="Approved",  # noqa: ANN001
                    lending_ids=(), line_decisions=None, **extra):
    """Stand in for the orchestrator's persist-before-signal: the AUTHORITATIVE committee decision
    the workflow will read + verify (the workflow NEVER trusts the signal payload) — plus the
    per-line SUBJECT-BOUND decisions (keyed "{wf_id}:lending:{line_id}") the orchestrator records
    so the Lending-scoped evidence can verify."""
    mock_register.state.committee[wf_id] = {
        "id": uuid.uuid4().hex, "workflow_id": wf_id, "decision": decision,
        "subject_type": "Deal", "subject_id": did, "decided_by": "chair@evamfinance.com",
        "roles": ["Credit Head"], "committee_reference": "committee/MIN-9",
        "sanction_letter_reference": "sanction/SL-9", "note": "committee note", **extra}
    for lid in lending_ids:
        key = f"{wf_id}:lending:{lid}"
        mock_register.state.committee[key] = {
            "id": uuid.uuid4().hex, "workflow_id": key,
            "decision": (line_decisions or {}).get(lid, decision),
            "subject_type": "Lending", "subject_id": lid,
            "decided_by": "chair@evamfinance.com", "roles": ["Credit Head"],
            "committee_reference": "committee/MIN-9",
            "sanction_letter_reference": "sanction/SL-9", "note": "committee note"}


async def test_deal_structuring_files_evidence_then_sanctions(mock_register):
    """The workflow derives the outcome from the AUTHORITATIVE persisted committee decision (not the
    signal — which is only a wake-up), files the committee-approval + sanction-letter evidence, and
    only then advances to Sanctioned."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Data Awaited"}

    async with env:
        tq = "biz-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha",
                                     credit_note_reference="note/CN-1"),
                id=wf_id, task_queue=tq)
            # Orchestrator would persist the decision (deal + per-line), THEN signal.
            _seed_committee(mock_register, wf_id, did, "Approved", lending_ids=[lid])
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "Sanctioned"
    assert result.decided_by == "chair@evamfinance.com"       # from the record, not the signal
    # The LENDING line is sanctioned; the deal's stage is the commercial funnel — untouched.
    assert mock_register.state.lending[lid]["stage"] == "Sanctioned"
    assert mock_register.state.deals[did]["stage"] == "In Pipeline"
    # The sanction basics land on the deal as plain data.
    assert mock_register.state.deals[did]["product_type"] == "Term Loan"
    assert mock_register.state.deals[did]["rm"] == "asha"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_type"] == "Lending" and e["subject_id"] == lid}
    assert {"credit_committee_approval", "sanction_letter", "credit_note"} <= kinds


async def test_deal_structuring_rejection_does_not_sanction(mock_register):
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Diligence"}

    async with env:
        tq = "biz-tq2"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-rej-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha"),
                id=wf_id, task_queue=tq)
            _seed_committee(mock_register, wf_id, did, "Rejected", lending_ids=[lid])
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "Rejected"
    # The LENDING line carries the rejection; the deal's funnel is the RM's call — untouched.
    assert mock_register.state.lending[lid]["stage"] == "Rejected"
    assert mock_register.state.deals[did]["stage"] == "In Pipeline"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence if e["subject_id"] == lid}
    assert "sanction_letter" not in kinds        # nothing sanctioned
    assert "credit_committee_rejection" in kinds


async def test_facility_specific_decisions_partially_sanction(mock_register):
    """Committee approval is FACILITY-SPECIFIC: with two lending lines and a mixed recorded
    outcome (one approved, one rejected), the approved line is sanctioned with its evidence,
    the rejected line moves to Rejected with the rejection evidence, and the run reports
    PartiallySanctioned with the per-line outcome map — a deal-wide result never implicitly
    sanctions every facility."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid_a, lid_b = uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    for lid in (lid_a, lid_b):
        mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                            "stage": "Data Awaited"}

    async with env:
        tq = "biz-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha",
                                     credit_note_reference="note/CN-2"),
                id=wf_id, task_queue=tq)
            # The orchestrator records the OVERALL outcome (Approved — a sanction happened)
            # plus one decision PER FACILITY, then signals.
            _seed_committee(mock_register, wf_id, did, "Approved",
                            lending_ids=[lid_a, lid_b],
                            line_decisions={lid_a: "Approved", lid_b: "Rejected"})
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "PartiallySanctioned"
    assert result.line_outcomes == {lid_a: "Sanctioned", lid_b: "Rejected"}
    assert mock_register.state.lending[lid_a]["stage"] == "Sanctioned"
    assert mock_register.state.lending[lid_b]["stage"] == "Rejected"
    # Evidence follows each facility's own outcome.
    kinds_a = {e["evidence_kind"] for e in mock_register.state.evidence
               if e["subject_type"] == "Lending" and e["subject_id"] == lid_a}
    kinds_b = {e["evidence_kind"] for e in mock_register.state.evidence
               if e["subject_type"] == "Lending" and e["subject_id"] == lid_b}
    assert {"credit_committee_approval", "sanction_letter"} <= kinds_a
    assert "credit_committee_rejection" not in kinds_a
    assert "credit_committee_rejection" in kinds_b
    assert "sanction_letter" not in kinds_b
    # The deal still records the sanction basics; its funnel stage is untouched.
    assert mock_register.state.deals[did]["stage"] == "In Pipeline"


async def test_verified_cancel_control_ends_a_waiting_run(mock_register):
    """A cancel that is DURABLY RECORDED (the orchestrator's persist-before-signal) ends the
    waiting run as Cancelled; a control signal with NO record is a spoof and is ignored."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Data Awaited"}

    async with env:
        tq = "biz-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com"),
                id=wf_id, task_queue=tq)
            # A SPOOFED control (no durable record) must be ignored — the run keeps waiting.
            await handle.signal(DealStructuringWorkflow.control,
                                args=["Cancelled", f"{wf_id}:control:forged"])
            state = await handle.query(DealStructuringWorkflow.state)
            assert state["business_status"] == "AwaitingDecision"
            # The REAL thing: record first (as the orchestrator does), then signal.
            ref = f"{wf_id}:control:{uuid.uuid4().hex[:12]}"
            mock_register.state.decisions[ref] = {
                "id": uuid.uuid4().hex, "workflow_id": ref, "decision": "Cancelled",
                "decided_by": "rm@evamfinance.com", "note": "client withdrew"}
            await handle.signal(DealStructuringWorkflow.control, args=["Cancelled", ref])
            result = await handle.result()

    assert result.status == "Cancelled"
    assert result.decided_by == "rm@evamfinance.com"
    assert result.note == "client withdrew"
    # No sanction/rejection ever touched the line.
    assert mock_register.state.lending[lid]["stage"] in ("Data Awaited", "Diligence",
                                                         "Note Circulated")


async def test_return_for_information_then_resubmit_then_decide(mock_register):
    """Return-for-information parks the run (business status flips; the run KEEPS waiting),
    resubmit restores it, and a committee decision afterwards completes normally."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Data Awaited"}

    async with env:
        tq = "biz-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha"),
                id=wf_id, task_queue=tq)
            for action in ("ReturnedForInformation", "Resubmitted"):
                ref = f"{wf_id}:control:{uuid.uuid4().hex[:12]}"
                mock_register.state.decisions[ref] = {
                    "id": uuid.uuid4().hex, "workflow_id": ref, "decision": action,
                    "decided_by": "chair@evamfinance.com", "note": None}
                await handle.signal(DealStructuringWorkflow.control, args=[action, ref])
                state = await handle.query(DealStructuringWorkflow.state)
                expected = ("ReturnedForInformation" if action == "ReturnedForInformation"
                            else "AwaitingDecision")
                assert state["business_status"] == expected
            # The loop ends the way it should: with a decision.
            _seed_committee(mock_register, wf_id, did, "Approved", lending_ids=[lid])
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "Sanctioned"


async def test_deal_structuring_ignores_a_spoofed_signal_without_a_record(mock_register):
    """A direct/spoofed committee_decision signal with NO authoritative decision record is IGNORED —
    the run keeps waiting and times out without sanctioning. The outcome can never come from the
    signal itself."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    lid = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Diligence"}

    async with env:
        tq = "biz-tq3"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            wf_id = f"struct-spoof-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha",
                                     decision_timeout_hours=1),   # short → times out under skip
                id=wf_id, task_queue=tq)
            # No committee decision is persisted → the signal is a spoof.
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "TimedOut"
    assert mock_register.state.lending[lid]["stage"] != "Sanctioned"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence if e["subject_id"] == lid}
    assert "credit_committee_approval" not in kinds and "sanction_letter" not in kinds


async def test_deal_structuring_fails_clearly_without_a_lending_line(mock_register):
    """Credit structuring runs on the lending record — a deal with no lending line has nothing to
    structure, and the run says so instead of waiting for a committee that could sanction
    nothing."""
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}

    async with env:
        tq = "biz-tq4"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow], activities=_biz_activities()):
            result = await env.client.execute_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com"),
                id=f"struct-noline-{did}", task_queue=tq)

    assert result.status == "NoLendingLine"
    assert "lending" in (result.note or "").lower()
    assert mock_register.state.deals[did]["stage"] == "In Pipeline"


async def test_lead_qualification_records_evidence(mock_register):
    env = await _env()
    from temporalio.worker import Worker

    lid = uuid.uuid4().hex
    mock_register.state.leads[lid] = {"id": lid, "version": 1, "status": "Active", "notes": ""}

    async with env:
        tq = "qual-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[LeadQualificationWorkflow], activities=_biz_activities()):
            result = await env.client.execute_workflow(
                LeadQualificationWorkflow.run,
                LeadQualificationInput(lead_id=lid, qualified_by="rm@evamfinance.com",
                                       qualification_reference="scorecard/Q-1", passed=True),
                id=f"qual-{lid}", task_queue=tq)

    assert result.status == "Qualified"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence if e["subject_id"] == lid}
    assert "lead_qualification" in kinds


async def test_advaya_handoff_hands_over_without_self_disbursing(mock_register):
    """The handover workflow (the MAKER's action) PREPARES the durable package but does NOT advance
    the stage — a different checker must approve it. It does NOT call Advaya or mark the loan
    disbursed."""
    env = await _env()
    from temporalio.worker import Worker

    lid = uuid.uuid4().hex
    mock_register.state.lending[lid] = {
        "id": lid, "version": 1, "stage": "Ready for Disbursement", "amount_cr": 20.0,
        "proposed_disbursement_amount": 12.5, "proposed_disbursement_date": "2026-02-01"}
    for kind in ("cp_cs_completion", "executed_agreement"):
        mock_register.state.evidence.append(
            {"id": uuid.uuid4().hex, "subject_type": "Lending", "subject_id": lid,
             "evidence_kind": kind})

    async with env:
        tq = "adv-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[AdvayaHandoffWorkflow], activities=_biz_activities()):
            result = await env.client.execute_workflow(
                AdvayaHandoffWorkflow.run,
                AdvayaHandoffInput(
                    lending_id=lid, requested_by="maker@evamfinance.com",
                    executed_document_refs=[{"reference": "fa/1", "sha256": "a" * 64}],
                    delivery_method="secure-email", recipient="advaya-ops"),
                id=f"advaya-{lid}", task_queue=tq)

    assert result.status == "Prepared"
    assert result.handover_package_id
    # The maker's prepare did NOT advance the stage — approval is a separate checker action.
    assert mock_register.state.lending[lid]["stage"] == "Ready for Disbursement"
    pkg = mock_register.state.handover_packages[lid]
    assert pkg["status"] == "Prepared"
    assert pkg["facility_amount"] == 20.0 and pkg["proposed_disbursement_amount"] == 12.5
    # No Advaya round-trip, no fabricated acknowledgement.
    assert f"advaya-handoff:{lid}" not in mock_register.state.handoffs
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence if e["subject_id"] == lid}
    assert "advaya_acknowledgement" not in kinds


async def test_document_collection_completes_when_all_received(mock_register):
    env = await _env()
    from temporalio.worker import Worker

    did = uuid.uuid4().hex

    async with env:
        tq = "doc-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DocumentCollectionWorkflow], activities=_biz_activities()):
            handle = await env.client.start_workflow(
                DocumentCollectionWorkflow.run,
                DocumentCollectionInput(subject_type="Deal", subject_id=did,
                                        requested_by="ops@evamfinance.com",
                                        required_documents=["kyc", "facility_agreement"]),
                id=f"doc-{did}", task_queue=tq)
            await handle.signal(DocumentCollectionWorkflow.document_received,
                                "kyc", "doc/kyc-1", "")
            await handle.signal(DocumentCollectionWorkflow.document_received,
                                "facility_agreement", "doc/fa-1", "")
            result = await handle.result()

    assert result.status == "Complete"
    assert set(result.received) == {"kyc", "facility_agreement"}
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence if e["subject_id"] == did}
    assert "executed_agreement" in kinds
    assert "document:kyc" in kinds and "document:facility_agreement" in kinds
