"""Increment 3 — lending depth: credit-note versioning + committee rework, conditional
approval with per-facility conditions, and the sanction validity window.

The rework loop end to end: return-for-information (inc-1 control) → revise_credit_note
(a NEW immutable credit_note version on every line) → resubmit → decide. A conditional
approval files its conditions as governance evidence beside the sanction, and a validity
window gets an abandoned monitor that files the expiry if the facility never progresses."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app import activities
from app.types import DealStructuringInput, SanctionExpiryInput
from app.workflows import DealStructuringWorkflow, SanctionExpiryMonitorWorkflow

pytestmark = pytest.mark.asyncio


async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


def _acts():
    return [activities.attach_evidence, activities.advance_stage, activities.get_resource,
            activities.verify_committee_decision, activities.verify_facility_decisions,
            activities.verify_control, activities.emit_operational_event,
            activities.find_lines_for_deal, activities.update_fields,
            activities.mark_lead_note]


def _deal_with_line(mock_register):  # noqa: ANN001
    did, lid = uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                        "stage": "Data Awaited"}
    return did, lid


def _seed(mock_register, wf_id, did, lid, decision="Approved", **extra):  # noqa: ANN001
    mock_register.state.committee[wf_id] = {
        "id": uuid.uuid4().hex, "workflow_id": wf_id, "decision": decision,
        "subject_type": "Deal", "subject_id": did, "decided_by": "chair@evamfinance.com",
        "roles": ["Credit Head"], "committee_reference": "committee/MIN-9",
        "sanction_letter_reference": "sanction/SL-9", "note": "committee note"}
    key = f"{wf_id}:lending:{lid}"
    mock_register.state.decisions[key] = {
        "id": uuid.uuid4().hex, "workflow_id": key, "decision": decision,
        "subject_type": "Lending", "subject_id": lid,
        "decided_by": "chair@evamfinance.com", "note": "committee note", **extra}
    return key


async def test_conditional_approval_files_conditions_beside_the_sanction(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, lid = _deal_with_line(mock_register)
    async with env:
        tq = "i3-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow,
                                     SanctionExpiryMonitorWorkflow],
                          activities=_acts()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha"),
                id=wf_id, task_queue=tq)
            _seed(mock_register, wf_id, did, lid,
                  conditions="quarterly covenant reporting", valid_days=90)
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "Sanctioned"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_id"] == lid}
    # The conditions are governance evidence BESIDE the sanction pair.
    assert {"credit_committee_approval", "sanction_letter", "sanction_conditions"} <= kinds
    cond = next(e for e in mock_register.state.evidence
                if e["evidence_kind"] == "sanction_conditions")
    assert cond["note"] == "quarterly covenant reporting"


async def test_rework_loop_versions_the_credit_note(mock_register):
    """return → revise (v2 filed on the line) → resubmit → approve: the run reports the
    version the committee decided on, and BOTH circulations are on the record."""
    from temporalio.worker import Worker
    env = await _env()
    did, lid = _deal_with_line(mock_register)
    async with env:
        tq = "i3-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[DealStructuringWorkflow,
                                     SanctionExpiryMonitorWorkflow],
                          activities=_acts()):
            wf_id = f"struct-{did}"
            handle = await env.client.start_workflow(
                DealStructuringWorkflow.run,
                DealStructuringInput(deal_id=did, requested_by="rm@evamfinance.com",
                                     product_type="Term Loan", rm="asha",
                                     credit_note_reference="note/CN-1"),
                id=wf_id, task_queue=tq)
            # Committee sends it back (inc-1 control, durably recorded)…
            ref = f"{wf_id}:control:{uuid.uuid4().hex[:12]}"
            mock_register.state.decisions[ref] = {
                "id": uuid.uuid4().hex, "workflow_id": ref,
                "decision": "ReturnedForInformation",
                "decided_by": "chair@evamfinance.com", "note": "rework the pricing"}
            await handle.signal(DealStructuringWorkflow.control,
                                args=["ReturnedForInformation", ref])
            # …the RM circulates the revision…
            await handle.signal(DealStructuringWorkflow.revise_credit_note,
                                args=["note/CN-2", "", "rm@evamfinance.com"])
            state = await handle.query(DealStructuringWorkflow.state)
            assert state["credit_note_version"] == 2
            # …resubmits, and the committee approves.
            ref2 = f"{wf_id}:control:{uuid.uuid4().hex[:12]}"
            mock_register.state.decisions[ref2] = {
                "id": uuid.uuid4().hex, "workflow_id": ref2, "decision": "Resubmitted",
                "decided_by": "rm@evamfinance.com", "note": None}
            await handle.signal(DealStructuringWorkflow.control, args=["Resubmitted", ref2])
            _seed(mock_register, wf_id, did, lid)
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
            result = await handle.result()

    assert result.status == "Sanctioned"
    assert result.credit_note_version == 2
    notes = [e for e in mock_register.state.evidence
             if e["subject_id"] == lid and e["evidence_kind"] == "credit_note"]
    assert [n["reference"] for n in notes] == ["note/CN-1", "note/CN-2"]
    assert "(v1)" in notes[0]["note"] and "v2" in notes[1]["note"]


async def test_expiry_monitor_files_the_lapse_when_nothing_progresses(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    lid = uuid.uuid4().hex
    mock_register.state.lending[lid] = {"id": lid, "deal_id": "d1", "version": 1,
                                        "stage": "Sanctioned"}
    async with env:
        tq = "i3-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[SanctionExpiryMonitorWorkflow], activities=_acts()):
            result = await env.client.execute_workflow(
                SanctionExpiryMonitorWorkflow.run,
                SanctionExpiryInput(lending_id=lid, deal_id="d1", valid_days=90,
                                    decision_ref="wf:lending:x"),
                id=f"expiry-{lid}", task_queue=tq,
                execution_timeout=timedelta(days=120))
    assert result.status == "Expired"
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_id"] == lid}
    assert "sanction_expired" in kinds


async def test_expiry_monitor_stays_quiet_when_the_facility_progresses(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    lid = uuid.uuid4().hex
    # By the time the monitor checks, CP/CS completed — the sanction was USED.
    mock_register.state.lending[lid] = {"id": lid, "deal_id": "d1", "version": 1,
                                        "stage": "CP/CS Completed"}
    async with env:
        tq = "i3-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[SanctionExpiryMonitorWorkflow], activities=_acts()):
            result = await env.client.execute_workflow(
                SanctionExpiryMonitorWorkflow.run,
                SanctionExpiryInput(lending_id=lid, deal_id="d1", valid_days=90,
                                    decision_ref="wf:lending:x"),
                id=f"expiry-{lid}", task_queue=tq,
                execution_timeout=timedelta(days=120))
    assert result.status == "Progressed" and result.stage_at_close == "CP/CS Completed"
    assert not [e for e in mock_register.state.evidence
                if e["subject_id"] == lid and e["evidence_kind"] == "sanction_expired"]
