#!/usr/bin/env python3
"""Generate PRISM_Demo_Users.postman_collection.json — the demo trio, one click.

Creating Divya / Arun / Priya by hand before every demo got old. This collection does
the WHOLE provisioning in order, idempotently (safe to run again — creates answer
"already exists", the sync reconciles, the reporting-line patches are absolute):

  01  sign in as ADMIN (Dex password grant; skips itself in dev posture)
  02  create the three Access users (identity + roles — the RBAC half)
  03  sync the roster FROM Access (the register half every dropdown reads)
  04  set the reporting lines (Arun → Divya, Priya → Divya) on the roster

Run it with the SAME environment as the E2E journey (PRISM_Full / PRISM_Full_Dex) —
it uses only variables that environment already carries (baseUrl, accessUrl, tenant,
dexUrl, ssoPassword, userEmail, adminToken).
"""

from __future__ import annotations

import json
import pathlib

OK = "pm.test('status ok', () => pm.expect(pm.response.code).to.be.oneOf([200, 201, 202]));"
OK_OR_EXISTS = ("pm.test('created or already exists', () => "
                "pm.expect(pm.response.code).to.be.oneOf([201, 409]));")

_H = [{"key": "X-Tenant", "value": "{{tenant}}"},
      {"key": "X-Actor", "value": "demo-users"}]
_ADMIN = [*_H, {"key": "Authorization", "value": "Bearer {{adminToken}}"},
          {"key": "X-User-Email", "value": "{{userEmail}}"},
          {"key": "X-User-Roles", "value": "Admin"}]

ACC = "{{accessUrl}}"
REG = "{{baseUrl}}"

# The demo cast — the same table the team works from.
USERS = [
    {"email": "divya.rao@evamfinance.com", "full_name": "Divya Rao",
     "short_name": "Divya", "is_active": True, "roles": ["Management"]},
    {"email": "arun.menon@evamfinance.com", "full_name": "Arun Menon",
     "short_name": "Arun", "is_active": True, "roles": ["Credit Head", "Deal Analyst"]},
    {"email": "priya.nair@evamfinance.com", "full_name": "Priya Nair",
     "short_name": "Priya", "phone": "+91-9800000001", "is_active": True,
     "roles": ["BDRM", "Syn RM", "AM RM"]},
]
# Roster-only reporting lines (Access keys users by id; the line lives on the roster).
REPORTS_TO = {"arun.menon@evamfinance.com": "Divya",
              "priya.nair@evamfinance.com": "Divya"}


def req(name, method, host, path, *, body=None, tests=None, headers=None, desc=None):
    hdrs = [dict(h) for h in (headers or _ADMIN)]
    r = {"method": method, "header": hdrs,
         "url": {"raw": host + path, "host": [host],
                 "path": [s for s in path.split("?")[0].split("/") if s]}}
    if "?" in path:
        r["url"]["query"] = [{"key": k, "value": v} for k, v in
                             (kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))]
    if desc:
        r["description"] = desc
    if body is not None:
        hdrs.append({"key": "Content-Type", "value": "application/json"})
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                     "options": {"raw": {"language": "json"}}}
    return {"name": name, "request": r,
            "event": [{"listen": "test",
                       "script": {"type": "text/javascript", "exec": tests or [OK]}}]}


