"""Orchestrator approval security — identity must be token-derived and mandatory.

These tests never touch Temporal: the refusals happen in the auth layer, before any
workflow client call, so we build the app and set app.state manually instead of running
the (Temporal-connecting) lifespan.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    # Stand in for what lifespan would set — no Temporal, no OIDC verifier.
    app.state.oidc = None
    app.state.temporal = None
    app.state.http = None
    return app


async def _post(app, path, json, api_key="k"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post(path, json=json, headers={"X-API-Key": api_key})


async def test_require_auth_refuses_approval_without_oidc(monkeypatch):
    """With require_auth on but no OIDC configured, an approval is REFUSED — the
    orchestrator will not trust a caller-supplied 'by'."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/leadconv-x/approve",
                    {"by": "attacker@example.com"})
    assert r.status_code == 401, r.text
    assert "verified identity" in r.text.lower()
    get_settings.cache_clear()


async def test_require_auth_refuses_conversion_request_without_oidc(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/lead-conversions",
                    {"lead_id": "l1", "requested_by": "attacker@example.com"})
    assert r.status_code == 401, r.text
    get_settings.cache_clear()


async def test_bad_api_key_blocks_before_anything(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        r = await c.post("/v1/workflows/leadconv-x/approve",
                         json={"by": "x"}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    get_settings.cache_clear()
