"""Increment 8 — the covenant clock and the EWS case clock.

The gate for this increment is the RECURRING and EXCEPTION paths:

* the covenant monitor loops the same sweep forever — each overdue submission and each
  lapsed waiver is reported (and alerted) EXACTLY once, because the Register's status
  flips make replays no-ops;
* the EWS case run trusts only the DURABLE case record: a nudge signal carries nothing;
  an investigation that outlives its SLA is AUTO-ESCALATED through the audited service
  route; closing the record ends the run with the disposition.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app import activities
from app.config import get_settings
from app.types import CovenantMonitorInput, EwsCaseInput
from app.workflows import CovenantMonitorWorkflow, EwsCaseWorkflow

pytestmark = pytest.mark.asyncio


async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


def _enable_notifications(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("WORKFLOWS_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOWS_NOTIFY_CHANNELS", "email")
    monkeypatch.setenv("WORKFLOWS_SMTP_HOST", "smtp.local")
    monkeypatch.delenv("WORKFLOWS_OPS_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------- #
# The covenant monitor: recurring sweep, exactly-once alerts
# --------------------------------------------------------------------------------------- #
async def test_covenant_monitor_alerts_overdue_and_lapsed_waivers(
        mock_register, monkeypatch):
    from temporalio.worker import Worker
    _enable_notifications(monkeypatch)
    try:
        env = await _env()
        oid, wid = uuid.uuid4().hex, uuid.uuid4().hex
        mock_register.state.covenant_report.update(
            generated=3,
            overdue=[{"id": oid, "covenant_name": "Quarterly DSCR certificate",
                      "due_date": "2026-07-15", "entity_id": "e1", "deal_id": "d1",
                      "period": "2026-07-15"}],
            waivers_expired=[{"id": wid, "covenant_name": "DSCR >= 1.20",
                              "due_date": "2026-03-31", "entity_id": "e1",
                              "deal_id": "d1", "period": "2026-03-31"}])
        async with env:
            tq = "cov-tq"
            async with Worker(env.client, task_queue=tq,
                              workflows=[CovenantMonitorWorkflow],
                              activities=[activities.sweep_covenants,
                                          activities.emit_operational_event]):
                handle = await env.client.start_workflow(
                    CovenantMonitorWorkflow.run,
                    CovenantMonitorInput(interval_hours=1000.0,
                                         notify=["credit@evamfinance.com"]),
                    id=f"cov-monitor-{uuid.uuid4().hex[:8]}", task_queue=tq)
                deadline = 100
                while (await handle.query(
                        CovenantMonitorWorkflow.state)).get("sweeps", 0) < 1:
                    deadline -= 1
                    assert deadline > 0, "monitor never swept"
                # A second, immediate sweep proves the RECURRING path is a no-op once
                # everything is reported (the mock mirrors the Register's status flips).
                await handle.signal(CovenantMonitorWorkflow.sweep_now)
                while (await handle.query(
                        CovenantMonitorWorkflow.state)).get("sweeps", 0) < 2:
                    deadline -= 1
                    assert deadline > 0, "second sweep never ran"
                await handle.signal(CovenantMonitorWorkflow.stop)
                result = await handle.result()

        assert result.sweeps == 2
        assert result.generated_total == 3
        assert result.overdue_total == 1 and result.waivers_expired_total == 1
        rows = list(mock_register.state.notifications.values())
        # One overdue warning + one critical waiver-expiry alert — and NOT doubled by
        # the second sweep.
        assert len(rows) == 2
        by_event = {r["event"]: r for r in rows}
        assert by_event["covenant_overdue"]["severity"] == "warning"
        assert by_event["covenant_waiver_expired"]["severity"] == "critical"
        assert all(r["recipient"] == "credit@evamfinance.com" for r in rows)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------------------- #
# The EWS case clock: SLA ladder + auto-escalation + record-driven closure
# --------------------------------------------------------------------------------------- #
def _seed_case(mock_register, status="Open", **extra):  # noqa: ANN001
    cid = uuid.uuid4().hex
    mock_register.state.ews[cid] = {
        "id": cid, "entity_id": "e1", "status": status, "severity": "Red",
        "title": "Covenant breach — DSCR", "opened_by": "rm@evamfinance.com",
        "assigned_to": None, "disposition": None, "closed_by": None, **extra}
    return cid


async def test_ews_case_auto_escalates_then_closes_from_the_record(
        mock_register, monkeypatch):
    from temporalio.worker import Worker
    _enable_notifications(monkeypatch)
    try:
        env = await _env()
        cid = _seed_case(mock_register)
        async with env:
            tq = "ews-tq"
            async with Worker(env.client, task_queue=tq, workflows=[EwsCaseWorkflow],
                              activities=[activities.get_resource,
                                          activities.auto_escalate_ews_case,
                                          activities.emit_operational_event]):
                handle = await env.client.start_workflow(
                    EwsCaseWorkflow.run,
                    EwsCaseInput(case_id=cid, assign_sla_hours=24.0,
                                 investigation_sla_hours=72.0,
                                 escalated_reminder_hours=48.0,
                                 notify=["credit@evamfinance.com"]),
                    id=f"ews-{cid}", task_queue=tq)
                # A forged nudge changes NOTHING — the record is still Open.
                await handle.signal(EwsCaseWorkflow.case_updated)
                state = await handle.query(EwsCaseWorkflow.state)
                assert state["case_status"] == "Open"
                # Drive the clock DETERMINISTICALLY: past the assign SLA (24h)…
                await env.sleep(timedelta(hours=25))
                state = await handle.query(EwsCaseWorkflow.state)
                assert state["reminded_unassigned"] is True
                assert state["auto_escalated"] is False
                # …then past the investigation SLA (72h) → AUTO-ESCALATED.
                await env.sleep(timedelta(hours=50))
                state = await handle.query(EwsCaseWorkflow.state)
                assert state["auto_escalated"] is True
                assert mock_register.state.ews[cid]["status"] == "Escalated"
                assert mock_register.state.ews[cid]["escalated_by"] == "system:sla"
                # The Credit Head closes the RECORD; the nudge makes the run see it.
                mock_register.state.ews[cid].update(
                    status="Closed", disposition="Resolved",
                    closed_by="ch@evamfinance.com")
                await handle.signal(EwsCaseWorkflow.case_updated)
                result = await handle.result()

        assert result.status == "Closed" and result.disposition == "Resolved"
        assert result.closed_by == "ch@evamfinance.com"
        assert result.auto_escalated is True
        events = {r["event"] for r in mock_register.state.notifications.values()}
        # The unassigned reminder fired at 24h, the auto-escalation alert at 72h.
        assert {"ews_unassigned", "ews_auto_escalated"} <= events
    finally:
        get_settings.cache_clear()


async def test_ews_case_closed_by_humans_never_escalates(mock_register, monkeypatch):
    from temporalio.worker import Worker
    _enable_notifications(monkeypatch)
    try:
        env = await _env()
        cid = _seed_case(mock_register, status="UnderInvestigation",
                         assigned_to="analyst@evamfinance.com")
        async with env:
            tq = "ews-tq"
            async with Worker(env.client, task_queue=tq, workflows=[EwsCaseWorkflow],
                              activities=[activities.get_resource,
                                          activities.auto_escalate_ews_case,
                                          activities.emit_operational_event]):
                handle = await env.client.start_workflow(
                    EwsCaseWorkflow.run,
                    EwsCaseInput(case_id=cid, assign_sla_hours=24.0,
                                 investigation_sla_hours=72.0),
                    id=f"ews-{cid}", task_queue=tq)
                # The analyst resolves it well inside the SLA.
                mock_register.state.ews[cid].update(
                    status="Closed", disposition="FalseAlarm",
                    closed_by="analyst@evamfinance.com")
                await handle.signal(EwsCaseWorkflow.case_updated)
                result = await handle.result()

        assert result.status == "Closed" and result.disposition == "FalseAlarm"
        assert result.auto_escalated is False
        assert mock_register.state.ews[cid]["status"] == "Closed"   # untouched by SLA
    finally:
        get_settings.cache_clear()
