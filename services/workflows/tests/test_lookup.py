"""GET /v1/workflows — subject → runs discovery.

The id-construction (``{prefix}-{tenantSlug}-{subject_id}``) and retry-suffix
(``-r2, -r3, …``) rules are SERVER-side: a UI asks "the runs for this lead's
conversion" and gets every attempt, newest first, with ready-made action URLs —
it never builds a workflow id itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings

pytestmark = pytest.mark.asyncio


class _Desc:
    def __init__(self, status: WorkflowExecutionStatus) -> None:
        self.status = status
        self.run_id = "run-1"
        self.start_time = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        self.close_time = None

    async def memo_value(self, key, default=None):  # noqa: ANN001
        return default


class _Handle:
    def __init__(self, desc: _Desc | None) -> None:
        self._desc = desc

    async def describe(self) -> _Desc:
        if self._desc is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return self._desc

    async def query(self, name):  # noqa: ANN001
        return "Awaiting committee decision"


class _FakeTemporal:
    """Knows a fixed set of workflow ids; every other id is a Temporal NOT_FOUND."""

    def __init__(self, known: dict[str, _Desc]) -> None:
        self.known = known

    def get_workflow_handle(self, wf_id):  # noqa: ANN001
        return _Handle(self.known.get(wf_id))


def _app(monkeypatch, temporal):  # noqa: ANN001
    # Dev posture (no signing/OIDC): the lookup itself is what's under test.
    for var in ("WORKFLOWS_INTERNAL_SIGNING_SECRET", "WORKFLOWS_OIDC_ISSUER",
                "WORKFLOWS_REQUIRE_AUTH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WORKFLOWS_REGISTER_TENANT", "EVAM")
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    app.state.oidc = None
    app.state.temporal = temporal
    return app


async def _get(app, params):  # noqa: ANN001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.get("/v1/workflows", params=params,
                           headers={"X-Tenant": "EVAM"})


def _slug() -> str:
    import hashlib
    return "EVAM" + hashlib.sha256(b"EVAM").hexdigest()[:10]


async def test_lookup_returns_every_attempt_newest_first(monkeypatch):
    base = f"leadconv-{_slug()}-LEAD1"
    temporal = _FakeTemporal({
        base: _Desc(WorkflowExecutionStatus.COMPLETED),        # rejected attempt
        f"{base}-r2": _Desc(WorkflowExecutionStatus.RUNNING),  # live retry
    })
    r = await _get(_app(monkeypatch, temporal),
                   {"kind": "lead-conversion", "subject_id": "LEAD1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    # Newest attempt first, and it is `current` — the only one a decision can land on.
    assert body["current"]["workflow_id"] == f"{base}-r2"
    assert body["current"]["status"] == "RUNNING"
    assert body["current"]["stage"] == "Awaiting committee decision"
    assert body["current"]["approve_url"] == f"/v1/workflows/{base}-r2/approve"
    assert body["current"]["reject_url"] == f"/v1/workflows/{base}-r2/reject"
    assert [run["workflow_id"] for run in body["runs"]] == [f"{base}-r2", base]


async def test_lookup_names_the_dedicated_decision_route(monkeypatch):
    base = f"struct-{_slug()}-DEAL1"
    temporal = _FakeTemporal({base: _Desc(WorkflowExecutionStatus.RUNNING)})
    r = await _get(_app(monkeypatch, temporal),
                   {"kind": "deal-structuring", "subject_id": "DEAL1"})
    assert r.status_code == 200
    current = r.json()["current"]
    assert current["decision_url"] == f"/v1/workflows/{base}/committee-decision"
    assert "approve_url" not in current


async def test_lookup_serves_the_subject_keyed_handover_approve_url(monkeypatch):
    # The handover checker approval is keyed by the LENDING id, not the workflow id —
    # the lookup still serves it ready-made so no client constructs it.
    base = f"handover-{_slug()}-LEND1"
    temporal = _FakeTemporal({base: _Desc(WorkflowExecutionStatus.RUNNING)})
    r = await _get(_app(monkeypatch, temporal),
                   {"kind": "advaya-handover", "subject_id": "LEND1"})
    assert r.status_code == 200
    assert (r.json()["current"]["approve_url"]
            == "/v1/workflows/advaya-handover/LEND1/approve")


async def test_lookup_with_no_runs_is_an_empty_200_not_a_404(monkeypatch):
    r = await _get(_app(monkeypatch, _FakeTemporal({})),
                   {"kind": "lead-conversion", "subject_id": "NOPE"})
    assert r.status_code == 200
    assert r.json() == {"kind": "lead-conversion", "subject_id": "NOPE",
                        "count": 0, "current": None, "runs": []}


async def test_lookup_refuses_an_unknown_kind(monkeypatch):
    r = await _get(_app(monkeypatch, _FakeTemporal({})),
                   {"kind": "bogus", "subject_id": "X"})
    assert r.status_code == 422
    assert "lead-conversion" in r.json()["error"]["detail"]
