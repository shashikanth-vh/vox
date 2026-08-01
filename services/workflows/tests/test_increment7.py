"""Increment 7 — notifications with retry, first-class calendar events, document expiry.

The gate for this increment is EXTERNAL FAILURE/RETRY BEHAVIOUR:

* the notifier sweep contains every channel failure — exponential backoff, then a loud
  dead-letter; one poisoned delivery can never stall the queue or crash the daemon;
* notification creation is idempotent (dedupe key) — an activity retry never
  double-notifies;
* the document-expiry monitor observes-and-records: the Register sweep is idempotent,
  every lapsed document raises a critical event + durable notifications, and warnings
  dedupe to one per document;
* the VOX follow-up becomes a real calendar event, idempotently per run.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities, notifier
from app.config import get_settings
from app.types import DocumentExpiryInput, VoxTouchpoint
from app.workflows import DocumentExpiryMonitorWorkflow, VoxTouchpointWorkflow

pytestmark = pytest.mark.asyncio


async def _env():
    try:
        from temporalio.testing import WorkflowEnvironment
        return await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")


# --------------------------------------------------------------------------------------- #
# Notifier: retry / dead-letter machinery (pure, injected senders — no mail server)
# --------------------------------------------------------------------------------------- #
async def test_backoff_is_exponential_and_capped():
    grow = [notifier.backoff_seconds(n, base=60, cap=3600) for n in (1, 2, 3, 4, 5, 6, 7)]
    assert grow == [60, 120, 240, 480, 960, 1920, 3600]     # doubles, then the cap holds


async def test_deliver_one_success_retry_and_dead_letter():
    async def ok(claim):  # noqa: ANN001
        return None

    async def down(claim):  # noqa: ANN001
        raise RuntimeError("SMTP connect refused")

    senders = {"email": ok}
    claim = {"channel": "email", "attempts": 1, "target": "rm@evamfinance.com"}
    assert (await notifier.deliver_one(claim, senders, max_attempts=3, backoff_base=60,
                                       backoff_cap=3600)) == ("delivered", None, 0)

    # A transient failure RETRIES with exponential backoff — the error is recorded, never raised.
    senders = {"email": down}
    outcome, err, backoff = await notifier.deliver_one(
        {**claim, "attempts": 2}, senders, max_attempts=3, backoff_base=60,
        backoff_cap=3600)
    assert outcome == "retry" and "SMTP connect refused" in err and backoff == 120

    # Attempts exhausted → dead-letter, loudly carrying the final error.
    outcome, err, _ = await notifier.deliver_one(
        {**claim, "attempts": 3}, senders, max_attempts=3, backoff_base=60,
        backoff_cap=3600)
    assert outcome == "dead" and "attempts exhausted (3/3)" in err

    # An unknown channel can never loop forever — straight to dead.
    outcome, err, _ = await notifier.deliver_one(
        {"channel": "pigeon", "attempts": 1}, senders, max_attempts=3,
        backoff_base=60, backoff_cap=3600)
    assert outcome == "dead" and "unknown channel" in err


class _FakeReg:
    """Records exactly what the sweep writes back — the register-side contract."""

    def __init__(self, claims):  # noqa: ANN001
        self._claims = claims
        self.updates: list[dict] = []
        self.update_fail = False

    async def claim_notification_deliveries(self, *, limit, lease_seconds):  # noqa: ANN001, ARG002
        return list(self._claims)

    async def update_notification_delivery(self, delivery_id, status, *, claim_token,
                                           error=None, backoff_seconds=60):  # noqa: ANN001
        if self.update_fail:
            raise RuntimeError("register briefly unreachable")
        self.updates.append({"id": delivery_id, "status": status, "token": claim_token,
                             "error": error, "backoff": backoff_seconds})


async def test_sweep_contains_failures_and_writes_back_each_outcome():
    async def flaky(claim):  # noqa: ANN001 - fails only the sms channel
        if claim["channel"] == "sms":
            raise RuntimeError("provider 503")

    claims = [
        {"delivery_id": "d1", "channel": "email", "target": "a@x", "attempts": 1,
         "claim_token": "t1"},
        {"delivery_id": "d2", "channel": "sms", "target": "+91-1", "attempts": 1,
         "claim_token": "t2"},
        {"delivery_id": "d3", "channel": "sms", "target": "+91-2", "attempts": 8,
         "claim_token": "t3"},
    ]
    reg = _FakeReg(claims)
    senders = {"email": flaky, "sms": flaky}
    n = await notifier.sweep_tenant(reg, senders, batch=10, lease_seconds=60,
                                    max_attempts=8, backoff_base=60, backoff_cap=3600)
    assert n == 3
    by_id = {u["id"]: u for u in reg.updates}
    assert by_id["d1"]["status"] == "delivered"
    assert by_id["d2"]["status"] == "retry" and by_id["d2"]["backoff"] == 60
    assert by_id["d3"]["status"] == "dead"          # 8th attempt of 8 → dead-letter

    # Even the STATUS WRITE-BACK failing is contained — the lease expiry re-queues the row;
    # the sweep itself never raises (one poisoned row cannot stall the queue).
    reg2 = _FakeReg(claims[:1])
    reg2.update_fail = True
    assert await notifier.sweep_tenant(reg2, senders, batch=10, lease_seconds=60,
                                       max_attempts=8, backoff_base=60,
                                       backoff_cap=3600) == 1


# --------------------------------------------------------------------------------------- #
# The upgraded ops seam: events with recipients become DURABLE notifications
# --------------------------------------------------------------------------------------- #
def _enable_notifications(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("WORKFLOWS_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOWS_NOTIFY_CHANNELS", "email,webhook")
    monkeypatch.setenv("WORKFLOWS_SMTP_HOST", "smtp.local")
    monkeypatch.setenv("WORKFLOWS_NOTIFY_WEBHOOK_URL", "http://hooks.local/notify")
    monkeypatch.delenv("WORKFLOWS_OPS_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()


async def test_ops_event_fans_out_durable_notifications_idempotently(
        mock_register, monkeypatch):
    _enable_notifications(monkeypatch)
    try:
        env = ActivityEnvironment()
        detail = {"subject": "Lead:l-9", "waiting_hours": 72.0,
                  "notify": {"recipients": ["rm@evamfinance.com", "head@evamfinance.com"],
                             "severity": "warning", "title": "Sla escalation — Lead:l-9",
                             "discriminator": "Lead:l-9|72.0",
                             "subject_type": "Lead", "subject_id": "l-9"}}
        out = await env.run(activities.emit_operational_event, "sla_escalation",
                            dict(detail))
        assert out["notified"] == 2 and set(out["channels"]) == {"email", "webhook"}
        # A RETRY of the same occurrence is a no-op (dedupe key) — never a double notify.
        out2 = await env.run(activities.emit_operational_event, "sla_escalation",
                             dict(detail))
        assert out2["notified"] == 2
        rows = list(mock_register.state.notifications.values())
        assert len(rows) == 2                       # one per recipient, despite the retry
        assert {r["recipient"] for r in rows} == {"rm@evamfinance.com",
                                                  "head@evamfinance.com"}
        for r in rows:
            assert r["severity"] == "warning" and r["subject_type"] == "Lead"
            assert {d["channel"] for d in r["deliveries"]} == {"email", "webhook"}
    finally:
        get_settings.cache_clear()


async def test_ops_event_without_recipients_stays_log_only(mock_register, monkeypatch):
    _enable_notifications(monkeypatch)
    try:
        env = ActivityEnvironment()
        out = await env.run(activities.emit_operational_event, "lender_update",
                            {"subject": "Syndication:s1"})
        assert out == {"delivered": False, "channel": "log"}
        assert mock_register.state.notifications == {}
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------------------- #
# Calendar: the follow-up becomes a first-class event, idempotently per run
# --------------------------------------------------------------------------------------- #
async def test_create_calendar_event_is_idempotent_per_run(mock_register):
    env = ActivityEnvironment()
    args = ["Lead", "l-1", "Site follow-up", "2026-08-20", "asha@evamfinance.com",
            [], "VOX", None]
    first = await env.run(activities.create_calendar_event, *args)
    again = await env.run(activities.create_calendar_event, *args)
    assert first["id"] == again["id"]               # the retry attached, not duplicated
    assert len(mock_register.state.calendar) == 1
    row = next(iter(mock_register.state.calendar.values()))
    assert row["source"] == "VOX" and row["organizer"] == "asha@evamfinance.com"
    assert row["starts_at"] == "2026-08-20T09:00:00+00:00"   # bare date → start-of-day


async def test_vox_capture_schedules_the_follow_up_meeting(mock_register):
    from temporalio.worker import Worker
    env = await _env()
    eid = uuid.uuid4().hex
    mock_register.state.entities.append({"id": eid, "legal_name": "GreenVolt Power"})
    async with env:
        tq = "vox-tq"
        acts = [activities.resolve_entity_candidates, activities.create_entity,
                activities.find_active_leads, activities.create_lead,
                activities.update_lead_touch, activities.log_touchpoint,
                activities.assign_lead_owner, activities.mark_lead_note,
                activities.create_calendar_event]
        async with Worker(env.client, task_queue=tq, workflows=[VoxTouchpointWorkflow],
                          activities=acts):
            result = await env.client.execute_workflow(
                VoxTouchpointWorkflow.run,
                VoxTouchpoint(company_name="GreenVolt Power",
                              capture_id=f"cap-{uuid.uuid4().hex[:8]}",
                              performed_by="asha@evamfinance.com",
                              assigned_rm="asha@evamfinance.com",
                              summary="site visit",
                              next_action="Walk the site with the CFO",
                              next_meeting_date="2026-08-20",
                              create_calendar_event=True),
                id=f"vox-{uuid.uuid4().hex}", task_queue=tq)
    assert result.follow_up.get("calendar") == "scheduled"
    event = mock_register.state.calendar[result.follow_up["calendar_event_id"]]
    assert event["subject_type"] == "Lead" and event["subject_id"] == result.lead_id
    assert event["title"] == "Walk the site with the CFO"
    assert event["organizer"] == "asha@evamfinance.com"


# --------------------------------------------------------------------------------------- #
# Document expiry monitor: sweep → critical events + durable notifications, then stop
# --------------------------------------------------------------------------------------- #
async def test_document_expiry_monitor_notifies_and_dedupes(mock_register, monkeypatch):
    from temporalio.worker import Worker
    _enable_notifications(monkeypatch)
    try:
        env = await _env()
        did = uuid.uuid4().hex
        mock_register.state.expiry_report["expired"] = [
            {"id": did, "title": "Insurance policy", "slot_key": "insurance",
             "subject_type": "Lending", "subject_id": "ln-1",
             "expires_on": "2026-07-30", "uploaded_by": "rm@evamfinance.com"}]
        mock_register.state.expiry_report["expiring"] = [
            {"id": uuid.uuid4().hex, "title": "Sanction letter", "slot_key": "sanction",
             "subject_type": "Lending", "subject_id": "ln-2",
             "expires_on": "2026-08-05", "uploaded_by": "rm@evamfinance.com"}]
        async with env:
            tq = "docexp-tq"
            async with Worker(env.client, task_queue=tq,
                              workflows=[DocumentExpiryMonitorWorkflow],
                              activities=[activities.sweep_document_expiry,
                                          activities.emit_operational_event]):
                handle = await env.client.start_workflow(
                    DocumentExpiryMonitorWorkflow.run,
                    DocumentExpiryInput(interval_hours=1000.0, warn_days=7,
                                        notify=["ops@evamfinance.com"]),
                    id=f"doc-expiry-{uuid.uuid4().hex[:8]}", task_queue=tq)
                # The first sweep runs immediately; the monitor then sleeps for days —
                # stop it and read the tally.
                deadline = 100
                while (await handle.query(
                        DocumentExpiryMonitorWorkflow.state)).get("sweeps", 0) < 1:
                    deadline -= 1
                    assert deadline > 0, "monitor never swept"
                await handle.signal(DocumentExpiryMonitorWorkflow.stop)
                result = await handle.result()

        assert result.sweeps == 1 and result.expired_total == 1
        rows = list(mock_register.state.notifications.values())
        # The lapsed document alerts BOTH the uploader and the ops recipient (critical);
        # the expiring one warns the same pair — 4 rows, each deduped by occurrence.
        assert len(rows) == 4
        by_event = {}
        for r in rows:
            by_event.setdefault(r["event"], set()).add(r["recipient"])
        assert by_event["document_expired"] == {"rm@evamfinance.com",
                                                "ops@evamfinance.com"}
        assert by_event["document_expiring"] == {"rm@evamfinance.com",
                                                 "ops@evamfinance.com"}
        expired_rows = [r for r in rows if r["event"] == "document_expired"]
        assert all(r["severity"] == "critical" for r in expired_rows)
        assert all(r["subject_type"] == "Lending" for r in rows)
    finally:
        get_settings.cache_clear()
