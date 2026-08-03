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
                    {"lead_id": "11111111-1111-1111-1111-111111111111",
                     "requested_by": "attacker@example.com"})
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


# --------------------------------------------------------------------------- #
# Id fields refuse client-variable garbage at the door.
# --------------------------------------------------------------------------- #
async def test_vox_rejects_null_string_entity_id(monkeypatch):
    """An unset Postman/JS variable arrives as the LITERAL string "null" — truthy, so
    it used to sail into the workflow and die deep in a register query as a 500. The
    door now refuses it as 422 with a message that names the problem."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    for bad in ("null", "undefined", "{{entityId}}"):
        r = await _post(app, "/v1/workflows/vox-touchpoints",
                        {"capture_id": "cap-1", "entity_id": bad})
        assert r.status_code == 422, f"{bad!r}: {r.status_code} {r.text}"
        assert "not a UUID" in r.text
    get_settings.cache_clear()


async def test_conversion_rejects_null_string_lead_id(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    r = await _post(app, "/v1/workflows/lead-conversions",
                    {"lead_id": "null", "requested_by": "rm@evamfinance.com"})
    assert r.status_code == 422, r.text
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Pre-flight: a conversion that CANNOT succeed is refused at the door.
# --------------------------------------------------------------------------- #
class _LeadHttp:
    """Serves GET /v1/leads/{id} with a fixed row (or 404 when row is None)."""

    def __init__(self, row: dict | None) -> None:
        self.row = row

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        if self.row is None:
            return httpx.Response(404, json={"error": "missing"},
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, json=self.row,
                              request=httpx.Request("GET", url))


async def _post_conv(app, lead_id="11111111-1111-1111-1111-111111111111"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        return await c.post("/v1/workflows/lead-conversions",
                            json={"lead_id": lead_id, "requested_by": "rm@evamfinance.com"},
                            headers={"X-API-Key": "k", "X-Tenant": "EVAM"})


async def test_conversion_refuses_a_lead_with_no_company(monkeypatch):
    """A lead with no entity_id can never become a deal — the workflow enforced that
    ~0.5s in, so the caller got "pending approval" and then a silently FAILED run that
    never reached any approver's queue. Refuse at the door, and say how to fix it."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp({"id": "l1", "lead_no": "LD-139", "company": "mukesh",
                                "entity_id": None, "status": "Active"})
    r = await _post_conv(app)
    assert r.status_code == 422, r.text
    assert "not linked to a company" in r.text
    assert "LD-139" in r.text                      # names the lead the user is looking at
    get_settings.cache_clear()


async def test_conversion_refuses_an_already_converted_lead(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp({"id": "l1", "lead_no": "LD-140", "company": "x",
                                "entity_id": "e-1", "status": "Converted"})
    r = await _post_conv(app)
    assert r.status_code == 409 and "already" in r.text
    get_settings.cache_clear()


async def test_conversion_reports_a_missing_lead(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _LeadHttp(None)
    r = await _post_conv(app)
    assert r.status_code == 404
    get_settings.cache_clear()
