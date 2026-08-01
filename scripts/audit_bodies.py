#!/usr/bin/env python3
"""Audit every request BODY in a Postman collection against the live OpenAPI schemas.

Catches the field-name drift a run-order audit cannot: a body key the endpoint's
(strict, extra="forbid") model refuses, or a required key the body never sends.
Requests are routed to the right service spec by their BASE VARIABLE — {{accessUrl}}
and {{orchestratorUrl}} requests carry no /access or /orchestrator path prefix, so
prefix-based routing silently mis-files them against the Register (the gap that let
a wrong buyer-update field ship).

    python3 scripts/audit_bodies.py postman/PRISM_E2E_Full.postman_collection.json

Exit 0 = clean, 2 = mismatches found (printed). Requires docs/openapi/*.openapi.json
(regenerate with scripts/export_openapi.sh).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# base-variable → (spec name, path prefix that variable already carries)
BASES = {
    "baseUrl": ("register", ""),           # gateway default route; /access/... etc.
    "registerDirectUrl": ("register", ""),
    "accessUrl": ("access", ""),
    "orchestratorUrl": ("orchestrator", ""),
}
PREFIXES = [("/access", "access"), ("/orchestrator", "orchestrator"),
            ("/atlas", "atlas"), ("/vocx", "vocx"), ("/pulse", "pulse")]


def _load_specs() -> dict[str, dict]:
    specs = {}
    for p in (ROOT / "docs" / "openapi").glob("*.openapi.json"):
        specs[p.name.split(".")[0]] = json.loads(p.read_text())
    return specs


def _resolve(schema: dict, spec: dict, depth: int = 0) -> dict:
    if depth > 6 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(spec["components"]["schemas"].get(name, {}), spec, depth + 1)
    return schema


def _route(raw_url: str) -> tuple[str | None, str | None]:
    """(spec name, service-local path) for a collection URL, or (None, None)."""
    m = re.match(r"^\{\{(\w+)\}\}(/.*)$", raw_url.split("?")[0])
    if not m:
        return None, None
    base, path = m.group(1), re.sub(r"\{\{[^}]+\}\}", "x", m.group(2))
    if base not in BASES:
        return None, None
    spec_name, _ = BASES[base]
    if base in ("baseUrl",):  # one door: sub-route by prefix
        for prefix, name in PREFIXES:
            if path.startswith(prefix + "/") or path == prefix:
                return name, path[len(prefix):] or "/"
    return spec_name, path


def _op_for(spec: dict, path: str, method: str) -> dict | None:
    for p, methods in spec.get("paths", {}).items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", p), path) and method in methods:
            return methods[method]
    return None


def main() -> int:
    col_path = sys.argv[1] if len(sys.argv) > 1 else "postman/PRISM_E2E_Full.postman_collection.json"
    col = json.loads(Path(col_path).read_text())
    specs = _load_specs()
    problems: list[str] = []
    checked = skipped = 0

    def walk(items: list, folder: str) -> None:
        nonlocal checked, skipped
        for it in items:
            if "item" in it:
                walk(it["item"], it["name"])
                continue
            req = it.get("request", {})
            raw_body = (req.get("body") or {}).get("raw")
            if not raw_body:
                continue
            url = req["url"]["raw"] if isinstance(req["url"], dict) else req["url"]
            spec_name, path = _route(url)
            spec = specs.get(spec_name or "")
            if spec is None:
                skipped += 1
                continue
            op = _op_for(spec, path, req["method"].lower())
            if op is None:
                problems.append(f"[{folder}] {it['name']}: no OpenAPI operation for "
                                f"{req['method']} {spec_name}:{path}")
                continue
            schema = _resolve((op.get("requestBody") or {}).get("content", {})
                              .get("application/json", {}).get("schema", {}), spec)
            props = schema.get("properties")
            if props is None:
                skipped += 1
                continue
            try:
                sent = json.loads(re.sub(r"\{\{[^}]+\}\}", "0", raw_body))
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(sent, dict):
                continue
            checked += 1
            strict = schema.get("additionalProperties") is False
            extra = sorted(k for k in sent if k not in props)
            missing = sorted(k for k in schema.get("required", []) if k not in sent)
            # Negative tests deliberately send an invalid body and assert the refusal —
            # the collection marks them REFUSED in the request name.
            negative = "REFUSED" in it["name"]
            if extra and strict and not negative:
                problems.append(f"[{folder}] {it['name']}: EXTRA field(s) {extra} "
                                f"(strict schema refuses the whole body)")
            if missing and not negative:
                problems.append(f"[{folder}] {it['name']}: MISSING required {missing}")

    walk(col["item"], "")
    print(f"body audit: {checked} bodies checked, {skipped} skipped "
          f"(non-JSON / schema-less), {len(problems)} problem(s)")
    for p in problems:
        print("  " + p)
    return 2 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
