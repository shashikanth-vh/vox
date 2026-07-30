#!/usr/bin/env python3
"""Static run-order audit of a Postman collection + environment.

Simulates a top-to-bottom Collection Runner pass WITHOUT sending anything:
  * the environment file provides the initial variable state;
  * each request's pre-request script runs (set/unset extracted textually), then the
    request "resolves" — every {{var}} in its URL, headers and body must exist;
  * then its test script runs (sets applied for subsequent requests).

Findings:
  HARD   var consumed but NEVER written anywhere and absent from the environment
  ORDER  var consumed before the request that writes it (works only out of order)
  UNSET  var consumed after a script clears it, with no rewrite in between
  COND   var consumed whose only earlier writes are inside if-blocks (may stay empty)
  EMPTY  var consumed whose value is the empty string at that point (env default "")

Exit code is non-zero only for BLOCKING findings (HARD / ORDER / UNSET) — COND and
EMPTY are warnings, since tokens are legitimately empty in the dev posture.

usage: audit_postman.py <collection.json> <environment.json> [--allow-empty v1,v2]
"""
import json
import re
import sys

VAR = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
SET = re.compile(r"pm\.(?:environment|collectionVariables|globals)\.set\(\s*['\"]([A-Za-z0-9_]+)['\"]")
UNSET = re.compile(r"pm\.(?:environment|collectionVariables|globals)\.unset\(\s*['\"]([A-Za-z0-9_]+)['\"]")
# unset via forEach list:  ['a','b'].forEach(k => pm.environment.unset(k))
LIST_UNSET = re.compile(r"\[((?:\s*'[A-Za-z0-9_]+'\s*,?)+)\]\s*\.forEach\(\s*\w+\s*=>\s*pm\.environment\.unset", re.S)


def scripts(item, kind):
    out = []
    for ev in item.get("event", []):
        if ev.get("listen") == kind:
            out.append("\n".join(ev.get("script", {}).get("exec", [])))
    return "\n".join(out)


def consumed(req):
    got = set()
    url = req.get("url", {})
    got |= set(VAR.findall(url.get("raw", "") if isinstance(url, dict) else str(url)))
    for h in req.get("header", []):
        if not h.get("disabled"):
            got |= set(VAR.findall(h.get("key", "") + " " + h.get("value", "")))
    body = req.get("body", {}) or {}
    got |= set(VAR.findall(body.get("raw", "") or ""))
    for p in body.get("urlencoded", []) or []:
        got |= set(VAR.findall(p.get("key", "") + " " + p.get("value", "")))
    return got


def apply_script(text, state, writes_conditional):
    for lst in LIST_UNSET.findall(text):
        for name in re.findall(r"'([A-Za-z0-9_]+)'", lst):
            state.pop(name, None)
    for name in UNSET.findall(text):
        state.pop(name, None)
    # crude conditionality: a set whose position is inside any { } block following an `if`
    depth_by_pos, depth = {}, 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        depth_by_pos[i] = depth
    for m in SET.finditer(text):
        name = m.group(1)
        cond = depth_by_pos.get(m.start(), 0) > 0 and "if" in text[:m.start()][-400:]
        if cond and name not in state:
            writes_conditional.add(name)
        state[name] = "<dynamic>"


def walk(items, order):
    for it in items:
        if "item" in it:
            walk(it["item"], order)
        else:
            order.append(it)


def main():
    col = json.load(open(sys.argv[1]))
    envf = json.load(open(sys.argv[2]))
    allow_empty = set()
    if "--allow-empty" in sys.argv:
        allow_empty = set(sys.argv[sys.argv.index("--allow-empty") + 1].split(","))
    state = {v["key"]: v.get("value", "") for v in envf["values"] if v.get("enabled", True)}
    initial_env = set(state)
    all_writers = {}          # var -> first request index that writes it
    order = []
    walk(col["item"], order)
    for idx, it in enumerate(order):
        for kind in ("prerequest", "test"):
            for name in SET.findall(scripts(it, kind)):
                all_writers.setdefault(name, idx)
    findings = []
    cond_writes = set()
    for idx, it in enumerate(order):
        apply_script(scripts(it, "prerequest"), state, cond_writes)
        for var in sorted(consumed(it.get("request", {}))):
            if var not in state:
                if var in initial_env:
                    findings.append(("UNSET", var, idx, it["name"]))
                elif var not in all_writers:
                    findings.append(("HARD", var, idx, it["name"]))
                elif all_writers[var] > idx:
                    findings.append(("ORDER", var, idx, it["name"]))
                else:
                    findings.append(("UNSET", var, idx, it["name"]))
            elif state[var] == "<dynamic>" and var in cond_writes and var not in allow_empty:
                findings.append(("COND", var, idx, it["name"]))
            elif state[var] == "" and var not in allow_empty:
                kind = "COND" if var in cond_writes else "EMPTY"
                findings.append((kind, var, idx, it["name"]))
        apply_script(scripts(it, "test"), state, cond_writes)
    sev = {"HARD": 0, "ORDER": 1, "UNSET": 2, "COND": 3, "EMPTY": 4}
    findings.sort(key=lambda f: (sev[f[0]], f[2]))
    for kind, var, idx, name in findings:
        print(f"{kind:5} #{idx:3} {var:22} in: {name[:70]}")
    print(f"\n{len(order)} requests audited, {len(findings)} findings "
          f"({sum(1 for f in findings if f[0] in ('HARD', 'ORDER', 'UNSET'))} blocking)")
    return 1 if any(f[0] in ("HARD", "ORDER", "UNSET") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
