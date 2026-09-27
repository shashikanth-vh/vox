"""One Swagger UI for every API behind the front door.

Each service still generates its own OpenAPI, but its built-in ``/docs`` page is useless
through the edge: the page fetches ``/openapi.json`` from the ROOT (which is the gateway's
own spec), and "Try it out" would call the service's paths without their ``/docrag``,
``/atlas``… prefix. So the gateway serves ``/docs`` itself: a service picker whose specs it
fetches server-side and rewrites to edge paths, so every call made from Swagger travels
the same route a real client does — edge → gateway (identity, RBAC gate) → service.

Swagger grants nothing: every request it sends still needs a bearer token (or, in the dev
posture, X-User-Email), entered once under "Authorize".
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import httpx

# Swagger UI from the same CDN FastAPI's own /docs pages use.
_SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
SPEC_TTL_S = 60.0

# What the edge expects on every call. Service-level schemes (X-API-Key) are dropped: the
# gateway strips a client's key and injects the service's own, so offering it would mislead.
_SECURITY_SCHEMES = {
    "bearerAuth": {"type": "http", "scheme": "bearer",
                   "description": "OIDC access token (Dex / Google). Required when the "
                                  "platform runs with verified identity."},
    "tenant": {"type": "apiKey", "in": "header", "name": "X-Tenant",
               "description": "Tenant code, e.g. EVAM."},
    "devUserEmail": {"type": "apiKey", "in": "header", "name": "X-User-Email",
                     "description": "DEV posture only (no OIDC configured): the caller's "
                                    "e-mail, trusted as identity. Ignored when OIDC is on."},
}


def upstreams(settings: Any) -> list[tuple[str, str, str, str, str]]:
    """(key, display name, base URL, injected key, edge prefix) for every routed service."""
    rows = [
        ("register", "Register", settings.register_url, settings.register_api_key, ""),
        ("access", "Access", settings.access_url, settings.access_api_key, "/access"),
        ("orchestrator", "Orchestrator", settings.orchestrator_url,
         settings.orchestrator_api_key, "/orchestrator"),
        ("atlas", "ATLAS", settings.atlas_url, settings.atlas_api_key, "/atlas"),
        ("vocx", "VocX", settings.vocx_url, settings.vocx_api_key, "/vocx"),
        ("pulse", "PULSE", settings.pulse_url, settings.pulse_api_key, "/pulse"),
        ("docrag", "DocRAG", settings.docrag_url, settings.docrag_api_key, "/docrag"),
        ("chitti", "Chitti", settings.chitti_url, settings.chitti_api_key, "/chitti"),
    ]
    return [r for r in rows if r[2]]


def edge_spec(spec: dict, prefix: str, title: str | None = None) -> dict:
    """A service's OpenAPI rewritten for the edge: paths carry the gateway prefix, the
    server is the page's own origin, and security is what the edge actually accepts."""
    out = copy.deepcopy(spec)
    out["paths"] = {prefix + path: ops for path, ops in out.get("paths", {}).items()}
    # No `servers`: Swagger then calls the origin the page was loaded from — the edge.
    out.pop("servers", None)
    components = out.setdefault("components", {})
    components["securitySchemes"] = copy.deepcopy(_SECURITY_SCHEMES)
    out["security"] = [{"bearerAuth": [], "tenant": [], "devUserEmail": []}]
    for ops in out["paths"].values():
        for op in ops.values():
            if isinstance(op, dict):
                op.pop("security", None)
    if title:
        out.setdefault("info", {})["title"] = title
    return out


class SpecCache:
    """Upstream specs, fetched on demand and reused for SPEC_TTL_S. An upstream that is
    down fails only its own entry in the picker, never the page."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, dict]] = {}

    async def get(self, client: httpx.AsyncClient, key: str, base_url: str, api_key: str,
                  prefix: str, title: str) -> dict:
        hit = self._entries.get(key)
        if hit and time.monotonic() - hit[0] < SPEC_TTL_S:
            return hit[1]
        headers = {"X-API-Key": api_key} if api_key else {}
        resp = await client.get(f"{base_url}/openapi.json", headers=headers, timeout=10.0)
        resp.raise_for_status()
        spec = edge_spec(resp.json(), prefix, title)
        self._entries[key] = (time.monotonic(), spec)
        return spec


def swagger_html(entries: list[tuple[str, str]], primary: str) -> str:
    """The multi-spec Swagger UI page; ``entries`` is (display name, spec URL)."""
    urls = json.dumps([{"name": n, "url": u} for n, u in entries])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRISM APIs</title>
<link rel="stylesheet" href="{_SWAGGER}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{_SWAGGER}/swagger-ui-bundle.js"></script>
<script src="{_SWAGGER}/swagger-ui-standalone-preset.js"></script>
<script>
window.ui = SwaggerUIBundle({{
  urls: {urls},
  "urls.primaryName": {json.dumps(primary)},
  dom_id: "#swagger-ui",
  deepLinking: true,
  persistAuthorization: true,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
  layout: "StandaloneLayout"
}});
</script>
</body>
</html>"""
