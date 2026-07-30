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


# --------------------------------------------------------------------------- #
# The three business-lifecycle workflow endpoints are equally auth-gated.
# --------------------------------------------------------------------------- #
async def test_business_workflow_starts_require_verified_identity(monkeypatch):
    """Starting qualification / structuring / document collection under require_auth (no OIDC)
    is refused — never started under a caller-supplied name."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    cases = [
        ("/v1/workflows/lead-qualifications", {"lead_id": "l1", "qualified_by": "a@x.com"}),
        ("/v1/workflows/deal-structurings", {"deal_id": "d1", "requested_by": "a@x.com"}),
        ("/v1/workflows/document-collections",
         {"subject_type": "Deal", "subject_id": "d1", "requested_by": "a@x.com"}),
    ]
    for path, body in cases:
        r = await _post(app, path, body)
        assert r.status_code == 401, f"{path}: {r.text}"
    get_settings.cache_clear()


async def test_committee_decision_requires_verified_identity(monkeypatch):
    """The Credit Committee decision is refused without a verified identity — a caller-supplied
    'by' can never manufacture a committee outcome at the door."""
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/struct-x/committee-decision",
                    {"by": "attacker@x.com", "approved": True})
    assert r.status_code == 401, r.text
    get_settings.cache_clear()


async def test_business_endpoints_reject_bad_api_key(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_REQUIRE_AUTH="true", WORKFLOWS_API_KEYS="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        for path, body in (
            ("/v1/workflows/deal-structurings", {"deal_id": "d1", "requested_by": "x"}),
            ("/v1/workflows/struct-x/committee-decision", {"by": "x", "approved": True}),
            ("/v1/workflows/docs-x/document-received", {"name": "kyc"}),
        ):
            r = await c.post(path, json=body, headers={"X-API-Key": "wrong"})
            assert r.status_code == 401, f"{path}: {r.text}"
    get_settings.cache_clear()
