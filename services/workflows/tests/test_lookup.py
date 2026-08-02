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
    def __init__(self, status: WorkflowExecutionStatus,
                 stage: str = "Awaiting committee decision",
                 initiator: str = "rm@evamfinance.com",
                 started: datetime | None = None) -> None:
        self.status = status
        self.stage = stage
        self.initiator = initiator
        self.run_id = "run-1"
        self.start_time = started or datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        self.close_time = None

    async def memo_value(self, key, default=None):  # noqa: ANN001
        return self.initiator if key == "initiator" else default


class _Handle:
    def __init__(self, desc: _Desc | None) -> None:
        self._desc = desc

    async def describe(self) -> _Desc:
        if self._desc is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return self._desc

    async def query(self, name):  # noqa: ANN001
        return self._desc.stage if self._desc else None


class _Listed:
    """One row of a WorkflowType listing — the shape /pending reads (id, start_time)."""

    def __init__(self, wf_id: str, desc: _Desc) -> None:
        self.id = wf_id
        self.start_time = desc.start_time


class _FakeTemporal:
    """Knows a fixed set of workflow ids; every other id is a Temporal NOT_FOUND.
    ``list_workflows`` serves the same ids grouped by workflow-type name."""

    def __init__(self, known: dict[str, _Desc],
                 types: dict[str, list[str]] | None = None) -> None:
        self.known = known
        self.types = types or {}

    def get_workflow_handle(self, wf_id):  # noqa: ANN001
        return _Handle(self.known.get(wf_id))

    def list_workflows(self, query: str):
        import re as _re
        wtype = _re.search(r"WorkflowType = '([^']+)'", query).group(1)
        rows = [_Listed(wf_id, self.known[wf_id]) for wf_id in self.types.get(wtype, [])]

        async def _gen():
            for row in rows:
                yield row
        return _gen()


class _FakeHttp:
    """Serves the register checker queues by URL fragment — and HONOURS the ?status=
    filter like the real register does, so a wrong status in the query surfaces as an
    empty queue here too (that exact mock drift once hid a Prepared-vs-Completed bug)."""

    def __init__(self, queues: dict[str, list] | None = None) -> None:
        self.queues = queues or {}

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        parsed = httpx.URL(url)
        want = parsed.params.get("status")
        for frag, rows in self.queues.items():
            if frag in parsed.path:
                hits = [r for r in rows if not want or r.get("status") == want]
                return httpx.Response(200, json=hits,
                                      request=httpx.Request("GET", url))
        return httpx.Response(404, json={"error": "nope"},
                              request=httpx.Request("GET", url))


def _app(monkeypatch, temporal, http=None):  # noqa: ANN001
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
    app.state.http = http or _FakeHttp({})
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


# ---------------------------------------------------------------------------------------- #
# GET /v1/workflows/pending — the tenant-wide approver Today list
# ---------------------------------------------------------------------------------------- #
async def _get_pending(app, params=None):  # noqa: ANN001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.get("/v1/workflows/pending", params=params or {},
                           headers={"X-Tenant": "EVAM"})


async def test_pending_lists_every_parked_approval_across_subjects(monkeypatch):
    slug = _slug()
    conv1 = f"leadconv-{slug}-LEAD1"
    conv2 = f"leadconv-{slug}-LEAD2-r2"          # retry attempt of another lead
    parked = f"struct-{slug}-DEAL1"
    midflight = f"struct-{slug}-DEAL2"
    other_tenant = "leadconv-OTHERffffffffff-LEADX"
    temporal = _FakeTemporal(
        known={
            conv1: _Desc(WorkflowExecutionStatus.RUNNING, stage="Pending",
                         started=datetime(2026, 8, 1, 9, 0, tzinfo=UTC)),
            conv2: _Desc(WorkflowExecutionStatus.RUNNING, stage="Pending",
                         started=datetime(2026, 8, 1, 11, 0, tzinfo=UTC)),
            parked: _Desc(WorkflowExecutionStatus.RUNNING,
                          stage="Awaiting committee decision",
                          started=datetime(2026, 8, 1, 10, 0, tzinfo=UTC)),
            midflight: _Desc(WorkflowExecutionStatus.RUNNING,
                             stage="Circulating credit note"),
            other_tenant: _Desc(WorkflowExecutionStatus.RUNNING, stage="Pending"),
        },
        types={"LeadConversionWorkflow": [conv1, conv2, other_tenant],
               "DealStructuringWorkflow": [parked, midflight]})
    r = await _get_pending(_app(monkeypatch, temporal))
    assert r.status_code == 200, r.text
    body = r.json()
    # The mid-flight structuring and the other tenant's run are excluded.
    assert body["count"] == 3
    assert [p["workflow_id"] for p in body["pending"]] == [conv1, parked, conv2]  # oldest first
    lead2 = next(p for p in body["pending"] if p["workflow_id"] == conv2)
    assert lead2["subject_id"] == "LEAD2"        # retry suffix stripped from the subject
    assert lead2["approve_url"] == f"/v1/workflows/{conv2}/approve"
    deal = next(p for p in body["pending"] if p["kind"] == "deal-structuring")
    assert deal["decision_url"] == f"/v1/workflows/{parked}/committee-decision"
    assert deal["requested_by"] == "rm@evamfinance.com"


