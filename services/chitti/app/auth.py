"""Authentication for Chitti's OpenAI-compatible front door."""

from __future__ import annotations

import hmac

from evam_backend_core.errors import UnauthorizedError
from fastapi import Request

from app.config import Settings


def require_client_key(request: Request, settings: Settings) -> None:
    """Accept a direct OpenAI bearer key or a Gateway-injected service key."""

    authorization = request.headers.get("Authorization", "")
    bearer = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    service_key = request.headers.get("X-API-Key", "").strip()
    provided = [candidate for candidate in (bearer, service_key) if candidate]

    if not any(
        hmac.compare_digest(candidate, expected)
        for candidate in provided
        for expected in settings.api_key_list()
    ):
        raise UnauthorizedError("Missing or invalid Chitti client key.")
