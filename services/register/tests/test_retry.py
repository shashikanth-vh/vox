"""Transient-DB-error retry (app.core.retry): the RetryableRoute recovers from
deadlock/serialization failures and does not retry non-transient or unsafe cases."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.retry import (
    RetryableRoute,
    configure_retry,
    is_connection_error,
    is_rollback_safe_transient,
)
from app.core.router import api_router

pytestmark = pytest.mark.asyncio


class FakeDeadlockError(Exception):
    sqlstate = "40P01"  # deadlock_detected


class FakeSerializationError(Exception):
    sqlstate = "40001"  # serialization_failure


class FakeConnDroppedError(Exception):
    connection_invalidated = True


def _app(fail_times: int, exc_factory) -> tuple[FastAPI, dict]:
    state = {"calls": 0}
    app = FastAPI()
    r = api_router()

    @r.get("/read")
    @r.post("/write")
    async def _ep() -> dict:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc_factory()
        return {"ok": True, "calls": state["calls"]}

    app.include_router(r)
    return app, state


def _client(app: FastAPI) -> AsyncClient:
    # raise_app_exceptions=False → unhandled errors come back as a 500 response.
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                       base_url="http://t")


async def test_classifiers():
    assert is_rollback_safe_transient(FakeDeadlockError())
    assert is_rollback_safe_transient(FakeSerializationError())
    assert not is_rollback_safe_transient(ValueError("nope"))
    assert is_connection_error(FakeConnDroppedError())
    wrapped = RuntimeError("boom")
    wrapped.orig = FakeDeadlockError()
    assert is_rollback_safe_transient(wrapped)


async def test_deadlock_is_retried_and_succeeds():
    configure_retry(3, 0.001)
    app, _state = _app(fail_times=2, exc_factory=FakeDeadlockError)  # fail 2×, succeed 3rd
    async with _client(app) as c:
        r = await c.post("/write")
    assert r.status_code == 200 and r.json()["calls"] == 3
    assert isinstance(app.router.routes[-1], RetryableRoute)


async def test_persistent_deadlock_gives_up():
    configure_retry(3, 0.001)
    app, state = _app(fail_times=99, exc_factory=FakeSerializationError)
    async with _client(app) as c:
        r = await c.post("/write")
    assert r.status_code == 500
    assert state["calls"] == 3  # exactly max_attempts tries, then gives up


async def test_non_transient_is_not_retried():
    configure_retry(3, 0.001)
    app, state = _app(fail_times=99, exc_factory=lambda: ValueError("bug"))
    async with _client(app) as c:
        r = await c.post("/write")
    assert r.status_code == 500
    assert state["calls"] == 1  # no retry for a real bug


async def test_connection_error_retries_reads_not_writes():
    configure_retry(3, 0.001)
    app, state = _app(fail_times=1, exc_factory=FakeConnDroppedError)      # GET → retried
    async with _client(app) as c:
        r = await c.get("/read")
    assert r.status_code == 200 and state["calls"] == 2

    app2, state2 = _app(fail_times=1, exc_factory=FakeConnDroppedError)    # POST → not retried
    async with _client(app2) as c:
        r = await c.post("/write")
    assert r.status_code == 500 and state2["calls"] == 1
