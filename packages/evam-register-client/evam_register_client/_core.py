"""Pure helpers shared by the async and sync clients (no I/O)."""

from __future__ import annotations

import random
import uuid

# HTTP statuses worth retrying: rate-limit + transient gateway/unavailable.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def build_headers(
    *,
    api_key: str,
    tenant: str,
    actor: str,
    idempotency_key: str | None = None,
    if_match: int | str | None = None,
    request_id: str | None = None,
    content_type_json: bool = False,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "X-API-Key": api_key,
        "X-Tenant": tenant,
        "X-Actor": actor,
    }
    if content_type_json:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"' if str(if_match).isdigit() else str(if_match)
    if request_id:
        headers["X-Request-ID"] = request_id
    if extra:
        headers.update(extra)
    return headers


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


def should_retry(*, method: str, status: int | None, is_network_error: bool,
                 idempotent_write: bool) -> bool:
    """Retry safe methods on any transient signal; retry writes only when they carry an
    idempotency key or If-Match (so a replay can't duplicate or clobber)."""
    transient = is_network_error or (status in RETRYABLE_STATUSES)
    if not transient:
        return False
    if method.upper() in SAFE_METHODS:
        return True
    return idempotent_write


def backoff_delay(attempt: int, *, base: float, cap: float,
                  retry_after: float | None = None) -> float:
    """Exponential backoff with full jitter; honour a server Retry-After if larger."""
    exp = min(cap, base * (2 ** max(0, attempt - 1)))
    jittered = random.uniform(0, exp)  # noqa: S311 - jitter, not cryptographic
    if retry_after is not None:
        return max(jittered, retry_after)
    return jittered


def prepare_request_id(request_id: str | None) -> str:
    """Use the caller's correlation id, or mint one so the call is always traceable."""
    return request_id or uuid.uuid4().hex
