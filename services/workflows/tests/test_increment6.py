"""Increment 6 — the asset-monetisation mandate lifecycle.

Teaser circulation is VERSIONED evidence; buyer activity lands on the deal's buyer rows
through the policy API (whitelisted); every NDA / data-room grant and every offer is
immutable evidence (the offer comparison can never be quietly edited); the AM Head's
closure decision is persist-before-signal and verified fail-closed; 'Closed' is reachable
only because the verified am_closure_approval evidence is on file."""

from __future__ import annotations

import uuid

import pytest

from app import activities
from app.types import AssetMonetisationInput
from app.workflows import AssetMonetisationWorkflow

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
            activities.attach_evidence, activities.verify_am_decision,
            activities.verify_control, activities.emit_operational_event,
            activities.mark_lead_note]


def _mandate_with_buyers(mock_register):  # noqa: ANN001
    did = uuid.uuid4().hex
    mid, b1, b2 = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.asset_mon[mid] = {
        "id": mid, "deal_id": did, "version": 1, "status": "Teaser Prepared",
        "indicative_value_cr": 200.0}
    for bid, inv in ((b1, "Actis"), (b2, "Brookfield")):
        mock_register.state.asset_mon[bid] = {
            "id": bid, "deal_id": did, "version": 1, "status": "Teaser Prepared",
            "investor": inv}
    return did, mid, b1, b2


def _seed_decision(mock_register, wf_id, mid, decision="Approved", **extra):  # noqa: ANN001
    mock_register.state.decisions[wf_id] = {
        "id": uuid.uuid4().hex, "workflow_id": wf_id, "decision": decision,
        "subject_type": "AssetMonetisation", "subject_id": mid,
        "decided_by": "amhead@evamfinance.com", "roles": ["AM Head"],
        "committee_reference": "am-closure/SPA-1", "note": "closure note", **extra}


async def test_am_journey_teaser_nda_offers_closure(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, mid, b1, b2 = _mandate_with_buyers(mock_register)
    async with env:
        tq = "am-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[AssetMonetisationWorkflow], activities=_acts()):
            wf_id = f"amon-{mid}"
            handle = await env.client.start_workflow(
                AssetMonetisationWorkflow.run,
                AssetMonetisationInput(asset_mon_id=mid, deal_id=did,
                                       requested_by="amrm@evamfinance.com",
                                       teaser_reference="teaser/T-1"),
                id=wf_id, task_queue=tq)
            # Buyer outreach: b1 walks legally; a signal for a row the run never
            # discovered is ignored outright (whitelist).
            await handle.signal(AssetMonetisationWorkflow.buyer_update,
                                args=[b1, "Teaser Shared", "", "amrm@evamfinance.com"])
            await handle.signal(AssetMonetisationWorkflow.buyer_update,
                                args=["forged-row", "Closed", "", "mallory@x"])
            # NDA + data room for b1; a revised teaser is v2.
            await handle.signal(AssetMonetisationWorkflow.record_nda,
                                args=[b1, "nda/ACTIS-1", True, "amrm@evamfinance.com"])
            await handle.signal(AssetMonetisationWorkflow.circulate_teaser,
                                args=["teaser/T-2", "", "amrm@evamfinance.com"])
            # Offers: an NBO from b1, then a binding offer from b2 — the mandate walks.
            await handle.signal(AssetMonetisationWorkflow.record_offer,
                                args=[b1, "nbo", 180.0, "offer/ACTIS-NBO",
                                      "amrm@evamfinance.com"])
            await handle.signal(AssetMonetisationWorkflow.record_offer,
                                args=[b2, "binding", 195.0, "offer/BF-BO",
                                      "amrm@evamfinance.com"])
            comparison = await handle.query(AssetMonetisationWorkflow.offer_comparison)
            assert [(o["buyer_row"], o["kind"], o["amount_cr"]) for o in comparison] == [
                (b1, "nbo", 180.0), (b2, "binding", 195.0)]
            # The AM Head closes it — durably recorded FIRST, then the wake-up.
            _seed_decision(mock_register, wf_id, mid)
            await handle.signal(AssetMonetisationWorkflow.am_decision, "")
            result = await handle.result()

    assert result.status == "Closed"
    assert result.decided_by == "amhead@evamfinance.com"
    assert result.teaser_version == 2
    assert len(result.offers) == 2
    # The mandate could ONLY reach Closed because the verified evidence is on file.
    assert mock_register.state.asset_mon[mid]["status"] == "Closed"
    kinds = [e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_id"] == mid]
    assert kinds.count("teaser_document") == 2
    assert kinds.count("am_offer") == 2
    assert "am_nda" in kinds and "am_closure_approval" in kinds
    # Buyer rows: the legal move landed; the forged row never existed to the run.
    assert mock_register.state.asset_mon[b1]["status"] == "Teaser Shared"
    assert "forged-row" not in mock_register.state.asset_mon


async def test_am_rejection_is_a_lost_mandate(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    did, mid, _b1, _b2 = _mandate_with_buyers(mock_register)
    async with env:
        tq = "am-tq"
        async with Worker(env.client, task_queue=tq,
                          workflows=[AssetMonetisationWorkflow], activities=_acts()):
            wf_id = f"amon-{mid}"
            handle = await env.client.start_workflow(
                AssetMonetisationWorkflow.run,
                AssetMonetisationInput(asset_mon_id=mid, deal_id=did,
                                       requested_by="amrm@evamfinance.com",
                                       teaser_reference="teaser/T-1"),
                id=wf_id, task_queue=tq)
            # A spoofed wake-up (no record) is ignored; the real rejection lands.
            await handle.signal(AssetMonetisationWorkflow.am_decision, "")
            state = await handle.query(AssetMonetisationWorkflow.state)
            assert state["business_status"] == "AwaitingDecision"
            _seed_decision(mock_register, wf_id, mid, decision="Rejected")
            await handle.signal(AssetMonetisationWorkflow.am_decision, "")
            result = await handle.result()

    assert result.status == "Lost"
    assert mock_register.state.asset_mon[mid]["status"] == "Dropped"
    assert not [e for e in mock_register.state.evidence
                if e["evidence_kind"] == "am_closure_approval"]
