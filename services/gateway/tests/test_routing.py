"""The gateway is the single trust boundary: it routes by path prefix, strips the
client's key, and injects the scoped per-upstream service credential."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import _is_tenant_admin_route, _route
from app.routes_map import operation_for


def _settings() -> Settings:
    return Settings(
        register_url="http://register:8000", register_api_key="reg-key",
        atlas_url="http://atlas:8000", atlas_api_key="atlas-key",
        vocx_url="http://vocx:8000", vocx_api_key="vocx-key",
        pulse_url="http://pulse:8000", pulse_api_key="pulse-key",
        orchestrator_url="http://orch:8000", orchestrator_api_key="orch-key",
    )


def test_prefix_routes_strip_and_inject_scoped_key():
    s = _settings()
    assert _route(s, "/atlas/v1/dashboard") == (
        "http://atlas:8000", "atlas-key", "/v1/dashboard")
    assert _route(s, "/pulse/v1/scan") == ("http://pulse:8000", "pulse-key", "/v1/scan")
    assert _route(s, "/vocx/v1/capture") == ("http://vocx:8000", "vocx-key", "/v1/capture")
    assert _route(s, "/orchestrator/v1/start") == (
        "http://orch:8000", "orch-key", "/v1/start")


def test_bare_prefix_maps_to_root():
    s = _settings()
    assert _route(s, "/atlas") == ("http://atlas:8000", "atlas-key", "/")


def test_default_route_is_register():
    s = _settings()
    assert _route(s, "/v1/leads") == ("http://register:8000", "reg-key", "/v1/leads")
    # A path that merely CONTAINS a service name (not a prefix segment) still hits Register.
    assert _route(s, "/v1/atlas-report") == (
        "http://register:8000", "reg-key", "/v1/atlas-report")


def test_unconfigured_prefix_falls_through_to_register():
    """A prefix with no URL configured is disabled — the route falls through to the
    Register rather than 502-ing, so a partial deployment still works."""
    s = Settings(register_url="http://register:8000", register_api_key="reg-key")
    assert _route(s, "/atlas/v1/dashboard") == (
        "http://register:8000", "reg-key", "/atlas/v1/dashboard")


def test_capability_routes_map_to_operations():
    """The gateway binary gate now classifies the fronted services' capability routes, so an
    unauthorized user is stopped at the door (each backend still enforces its own final
    authorization)."""
    assert operation_for("POST", "/vocx/v1/touchpoints") == "log_interaction"
    assert operation_for("POST", "/pulse/v1/scan") == "run_news_scan"
    assert operation_for("POST", "/pulse/v1/items") == "run_news_scan"
    assert operation_for("POST", "/orchestrator/v1/workflows/lead-conversions") == \
        "push_lead_to_deals"
    assert operation_for("POST", "/orchestrator/v1/workflows/abc123/approve") == \
        "approve_stage_change"
    assert operation_for("POST", "/orchestrator/v1/workflows/abc123/reject") == \
        "approve_stage_change"
    # A GET to a workflow status is not a mutating capability → unmapped (enforced downstream).
    assert operation_for("GET", "/orchestrator/v1/workflows/abc123") is None
    # Business-lifecycle workflow routes are classified at the door too.
    assert operation_for("POST", "/orchestrator/v1/workflows/lead-qualifications") == "edit_lead"
    assert operation_for("POST", "/orchestrator/v1/workflows/deal-structurings") == \
        "change_lending_stage"
    assert operation_for("POST", "/orchestrator/v1/workflows/document-collections") == \
        "upload_remove_documents"
    assert operation_for("POST", "/orchestrator/v1/workflows/struct-x/committee-decision") == \
        "approve_stage_change"
    assert operation_for("POST", "/orchestrator/v1/workflows/docs-x/document-received") == \
        "upload_remove_documents"


def test_tenant_admin_route_detection():
    # Every method on /v1/tenants is admin-gated by the Register — including GET/list, so
    # the gateway injects the admin key for a verified Admin on all of them.
    assert _is_tenant_admin_route("POST", "/v1/tenants") is True
    assert _is_tenant_admin_route("PATCH", "/v1/tenants/abc") is True
    assert _is_tenant_admin_route("DELETE", "/v1/tenants/abc") is True
    assert _is_tenant_admin_route("GET", "/v1/tenants") is True
    assert _is_tenant_admin_route("GET", "/v1/tenants/abc") is True
    # Non-tenant routes never trigger admin-key injection.
    assert _is_tenant_admin_route("POST", "/v1/leads") is False
    assert _is_tenant_admin_route("GET", "/v1/leads") is False


@pytest.mark.asyncio
async def test_client_key_is_stripped_and_upstream_key_injected(gw, access_direct):
    """A client that presents its own X-API-Key never reaches the data plane with it —
    the gateway strips it and injects the Register's scoped key. (The gw fixture already
    sends X-API-Key: test-key; the Register accepts only that when injected by the
    gateway, proving the strip+inject path end to end.)"""
    r = await gw.post("/v1/entities", json={"code": "ROUTE1", "legal_name": "Route One"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_tokenless_bearer_is_dropped_not_a_502(gw):
    """Postman's dev posture resolves 'Bearer {{token}}' to 'Bearer ' — no identity, and
    a header value the upstream HTTP client refuses to SEND (trailing whitespace), which
    used to turn every proxied request into 502 'Illegal header value'. The gateway must
    treat a tokenless scheme as absent and forward cleanly. A real token still rides."""
    for auth in ("Bearer ", "Bearer", "  "):
        r = await gw.get("/v1/ref", headers={"Authorization": auth})
        assert r.status_code == 200, (auth, r.status_code, r.text)
    # A non-empty (even if unverifiable-here) credential is preserved, not stripped:
    # dev posture has no OIDC verifier, so it must simply not break the request.
    r = await gw.get("/v1/ref", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200, r.text


def test_a_speech_capture_gets_the_long_upstream_window():
    """Transcription is synchronous, so the capture path needs minutes — and nothing else
    does.

    A multi-minute recording is decoded on CPU while the request is held open. Under the
    ordinary 60s budget the gateway abandons it and the user loses a recording they have
    already made, reported as an upstream failure they cannot act on. The long window is
    scoped to the capture prefixes on purpose: a hung register call must still fail fast.
    """
    from app.config import Settings
    from app.main import _timeout_for

    s = Settings(upstream_timeout_s=60.0, slow_upstream_timeout_s=300.0)
    assert _timeout_for(s, "/vocx/v1/capture_audio") == 300.0
    assert _timeout_for(s, "/vocx/v1/capture") == 300.0
    assert _timeout_for(s, "/orchestrator/v1/cam/some-line/generate") == 300.0
    # The all-firms news sweep: four hundred terms across three sources in ONE request.
    # Minutes of real work, and abandoning it halfway throws away a scan that was
    # succeeding — the same bargain as a capture, for the same reason.
    assert _timeout_for(s, "/pulse/v1/news/sweep") == 300.0
    # Everything else keeps the short one — including the rest of VocX and of PULSE.
    assert _timeout_for(s, "/v1/leads") == 60.0
    assert _timeout_for(s, "/vocx/v1/reports") == 60.0
    assert _timeout_for(s, "/pulse/v1/news/search") == 60.0
    assert _timeout_for(s, "/pulse/v1/news/diagnostics") == 60.0
    assert _timeout_for(s, "/orchestrator/v1/workflows/pending") == 60.0
    # Never SHORTER than the default, however the two are configured.
    lopsided = Settings(upstream_timeout_s=120.0, slow_upstream_timeout_s=30.0)
    assert _timeout_for(lopsided, "/vocx/v1/capture_audio") == 120.0
