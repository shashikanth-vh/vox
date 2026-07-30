"""The decision-delivery reconciler's core logic: deliver_one decides the right outcome from
the workflow's actual state, and reconcile_tenant drives a claimed batch and dead-letters/retries
correctly — proven with fakes (no live Temporal / Register)."""

from __future__ import annotations

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app import reconciler

pytestmark = pytest.mark.asyncio


class _Handle:
    def __init__(self, status=WorkflowExecutionStatus.RUNNING, result=None, missing=False,  # noqa: ANN001
                 result_raises=False):
        self._status = status
        self._result = result
        self._missing = missing
        self._result_raises = result_raises
        self.signalled: list = []

    async def describe(self):
        if self._missing:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return type("_D", (), {"status": self._status})()

    async def result(self):
        if self._result_raises:
            raise RPCError("temporarily unavailable", RPCStatusCode.UNAVAILABLE, b"")
        return self._result

    async def signal(self, name, args=None):  # noqa: ANN001
        self.signalled.append((name, args))


async def test_deliver_one_running_resignals_and_retries():
    h = _Handle(status=WorkflowExecutionStatus.RUNNING)
    outcome, _ = await reconciler.deliver_one(h, "leadconv-x-lead1", "Approved")
    assert outcome == "retry"
    assert h.signalled and h.signalled[0][0] == "approve"


async def test_deliver_one_completed_matching_is_applied():
    h = _Handle(status=WorkflowExecutionStatus.COMPLETED, result={"status": "Approved"})
    outcome, _ = await reconciler.deliver_one(h, "wf", "Approved")
    assert outcome == "applied"


async def test_deliver_one_completed_other_outcome_is_dead():
    h = _Handle(status=WorkflowExecutionStatus.COMPLETED, result={"status": "Rejected"})
    outcome, err = await reconciler.deliver_one(h, "wf", "Approved")
    assert outcome == "dead" and err


async def test_deliver_one_missing_workflow_is_dead():
    h = _Handle(missing=True)
    outcome, err = await reconciler.deliver_one(h, "wf", "Approved")
    assert outcome == "dead" and "not found" in err


async def test_deliver_one_timed_out_is_dead():
    h = _Handle(status=WorkflowExecutionStatus.TIMED_OUT)
    outcome, _ = await reconciler.deliver_one(h, "wf", "Rejected")
    assert outcome == "dead"


async def test_deliver_one_transient_result_failure_propagates_not_dead():
    """P1: a COMPLETED run whose result() fails transiently must RAISE (→ caller retries), not
    be dead-lettered — a network blip on a good result is not an authoritative mismatch."""
    h = _Handle(status=WorkflowExecutionStatus.COMPLETED, result_raises=True)
    with pytest.raises(RPCError):
        await reconciler.deliver_one(h, "wf", "Approved")


class _Reg:
    """Records the update_delivery calls the reconciler makes (and the token it fenced with)."""
    def __init__(self, claimed):  # noqa: ANN001
        self._claimed = claimed
        self.updates: list = []

    async def claim_deliveries(self, *, limit, lease_seconds):  # noqa: ANN001
        return self._claimed

    async def update_delivery(self, workflow_id, status, *, claim_token, error=None,  # noqa: ANN001
                              backoff_seconds=60):
        self.updates.append((workflow_id, status, claim_token))


class _Client:
    def __init__(self, handles):  # noqa: ANN001
        self._handles = handles

    def get_workflow_handle(self, wf):  # noqa: ANN001
        return self._handles[wf]


async def test_reconcile_tenant_applies_retries_and_fences_with_claim_token():
    handles = {
        "wf-applied": _Handle(WorkflowExecutionStatus.COMPLETED, {"status": "Approved"}),
        "wf-running": _Handle(WorkflowExecutionStatus.RUNNING),
        "wf-highattempts": _Handle(WorkflowExecutionStatus.RUNNING),
        "wf-blip": _Handle(WorkflowExecutionStatus.COMPLETED, result_raises=True),
    }
    reg = _Reg([
        {"workflow_id": "wf-applied", "decision": "Approved", "attempts": 1, "claim_token": "t1"},
        {"workflow_id": "wf-running", "decision": "Approved", "attempts": 2, "claim_token": "t2"},
        {"workflow_id": "wf-highattempts", "decision": "Approved", "attempts": 999,
         "claim_token": "t3"},
        {"workflow_id": "wf-blip", "decision": "Approved", "attempts": 5, "claim_token": "t4"},
    ])
    n = await reconciler.reconcile_tenant(
        _Client(handles), reg, batch=10, lease_seconds=60, backoff_seconds=60)
    assert n == 4
    outcomes = {wf: (status, token) for wf, status, token in reg.updates}
    assert outcomes["wf-applied"] == ("applied", "t1")
    assert outcomes["wf-running"] == ("retry", "t2")
    # High attempt count does NOT dead-letter — a running workflow keeps retrying.
    assert outcomes["wf-highattempts"] == ("retry", "t3")
    # A transient result-read blip on a completed run → retry (not a false dead-letter).
    assert outcomes["wf-blip"] == ("retry", "t4")
