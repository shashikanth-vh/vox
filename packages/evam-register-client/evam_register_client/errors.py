"""Typed exceptions mapped from the Register's RFC-9457 ``problem+json`` responses.

Every error carries the Register's ``request_id`` so a failure in a vertical can be traced
straight to the Register log line that produced it.
"""

from __future__ import annotations

from typing import Any


class RegisterError(Exception):
    """Base error for any non-2xx Register response (or transport failure)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.title = title
        self.detail = detail
        self.request_id = request_id
        self.extra = extra or {}

    def __str__(self) -> str:
        bits = [self.args[0] if self.args else ""]
        if self.status:
            bits.append(f"(HTTP {self.status}")
            if self.error_type:
                bits[-1] += f", {self.error_type}"
            bits[-1] += ")"
        if self.request_id:
            bits.append(f"[request_id={self.request_id}]")
        return " ".join(b for b in bits if b)


class BadRequestError(RegisterError):
    """400."""


class AuthError(RegisterError):
    """401 — missing/invalid API key."""


class ForbiddenError(RegisterError):
    """403 — unknown/inactive tenant or not permitted."""


class NotFoundError(RegisterError):
    """404."""


class ConflictError(RegisterError):
    """409 — integrity/idempotency conflict."""


class VersionConflictError(ConflictError):
    """409 with ``type=version_conflict`` — the row changed under you; re-read and retry."""

    @property
    def expected_version(self) -> int | None:
        return self.extra.get("expected_version")

    @property
    def actual_version(self) -> int | None:
        return self.extra.get("actual_version")


class ValidationError(RegisterError):
    """422 — request failed validation."""


class RateLimitedError(RegisterError):
    """429 — retry after ``retry_after`` seconds (if provided)."""

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ServerError(RegisterError):
    """5xx."""


_STATUS_MAP: dict[int, type[RegisterError]] = {
    400: BadRequestError,
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitedError,
}


def error_from_payload(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> RegisterError:
    """Build the most specific typed error from a Register error response."""
    headers = headers or {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    error_type = err.get("type")
    title = err.get("title")
    detail = err.get("detail") or (body if isinstance(body, str) else None)
    request_id = err.get("request_id") or headers.get("x-request-id")
    # Extra fields beyond the standard envelope (e.g. expected/actual_version, constraint).
    extra = {k: v for k, v in err.items()
             if k not in {"type", "title", "status", "detail", "request_id"}}

    cls: type[RegisterError]
    if status == 409 and error_type == "version_conflict":
        cls = VersionConflictError
    elif status in _STATUS_MAP:
        cls = _STATUS_MAP[status]
    elif status >= 500:
        cls = ServerError
    else:
        cls = RegisterError

    kwargs: dict[str, Any] = dict(
        status=status, error_type=error_type, title=title, detail=detail,
        request_id=request_id, extra=extra,
    )
    if cls is RateLimitedError:
        kwargs["retry_after"] = _parse_retry_after(headers.get("retry-after"))
    message = detail or title or f"Register request failed with HTTP {status}"
    return cls(message, **kwargs)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)  # delta-seconds form
    except ValueError:
        return None  # HTTP-date form is not handled; caller falls back to backoff
