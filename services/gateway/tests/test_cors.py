"""GATEWAY_CORS_ORIGINS — cross-origin UI support at the single trust boundary.

Same-origin deployments (UI served from deploy/ui/ behind the edge NGINX) need no
CORS at all, so the default is OFF. When a UI is hosted elsewhere, its origin is
allowed here — and ONLY here: auth is a bearer header, never a cookie, so an allowed
origin still needs a valid token on every request.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _app(monkeypatch, origins):  # noqa: ANN001
    if origins is None:
        monkeypatch.delenv("GATEWAY_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_CORS_ORIGINS", origins)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    get_settings.cache_clear()
    return app


async def _client(app):  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://gw")


async def test_allowed_origin_gets_preflight_and_response_headers(monkeypatch):
    app = _app(monkeypatch, "https://ui.example.com, http://localhost:5173")
    async with await _client(app) as c:
        # Preflight: answered by the middleware itself — no upstream involved.
        r = await c.options("/v1/entities", headers={
            "Origin": "https://ui.example.com",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type,x-tenant"})
        assert r.status_code == 200, r.text
        assert r.headers["access-control-allow-origin"] == "https://ui.example.com"
        assert "PATCH" in r.headers["access-control-allow-methods"]
        allowed = r.headers["access-control-allow-headers"].lower()
        for h in ("authorization", "content-type", "x-tenant"):
            assert h in allowed
        # Actual response: the grant + the headers a UI needs to read (ETag etc.).
        r = await c.get("/healthz", headers={"Origin": "http://localhost:5173"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "ETag" in r.headers["access-control-expose-headers"]
        # Bearer model: no cookie credentials are ever granted.
        assert "access-control-allow-credentials" not in r.headers


async def test_unlisted_origin_gets_no_grant(monkeypatch):
    app = _app(monkeypatch, "https://ui.example.com")
    async with await _client(app) as c:
        r = await c.options("/v1/entities", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET"})
        assert r.headers.get("access-control-allow-origin") is None


async def test_cors_is_off_by_default(monkeypatch):
    app = _app(monkeypatch, None)
    async with await _client(app) as c:
        r = await c.get("/healthz", headers={"Origin": "https://ui.example.com"})
        assert "access-control-allow-origin" not in r.headers
