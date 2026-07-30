"""The orchestrator records a conversion decision DURABLY (synchronously, in the Register)
BEFORE it signals the workflow — and if that persist fails, it does NOT signal, so a decision
is never acknowledged unless it is already recorded."""

from __future__ import annotations

import hashlib
import re

import httpx
import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _slug(tenant: str) -> str:
    alnum = re.sub(r"[^A-Za-z0-9]", "", tenant) or "T"
    return f"{alnum}{hashlib.sha256(tenant.encode()).hexdigest()[:10]}"


class _FakeDesc:
    """Mirrors the real Temporal description: ``memo`` is an ASYNC accessor (memo_value),
    NOT a dict property — so a test that reads memo the wrong way fails here too."""

    def __init__(self, status, memo=None, run_id="run-1", workflow_type="X") -> None:  # noqa: ANN001
        self.status = status
        self._memo = memo or {}
        self.run_id = run_id
        self.workflow_type = workflow_type
        self.start_time = None
        self.close_time = None

    async def memo_value(self, key, default=None):  # noqa: ANN001
        return self._memo.get(key, default)


class _FakeHandle:
    def __init__(self, status=WorkflowExecutionStatus.RUNNING, missing=False,  # noqa: ANN001
                 memo=None, result=None, signal_closes_to=None,
                 signal_transient=False) -> None:
        self.signals: list = []
        self._status = status
        self._missing = missing
        self._memo = memo or {}
        self._result = result
        # If set, describe() reports RUNNING until a signal is attempted, then flips to this
        # (closed) status and the signal raises — simulating a close in the race window.
        self._signal_closes_to = signal_closes_to
        # If true, signal raises a TRANSIENT RPCError but the run stays RUNNING (a Temporal blip).
        self._signal_transient = signal_transient
        self._closed = False

    async def describe(self):
        if self._missing:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        status = self._signal_closes_to if self._closed else self._status
        return _FakeDesc(status, self._memo)

    async def result(self):
        return self._result

    async def query(self, name):  # noqa: ANN001
        return "Pending"

    async def signal(self, name, args=None):  # noqa: ANN001
        if self._signal_transient:
            # Transport blip: the run is UNAFFECTED (stays RUNNING).
            raise RPCError("temporarily unavailable", RPCStatusCode.UNAVAILABLE, b"")
        if self._signal_closes_to is not None:
            self._closed = True
            raise RPCError("workflow closed", RPCStatusCode.NOT_FOUND, b"")
        self.signals.append((name, args))


class _FakeTemporal:
    def __init__(self, handle=None) -> None:  # noqa: ANN001
        self.handle = handle or _FakeHandle()

    def get_workflow_handle(self, wf):  # noqa: ANN001
        return self.handle


def _app(monkeypatch, mock_app, register_key="test-key", handle=None):
    monkeypatch.setenv("WORKFLOWS_INTERNAL_SIGNING_SECRET", "sign-secret")
    monkeypatch.setenv("WORKFLOWS_REGISTER_BASE_URL", "http://reg")
    monkeypatch.setenv("WORKFLOWS_REGISTER_API_KEY", register_key)
    monkeypatch.setenv("WORKFLOWS_REGISTER_TENANT", "EVAM")
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    app.state.oidc = None
    app.state.temporal = _FakeTemporal(handle)
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app),
                                       base_url="http://reg")
    return app


async def _approve(app, wf):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(f"/v1/workflows/{wf}/approve",
                            json={"by": "head@evamfinance.com", "note": "ok"},
                            headers={"X-Tenant": "EVAM"})


