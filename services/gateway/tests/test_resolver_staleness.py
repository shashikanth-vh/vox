"""The last-known-good answer is a BOUNDED degraded mode, not a policy source: within
cache_max_stale_s an Access outage serves the cached grants; past it the gateway fails
CLOSED (AccessUnavailableError → 503) rather than serving ever-staler permissions."""

from __future__ import annotations

import time

import httpx
import pytest

from app.config import get_settings
from app.resolver import AccessUnavailableError, ResolvedUser, Resolver

pytestmark = pytest.mark.asyncio


class _DownClient:
    async def get(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        raise httpx.ConnectError("access down")


def _cached(age_s: float) -> ResolvedUser:
    return ResolvedUser(id="u1", email="rm@evamfinance.com", roles=["BDRM"],
                        views={}, operations={}, version=1,
                        fetched_at=time.monotonic() - age_s, epoch=2)


async def test_fresh_cache_survives_outage_but_stale_fails_closed(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_ttl_s", 0.0)          # force a re-fetch attempt
    monkeypatch.setattr(s, "cache_max_stale_s", 300.0)
    r = Resolver(_DownClient())  # type: ignore[arg-type]

    # Within the staleness bound → last-known-good serves (degraded, not down).
    r._cache[("EVAM", "rm@evamfinance.com")] = _cached(age_s=60)
    got = await r.resolve("EVAM", "rm@evamfinance.com")
    assert got.email == "rm@evamfinance.com" and got.epoch == 2

    # Past the bound → FAIL CLOSED: no ever-staler grants through an outage.
    r._cache[("EVAM", "rm@evamfinance.com")] = _cached(age_s=301)
    with pytest.raises(AccessUnavailableError):
        await r.resolve("EVAM", "rm@evamfinance.com")

    # And with no cache at all → fail closed immediately.
    r._cache.clear()
    with pytest.raises(AccessUnavailableError):
        await r.resolve("EVAM", "rm@evamfinance.com")
