"""Increment 5 — the syndication mandate lifecycle.

IM circulation is VERSIONED evidence; lender activity lands on the deal's lender rows
through the policy-enforcing API (whitelisted to the run's rows); the Syn Head's decision
is persist-before-signal and verified fail-closed; 'Sanctioned' is reachable only because
the verified syndication_sanction evidence is on file (the mock enforces the same shared
policy engine the real Register does); the allocation is validated against the mandate."""

from __future__ import annotations

import uuid

import pytest

from app import activities
from app.types import SyndicationMandateInput
from app.workflows import SyndicationMandateWorkflow

pytestmark = pytest.mark.asyncio


async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


def _acts():
    return [activities.get_resource, activities.find_lines_for_deal,
            activities.advance_stage, activities.update_fields,
            activities.attach_evidence, activities.verify_syndication_decision,
            activities.verify_control, activities.emit_operational_event,
            activities.mark_lead_note]


def _mandate_with_lenders(mock_register, amount=100.0):  # noqa: ANN001
    did = uuid.uuid4().hex
    sid, l1, l2 = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.syndication[sid] = {
        "id": sid, "deal_id": did, "version": 1, "status": "IM in Prep",
        "amount_cr": amount}
    for lid, bank in ((l1, "SBI"), (l2, "HDFC")):
        mock_register.state.syndication[lid] = {
            "id": lid, "deal_id": did, "version": 1, "status": "Deal Sourced",
            "potential": bank}
    return did, sid, l1, l2


def _seed_decision(mock_register, wf_id, sid, decision="Approved", **extra):  # noqa: ANN001
    mock_register.state.decisions[wf_id] = {
        "id": uuid.uuid4().hex, "workflow_id": wf_id, "decision": decision,
        "subject_type": "Syndication", "subject_id": sid,
        "decided_by": "synhead@evamfinance.com", "roles": ["Syn Head"],
        "committee_reference": "syn-sanction/SL-1", "note": "syn note", **extra}


async def test_mandate_journey_im_decision_sanction_allocation(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, sid, l1, l2 = _mandate_with_lenders(mock_register)
    async with env:
        tq = "syn-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[SyndicationMandateWorkflow], activities=_acts()):
            wf_id = f"synd-{sid}"
            handle = await env.client.start_workflow(
                SyndicationMandateWorkflow.run,
                SyndicationMandateInput(syndication_id=sid, deal_id=did,
                                        requested_by="synrm@evamfinance.com",
                                        im_reference="im/IM-1"),
                id=wf_id, task_queue=tq)
            # Lender activity: a legal move lands; an ILLEGAL one is refused by policy and
            # surfaced as an ops event — the run never dies.
            await handle.signal(SyndicationMandateWorkflow.lender_update,
                                args=[l1, "Docs Pending", "docs list sent",
                                      "synrm@evamfinance.com"])
            await handle.signal(SyndicationMandateWorkflow.lender_update,
                                args=[l2, "Sanctioned", "", "synrm@evamfinance.com"])
            # A revised IM is the NEXT immutable version.
            await handle.signal(SyndicationMandateWorkflow.circulate_im,
                                args=["im/IM-2", "", "synrm@evamfinance.com"])
            # The Syn Head decides — durably recorded FIRST, then the wake-up.
            _seed_decision(mock_register, wf_id, sid)
            await handle.signal(SyndicationMandateWorkflow.syndication_decision, "")
            # Post-sanction: the allocation (within the mandate amount).
            await handle.signal(SyndicationMandateWorkflow.allocate,
                                args=[{l1: 60.0, l2: 40.0}, "synhead@evamfinance.com"])
            result = await handle.result()

    assert result.status == "Sanctioned"
    assert result.decided_by == "synhead@evamfinance.com"
    assert result.im_version == 2
    assert result.allocations == {l1: 60.0, l2: 40.0}
    # The mandate walked its pipeline and could ONLY reach Sanctioned because the verified
    # evidence was on file (the mock enforces the same evidence gate).
    assert mock_register.state.syndication[sid]["status"] == "Sanctioned"
    kinds = [e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_id"] == sid]
    assert kinds.count("im_document") == 2
    assert "syndication_sanction" in kinds and "syndication_allocation" in kinds
    # Lender rows: the legal move landed; the illegal jump did not.
    assert mock_register.state.syndication[l1]["status"] == "Docs Pending"
    assert mock_register.state.syndication[l1]["amount_cr"] == 60.0
    assert mock_register.state.syndication[l2]["status"] == "Deal Sourced"
    assert mock_register.state.syndication[l2]["amount_cr"] == 40.0


async def test_mandate_rejection_and_spoofed_decision_ignored(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, sid, _l1, _l2 = _mandate_with_lenders(mock_register)
    async with env:
        tq = "syn-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[SyndicationMandateWorkflow], activities=_acts()):
            wf_id = f"synd-{sid}"
            handle = await env.client.start_workflow(
                SyndicationMandateWorkflow.run,
                SyndicationMandateInput(syndication_id=sid, deal_id=did,
                                        requested_by="synrm@evamfinance.com",
                                        im_reference="im/IM-1"),
                id=wf_id, task_queue=tq)
            # A wake-up with NO recorded decision is a spoof — ignored, still waiting.
            await handle.signal(SyndicationMandateWorkflow.syndication_decision, "")
            state = await handle.query(SyndicationMandateWorkflow.state)
            assert state["business_status"] == "AwaitingDecision"
            _seed_decision(mock_register, wf_id, sid, decision="Rejected")
            await handle.signal(SyndicationMandateWorkflow.syndication_decision, "")
            result = await handle.result()

    assert result.status == "Rejected"
    assert mock_register.state.syndication[sid]["status"] == "Rejected"
    assert not [e for e in mock_register.state.evidence
                if e["evidence_kind"] == "syndication_sanction"]


async def test_over_allocation_is_rejected_and_window_lapses(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, sid, l1, l2 = _mandate_with_lenders(mock_register, amount=50.0)
    async with env:
        tq = "syn-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[SyndicationMandateWorkflow], activities=_acts()):
            wf_id = f"synd-{sid}"
            handle = await env.client.start_workflow(
                SyndicationMandateWorkflow.run,
                SyndicationMandateInput(syndication_id=sid, deal_id=did,
                                        requested_by="synrm@evamfinance.com",
                                        im_reference="im/IM-1",
                                        allocation_timeout_hours=1.0),
                id=wf_id, task_queue=tq)
            _seed_decision(mock_register, wf_id, sid)
            await handle.signal(SyndicationMandateWorkflow.syndication_decision, "")
            # 60 + 40 against a 50 Cr mandate: refused, loudly — never silently absorbed.
            await handle.signal(SyndicationMandateWorkflow.allocate,
                                args=[{l1: 60.0, l2: 40.0}, "synhead@evamfinance.com"])
            result = await handle.result()

    assert result.status == "Sanctioned"
    assert result.allocations == {}                     # nothing was applied
    assert "amount_cr" not in mock_register.state.syndication[l1]  # untouched
    assert not [e for e in mock_register.state.evidence
                if e["evidence_kind"] == "syndication_allocation"]
