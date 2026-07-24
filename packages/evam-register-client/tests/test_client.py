"""Client behaviour against the mock Register contract."""

from __future__ import annotations

import httpx
import pytest

from evam_register_client import (
    AsyncRegisterClient,
    AuthError,
    NotFoundError,
    RegisterClient,
    RegisterClientConfig,
    ServerError,
    VersionConflictError,
)
from evam_register_client._core import backoff_delay, should_retry


# ---- pure logic -----------------------------------------------------------
def test_retry_policy():
    # reads always retry on transient; writes only when idempotent
    assert should_retry(method="GET", status=503, is_network_error=False, idempotent_write=False)
    assert should_retry(method="POST", status=503, is_network_error=False, idempotent_write=True)
    assert not should_retry(method="POST", status=503, is_network_error=False, idempotent_write=False)
    # non-transient never retries
    assert not should_retry(method="GET", status=404, is_network_error=False, idempotent_write=False)


def test_backoff_honours_retry_after():
    assert backoff_delay(1, base=0.1, cap=5, retry_after=2.0) >= 2.0


# ---- auth -----------------------------------------------------------------
async def test_bad_key_raises_auth_error(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with AsyncRegisterClient("http://reg", "WRONG", transport=transport) as c:
        with pytest.raises(AuthError) as ei:
            await c.get("things", "x")
    assert ei.value.status == 401


# ---- CRUD + idempotency + optimistic concurrency --------------------------
async def test_create_get_roundtrip(reg):
    obj = await reg.create("things", {"name": "widget"})
    assert obj["name"] == "widget" and obj["version"] == 1
    got = await reg.get("things", obj["id"])
    assert got["id"] == obj["id"]


async def test_idempotent_create_does_not_duplicate(reg, mock_app):
    key = "vox-event-123"
    a = await reg.create("things", {"name": "x"}, idempotency_key=key)
    b = await reg.create("things", {"name": "x"}, idempotency_key=key)  # replay
    assert a["id"] == b["id"]
    assert mock_app.state.calls["create"] == 1  # only one real create


async def test_get_missing_raises_not_found(reg):
    with pytest.raises(NotFoundError):
        await reg.get("things", "does-not-exist")


async def test_version_conflict(reg):
    obj = await reg.create("things", {"name": "x"})
    await reg.update("things", obj["id"], {"name": "y"}, expected_version=1)  # ok → v2
    with pytest.raises(VersionConflictError) as ei:
        await reg.update("things", obj["id"], {"name": "z"}, expected_version=1)  # stale
    assert ei.value.expected_version == 1 and ei.value.actual_version == 2


# ---- pagination -----------------------------------------------------------
async def test_pagination_and_iterate(reg):
    for i in range(5):
        await reg.create("things", {"name": f"n{i}"})
    page = await reg.list("things", limit=2, with_total=True)
    assert page.count == 2 and page.has_more and page.total == 5
    allrows = [x async for x in reg.iterate("things", page_size=2)]
    assert len(allrows) == 5


# ---- retry ----------------------------------------------------------------
async def test_retry_recovers_from_503(reg, mock_app):
    body = await reg._send(_plan("GET", "/flaky503"))
    assert body["ok"] is True and mock_app.state.flaky["503"] == 3  # 2 fails + 1 success


async def test_retry_exhausted_raises_server_error(reg):
    with pytest.raises(ServerError) as ei:
        await reg._send(_plan("GET", "/always503"))
    assert ei.value.status == 503


async def test_rate_limit_retried(reg):
    body = await reg._send(_plan("GET", "/rate"))
    assert body["ok"] is True


# ---- correlation ----------------------------------------------------------
async def test_request_id_is_forwarded(reg, mock_app):
    await reg.log_interaction("Deal", "d1", "Phone Call", source="VOX", summary="hi",
                              request_id="corr-999")
    assert mock_app.state.last["interaction_rid"] == "corr-999"


# ---- vertical helpers -----------------------------------------------------
async def test_vox_log_interaction_payload(reg, mock_app):
    await reg.log_interaction("Deal", "d1", "Phone Call", source="VOX", summary="promoter call",
                              transcript="…")
    p = mock_app.state.last["interaction"]
    assert p["subject_type"] == "Deal" and p["subject_id"] == "d1"
    assert p["source"] == "VOX" and p["interaction_type"] == "Phone Call"
    assert p["summary"] == "promoter call"


async def test_cipher_financial_version_payload(reg, mock_app):
    await reg.create_financial_version("e1", "Audited", "2026-03-31", revenue=120.0,
                                       is_consolidated=True, scale="Crore")
    p = mock_app.state.last["financial"]
    assert p["entity_id"] == "e1" and p["statement_type"] == "Audited"
    assert p["period_end"] == "2026-03-31" and p["revenue"] == 120.0 and p["is_consolidated"] is True


async def test_pulse_intelligence_and_ack(reg, mock_app):
    intel = await reg.create_intelligence("e1", "Court Case", signal="RED", title="Suit filed")
    assert intel["signal"] == "RED"
    acked = await reg.acknowledge_intelligence(intel["id"])
    assert acked["acknowledged_by"] == "tester"


# ---- sync facade ----------------------------------------------------------
def test_sync_client(mock_app):
    cfg = RegisterClientConfig(retry_base_delay_s=0.001)
    transport = httpx.ASGITransport(app=mock_app)
    with RegisterClient("http://reg", "test-key", config=cfg, transport=transport) as reg:
        obj = reg.create("things", {"name": "sync"})
        got = reg.get("things", obj["id"])
        assert got["name"] == "sync"
        for i in range(3):
            reg.create("things", {"name": f"s{i}"})
        rows = reg.iterate("things", page_size=2)  # drained to a list in sync mode
        assert isinstance(rows, list) and len(rows) == 4


# helper to build a raw plan for the retry tests
def _plan(method, path):
    from evam_register_client.client import _Plan
    return _Plan(method, path)