def admin_token():
    """Dex password grant → adminToken. Skips itself when dexUrl is empty (dev posture:
    the gateway trusts X-User-Email, `Bearer ` resolves empty and is ignored)."""
    return {"name": "POST /dex/token — sign in as ADMIN",
            "request": {"method": "POST",
                "header": [{"key": "Content-Type",
                            "value": "application/x-www-form-urlencoded"}],
                "url": {"raw": "{{dexUrl}}/dex/token", "host": ["{{dexUrl}}"],
                        "path": ["dex", "token"]},
                "body": {"mode": "urlencoded", "urlencoded": [
                    {"key": "grant_type", "value": "password"},
                    {"key": "client_id", "value": "prism"},
                    {"key": "scope", "value": "openid email profile"},
                    {"key": "username", "value": "{{userEmail}}"},
                    {"key": "password", "value": "{{ssoPassword}}"}]},
                "description": "Production posture only — captures the ID token the "
                               "gateway verifies. Dev posture (empty dexUrl) skips this "
                               "and rides on header trust."},
            "event": [
                {"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
                    "if (!pm.environment.get('dexUrl')) {",
                    "  pm.environment.set('adminToken', '');",
                    "  console.log('dexUrl empty — dev posture, header trust.');",
                    "  if (pm.execution && pm.execution.skipRequest) pm.execution.skipRequest();",
                    "}"]}},
                {"listen": "test", "script": {"type": "text/javascript", "exec": [
                    "const b = pm.response.code === 200 ? pm.response.json() : {};",
                    "const tok = b.id_token || b.access_token || '';",
                    "pm.environment.set('adminToken', tok);",
                    "pm.test('admin signed in', () => pm.expect(tok, "
                    "'no token — check dexUrl/ssoPassword and that the admin exists "
                    "in Dex').to.not.eql(''));"]}}]}


def resolve_and_set_line(email: str, manager: str, slug: str):
    """Two requests: resolve the roster row by e-mail, then set its reporting line."""
    var = f"demo_{slug}_personId"
    return [
        req(f"GET /v1/people/resolve — {slug}", "GET", REG,
            f"/v1/people/resolve?name={email}",
            tests=[OK,
                   "const r = (pm.response.json() || {}).resolved;",
                   f"if (r && r.id) pm.environment.set('{var}', r.id); "
                   f"else pm.environment.unset('{var}');",
                   f"pm.test('{slug} is on the roster', () => pm.expect(r, "
                   "'not resolved — did the sync request fail?').to.exist);"]),
        req(f"PATCH /v1/people — {slug} reports to {manager}", "PATCH", REG,
            f"/v1/people/{{{{{var}}}}}", body={"reports_to": manager},
            desc="The reporting line drives team scope (a Head sees their reports' "
                 "book). Roster-only — Access keys users by id, so the line lives here."),
    ]


items = [admin_token()]
for u in USERS:
    items.append(req(
        f"POST /access/v1/users — {u['short_name']} ({', '.join(u['roles'])})",
        "POST", ACC, "/v1/users", body=u, tests=[OK_OR_EXISTS],
        desc="Identity + roles (the RBAC half). 409 = already provisioned — fine."))
items.append(req(
    "POST /v1/internal/people/sync-access — roster catches up", "POST", REG,
    "/v1/internal/people/sync-access",
    tests=[OK, "const b = pm.response.json() || {};",
           "pm.test('roster holds the demo team', () => "
           "pm.expect(b.roster_total || 0).to.be.at.least(3));"],
    desc="The register reconciles its people table FROM Access — dropdowns, scope and "
         "VocX all read this half."))
items += resolve_and_set_line("arun.menon@evamfinance.com", "Divya", "arun")
items += resolve_and_set_line("priya.nair@evamfinance.com", "Divya", "priya")

collection = {
    "info": {
        "name": "PRISM Demo Users (Divya · Arun · Priya)",
        "description": (
            "One click instead of the Add-employee dialog three times: sign in as "
            "ADMIN, provision the demo trio in Access, sync the roster, set the "
            "reporting lines. Idempotent — run it after every fresh start.\n\n"
            "Use the PRISM_Full (or PRISM_Full_Dex) environment. Sign-in passwords "
            "for all three ship in the Dex dev config: 'prism'."),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": items,
}

out = pathlib.Path(__file__).resolve().parents[1] / "postman" / \
    "PRISM_Demo_Users.postman_collection.json"
out.write_text(json.dumps(collection, indent=2) + "\n")
print(f"{out.name}: {len(items)} requests in sequence")
