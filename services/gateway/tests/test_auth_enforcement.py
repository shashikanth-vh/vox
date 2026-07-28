"""Gateway auth enforcement — the proxy must not forward anonymous/forged identity.

These don't need the full 3-service stack: the 401 short-circuits before the resolver
or upstream is touched, so we drive the ASGI app directly with require_auth on."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def _app(monkeypatch, **env):
    for k, val in env.items():
        monkeypatch.setenv(k, val)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    # Minimal state so the proxy's pre-auth branch runs without real upstreams.
    app.state.oidc = None
    return app


async def test_require_auth_refuses_anonymous_proxy(monkeypatch):
    app = await _app(monkeypatch, GATEWAY_REQUIRE_AUTH="true",
                     GATEWAY_GATEWAY_SHARED_SECRET="s")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gw") as c:
        # No identity at all → 401, never a machine-caller passthrough.
        r = await c.get("/v1/entities")
        assert r.status_code == 401
        # A forged X-User-Email is NOT accepted under require_auth (OIDC would be the
        # only trusted source; here there is none configured, so still 401).
        r = await c.get("/v1/entities", headers={"X-User-Email": "admin@evamfinance.com"})
        assert r.status_code == 401
    from app.config import get_settings
    get_settings.cache_clear()


async def test_incoming_user_email_is_stripped_from_forwarding(monkeypatch):
    """The forward-header skip set must include x-user-email so a client can never
    smuggle an identity straight through to the Register."""
    await _app(monkeypatch, GATEWAY_REQUIRE_AUTH="false")
    from app.main import _SKIP_REQUEST_HEADERS
    for h in ("x-user-email", "x-user-id", "x-user-roles", "x-authz-decision",
              "x-gateway-auth", "x-user-report-ids", "x-user-reports"):
        assert h in _SKIP_REQUEST_HEADERS
    from app.config import get_settings
    get_settings.cache_clear()
