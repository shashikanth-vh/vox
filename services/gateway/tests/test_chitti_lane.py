"""The Chitti chat lane — a reserved namespace with its own trust rules.

The gateway fronts the Chitti bot (a separate VM) for the in-app chat bubble:
it verifies the browser's bearer BEFORE anything reaches the bot, allowlists
exactly two routes, and forwards a signed, path-bound delegation — never the
gateway's own static credentials. These tests drive the ASGI app directly with
fakes for the verifier/resolver and an httpx.MockTransport as the upstream, so
the security properties hold without a real bot.
"""

from __future__ import annotations

from types import SimpleNamespace

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
    app.state.oidc = None
    return app


def _clear_settings():
    from app.config import get_settings
    get_settings.cache_clear()


class _FakeVerifier:
    async def verify(self, token):  # noqa: ANN001
        assert token == "good-token"
        return SimpleNamespace(email="rm@evamfinance.com")


class _FakeResolver:
    async def resolve(self, tenant, email):  # noqa: ANN001
        return SimpleNamespace(
            email=email, id=7, roles=["RM"], reports=[],
            views={}, operations={}, version=1, epoch=0,
        )


async def test_the_chitti_namespace_is_reserved_never_a_register_fallthrough(monkeypatch):
    """/chitti/* must NEVER fall through to the Register: an unknown route is 404
    and an allowlisted route without a verified bearer is 401 — configured or not."""
    app = await _app(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gw") as c:
        # Outside the allowlist: 404, whatever the method.
        assert (await c.get("/chitti/v1/anything")).status_code == 404
        assert (await c.delete("/chitti/v1/models")).status_code == 404
        assert (await c.post("/chitti/v1/models")).status_code == 404
        # On the allowlist but anonymous (no OIDC configured at all): 401.
        assert (await c.get("/chitti/v1/models")).status_code == 401
        r = await c.post("/chitti/v1/chat/completions", json={"q": "hi"})
        assert r.status_code == 401
    _clear_settings()


async def test_chitti_unconfigured_reads_as_503_for_a_verified_user(monkeypatch):
    """A signed-in user on a box with no Chitti wiring gets an honest 503,
    not a misrouted Register response."""
    app = await _app(monkeypatch, GATEWAY_INTERNAL_SIGNING_SECRET="sekrit")
    app.state.oidc = _FakeVerifier()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gw") as c:
        r = await c.get("/chitti/v1/models",
                        headers={"Authorization": "Bearer good-token"})
        assert r.status_code == 503
    _clear_settings()


async def test_the_chitti_hop_carries_delegation_but_never_gateway_credentials(monkeypatch):
    """Happy path: the request that leaves for the bot must carry the bot's own
    X-API-Key, the signed X-Internal-Context and tenant — and must NOT carry the
    browser bearer or X-Gateway-Auth (the static secret the Register trusts for
    identity headers; on the bot's box it would be an impersonation key)."""
    import anyio

    seen: list[httpx.Request] = []

    class _Body(httpx.AsyncByteStream):
        # A Response built from content/json is born already-consumed and its
        # aiter_raw() refuses; the proxy streams, so the mock must stream too.
        async def __aiter__(self):
            yield b'{"data": [{"id": "prism-chitti"}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=_Body(),
                              headers={"content-type": "application/json"})

    app = await _app(
        monkeypatch,
        GATEWAY_CHITTI_URL="https://chitti.test:8443",
        GATEWAY_CHITTI_API_KEY="chitti-lane-key",
        GATEWAY_INTERNAL_SIGNING_SECRET="sekrit",
        GATEWAY_GATEWAY_SHARED_SECRET="the-static-register-secret",
    )
    app.state.oidc = _FakeVerifier()
    app.state.resolver = _FakeResolver()
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from app.config import get_settings
    app.state.chitti_limiter = anyio.CapacityLimiter(
        get_settings().chitti_max_concurrent_requests)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gw") as c:
        r = await c.get("/chitti/v1/models",
                        headers={"Authorization": "Bearer good-token",
                                 "X-Tenant": "EVAM"})
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "prism-chitti"

    assert len(seen) == 1
    out = seen[0]
    # Prefix stripped: the bot sees its own /v1/* namespace.
    assert str(out.url) == "https://chitti.test:8443/v1/models"
    # The bot's scoped credential and the delegation ride along.
    assert out.headers["X-API-Key"] == "chitti-lane-key"
    assert out.headers.get("X-Internal-Context")
    assert out.headers["x-tenant"] == "EVAM"
    assert out.headers["x-chitti-presentation"] == "business"
    assert out.headers.get("x-request-id")
    # The gateway's own credentials stay home.
    assert "authorization" not in out.headers
    assert "x-gateway-auth" not in out.headers
    await app.state.client.aclose()
    _clear_settings()


async def test_a_busy_chitti_lane_returns_429_not_a_queue(monkeypatch):
    """The admission limiter refuses immediately when every slot is taken —
    the browser gets a retryable 429, the bot never sees the request."""
    import anyio

    app = await _app(
        monkeypatch,
        GATEWAY_CHITTI_URL="https://chitti.test:8443",
        GATEWAY_CHITTI_API_KEY="chitti-lane-key",
        GATEWAY_INTERNAL_SIGNING_SECRET="sekrit",
    )
    app.state.oidc = _FakeVerifier()
    app.state.resolver = _FakeResolver()
    limiter = anyio.CapacityLimiter(1)
    limiter.acquire_on_behalf_of_nowait(object())   # occupy the only slot
    app.state.chitti_limiter = limiter

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gw") as c:
        r = await c.get("/chitti/v1/models",
                        headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    _clear_settings()
