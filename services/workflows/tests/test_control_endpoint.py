"""The run-control ENDPOINT (cancel / return-for-information / resubmit).

Same trust posture as a decision: verified identity, tenant-bound workflow id, and the
action is PERSISTED as an immutable control record BEFORE the run is signalled — the signal
is only a wake-up the workflow re-verifies against that record."""

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
    def __init__(self, status, memo=None, run_id="run-1") -> None:  # noqa: ANN001
        self.status = status
        self._memo = memo or {}
        self.run_id = run_id
        self.workflow_type = "X"
        self.start_time = None
        self.close_time = None

    async def memo_value(self, key, default=None):  # noqa: ANN001
        return self._memo.get(key, default)


class _FakeHandle:
    def __init__(self, status=WorkflowExecutionStatus.RUNNING, memo=None,  # noqa: ANN001
                 signal_fails=False) -> None:
        self.signals: list = []
        self._status = status
        self._memo = memo or {}
        self._signal_fails = signal_fails

    async def describe(self):
        return _FakeDesc(self._status, self._memo)

    async def signal(self, name, args=None):  # noqa: ANN001
        if self._signal_fails:
            raise RPCError("workflow closed", RPCStatusCode.NOT_FOUND, b"")
        self.signals.append((name, args))


class _FakeTemporal:
    def __init__(self, handle) -> None:  # noqa: ANN001
        self.handle = handle

    def get_workflow_handle(self, wf):  # noqa: ANN001
        return self.handle


def _app(monkeypatch, mock_app, handle):  # noqa: ANN001
    monkeypatch.setenv("WORKFLOWS_INTERNAL_SIGNING_SECRET", "sign-secret")
    monkeypatch.setenv("WORKFLOWS_REGISTER_BASE_URL", "http://reg")
    monkeypatch.setenv("WORKFLOWS_REGISTER_API_KEY", "test-key")
    monkeypatch.setenv("WORKFLOWS_REGISTER_TENANT", "EVAM")
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    app.state.oidc = None
    app.state.temporal = _FakeTemporal(handle)
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app),
                                       base_url="http://reg")
    return app


async def _control(app, wf_id, body):  # noqa: ANN001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(f"/v1/workflows/{wf_id}/control", json=body,
                            headers={"X-Tenant": "EVAM"})


async def test_cancel_is_recorded_durably_then_signalled(monkeypatch, mock_register):
    wf_id = f"leadconv-{_slug('EVAM')}-lead1"
    handle = _FakeHandle(memo={"lead_id": "lead1", "tenant": "EVAM",
                               "initiator": "rm@evamfinance.com"})
    app = _app(monkeypatch, mock_register, handle)

    r = await _control(app, wf_id, {"action": "cancel", "by": "rm@evamfinance.com",
                                    "note": "client withdrew"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "Cancelled"
    ref = body["control_ref"]
    assert ref.startswith(f"{wf_id}:control:")
    # Persist-before-signal: the durable record exists AND the signal carried the same ref.
    assert mock_register.state.decisions[ref]["decision"] == "Cancelled"
    assert handle.signals == [("control", ["Cancelled", ref])]


async def test_return_then_resubmit_records_two_separate_actions(monkeypatch, mock_register):
    wf_id = f"struct-{_slug('EVAM')}-deal1"
    handle = _FakeHandle(memo={"deal_id": "deal1", "tenant": "EVAM",
                               "initiator": "chair@evamfinance.com"})
    app = _app(monkeypatch, mock_register, handle)

    r1 = await _control(app, wf_id, {"action": "return", "by": "chair@evamfinance.com",
                                     "note": "need the latest financials"})
    r2 = await _control(app, wf_id, {"action": "resubmit",
                                     "by": "chair@evamfinance.com"})
    assert r1.status_code == 200 and r2.status_code == 200
    ref1, ref2 = r1.json()["control_ref"], r2.json()["control_ref"]
    # A run can go around the loop more than once — every action is its OWN immutable record.
    assert ref1 != ref2
    assert mock_register.state.decisions[ref1]["decision"] == "ReturnedForInformation"
    assert mock_register.state.decisions[ref2]["decision"] == "Resubmitted"
    assert [sig[1][0] for sig in handle.signals] == ["ReturnedForInformation",
                                                     "Resubmitted"]


async def test_control_on_a_closed_run_is_409(monkeypatch, mock_register):
    wf_id = f"leadconv-{_slug('EVAM')}-lead2"
    handle = _FakeHandle(status=WorkflowExecutionStatus.COMPLETED)
    app = _app(monkeypatch, mock_register, handle)
    r = await _control(app, wf_id, {"action": "cancel", "by": "rm@evamfinance.com"})
    assert r.status_code == 409, r.text
    assert not handle.signals


async def test_unknown_action_is_422(monkeypatch, mock_register):
    wf_id = f"leadconv-{_slug('EVAM')}-lead3"
    app = _app(monkeypatch, mock_register, _FakeHandle())
    r = await _control(app, wf_id, {"action": "pause", "by": "rm@evamfinance.com"})
    assert r.status_code == 422


async def test_close_race_reports_recorded_but_undelivered(monkeypatch, mock_register):
    """If the run closes between the durable record and the signal, the caller learns the
    truth: recorded, not delivered — never a silent success."""
    wf_id = f"leadconv-{_slug('EVAM')}-lead4"
    handle = _FakeHandle(signal_fails=True,
                         memo={"initiator": "rm@evamfinance.com", "tenant": "EVAM"})
    app = _app(monkeypatch, mock_register, handle)
    r = await _control(app, wf_id, {"action": "cancel", "by": "rm@evamfinance.com"})
    assert r.status_code == 409
    assert "recorded" in r.json()["error"]["detail"]
