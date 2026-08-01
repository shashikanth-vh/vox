"""The committee-decision ENDPOINT records one outcome PER FACILITY before it signals.

A facility-specific submission must cover exactly the deal's lending lines (no gaps, no
unknowns, no duplicates) — a committee cannot decide a facility that does not exist and
cannot leave one undecided. A grouped submission is still accepted, but it is RECORDED as a
separate per-facility decision for every line, so the audit trail always answers per
facility."""

from __future__ import annotations

import hashlib
import re

import httpx
import pytest
from temporalio.client import WorkflowExecutionStatus

from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _slug(tenant: str) -> str:
    alnum = re.sub(r"[^A-Za-z0-9]", "", tenant) or "T"
    return f"{alnum}{hashlib.sha256(tenant.encode()).hexdigest()[:10]}"


class _FakeDesc:
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
    def __init__(self, memo=None) -> None:  # noqa: ANN001
        self.signals: list = []
        self._memo = memo or {}

    async def describe(self):
        return _FakeDesc(WorkflowExecutionStatus.RUNNING, self._memo)

    async def query(self, name):  # noqa: ANN001
        return "Awaiting committee decision"

    async def signal(self, name, args=None):  # noqa: ANN001
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


def _two_line_deal(mock_register):  # noqa: ANN001
    import uuid
    did = uuid.uuid4().hex
    lid_a, lid_b = uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.deals[did] = {"id": did, "version": 1, "stage": "In Pipeline"}
    for lid in (lid_a, lid_b):
        mock_register.state.lending[lid] = {"id": lid, "deal_id": did, "version": 1,
                                            "stage": "Note Circulated"}
    return did, lid_a, lid_b


async def _decide(app, wf_id, body):  # noqa: ANN001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(f"/v1/workflows/{wf_id}/committee-decision", json=body,
                            headers={"X-Tenant": "EVAM"})


async def test_facility_specific_submission_records_each_outcome(monkeypatch, mock_register):
    did, lid_a, lid_b = _two_line_deal(mock_register)
    wf_id = f"struct-{_slug('EVAM')}-{did}"
    handle = _FakeHandle(memo={"deal_id": did})
    app = _app(monkeypatch, mock_register, handle)

    r = await _decide(app, wf_id, {
        "by": "chair@evamfinance.com",
        "facilities": [{"lending_id": lid_a, "approved": True,
                        "conditions": "quarterly covenant reporting", "valid_days": 90},
                       {"lending_id": lid_b, "approved": False,
                        "note": "tenor beyond policy"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    # A sanction happened → the deal-level submission record is Approved…
    assert body["decision"] == "Approved"
    # …and the response answers per facility (outcome + conditions + validity).
    assert body["facilities"][lid_a] == {"outcome": "Approved",
                                         "conditions": "quarterly covenant reporting",
                                         "valid_days": 90}
    assert body["facilities"][lid_b]["outcome"] == "Rejected"
    # One durable, subject-bound record PER FACILITY, each with its own outcome and note.
    rec_a = mock_register.state.decisions[f"{wf_id}:lending:{lid_a}"]
    rec_b = mock_register.state.decisions[f"{wf_id}:lending:{lid_b}"]
    assert rec_a["decision"] == "Approved"
    # The conditional approval is DURABLE per facility: conditions + validity live on the
    # record the workflow verifies, not just in the response.
    assert rec_a["conditions"] == "quarterly covenant reporting"
    assert rec_a["valid_days"] == 90
    assert rec_b["decision"] == "Rejected"
    assert rec_b["note"] == "tenor beyond policy"
    # The workflow was signalled only after everything was recorded.
    assert handle.signals


async def test_grouped_submission_still_records_per_facility(monkeypatch, mock_register):
    did, lid_a, lid_b = _two_line_deal(mock_register)
    wf_id = f"struct-{_slug('EVAM')}-{did}"
    handle = _FakeHandle(memo={"deal_id": did})
    app = _app(monkeypatch, mock_register, handle)

    r = await _decide(app, wf_id, {"by": "chair@evamfinance.com", "approved": True})
    assert r.status_code == 200, r.text
    assert {lid: f["outcome"] for lid, f in r.json()["facilities"].items()} == {
        lid_a: "Approved", lid_b: "Approved"}
    for lid in (lid_a, lid_b):
        assert mock_register.state.decisions[f"{wf_id}:lending:{lid}"]["decision"] == "Approved"


async def test_facilities_must_cover_exactly_the_deals_lines(monkeypatch, mock_register):
    did, lid_a, lid_b = _two_line_deal(mock_register)
    wf_id = f"struct-{_slug('EVAM')}-{did}"
    handle = _FakeHandle(memo={"deal_id": did})
    app = _app(monkeypatch, mock_register, handle)

    # A line left undecided → refused.
    r = await _decide(app, wf_id, {
        "by": "chair@evamfinance.com",
        "facilities": [{"lending_id": lid_a, "approved": True}]})
    assert r.status_code == 422, r.text
    # An unknown facility → refused.
    r = await _decide(app, wf_id, {
        "by": "chair@evamfinance.com",
        "facilities": [{"lending_id": lid_a, "approved": True},
                       {"lending_id": "nope", "approved": False}]})
    assert r.status_code == 422, r.text
    # A duplicate entry → refused.
    r = await _decide(app, wf_id, {
        "by": "chair@evamfinance.com",
        "facilities": [{"lending_id": lid_a, "approved": True},
                       {"lending_id": lid_a, "approved": False},
                       {"lending_id": lid_b, "approved": True}]})
    assert r.status_code == 422, r.text
    # Nothing was recorded and the workflow was never signalled by the refused attempts.
    assert f"{wf_id}:lending:{lid_a}" not in mock_register.state.decisions
    assert not handle.signals
