"""The unified Swagger UI: every routed service's spec, rewritten to edge paths."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.docs import edge_spec

SERVICE_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "PRISM DocRAG", "version": "0.1.0"},
    "servers": [{"url": "http://docrag:8000"}],
    "paths": {"/v1/query": {"post": {"summary": "Query", "security": [{"APIKeyHeader": []}]}}},
    "components": {"securitySchemes": {"APIKeyHeader": {"type": "apiKey", "in": "header",
                                                        "name": "X-API-Key"}}},
}


def test_edge_spec_prefixes_paths_and_uses_edge_security():
    out = edge_spec(SERVICE_SPEC, "/docrag", "PRISM DocRAG")
    assert list(out["paths"]) == ["/docrag/v1/query"]
    assert "servers" not in out                      # Swagger calls the page's own origin
    schemes = out["components"]["securitySchemes"]
    assert set(schemes) == {"bearerAuth", "tenant", "devUserEmail"}   # no X-API-Key offered
    assert "security" not in out["paths"]["/docrag/v1/query"]["post"]
    assert SERVICE_SPEC["paths"]["/v1/query"]["post"]["security"]    # input left untouched


def test_register_keeps_root_paths():
    assert list(edge_spec(SERVICE_SPEC, "")["paths"]) == ["/v1/query"]


@pytest.fixture
def docs_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_DOCRAG_URL", "http://docrag:8000")
    monkeypatch.setenv("GATEWAY_DOCRAG_API_KEY", "docrag-key")
    monkeypatch.setenv("GATEWAY_PULSE_URL", "http://pulse:8000")
    monkeypatch.setenv("GATEWAY_CHITTI_URL", "http://chitti:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _client(app, seen: list):
    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "docrag":
            return httpx.Response(200, json=SERVICE_SPEC)
        return httpx.Response(503, text="down")

    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://edge")


@pytest.mark.asyncio
async def test_docs_page_and_specs(docs_env):
    from app.main import create_app

    app = create_app()
    seen: list = []
    async with app.router.lifespan_context(app), await _client(app, seen) as c:
        page = await c.get("/docs")
        spec = await c.get("/docs/specs/docrag.json")
        again = await c.get("/docs/specs/docrag.json")
        down = await c.get("/docs/specs/pulse.json")
        unknown = await c.get("/docs/specs/nope.json")
        own = await c.get("/docs/specs/gateway.json")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "/docs/specs/docrag.json" in page.text and "/docs/specs/register.json" in page.text
    assert "/docs/specs/chitti.json" in page.text
    assert spec.status_code == 200 and "/docrag/v1/query" in spec.json()["paths"]
    # Fetched with the injected service key, and cached (one upstream call for two reads).
    docrag_calls = [r for r in seen if r.url.host == "docrag"]
    assert len(docrag_calls) == 1 and docrag_calls[0].headers["X-API-Key"] == "docrag-key"
    assert again.json() == spec.json()
    assert down.status_code == 502 and "PULSE" in down.json()["error"]["detail"]
    assert unknown.status_code == 404
    assert "/v1/me" in own.json()["paths"]


@pytest.mark.asyncio
async def test_docs_can_be_disabled(monkeypatch):
    monkeypatch.setenv("GATEWAY_DOCS_ENABLED", "false")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    seen: list = []
    async with app.router.lifespan_context(app), await _client(app, seen) as c:
        docs = await c.get("/docs")
        spec = await c.get("/openapi.json")
    get_settings.cache_clear()
    # Not served by the gateway: both fall through to the proxy (here, to the Register).
    assert docs.status_code == 503 and spec.status_code == 503
    assert [r.url.path for r in seen] == ["/docs", "/openapi.json"]
