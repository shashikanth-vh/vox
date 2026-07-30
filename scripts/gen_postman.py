"""Generate Postman collections from the FROZEN OpenAPI contracts (docs/openapi/*.json).

Decoupled from importing each service (no cross-service path clashes): it reads the committed specs,
so it always matches what the ATLAS/Node.js team codegens against. Produces:

  postman/Register.postman_collection.json       — every Register endpoint
  postman/Orchestrator.postman_collection.json   — every workflow-plane (orchestrator) endpoint
  postman/PRISM.postman_environment.json          — shared variables

    python scripts/gen_postman.py     # run scripts/export_openapi.sh first to refresh the specs
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "docs" / "openapi"
OUT = ROOT / "postman"


def resolve(schema: dict, root: dict) -> dict:
    if "$ref" in schema:
        return root["components"]["schemas"][schema["$ref"].split("/")[-1]]
    return schema


def sample_value(name: str, prop: dict, root: dict, depth: int = 0):
    prop = resolve(prop, root)
    if "anyOf" in prop:
        for b in prop["anyOf"]:
            if b.get("type") != "null":
                return sample_value(name, b, root, depth)
        return None
    t, fmt = prop.get("type"), prop.get("format")
    if name.endswith("_id") or fmt == "uuid":
        return "{{entityId}}" if name == "entity_id" else "{{id}}"
    if "enum" in prop:
        return prop["enum"][0]
    if fmt == "date":
        return "2026-03-31"
    if fmt == "date-time":
        return "2026-03-31T10:00:00Z"
    if t == "string":
        return f"sample {name}"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "array":
        item = prop.get("items", {})
        return [sample_value(name, item, root, depth + 1)] if item else []
    if t == "object" or "properties" in prop:
        # Recurse into referenced object schemas so nested payloads (e.g. document refs,
        # checklist items) are filled in, not left as {}. Depth cap guards self-referential schemas.
        if depth >= 3:
            return {}
        return sample_body(prop, root, depth + 1)
    return None


def sample_body(schema_ref: dict, root: dict, depth: int = 0) -> dict:
    schema = resolve(schema_ref, root)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    body: dict = {}
    for pname, prop in props.items():
        if pname == "expected_version":
            continue
        if pname in required or len(body) < 14:
            body[pname] = sample_value(pname, prop, root, depth)
    return body


def _path_var(path: str) -> str:
    # Friendly, known vars first; then convert any remaining {param} -> {{param}}.
    for src, dst in (("{obj_id}", "{{id}}"), ("{entity_id}", "{{entityId}}"),
                     ("{syndication_id}", "{{synId}}"), ("{category}", "Sector"),
                     ("{code}", "{{tenantCode}}"), ("{lending_id}", "{{lendingId}}"),
                     ("{checklist_id}", "{{checklistId}}"), ("{workflow_id}", "{{workflowId}}")):
        path = path.replace(src, dst)
    # Convert any remaining OpenAPI {param} placeholders to Postman {{param}} vars in one pass
    # (a plain while-loop would re-match the {{ }} it just wrote and never terminate).
    return re.sub(r"\{([^{}]+)\}", r"{{\1}}", path)


def request_item(path: str, method: str, op: dict, root: dict, headers: list[dict]) -> dict:
    url_path = _path_var(path)
    raw = "{{baseUrl}}" + url_path
    segments = [s for s in url_path.split("/") if s]
    request: dict = {"method": method.upper(), "header": [dict(h) for h in headers],
                     "url": {"raw": raw, "host": ["{{baseUrl}}"], "path": segments}}
    if method == "get" and not path.endswith("}"):
        query = [{"key": p["name"], "value": "", "disabled": True}
                 for p in op.get("parameters", []) if p.get("in") == "query"]
        if query:
            request["url"]["query"] = query
    rb = op.get("requestBody")
    content = (rb or {}).get("content", {})
    if rb and "multipart/form-data" in content:
        request["body"] = {"mode": "formdata",
                           "formdata": [{"key": "file", "type": "file", "src": []}]}
    elif rb and "application/json" in content:
        body = sample_body(content["application/json"]["schema"], root)
        request["header"] = request["header"] + [
            {"key": "Content-Type", "value": "application/json"}]
        if method == "patch":
            request["header"].append({"key": "If-Match", "value": '"1"', "disabled": True})
        request["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                           "options": {"raw": {"language": "json"}}}
    return {"name": f"{method.upper()} {url_path}", "request": request}


def build_collection(spec: dict, name: str, description: str, headers: list[dict]) -> dict:
    folders: dict[str, list] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            tag = (op.get("tags") or ["Other"])[0]
            folders.setdefault(tag, []).append(request_item(path, method, op, spec, headers))
    items = [{"name": tag, "item": reqs} for tag, reqs in sorted(folders.items())]
    return {"info": {"name": name, "description": description,
                     "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "item": items}


# EVERY request enters at the NGINX edge, which forwards to the GATEWAY, and the gateway routes
# by path prefix (/atlas, /vocx, /pulse, /orchestrator → those services; anything else → the
# Register). One header set therefore serves all collections.
#
# What the gateway does to these headers (services/gateway/app/main.py):
#   * X-API-Key      — STRIPPED. The gateway injects the correct scoped upstream credential
#                      itself, so a client never presents a backend key. Kept here (disabled)
#                      only for pointing baseUrl at a direct port to bypass the gate.
#   * Authorization  — PRESERVED. This is the real identity in production (verified OIDC bearer).
#   * X-User-Email   — read as the caller ONLY in dev (no OIDC issuer configured), then stripped
#                      and re-minted by the gateway. This is what makes dev testing work.
#   * X-User-Roles   — NOT trusted: the gateway resolves the caller's roles from the ACCESS
#                      service. Changing it here does not change authorization at the edge.
_EDGE_HEADERS = [
    {"key": "X-Tenant", "value": "{{tenant}}"},
    {"key": "X-User-Email", "value": "{{userEmail}}"},
    {"key": "X-Actor", "value": "{{actor}}"},
    # Production identity — enable this (and set bearerToken) once OIDC is on.
    {"key": "Authorization", "value": "Bearer {{bearerToken}}", "disabled": True},
    # Only needed when talking to a service DIRECTLY (registerDirectUrl / orchestratorDirectUrl),
    # which bypasses the gateway's RBAC gate. The edge strips it.
    {"key": "X-API-Key", "value": "{{apiKey}}", "disabled": True},
]
_REGISTER_HEADERS = _EDGE_HEADERS
_ORCH_HEADERS = _EDGE_HEADERS

# The business TABLES the PRISM UI does CRUD against. Excludes the governance tags, which the
# UI reaches through the workflow plane's maker/checker pair rather than plain CRUD.
_UI_TABLE_TAGS = {
    "Entities", "Leads", "Deals", "Lending Tracker", "Syndication Tracker",
    "Syndication Lenders", "Asset Monetisation", "Contracts & Assets", "Financials",
    "Monitoring & Reporting", "External Intelligence", "People", "Counterparties",
    "Interactions", "Documents", "Users & RBAC", "Reference", "Settings", "Export", "Audit",
}


def filter_spec(spec: dict, tags: set[str]) -> dict:
    """A spec containing only the operations whose first tag is in ``tags``."""
    out: dict = {"paths": {}, "components": spec.get("components", {})}
    for path, methods in spec["paths"].items():
        keep = {m: op for m, op in methods.items()
                if m in ("get", "post", "patch", "delete", "put")
                and (op.get("tags") or ["Other"])[0] in tags}
        if keep:
            out["paths"][path] = keep
    return out


def build_environment() -> dict:
    # ONE contact point: the NGINX edge (:8080). The edge forwards EVERYTHING to the gateway,
    # and the GATEWAY routes by path prefix — /atlas, /vocx, /pulse, /orchestrator to those
    # services (stripping the prefix and injecting that service's scoped key), anything else to
    # the Register behind the RBAC gate. So both hosts below are the same door:
    #     baseUrl         = :8080            -> gateway -> Register
    #     orchestratorUrl = :8080/orchestrator -> gateway -> orchestrator
    # The edge terminates TLS, so the public door is HTTPS. The *DirectUrl values are plaintext
    # (the services do not terminate TLS themselves), bypass the gateway's RBAC gate, and
    # exist for debugging only — they need X-API-Key enabled, which the edge strips.
    return {"name": "PRISM — Local",
            "values": [
                # HTTPS — the edge terminates TLS with a self-signed cert for local/demo.
                # Postman: turn OFF "SSL certificate verification" (Settings -> General), or
                # trust deploy/nginx/certs/tls.crt. :8080 only 301-redirects here.
                {"key": "baseUrl", "value": "https://localhost:8443", "enabled": True},
                {"key": "orchestratorUrl", "value": "https://localhost:8443/orchestrator",
                 "enabled": True},
                {"key": "registerDirectUrl", "value": "http://localhost:8000", "enabled": True},
                {"key": "orchestratorDirectUrl", "value": "http://localhost:8006",
                 "enabled": True},
                # The Access service is NOT prefix-routed by the gateway (the gateway consumes it
                # internally), so user provisioning in the E2E journey talks to it directly.
                {"key": "accessUrl", "value": "http://localhost:8002", "enabled": True},
                # Verified OIDC bearer — the real identity once GATEWAY_OIDC_ISSUER is set.
                {"key": "bearerToken", "value": "", "enabled": True},
                # Direct-port debugging only; the gateway injects the correct upstream key.
                {"key": "apiKey", "value": "dev-local-key", "enabled": True},
                {"key": "tenant", "value": "EVAM", "enabled": True},
                {"key": "actor", "value": "postman", "enabled": True},
                {"key": "userEmail", "value": "admin@evamfinance.com", "enabled": True},
                # NOT trusted at the edge — the gateway resolves roles from the Access
                # service. Only takes effect on a DIRECT (gate-bypassing) call.
                {"key": "userRoles", "value": "Admin", "enabled": True},
                # Maker and checker MUST be different people — the Register refuses
                # self-approval on CP/CS and Advaya handover. Both must exist in Access
                # with senior credit authority for the approval to be permitted.
                {"key": "makerEmail", "value": "maker@evamfinance.com", "enabled": True},
                {"key": "checkerEmail", "value": "checker@evamfinance.com", "enabled": True},
                {"key": "seniorRoles", "value": "Credit Head", "enabled": True},
                {"key": "id", "value": "", "enabled": True},
                {"key": "entityId", "value": "", "enabled": True},
                {"key": "lendingId", "value": "", "enabled": True},
                {"key": "checklistId", "value": "", "enabled": True},
                {"key": "workflowId", "value": "", "enabled": True},
                {"key": "synId", "value": "", "enabled": True},
                {"key": "tenantCode", "value": "EVAM", "enabled": True},
            ]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    reg = json.load(open(OPENAPI / "register.openapi.json"))
    orch = json.load(open(OPENAPI / "orchestrator.openapi.json"))
    reg_col = build_collection(
        reg, "PRISM Register API",
        "Every Register endpoint (CRUD + governance: evidence, decisions, CP/CS checklists, "
        "handover packages). Set the environment first (baseUrl, apiKey, tenant).",
        _REGISTER_HEADERS)
    orch_col = build_collection(
        orch, "PRISM Orchestrator API",
        "Workflow-plane endpoints (start/decide workflows: qualification, structuring, document "
        "collection, CP/CS checklist, Advaya handover prepare+approve). Reached through the NGINX "
        "edge (orchestratorUrl = http://localhost:8080/orchestrator); the edge forwards everything to "
        "the gateway, which routes the /orchestrator prefix here and injects the scoped key.",
        _ORCH_HEADERS)
    # A UI-facing subset: table CRUD only, every request through the edge, so a PRISM UI dev
    # exercises the SAME path the browser takes (NGINX -> gateway RBAC gate -> Register).
    ui_col = build_collection(
        filter_spec(reg, _UI_TABLE_TAGS), "PRISM UI — Register CRUD (via NGINX)",
        "Table CRUD the PRISM UI calls, routed through the NGINX edge "
        "({{baseUrl}} = http://localhost:8080 -> gateway -> Register). Each request carries the "
        "acting user's identity headers, so the RBAC matrix and record scope apply exactly as "
        "they will for a signed-in user. Governance operations (CP/CS checklists, handover "
        "packages) are in the full PRISM Register API collection.",
        _REGISTER_HEADERS)
    # The orchestrator collection uses {{orchestratorUrl}} as its host var.
    for folder in orch_col["item"]:
        for it in folder["item"]:
            it["request"]["url"]["raw"] = it["request"]["url"]["raw"].replace(
                "{{baseUrl}}", "{{orchestratorUrl}}")
            it["request"]["url"]["host"] = ["{{orchestratorUrl}}"]
    json.dump(reg_col, open(OUT / "Register.postman_collection.json", "w"), indent=2)
    json.dump(orch_col, open(OUT / "Orchestrator.postman_collection.json", "w"), indent=2)
    json.dump(ui_col, open(OUT / "PRISM_UI_CRUD.postman_collection.json", "w"), indent=2)
    json.dump(build_environment(), open(OUT / "PRISM.postman_environment.json", "w"), indent=2)
    reg_n = sum(len(f["item"]) for f in reg_col["item"])
    orch_n = sum(len(f["item"]) for f in orch_col["item"])
    ui_n = sum(len(f["item"]) for f in ui_col["item"])
    print(f"Register: {reg_n} requests · Orchestrator: {orch_n} · UI CRUD (via NGINX): {ui_n}")


if __name__ == "__main__":
    main()
