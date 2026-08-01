"""Increment 2 — VOX + lead lifecycle completion.

* Ambiguous-company confirmation: close candidates but no exact match parks the capture for
  the RM instead of silently creating a near-duplicate company (flag-gated).
* Multi-active-lead selection: deterministic ranking (owning RM > lens > sector > recency),
  with a human tiebreak only on a genuine tie (flag-gated).
* Configurable qualification checklist: the outcome is COMPUTED from per-item results
  against deployment-configured definitions, and recorded in the evidence.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.types import LeadQualificationInput, VoxTouchpoint
from app.workflows import (
    LeadQualificationWorkflow,
    VoxTouchpointWorkflow,
    _lead_rank,
    evaluate_checklist,
)

pytestmark = pytest.mark.asyncio


async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


def _vox_activities():
    return [activities.resolve_entity_candidates, activities.create_entity,
            activities.find_active_leads, activities.create_lead,
            activities.update_lead_touch, activities.log_touchpoint,
            activities.assign_lead_owner, activities.attach_evidence,
            activities.mark_lead_note]


# --------------------------------------------------------------------------------------- #
# Pure ranking + checklist semantics (exactly what runs inside the workflows)
# --------------------------------------------------------------------------------------- #
def test_lead_ranking_prefers_rm_then_lens_then_sector_then_recency():
    tp = VoxTouchpoint(company_name="X", performed_by="asha@evamfinance.com",
                       lens="Green", sector="Solar")
    mine = {"id": "a", "rm": "asha@evamfinance.com", "lens": "Wind", "sector": "Hydro"}
    lens_match = {"id": "b", "rm": "other@", "lens": "Green", "sector": "Solar"}
    newer = {"id": "c", "rm": "other@", "lens": "Wind", "sector": "Hydro",
             "last_interaction_date": "2026-07-30"}
    older = {"id": "d", "rm": "other@", "lens": "Wind", "sector": "Hydro",
             "last_interaction_date": "2026-01-01"}
    ranked = sorted([older, lens_match, newer, mine],
                    key=lambda ld: _lead_rank(ld, tp), reverse=True)
    # The RM's own lead wins outright; context beats recency; recency breaks the rest.
    assert [ld["id"] for ld in ranked] == ["a", "b", "c", "d"]
    # A genuine tie is equal SCORES (the human-tiebreak trigger), not equal full keys.
    assert _lead_rank(newer, tp)[0] == _lead_rank(older, tp)[0]
    assert _lead_rank(mine, tp)[0] != _lead_rank(newer, tp)[0]


def test_checklist_requires_every_required_item():
    items = [{"key": "kyc", "required": True, "passed": True},
             {"key": "financials", "required": True, "passed": False},
             {"key": "site-visit", "required": False, "passed": False}]
    out = evaluate_checklist(items)
    assert out == {"passed": False, "failed_required": ["financials"],
                   "items_total": 3, "items_passed": 1}
    items[1]["passed"] = True
    assert evaluate_checklist(items)["passed"] is True   # optional items never block


# --------------------------------------------------------------------------------------- #
# Company resolution: the ambiguity is explicit
# --------------------------------------------------------------------------------------- #
async def test_resolution_separates_exact_match_from_near_candidates(mock_register):
    mock_register.state.entities = [
        {"id": "e1", "legal_name": "EcoSoch Solar Pvt Ltd", "display_name": "EcoSoch Solar"},
        {"id": "e2", "legal_name": "EcoSoch Energy Ltd", "display_name": "EcoSoch Energy"},
        {"id": "e3", "legal_name": "GreenVolt Power", "display_name": "GreenVolt"},
    ]
    env = ActivityEnvironment()
    out = await env.run(activities.resolve_entity_candidates, "EcoSoch Solar", None)
    # Suffix-stripped exact match wins; the sibling company is a CANDIDATE, not a match;
    # the unrelated one is neither.
    assert out["exact"]["id"] == "e1"
    assert [c["id"] for c in out["candidates"]] == ["e2"]

    out = await env.run(activities.resolve_entity_candidates, "EcoSoch Hydro", None)
    assert out["exact"] is None
    assert {c["id"] for c in out["candidates"]} == {"e1", "e2"}

    out = await env.run(activities.resolve_entity_candidates, "Totally New Co", None)
    assert out["exact"] is None and out["candidates"] == []


# --------------------------------------------------------------------------------------- #
# Workflow journeys (Temporal test server; skipped where unavailable, run in CI)
# --------------------------------------------------------------------------------------- #
async def test_ambiguous_company_parks_until_confirmed(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    mock_register.state.entities = [
        {"id": "e1", "legal_name": "EcoSoch Solar Pvt Ltd", "display_name": "EcoSoch Solar"},
        {"id": "e2", "legal_name": "EcoSoch Energy Ltd", "display_name": "EcoSoch Energy"},
    ]
    async with env:
        tq = "vox-tq"
        async with Worker(env.client, task_queue=tq, workflows=[VoxTouchpointWorkflow],
                          activities=_vox_activities()):
            handle = await env.client.start_workflow(
                VoxTouchpointWorkflow.run,
                VoxTouchpoint(company_name="EcoSoch Hydro", capture_id="cap-1",
                              performed_by="asha@evamfinance.com", summary="site visit",
                              require_company_confirmation=True),
                id=f"vox-{uuid.uuid4().hex}", task_queue=tq)
            # The run PARKS and shows its candidates.
            pending = await handle.query(VoxTouchpointWorkflow.pending_confirmation)
            assert pending["kind"] == "company"
            assert {c["id"] for c in pending["candidates"]} == {"e1", "e2"}
            # An id the run never proposed is IGNORED (whitelist, not trust)…
            await handle.signal(VoxTouchpointWorkflow.confirm_company,
                                args=["evil-id", "mallory@x"])
            assert (await handle.query(
                VoxTouchpointWorkflow.pending_confirmation))["kind"] == "company"
            # …a legitimate candidate resolves it.
            await handle.signal(VoxTouchpointWorkflow.confirm_company,
                                args=["e2", "asha@evamfinance.com"])
            result = await handle.result()
    assert result.entity_id == "e2" and result.entity_created is False
    # No duplicate company was created.
    assert len(mock_register.state.entities) == 2


async def test_confirming_create_new_registers_the_company(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    mock_register.state.entities = [
        {"id": "e1", "legal_name": "EcoSoch Solar Pvt Ltd", "display_name": "EcoSoch Solar"}]
    async with env:
        tq = "vox-tq"
        async with Worker(env.client, task_queue=tq, workflows=[VoxTouchpointWorkflow],
                          activities=_vox_activities()):
            handle = await env.client.start_workflow(
                VoxTouchpointWorkflow.run,
                VoxTouchpoint(company_name="EcoSoch Marine", capture_id="cap-2",
                              performed_by="asha@evamfinance.com", summary="intro call",
                              require_company_confirmation=True),
                id=f"vox-{uuid.uuid4().hex}", task_queue=tq)
            await handle.signal(VoxTouchpointWorkflow.confirm_company,
                                args=["", "asha@evamfinance.com"])   # "" = genuinely new
            result = await handle.result()
    assert result.entity_created is True
    assert len(mock_register.state.entities) == 2


async def test_multiple_active_leads_pick_the_rms_own_lead(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    mock_register.state.entities = [
        {"id": "e1", "legal_name": "GreenVolt Power", "display_name": "GreenVolt"}]
    mock_register.state.leads = {
        "l1": {"id": "l1", "entity_id": "e1", "status": "Active", "rm": "other@x",
               "last_interaction_date": "2026-07-30"},
        "l2": {"id": "l2", "entity_id": "e1", "status": "Active",
               "rm": "asha@evamfinance.com", "last_interaction_date": "2026-01-01"},
    }
    async with env:
        tq = "vox-tq"
        async with Worker(env.client, task_queue=tq, workflows=[VoxTouchpointWorkflow],
                          activities=_vox_activities()):
            result = await env.client.execute_workflow(
                VoxTouchpointWorkflow.run,
                VoxTouchpoint(company_name="GreenVolt Power", capture_id="cap-3",
                              performed_by="asha@evamfinance.com", summary="follow-up"),
                id=f"vox-{uuid.uuid4().hex}", task_queue=tq)
    # Ownership outranks recency — the touchpoint lands on the RM's OWN lead.
    assert result.lead_id == "l2" and result.lead_created is False


async def test_checklist_drives_the_qualification_outcome(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    lead_id = uuid.uuid4().hex
    mock_register.state.leads[lead_id] = {"id": lead_id, "status": "Active", "version": 1}
    checklist = [
        {"key": "kyc", "label": "KYC complete", "required": True, "passed": True},
        {"key": "financials", "label": "3y financials", "required": True, "passed": False},
    ]
    async with env:
        tq = "vox-tq"
        async with Worker(env.client, task_queue=tq, workflows=[LeadQualificationWorkflow],
                          activities=_vox_activities()):
            result = await env.client.execute_workflow(
                LeadQualificationWorkflow.run,
                LeadQualificationInput(lead_id=lead_id, qualified_by="head@evamfinance.com",
                                       passed=True,     # asserted pass is OVERRIDDEN
                                       checklist=checklist),
                id=f"qual-{uuid.uuid4().hex}", task_queue=tq)
    assert result.status == "NotQualified"
    assert result.checklist_summary["failed_required"] == ["financials"]
    # The evidence records the failure, immutably, under the failed kind.
    kinds = {e["evidence_kind"] for e in mock_register.state.evidence
             if e["subject_id"] == lead_id}
    assert "lead_qualification_failed" in kinds


# --------------------------------------------------------------------------------------- #
# The confirmation ENDPOINTS: verified identity, tenant binding, and "actually waiting"
# --------------------------------------------------------------------------------------- #
def _slug(tenant: str) -> str:
    import hashlib
    import re
    alnum = re.sub(r"[^A-Za-z0-9]", "", tenant) or "T"
    return f"{alnum}{hashlib.sha256(tenant.encode()).hexdigest()[:10]}"


class _FakeDesc:
    def __init__(self, memo=None) -> None:  # noqa: ANN001
        from temporalio.client import WorkflowExecutionStatus
        self.status = WorkflowExecutionStatus.RUNNING
        self._memo = memo or {}
        self.run_id = "run-1"
        self.workflow_type = "VoxTouchpointWorkflow"
        self.start_time = None
        self.close_time = None

    async def memo_value(self, key, default=None):  # noqa: ANN001
        return self._memo.get(key, default)


class _FakeHandle:
    def __init__(self, pending=None, memo=None) -> None:  # noqa: ANN001
        self.signals: list = []
        self._pending = pending or {}
        self._memo = memo or {"initiator": "asha@evamfinance.com", "tenant": "EVAM"}

    async def describe(self):
        return _FakeDesc(self._memo)

    async def query(self, name):  # noqa: ANN001
        assert name == "pending_confirmation"
        return self._pending

    async def signal(self, name, args=None):  # noqa: ANN001
        self.signals.append((name, args))


def _api(monkeypatch, mock_register, handle):  # noqa: ANN001
    import httpx

    from app.config import get_settings
    monkeypatch.setenv("WORKFLOWS_INTERNAL_SIGNING_SECRET", "sign-secret")
    monkeypatch.setenv("WORKFLOWS_REGISTER_BASE_URL", "http://reg")
    monkeypatch.setenv("WORKFLOWS_REGISTER_TENANT", "EVAM")
    get_settings.cache_clear()
    from app.api import create_app

    class _FakeTemporal:
        def get_workflow_handle(self, wf):  # noqa: ANN001
            return handle

    app = create_app()
    app.state.oidc = None
    app.state.temporal = _FakeTemporal()
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_register),
                                       base_url="http://reg")
    return app


async def _post(app, path, body):  # noqa: ANN001
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(path, json=body, headers={"X-Tenant": "EVAM"})


async def test_confirm_company_delivers_only_while_awaiting(monkeypatch, mock_register):
    wf_id = f"vox-{_slug('EVAM')}-cap1"
    waiting = _FakeHandle(pending={"kind": "company", "candidates": [{"id": "e2"}]})
    app = _api(monkeypatch, mock_register, waiting)
    r = await _post(app, f"/v1/workflows/{wf_id}/confirm-company",
                    {"entity_id": "e2", "by": "asha@evamfinance.com"})
    assert r.status_code == 200, r.text
    # The signal carries the VERIFIED identity, not whatever the body claimed.
    assert waiting.signals == [("confirm_company", ["e2", "asha@evamfinance.com"])]

    idle = _FakeHandle(pending={})
    app = _api(monkeypatch, mock_register, idle)
    r = await _post(app, f"/v1/workflows/{wf_id}/confirm-company",
                    {"entity_id": "e2", "by": "asha@evamfinance.com"})
    assert r.status_code == 409          # not awaiting anything → refused, not queued
    assert not idle.signals


async def test_select_lead_requires_the_matching_gate(monkeypatch, mock_register):
    wf_id = f"vox-{_slug('EVAM')}-cap2"
    # Awaiting a COMPANY confirmation — a lead selection is the wrong answer: 409.
    company_gate = _FakeHandle(pending={"kind": "company", "candidates": []})
    app = _api(monkeypatch, mock_register, company_gate)
    r = await _post(app, f"/v1/workflows/{wf_id}/select-lead",
                    {"lead_id": "l1", "by": "asha@evamfinance.com"})
    assert r.status_code == 409
    lead_gate = _FakeHandle(pending={"kind": "lead", "candidates": [{"id": "l1"}]})
    app = _api(monkeypatch, mock_register, lead_gate)
    r = await _post(app, f"/v1/workflows/{wf_id}/select-lead",
                    {"lead_id": "l1", "by": "asha@evamfinance.com"})
    assert r.status_code == 200, r.text
    assert lead_gate.signals == [("select_lead", ["l1", "asha@evamfinance.com"])]


async def test_configured_checklist_is_merged_and_enforced_at_the_door(monkeypatch,
                                                                       mock_register):
    """With a deployment checklist configured, a qualification request must answer every
    defined item (unknown keys refused), and the workflow input carries the MERGED items —
    definitions from config, results from the caller."""
    import json

    monkeypatch.setenv("WORKFLOWS_QUALIFICATION_CHECKLIST", json.dumps([
        {"key": "kyc", "label": "KYC complete", "required": True},
        {"key": "financials", "label": "3y financials", "required": True},
    ]))
    started: list = []

    class _Handle:
        id = "qual-x"

    class _FakeTemporal:
        async def start_workflow(self, run, arg, **kw):  # noqa: ANN001, ANN003
            started.append(arg)
            return _Handle()

        def get_workflow_handle(self, wf):  # noqa: ANN001
            return _Handle()

    import httpx

    from app.config import get_settings
    monkeypatch.setenv("WORKFLOWS_INTERNAL_SIGNING_SECRET", "")
    get_settings.cache_clear()
    from app.api import create_app
    app = create_app()
    app.state.oidc = None
    app.state.temporal = _FakeTemporal()
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_register),
                                       base_url="http://reg")

    async def post(body):  # noqa: ANN001
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://orch"
        ) as c:
            return await c.post("/v1/workflows/lead-qualifications", json=body,
                                headers={"X-Tenant": "EVAM"})

    base = {"lead_id": "l1", "qualified_by": "head@evamfinance.com"}
    # Missing a defined item → refused at the door.
    r = await post({**base, "checklist": [{"key": "kyc", "passed": True}]})
    assert r.status_code == 422 and "financials" in r.text
    # An unknown key → refused.
    r = await post({**base, "checklist": [{"key": "kyc", "passed": True},
                                          {"key": "financials", "passed": True},
                                          {"key": "made-up", "passed": True}]})
    assert r.status_code == 422 and "made-up" in r.text
    # Complete → started, with the MERGED checklist on the workflow input.
    r = await post({**base, "checklist": [{"key": "kyc", "passed": True},
                                          {"key": "financials", "passed": False,
                                           "note": "FY24 missing"}]})
    assert r.status_code == 202, r.text
    merged = started[-1].checklist
    assert [(i["key"], i["required"], i["passed"]) for i in merged] == [
        ("kyc", True, True), ("financials", True, False)]
    assert merged[1]["label"] == "3y financials" and merged[1]["note"] == "FY24 missing"
    get_settings.cache_clear()
