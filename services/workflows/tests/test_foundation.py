"""Release-1 workflow foundation: payload encryption, operational events, and the SLA
bookkeeping — the pieces every workflow shares."""

from __future__ import annotations

import base64
import os
from datetime import timedelta

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.testing import ActivityEnvironment

from app import activities
from app.codec import EncryptionCodec, build_data_converter
from app.config import get_settings
from app.workflows import _Foundation

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------------------- #
# Payload encryption
# ---------------------------------------------------------------------------------------- #
async def test_codec_roundtrips_and_produces_ciphertext():
    key = os.urandom(32)
    codec = EncryptionCodec(key)
    plain = Payload(metadata={"encoding": b"json/plain"}, data=b'{"amount_cr": 125.5}')
    [sealed] = await codec.encode([plain])
    assert sealed.metadata["encoding"] == b"binary/encrypted"
    assert b"amount_cr" not in sealed.data          # business data is not in the clear
    [opened] = await codec.decode([sealed])
    assert opened == plain


async def test_codec_key_rotation_reads_old_payloads():
    old_key, new_key = os.urandom(32), os.urandom(32)
    [sealed] = await EncryptionCodec(old_key).encode(
        [Payload(metadata={"encoding": b"json/plain"}, data=b"x")])
    # The rotated codec (new current, old retired) still opens history written under the
    # old key; a codec WITHOUT the retired key refuses loudly instead of guessing.
    [opened] = await EncryptionCodec(new_key, retired=[old_key]).decode([sealed])
    assert opened.data == b"x"
    with pytest.raises(ValueError, match="unknown key id"):
        await EncryptionCodec(new_key).decode([sealed])


async def test_codec_passes_plaintext_payloads_through():
    # Histories written BEFORE encryption was enabled must stay readable.
    plain = Payload(metadata={"encoding": b"json/plain"}, data=b"1")
    assert (await EncryptionCodec(os.urandom(32)).decode([plain])) == [plain]


def test_build_data_converter_is_plaintext_without_a_key():
    assert build_data_converter("").payload_codec is None
    key_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert isinstance(build_data_converter(key_b64).payload_codec, EncryptionCodec)


# ---------------------------------------------------------------------------------------- #
# Operational events
# ---------------------------------------------------------------------------------------- #
async def test_ops_event_logs_without_a_webhook(monkeypatch):
    monkeypatch.delenv("WORKFLOWS_OPS_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    env = ActivityEnvironment()
    out = await env.run(activities.emit_operational_event, "sla_reminder",
                        {"subject": "Lead:l1"})
    assert out == {"delivered": False, "channel": "log"}


async def test_ops_event_webhook_delivery_and_bounded_failure(monkeypatch):
    """The webhook is best-effort: a healthy receiver gets the JSON event; a dead one is
    retried a bounded number of times and the activity still RETURNS (never raises) — ops
    visibility must not take a business workflow down."""
    monkeypatch.setenv("WORKFLOWS_OPS_WEBHOOK_URL", "http://ops.invalid/hook")
    monkeypatch.setenv("WORKFLOWS_OPS_WEBHOOK_RETRIES", "1")
    get_settings.cache_clear()

    import httpx

    calls: list[dict] = []

    class _OkClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

        async def post(self, url, json=None):  # noqa: ANN001
            calls.append(json)
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _OkClient)
    env = ActivityEnvironment()
    out = await env.run(activities.emit_operational_event, "sla_escalation",
                        {"subject": "Deal:d1", "waiting_hours": 80})
    assert out["delivered"] is True and calls[0]["event"] == "sla_escalation"
    assert calls[0]["subject"] == "Deal:d1"

    class _DownClient(_OkClient):
        async def post(self, url, json=None):  # noqa: ANN001
            raise httpx.ConnectError("down", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _DownClient)
    out = await env.run(activities.emit_operational_event, "sla_reminder", {})
    assert out["delivered"] is False and "down" in out["error"]
    get_settings.cache_clear()


# ---------------------------------------------------------------------------------------- #
# SLA bookkeeping (pure, deterministic — exactly what runs inside the workflow)
# ---------------------------------------------------------------------------------------- #
def test_sla_reminders_then_escalation_then_silence():
    f = _Foundation()
    h = timedelta(hours=1)
    # Nothing due early; first reminder at 24h; second at 48h; escalation outranks at 72h.
    assert f.due_sla_event(10 * h, 24.0, 72.0) is None
    assert f.due_sla_event(24 * h, 24.0, 72.0) == "sla_reminder"
    assert f.due_sla_event(25 * h, 24.0, 72.0) is None          # not due again yet
    assert f.due_sla_event(48 * h, 24.0, 72.0) == "sla_reminder"
    assert f.due_sla_event(72 * h, 24.0, 72.0) == "sla_escalation"
    assert f.escalated and f.reminders_sent == 2
    # Escalation fires ONCE (it outranks the coinciding 72h reminder, which follows on the
    # next check); the reminder cadence then carries on unchanged.
    assert f.due_sla_event(73 * h, 24.0, 72.0) == "sla_reminder"
    assert f.due_sla_event(74 * h, 24.0, 72.0) is None
    assert f.due_sla_event(96 * h, 24.0, 72.0) == "sla_reminder"


def test_sla_timers_disabled_by_zero():
    f = _Foundation()
    assert f.due_sla_event(timedelta(hours=1000), 0.0, 0.0) is None
    # With timers off, the next wake-up is simply the decision deadline.
    assert f.next_wakeup(timedelta(hours=1), timedelta(hours=5), 0.0, 0.0) == timedelta(hours=5)


def test_next_wakeup_is_the_earliest_due_moment():
    f = _Foundation()
    h = timedelta(hours=1)
    # 10h in: next reminder (24h) is nearer than escalation (72h) or the deadline (100h).
    assert f.next_wakeup(10 * h, 90 * h, 24.0, 72.0) == 14 * h
    f.reminders_sent = 2
    # 60h in, 2 reminders sent: third reminder due at 72h — ties with escalation; 12h away.
    assert f.next_wakeup(60 * h, 40 * h, 24.0, 72.0) == 12 * h
    # Never sleeps a non-positive interval.
    assert f.next_wakeup(80 * h, 1 * h, 24.0, 72.0) >= timedelta(seconds=1)
