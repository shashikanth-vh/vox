"""Cached identity/matrix resolution against the Access service.

R1 from the design: the gateway NEVER asks per request. It fetches a user's roles +
effective matrices from ``/v1/resolve`` once, caches them for ``cache_ttl_s``, and keeps
serving the last-known-good answer if the Access service is briefly unreachable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from evam_backend_core.logging import get_logger

from app.config import get_settings

log = get_logger("gateway.resolver")


@dataclass
class ResolvedUser:
    id: str
    email: str
    roles: list[str]
    views: dict[str, str]
    operations: dict[str, str]
    version: int
    fetched_at: float
    # The user's revocation epoch from Access (bumped on any role change / deactivation) —
    # carried into the signed context so sensitive-operation revalidation can compare.
    epoch: int = 0
    # Transitive subordinates from the Access service — forwarded to the Register
    # as the basis of a Head's TEAM scope.
    reports: list[dict] = field(default_factory=list)


class UserDeniedError(Exception):
    """The Access service says this user does not exist or is inactive."""


class AccessUnavailableError(Exception):
    """The Access service is unreachable and no cached answer exists."""


class Resolver:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, str], ResolvedUser] = {}

    async def resolve(self, tenant: str, email: str) -> ResolvedUser:
        settings = get_settings()
        key = (tenant, email.strip().lower())
        cached = self._cache.get(key)
        if cached is not None and (time.monotonic() - cached.fetched_at) < settings.cache_ttl_s:
            return cached
        try:
            resp = await self._client.get(
                f"{settings.access_url}/v1/resolve",
                params={"email": email},
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            # Last-known-good is a BOUNDED degraded mode, not a policy source: past
            # cache_max_stale_s the gateway fails closed rather than serving ever-staler
            # grants through an Access outage.
            if cached is not None and (
                    time.monotonic() - cached.fetched_at) < settings.cache_max_stale_s:
                log.warning("access unreachable — serving last-known-good for %s", email)
                return cached
            raise AccessUnavailableError(str(exc)) from exc
        if resp.status_code == 404:
            self._cache.pop(key, None)
            raise UserDeniedError(email)
        if resp.status_code >= 400:
            if cached is not None and (
                    time.monotonic() - cached.fetched_at) < settings.cache_max_stale_s:
                return cached
            raise AccessUnavailableError(f"access /resolve returned {resp.status_code}")
        body = resp.json()
        resolved = ResolvedUser(
            id=body["id"], email=body["email"], roles=body["roles"],
            views=body["views"], operations=body["operations"],
            version=body["version"], fetched_at=time.monotonic(),
            epoch=int(body.get("epoch", 0)),
            reports=body.get("reports", []),
        )
        self._cache[key] = resolved
        return resolved

    def invalidate(self) -> None:
        self._cache.clear()
