"""Typed errors + RFC-9457 handlers — re-exported from evam_backend_core."""

from __future__ import annotations

from evam_backend_core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationAppError,
    VersionConflictError,
    register_exception_handlers,
)

__all__ = [
    "AppError", "ConflictError", "ForbiddenError", "NotFoundError", "UnauthorizedError",
    "ValidationAppError", "VersionConflictError", "register_exception_handlers",
]
