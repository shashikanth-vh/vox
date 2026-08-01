#!/usr/bin/env python3
"""Generate ONE Postman collection covering EVERY backend REST API, via the NGINX edge.

    postman/PRISM_All_APIs.postman_collection.json
    postman/PRISM_All_APIs.postman_environment.json

Folders (one per service, sub-folders per tag), all through the single front door:

    Register       {{baseUrl}}/v1/...                 (edge → gateway → register)
    Access         {{baseUrl}}/access/v1/...          (users, roles, live matrix, resolve)
    Orchestrator   {{baseUrl}}/orchestrator/v1/...    (workflow plane)
    ATLAS          {{baseUrl}}/atlas/v1/...           (dashboard / today / pipeline)
    VocX           {{baseUrl}}/vocx/v1/...            (voice touchpoint capture + /api/vox/*)
    PULSE          {{baseUrl}}/pulse/v1/...           (news radar)

Requests are generated from each service's LIVE FastAPI OpenAPI, so the collection can
never drift from the code. Bodies are scaffolding samples typed from the schemas; path
ids are {{variables}}. Works in both postures: every request carries X-Tenant +
X-User-Email + Bearer {{adminToken}} (empty token ⇒ dev header trust).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postman"

# service key → (dir, module:factory, edge prefix, display name)
SERVICES = [
    ("register",     "services/register",  "app.main:create_app",  "",              "Register"),
    ("access",       "services/access",    "app.main:create_app",  "/access",       "Access"),
    ("orchestrator", "services/workflows", "app.api:create_app",   "/orchestrator", "Orchestrator"),
    ("atlas",        "services/atlas",     "app.main:create_app",  "/atlas",        "ATLAS"),
    ("vocx",         "services/vocx",      "app.main:create_app",  "/vocx",         "VocX"),
    ("pulse",        "services/pulse",     "app.main:create_app",  "/pulse",        "PULSE"),
]

_H = [{"key": "X-Tenant", "value": "{{tenant}}"},
      {"key": "X-User-Email", "value": "{{userEmail}}"},
      {"key": "X-User-Roles", "value": "Admin"},
      {"key": "Authorization", "value": "Bearer {{adminToken}}"}]


def load_spec(sdir: str, factory: str) -> dict:
    """Dump the service's live OpenAPI in a FRESH interpreter.

    In-process imports collide: every service defines its models on the shared
    evam-backend-core metadata, so the second app import dies with "Table 'tenants' is
    already defined". A subprocess per service gives each app a clean world — the same
    approach as scripts/export_openapi.sh.
    """
    import subprocess
    mod_name, fn_name = factory.split(":")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([
        str(ROOT / "packages" / "evam-backend-core"),
        str(ROOT / "packages" / "evam-register-client"),
        str(ROOT / sdir)]))
    code = (f"import json, sys; from {mod_name} import {fn_name}; "
            f"json.dump({fn_name}().openapi(), sys.stdout)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=str(ROOT / sdir))
    if res.returncode != 0:
        raise SystemExit(f"OpenAPI export failed for {sdir}:\n{res.stderr[-2000:]}")
    return json.loads(res.stdout)


def _resolve(schema: dict, spec: dict, depth: int = 0) -> dict:
    if "$ref" in schema:
        if depth > 4:
            return {}
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(spec["components"]["schemas"].get(name, {}), spec, depth + 1)
    return schema


def sample(schema: dict, spec: dict, depth: int = 0):
    """A type-correct scaffolding value for a schema (NOT business-valid data)."""
    s = _resolve(schema, spec, depth)
    if "anyOf" in s:
        opts = [o for o in s["anyOf"] if _resolve(o, spec, depth).get("type") != "null"]
        return sample(opts[0], spec, depth + 1) if opts else None
    if s.get("enum"):
        return s["enum"][0]
    t = s.get("type")
    if t == "object" or "properties" in s:
        if depth > 3:
            return {}
        props = s.get("properties", {})
        return {k: sample(v, spec, depth + 1) for k, v in props.items()}
    if t == "array":
        return [sample(s.get("items", {}), spec, depth + 1)] if depth <= 3 else []
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    fmt = s.get("format")
    if fmt == "uuid":
        return "{{id}}"
    if fmt == "date":
        return "2026-01-31"
    if fmt == "date-time":
        return "2026-01-31T10:00:00Z"
    if s.get("pattern"):
        return s.get("example", "REPLACE-to-match-" + s["pattern"][:20])
    maxlen = s.get("maxLength")
    text = s.get("example", "sample")
    return text[:maxlen] if maxlen else text


def _path_var(path: str) -> str:
    return re.sub(r"\{([^{}]+)\}", r"{{\1}}", path)


def build_service(key: str, spec: dict, prefix: str, display: str) -> dict:
    by_tag: dict[str, list] = {}
    for path, methods in sorted(spec.get("paths", {}).items()):
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            tag = (op.get("tags") or ["General"])[0]
            url_path = prefix + _path_var(path)
            item: dict = {
                "name": f"{method.upper()} {prefix}{path}"
                        + (f" — {op['summary']}" if op.get("summary") else ""),
                "request": {
                    "method": method.upper(),
                    "header": [dict(h) for h in _H],
                    "url": {"raw": "{{baseUrl}}" + url_path,
                            "host": ["{{baseUrl}}"],
                            "path": [s for s in url_path.split("/") if s]},
                },
            }
            if op.get("description"):
                item["request"]["description"] = op["description"]
            q = [{"key": p["name"],
                  "value": str(sample(p.get("schema", {}), spec)),
                  "disabled": not p.get("required", False)}
                 for p in op.get("parameters", []) if p.get("in") == "query"]
            if q:
                item["request"]["url"]["query"] = q
            body = (op.get("requestBody", {}).get("content", {})
                    .get("application/json", {}).get("schema"))
            if body is not None:
                item["request"]["header"].append(
                    {"key": "Content-Type", "value": "application/json"})
                item["request"]["body"] = {
                    "mode": "raw", "raw": json.dumps(sample(body, spec), indent=2),
                    "options": {"raw": {"language": "json"}}}
            by_tag.setdefault(tag, []).append(item)
    return {"name": f"{display}  ({sum(len(v) for v in by_tag.values())} requests"
                    f" · {{{{baseUrl}}}}{prefix or '/'})",
            "item": [{"name": tag, "item": items} for tag, items in sorted(by_tag.items())]}


def main() -> None:
    os.environ.setdefault("REGISTER_ENVIRONMENT", "test")
    folders, total = [], 0
    for key, sdir, factory, prefix, display in SERVICES:
        spec = load_spec(sdir, factory)
        folder = build_service(key, spec, prefix, display)
        n = sum(len(t["item"]) for t in folder["item"])
        total += n
        folders.append(folder)
        print(f"  {display:12} {n:4} requests  ({spec.get('info', {}).get('title', '?')})")
    col = {"info": {
        "name": "PRISM · ALL Backend APIs (via NGINX)",
        "description":
            "Every REST endpoint of every backend service, generated from the LIVE OpenAPI "
            "of each app. One front door: NGINX terminates TLS on :8443 and the gateway "
            "routes by prefix (/access, /orchestrator, /atlas, /vocx, /pulse, else Register)."
            " Bodies are TYPE-correct scaffolding — replace values before sending. Sending "
            "requires an environment: select 'PRISM — All APIs' (or the E2E environments, "
            "which share the same variables).",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        # Dev posture leaves {{adminToken}} empty, resolving Authorization to a bare
        # "Bearer " — no identity, and an illegal (whitespace-tailed) header value for the
        # gateway's upstream client, which would 502 every request. Strip it client-side
        # whenever it carries no token; with a real token it rides through untouched.
        "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
            "const a = pm.request.headers.find(h => h.key.toLowerCase() === 'authorization' && !h.disabled);",
            "if (a && /^\\s*(Bearer|Basic)?\\s*$/i.test(pm.variables.replaceIn(a.value))) {",
            "    pm.request.headers.remove(a.key);",
            "}",
        ]}}],
        "item": folders}
    OUT.mkdir(exist_ok=True)
    with open(OUT / "PRISM_All_APIs.postman_collection.json", "w") as fh:
        json.dump(col, fh, indent=2)
    env = {"name": "PRISM — All APIs", "values": [
        {"key": "baseUrl", "value": "https://localhost:8443", "enabled": True},
        {"key": "tenant", "value": "EVAM", "enabled": True},
        {"key": "userEmail", "value": "admin@evamfinance.com", "enabled": True},
        {"key": "adminToken", "value": "", "enabled": True},
        {"key": "id", "value": "", "enabled": True},
    ]}
    with open(OUT / "PRISM_All_APIs.postman_environment.json", "w") as fh:
        json.dump(env, fh, indent=2)
    print(f"PRISM_All_APIs: {len(folders)} services · {total} requests")


if __name__ == "__main__":
    main()
