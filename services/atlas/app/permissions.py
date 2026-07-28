"""View-level RBAC for ATLAS — "may this user open this dashboard view at all?"

ATLAS asks the Access service to resolve the caller (roles + effective view matrix)
and caches the answer for a short TTL, exactly like the gateway does for operations.
Row-level security is NOT ATLAS's job: every read ATLAS makes forwards the caller's
identity headers to the Register, which enforces data access next to the data.

Degradation policy (same trade-off as the gateway):
* Access service down + we have a cached answer  → serve from cache (last known good).
* Access service down + no cached answer         → 502; we never guess permissions.
* Gating disabled (no ``ATLAS_ACCESS_URL``)      → every view allowed (dev mode).
"""

from __future__ import annotations

import time

import httpx
from evam_backend_core.logging import get_logger

log = get_logger("atlas.permissions")


class ViewDeniedError(Exception):
    """The user exists but their roles grant NONE on the requested view."""


class UserUnknownError(Exception):
    """The Access service does not know (or has deactivated) this user."""


class AccessUnavailableError(Exception):
    """The Access service cannot be reached and we have nothing cached."""


class ViewGate:
    """Tiny resolver-with-cache. One instance per process, keyed by (tenant, email)."""

    def __init__(self, client: httpx.AsyncClient, access_url: str, api_key: str,
                 ttl_s: float) -> None:
        self._client = client
        self._access_url = access_url.rstrip("/")
        self._api_key = api_key
        self._ttl_s = ttl_s
        # (tenant, email) -> (expires_at_monotonic, views dict)
        self._cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._access_url)

    async def resolve(self, tenant: str, email: str) -> dict:
        """The user's full resolution (id, roles, views, reports) — cached. This is the
        one Access round-trip; the view gate and the Register identity-forwarding both
        read from it."""
        key = (tenant, email.lower())
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        try:
            resp = await self._client.get(
                f"{self._access_url}/v1/resolve", params={"email": email},
                headers={"X-API-Key": self._api_key, "X-Tenant": tenant})
        except httpx.HTTPError as exc:
            if cached:  # expired but present — better stale permissions than a dead UI
                log.warning("atlas_access_stale_cache", extra={"email": email})
                return cached[1]
            raise AccessUnavailableError(str(exc)) from exc
        if resp.status_code == 404:
            raise UserUnknownError(email)
        resp.raise_for_status()
        body = resp.json()
        self._cache[key] = (now + self._ttl_s, body)
        return body

    async def views_for(self, tenant: str, email: str) -> dict[str, str]:
        """The user's effective view matrix, e.g. ``{"dashboard": "FULL", ...}``."""
        return (await self.resolve(tenant, email)).get("views", {})

    async def check(self, tenant: str, email: str, view: str) -> None:
        """Raise ``ViewDeniedError`` unless the user's stacked roles grant the view."""
        views = await self.views_for(tenant, email)
        if views.get(view, "NONE") == "NONE":
            raise ViewDeniedError(view)

    def invalidate(self) -> None:
        self._cache.clear()