async def test_pending_kind_filter_and_unknown_kind(monkeypatch):
    slug = _slug()
    conv = f"leadconv-{slug}-LEAD1"
    temporal = _FakeTemporal(
        known={conv: _Desc(WorkflowExecutionStatus.RUNNING, stage="Pending")},
        types={"LeadConversionWorkflow": [conv]})
    app = _app(monkeypatch, temporal)
    r = await _get_pending(app, {"kind": "deal-structuring"})
    assert r.status_code == 200 and r.json()["count"] == 0
    r = await _get_pending(app, {"kind": "lead-conversion"})
    assert r.status_code == 200 and r.json()["count"] == 1
    r = await _get_pending(app, {"kind": "bogus"})
    assert r.status_code == 422


async def test_pending_includes_the_register_checker_queues(monkeypatch):
    """CP/CS checklists and handover packages awaiting a checker are Prepared REGISTER
    rows (their prepare-workflows complete immediately) — the Today list reads them
    from the Register so the checker sees them regardless of which lane prepared them."""
    http = _FakeHttp({
        # A maker-finished checklist is 'Completed' (vocabulary: Draft | Completed |
        # Approved | Returned); a package awaiting its check is 'Prepared'. The extra
        # rows prove the queues filter: a Returned checklist and an Accepted package
        # must NOT appear as pending.
        "/v1/internal/cpcs-checklists": [
            {"id": "chk-1", "lending_id": "LEND9", "checklist_version": 2,
             "status": "Completed", "prepared_by": "maker@evamfinance.com",
             "created_at": "2026-08-01T09:00:00+00:00"},
            {"id": "chk-0", "lending_id": "LEND9", "checklist_version": 1,
             "status": "Returned", "prepared_by": "maker@evamfinance.com",
             "created_at": "2026-07-30T09:00:00+00:00"}],
        "/v1/internal/handover-packages": [
            {"id": "pkg-1", "lending_id": "LEND9", "status": "Prepared",
             "prepared_by": "maker@evamfinance.com",
             "created_at": "2026-08-01T10:00:00+00:00"},
            {"id": "pkg-0", "lending_id": "LEND8", "status": "Accepted",
             "prepared_by": "maker@evamfinance.com",
             "created_at": "2026-07-29T10:00:00+00:00"}]})
    app = _app(monkeypatch, _FakeTemporal({}), http)
    r = await _get_pending(app)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    cp = next(p for p in body["pending"] if p["kind"] == "cpcs-checklist")
    assert cp["subject_id"] == "LEND9" and cp["checklist_version"] == 2
    assert cp["approve_url"] == "/v1/workflows/cpcs-checklists/chk-1/approve"
    assert cp["status"] == "Completed" and cp["stage"] == "Awaiting checker approval"
    ho = next(p for p in body["pending"] if p["kind"] == "advaya-handover")
    assert ho["approve_url"] == "/v1/workflows/advaya-handover/LEND9/approve"
    # The kind filter reaches the register-sourced kinds too.
    r = await _get_pending(app, {"kind": "cpcs-checklist"})
    assert r.json()["count"] == 1
