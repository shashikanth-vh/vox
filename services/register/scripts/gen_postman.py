"""Generate a Postman collection + environment from the Register's OpenAPI schema.

Because it is derived from the live schema, the collection always covers *every* table
and every operation exactly as implemented — CRUD for each resource, plus the custom
financials/versioning, syndication-lender, ref, audit and dossier endpoints.

    python scripts/gen_postman.py
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]          # services/register
REPO_ROOT = ROOT.parents[1]                          # repo root (services/register → …)
OUT_DIR = REPO_ROOT / "postman"
OUT_DIR.mkdir(exist_ok=True)


def resolve(schema: dict, root: dict) -> dict:
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        return root["components"]["schemas"][ref]
    return schema


def sample_value(name: str, prop: dict, root: dict):
    prop = resolve(prop, root)
    if "anyOf" in prop:
        # pick the first non-null branch
        for branch in prop["anyOf"]:
            if branch.get("type") != "null":
                return sample_value(name, branch, root)
        return None
    t = prop.get("type")
    fmt = prop.get("format")
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
        return []
    if t == "object":
        return {}
    return None


def sample_body(schema_ref: dict, root: dict) -> dict:
    schema = resolve(schema_ref, root)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    body = {}
    for name, prop in props.items():
        if name == "expected_version":
            continue
        # Always include required fields; include a helpful handful of optionals.
        if name in required or len(body) < 12:
            body[name] = sample_value(name, prop, root)
    return body


def request_item(path: str, method: str, op: dict, root: dict) -> dict:
    # Convert /v1/entities/{obj_id} → /v1/entities/{{id}}
    url_path = path.replace("{obj_id}", "{{id}}").replace("{entity_id}", "{{entityId}}")
    url_path = url_path.replace("{syndication_id}", "{{synId}}").replace("{category}", "Sector")
    url_path = url_path.replace("{code}", "{{tenantCode}}")
    raw = "{{baseUrl}}" + url_path
    segments = [s for s in url_path.split("/") if s]

    headers = [
        {"key": "X-API-Key", "value": "{{apiKey}}"},
        {"key": "X-Tenant", "value": "{{tenant}}"},
        {"key": "X-Actor", "value": "{{actor}}"},
    ]
    request: dict = {"method": method.upper(), "header": headers,
                     "url": {"raw": raw, "host": ["{{baseUrl}}"], "path": segments}}

    # Query params for list endpoints.
    if method == "get" and not path.endswith("}"):
        query = []
        for p in op.get("parameters", []):
            if p.get("in") == "query":
                query.append({"key": p["name"], "value": "", "disabled": True})
        if query:
            request["url"]["query"] = query

    # Body for create/update.
    rb = op.get("requestBody")
    content = (rb or {}).get("content", {})
    if rb and "multipart/form-data" in content:
        # File-upload endpoints (e.g. the xlsx import).
        request["body"] = {"mode": "formdata", "formdata": [
            {"key": "file", "type": "file", "src": []},
        ]}
    elif rb and "application/json" in content:
        schema_ref = content["application/json"]["schema"]
        body = sample_body(schema_ref, root)
        request["header"] = headers + [{"key": "Content-Type", "value": "application/json"}]
        if method == "post" and path.endswith(("entities", "counterparties", "leads")):
            request["header"].append(
                {"key": "Idempotency-Key", "value": "{{$guid}}", "disabled": True}
            )
        if method == "patch":
            request["header"].append({"key": "If-Match", "value": '"1"', "disabled": True})
        request["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                           "options": {"raw": {"language": "json"}}}
    return {"name": f"{method.upper()} {url_path}", "request": request}


def build_collection(spec: dict) -> dict:
    folders: dict[str, list] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            tag = (op.get("tags") or ["Other"])[0]
            folders.setdefault(tag, []).append(request_item(path, method, op, spec))

    items = [{"name": tag, "item": reqs} for tag, reqs in sorted(folders.items())]
    return {
        "info": {
            "name": "PRISM Register API",
            "description": "CRUD for every Register table + custom endpoints. "
                           "Set the environment first (baseUrl, apiKey, tenant).",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }


def build_environment() -> dict:
    return {
        "name": "PRISM Register — Local",
        "values": [
            {"key": "baseUrl", "value": "http://localhost:8000", "enabled": True},
            {"key": "apiKey", "value": "dev-local-key", "enabled": True},
            {"key": "tenant", "value": "EVAM", "enabled": True},
            {"key": "actor", "value": "postman", "enabled": True},
            {"key": "id", "value": "", "enabled": True},
            {"key": "entityId", "value": "", "enabled": True},
            {"key": "synId", "value": "", "enabled": True},
            {"key": "tenantCode", "value": "EVAM", "enabled": True},
        ],
    }


def main() -> None:
    app = create_app()
    spec = app.openapi()
    collection = build_collection(spec)
    (OUT_DIR / "Register.postman_collection.json").write_text(json.dumps(collection, indent=2))
    (OUT_DIR / "Register.postman_environment.json").write_text(
        json.dumps(build_environment(), indent=2)
    )
    n = sum(len(f["item"]) for f in collection["item"])
    print(f"Wrote {n} requests across {len(collection['item'])} folders to {OUT_DIR}")


if __name__ == "__main__":
    main()