async def test_decision_is_persisted_then_signalled(mock_register, monkeypatch):
    app = _app(monkeypatch, mock_register)
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 200, r.text
    # A durable decision was recorded on the single-winner resource...
    assert wf in mock_register.state.decisions
    assert mock_register.state.decisions[wf]["decision"] == "Approved"
    # ...and only THEN was the workflow signalled, carrying the record id.
    signals = app.state.temporal.handle.signals
    assert len(signals) == 1
    name, args = signals[0]
    assert name == "approve"
    assert args[-1]  # decision_ref present (non-empty)
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_no_signal_when_persist_fails(mock_register, monkeypatch):
    # Register outage on the decision write → the endpoint must FAIL and must NOT signal.
    app = _app(monkeypatch, mock_register)
    mock_register.state.decision_write_fail = True
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 502, r.text
    assert app.state.temporal.handle.signals == []   # never signalled
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_opposite_decision_conflicts_and_is_not_signalled(mock_register, monkeypatch):
    # A decision already exists for this workflow; the opposite decision → 409, no signal.
    app = _app(monkeypatch, mock_register)
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    mock_register.state.decisions[wf] = {"id": "x", "workflow_id": wf, "decision": "Approved",
                                         "lead_id": "lead1", "decided_by": "h@e.com",
                                         "decided_by_id": "u", "roles": [], "operations": {},
                                         "views": {}, "note": None}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        r = await c.post(f"/v1/workflows/{wf}/reject",
                         json={"by": "head@evamfinance.com", "note": "no"},
                         headers={"X-Tenant": "EVAM"})
    assert r.status_code == 409, r.text
    assert app.state.temporal.handle.signals == []
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_decision_for_nonexistent_workflow_is_404_and_not_persisted(mock_register,
                                                                          monkeypatch):
    # The workflow does not exist → 404 BEFORE any decision row is written.
    app = _app(monkeypatch, mock_register, handle=_FakeHandle(missing=True))
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 404, r.text
    assert wf not in mock_register.state.decisions   # nothing persisted
    assert app.state.temporal.handle.signals == []
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_decision_for_completed_workflow_with_other_outcome_is_409(mock_register,
                                                                         monkeypatch):
    # Completed with a DIFFERENT outcome → 409, and no decision row is written.
    app = _app(monkeypatch, mock_register,
               handle=_FakeHandle(status=WorkflowExecutionStatus.COMPLETED,
                                  result={"status": "Rejected"}))
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)   # approving a run that already Rejected
    assert r.status_code == 409, r.text
    assert wf not in mock_register.state.decisions
    assert app.state.temporal.handle.signals == []
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_retry_after_applied_returns_authoritative_result(mock_register, monkeypatch):
    # P1: the signal was delivered and the run COMPLETED with THIS outcome, but the caller
    # lost the response and retried → return the authoritative applied result (200), not 409.
    app = _app(monkeypatch, mock_register,
               handle=_FakeHandle(status=WorkflowExecutionStatus.COMPLETED,
                                  result={"status": "Approved", "deal_id": "d-1"}))
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "already_applied"
    assert body["result"]["deal_id"] == "d-1"
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_transient_signal_failure_while_running_returns_503(mock_register, monkeypatch):
    # P1: a TRANSIENT signal failure while the run is still RUNNING must NOT be reported as a
    # closed-workflow conflict. The decision is persisted; the API asks the caller to retry
    # delivery (503) — a retry re-signals safely.
    app = _app(monkeypatch, mock_register,
               handle=_FakeHandle(status=WorkflowExecutionStatus.RUNNING,
                                  signal_transient=True))
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 503, r.text
    assert "retry delivery" in r.text.lower()
    assert wf in mock_register.state.decisions          # decision durably persisted
    assert app.state.temporal.handle.signals == []      # not (yet) delivered
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_close_in_the_signal_race_reconciles(mock_register, monkeypatch):
    # P1: RUNNING at describe, then the run closes right before the signal lands. The decision
    # was persisted; reconciliation returns the applied result rather than a misleading error.
    app = _app(monkeypatch, mock_register, handle=_FakeHandle(
        status=WorkflowExecutionStatus.RUNNING,
        signal_closes_to=WorkflowExecutionStatus.COMPLETED,
        result={"status": "Approved", "deal_id": "d-9"}))
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    r = await _approve(app, wf)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "already_applied"
    # The decision WAS durably persisted before the (failed) signal.
    assert wf in mock_register.state.decisions
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_retry_decision_records_the_real_lead_id_from_memo(mock_register, monkeypatch):
    # A retry attempt's id has a '-r2' suffix; the persisted lead_id must be the REAL lead id
    # from the memo, not the suffixed id.
    app = _app(monkeypatch, mock_register,
               handle=_FakeHandle(memo={"lead_id": "lead1", "tenant": "EVAM"}))
    wf = f"leadconv-{_slug('EVAM')}-lead1-r2"
    r = await _approve(app, wf)
    assert r.status_code == 200, r.text
    assert mock_register.state.decisions[wf]["lead_id"] == "lead1"   # not 'lead1-r2'
    await app.state.http.aclose()
    get_settings.cache_clear()


class _FakeOidc:
    def __init__(self, email) -> None:  # noqa: ANN001
        self._email = email

    async def verify(self, token):  # noqa: ANN001
        return type("_Id", (), {"email": self._email})()


async def test_status_is_readable_by_the_memo_initiator(mock_register, monkeypatch):
    """The initiator recorded in the workflow memo can read the run's status — proving memo is
    read via the async accessor (a dict-style read would 403 a legitimate requester)."""
    app = _app(monkeypatch, mock_register,
               handle=_FakeHandle(memo={"initiator": "rm@evamfinance.com", "tenant": "EVAM"}))
    wf = f"leadconv-{_slug('EVAM')}-lead1"

    async def get_status(email):
        app.state.oidc = _FakeOidc(email)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://orch"
        ) as c:
            return await c.get(f"/v1/workflows/{wf}",
                               headers={"X-Tenant": "EVAM", "Authorization": "Bearer t"})

    # The initiator is authorized...
    assert (await get_status("rm@evamfinance.com")).status_code == 200
    # ...a random same-tenant caller (not initiator, no approver role) is not.
    assert (await get_status("stranger@evamfinance.com")).status_code == 403
    await app.state.http.aclose()
    get_settings.cache_clear()


async def test_response_reports_authoritative_first_approver(mock_register, monkeypatch):
    # A pre-existing decision by a FIRST approver; a second, same-outcome caller must get the
    # FIRST approver back in the response (the record is authoritative), not their own name.
    app = _app(monkeypatch, mock_register)
    wf = f"leadconv-{_slug('EVAM')}-lead1"
    mock_register.state.decisions[wf] = {"id": "rec-1", "workflow_id": wf,
                                         "decision": "Approved", "lead_id": "lead1",
                                         "decided_by": "first@evamfinance.com",
                                         "decided_by_id": "u1", "roles": [], "operations": {},
                                         "views": {}, "note": "first"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        r = await c.post(f"/v1/workflows/{wf}/approve",
                         json={"by": "second@evamfinance.com", "note": "second"},
                         headers={"X-Tenant": "EVAM"})
    assert r.status_code == 200, r.text
    assert r.json()["by"] == "first@evamfinance.com"   # authoritative, not the latest caller
    await app.state.http.aclose()
    get_settings.cache_clear()
