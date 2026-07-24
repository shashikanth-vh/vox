"""evam-register-client — typed client for the PRISM Register (source of truth).

Every PRISM vertical (VOX, CIPHER, PULSE, portal/gateway APIs) talks to the Register
through this client, so they all share the same auth, idempotency, optimistic-concurrency,
retry, correlation and error semantics.

    from evam_register_client import AsyncRegisterClient, RegisterClient
    from evam_register_client.errors import VersionConflictError, NotFoundError
"""

from __future__ import annotations

from evam_register_client.client import AsyncRegisterClient, RegisterClient
from evam_register_client.config import RegisterClientConfig
from evam_register_client.errors import (
    AuthError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    RegisterError,
    ServerError,
    ValidationError,
    VersionConflictError,
)
from evam_register_client.models import Page

__version__ = "0.1.0"

__all__ = [
    "AsyncRegisterClient", "RegisterClient", "RegisterClientConfig", "Page",
    "RegisterError", "BadRequestError", "AuthError", "ForbiddenError", "NotFoundError",
    "ConflictError", "VersionConflictError", "ValidationError", "RateLimitedError",
    "ServerError",
]
