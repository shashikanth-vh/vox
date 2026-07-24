"""Transparent retry of transient database failures.

Some database errors are *not* the caller's fault and are safe to retry:

* **deadlock_detected (40P01)** and **serialization_failure (40001)** — PostgreSQL has
  already rolled the transaction back, so nothing was committed. Retrying the whole unit
  of work is always safe and usually succeeds immediately (the contending transaction has
  moved on).
* **connection errors** (failover, dropped socket) — safe to retry only for *read* methods,
  because a write might have committed just before the socket died (the classic
  at-least-once ambiguity). We therefore retry these only for GET/HEAD/OPTIONS.

Implemented as a custom ``APIRoute`` so the *entire* request — dependency resolution
(which opens a fresh session), the handler, and the commit — is re-run on a clean
transaction. The request body is cached by Starlette, so re-running is safe.
"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.responses import Response

from evam_backend_core.logging import get_logger

log = get_logger("register.retry")

# SQLSTATEs PostgreSQL guarantees have rolled back → always safe to retry.
_ROLLBACK_SAFE_SQLSTATES = {"40001", "40P01"}  # serialization_failure, deadlock_detected
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Defaults; overridden from settings at app-construction time.
_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 0.05


def configure_retry(max_attempts: int, base_delay_s: float) -> None:
    global _MAX_ATTEMPTS, _BASE_DELAY_S
    _MAX_ATTEMPTS = max(1, max_attempts)
    _BASE_DELAY_S = max(0.0, base_delay_s)


def _sqlstate(exc: BaseException) -> str | None:
    """Dig the PostgreSQL SQLSTATE out of a (possibly wrapped) exception."""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code:
            return str(code)
        cur = getattr(cur, "orig", None) or cur.__cause__
    return None


def is_rollback_safe_transient(exc: BaseException) -> bool:
    """Deadlock / serialization failure — the txn was rolled back; retry is always safe."""
    return _sqlstate(exc) in _ROLLBACK_SAFE_SQLSTATES


def is_connection_error(exc: BaseException) -> bool:
    """A dropped/invalidated connection (failover, socket reset)."""
    if getattr(exc, "connection_invalidated", False):
        return True
    cur: BaseException | None = exc
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if cur.__class__.__name__ in {
            "ConnectionDoesNotExistError", "ConnectionFailureError",
            "InterfaceError", "CannotConnectNowError",
        }:
            return True
        cur = getattr(cur, "orig", None) or cur.__cause__
    return False


def _should_retry(exc: BaseException, method: str) -> bool:
    if is_rollback_safe_transient(exc):
        return True
    return is_connection_error(exc) and method.upper() in _SAFE_METHODS


class RetryableRoute(APIRoute):
    """APIRoute that transparently retries transient DB failures with backoff + jitter."""

    def get_route_handler(self):  # noqa: ANN201
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            attempt = 0
            while True:
                try:
                    return await original(request)
                except Exception as exc:
                    attempt += 1
                    if attempt >= _MAX_ATTEMPTS or not _should_retry(exc, request.method):
                        raise
                    # Exponential backoff with full jitter to de-correlate retriers.
                    ceil = _BASE_DELAY_S * (2 ** (attempt - 1))
                    delay = secrets.randbelow(int(ceil * 1000) + 1) / 1000 if ceil > 0 else 0
                    log.warning(
                        "retrying_transient_db_error",
                        extra={"method": request.method, "path": request.url.path,
                               "attempt": attempt, "sqlstate": _sqlstate(exc),
                               "delay_ms": round(delay * 1000, 1)},
                    )
                    await asyncio.sleep(delay)

        return handler
