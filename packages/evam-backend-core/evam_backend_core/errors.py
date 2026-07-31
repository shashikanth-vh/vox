"""Typed application errors and JSON error responses.

Errors are returned as RFC 9457-style problem objects so every client (ATLAS, VOX,
Postman) gets a consistent, machine-readable shape:

    {"error": {"type": "...", "title": "...", "detail": "...", "request_id": "..."}}
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from sqlalchemy.exc import IntegrityError

from evam_backend_core.logging import get_logger, request_id_ctx

log = get_logger(__name__)


class AppError(Exception):
    """Base class for all deliberately-raised API errors."""

    status_code: int = 400
    error_type: str = "about:blank"
    title: str = "Error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    error_type = "not_found"
    title = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    error_type = "conflict"
    title = "Conflict"


class VersionConflictError(ConflictError):
    """Optimistic-locking failure — the row changed under the caller's feet."""

    error_type = "version_conflict"
    title = "Version conflict"

    def __init__(self, expected: int | None = None, actual: int | None = None) -> None:
        super().__init__(
            "The record was modified by another request. Re-read it and retry.",
            expected_version=expected,
            actual_version=actual,
        )


class ValidationAppError(AppError):
    status_code = 422
    error_type = "validation_error"
    title = "Validation failed"


class UnauthorizedError(AppError):
    status_code = 401
    error_type = "unauthorized"
    title = "Missing or invalid API key"


class ForbiddenError(AppError):
    status_code = 403
    error_type = "forbidden"
    title = "Not permitted for this tenant"


class ServiceUnavailableError(AppError):
    """A required upstream authority (e.g. Access, for sensitive-operation online
    revalidation) cannot answer — the caller fails CLOSED rather than degrading."""

    status_code = 503
    error_type = "service_unavailable"
    title = "Required upstream authority unavailable"


def _payload(status: int, error_type: str, title: str, detail: str, **extra: Any) -> dict:
    body = {
        "error": {
            "type": error_type,
            "title": title,
            "status": status,
            "detail": detail,
            "request_id": request_id_ctx.get(),
        }
    }
    if extra:
        body["error"].update({k: v for k, v in extra.items() if v is not None})
    return body


def register_exception_handlers(app) -> None:  # noqa: ANN001 - FastAPI app
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> ORJSONResponse:
        return ORJSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.status_code, exc.error_type, exc.title, exc.detail, **exc.extra),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        return ORJSONResponse(
            status_code=422,
            content=_payload(
                422,
                "validation_error",
                "Validation failed",
                "One or more fields are invalid.",
                errors=exc.errors(),
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> ORJSONResponse:
        # Unique / foreign-key / check violations from the source-of-truth DB.
        detail = "The write violates a database constraint (duplicate, missing reference, or check)."
        orig = getattr(exc, "orig", None)
        constraint = getattr(getattr(orig, "__cause__", None), "constraint_name", None)
        log.warning("integrity_error", extra={"constraint": constraint})
        return ORJSONResponse(
            status_code=409,
            content=_payload(409, "integrity_error", "Constraint violation", detail,
                             constraint=constraint),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> ORJSONResponse:
        log.exception("unhandled_error: %s", exc)
        return ORJSONResponse(
            status_code=500,
            content=_payload(500, "internal_error", "Internal server error",
                             "An unexpected error occurred. The request_id can be used to trace it."),
        )
