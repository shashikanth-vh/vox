"""The signed **internal user context** the gateway hands to downstream services.

This is the "Signed user context + internal credential" edge in the architecture: after
the gateway has (a) verified the caller's OIDC bearer token and (b) resolved their roles +
**effective permissions** from the Access service, it mints a short-lived signed token that
carries all of that. Downstream services (the Register) verify the signature and enforce
from the token — they never re-derive authorization from a static, possibly-stale copy of
the matrix, and a leaked static secret can no longer be replayed to forge an identity.

Why a signed token beats the old "plaintext X-User-* headers + a static X-Gateway-Auth
secret":

* **Tamper-evident** — the identity, roles AND effective grant are covered by the
  signature; a downstream/compromised component can't rewrite ``X-User-Roles``.
* **Per-request + expiring** — a stolen token dies in ``ttl`` seconds; a static secret
  never does.
* **Single source of truth** — the token carries the *live* effective matrix (with its
  version), so the Register enforces exactly what Access resolved.

Two signing modes, chosen by config:

* **HS256** (default) — a shared ``internal_signing_secret`` between gateway and Register.
  Simple; fine inside one trust boundary.
* **RS256** — the gateway signs with a private key, the Register verifies with the public
  key. Use when you want the Register to be provably unable to mint gateway tokens.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

ISSUER = "prism-gateway"
AUDIENCE = "prism-internal"


@dataclass
class InternalContext:
    """The verified claims a downstream service acts on."""

    tenant: str
    email: str
    user_id: str
    roles: list[str] = field(default_factory=list)
    report_ids: list[str] = field(default_factory=list)
    report_emails: list[str] = field(default_factory=list)
    # Effective (LIVE, post-stacking) access levels from Access, by view / operation name.
    effective_views: dict[str, str] = field(default_factory=dict)
    effective_operations: dict[str, str] = field(default_factory=dict)
    matrix_version: int = 0
    # The gateway's binary decision for THIS request's mapped route, if any.
    decision: str | None = None
    # Request binding: the token is minted for ONE request and is only valid for that
    # method + path (+ the operation it was decided against), so it cannot be replayed
    # against a different route during its short validity.
    method: str | None = None
    path: str | None = None
    operation: str | None = None


class InternalTokenError(Exception):
    """Raised when an internal-context token is missing, malformed, or invalid."""


def mint_internal_context(
    *,
    signing_key: str,
    tenant: str,
    email: str,
    user_id: str,
    roles: list[str] | None = None,
    report_ids: list[str] | None = None,
    report_emails: list[str] | None = None,
    effective_views: dict[str, str] | None = None,
    effective_operations: dict[str, str] | None = None,
    matrix_version: int = 0,
    decision: str | None = None,
    method: str | None = None,
    path: str | None = None,
    operation: str | None = None,
    algorithm: str = "HS256",
    ttl_seconds: int = 120,
    now: int | None = None,
) -> str:
    """Mint a signed internal-context token. ``signing_key`` is the shared secret (HS256)
    or the PEM private key (RS256). ``now`` is injectable for tests."""
    import jwt  # pyjwt

    issued = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl_seconds,
        "tenant": tenant,
        "email": email,
        "uid": user_id,
        "roles": roles or [],
        "report_ids": report_ids or [],
        "report_emails": report_emails or [],
        "eff_views": effective_views or {},
        "eff_ops": effective_operations or {},
        "matrix_version": matrix_version,
        "decision": decision,
        "method": method,
        "path": path,
        "operation": operation,
    }
    return jwt.encode(claims, signing_key, algorithm=algorithm)


def verify_internal_context(
    token: str,
    *,
    verify_key: str,
    algorithms: tuple[str, ...] = ("HS256",),
    leeway_seconds: int = 10,
) -> InternalContext:
    """Verify a token's signature + iss/aud/exp and return its claims. ``verify_key`` is
    the shared secret (HS256) or the PEM public key (RS256)."""
    import jwt

    try:
        claims = jwt.decode(
            token,
            key=verify_key,
            algorithms=list(algorithms),
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InternalTokenError(f"Invalid internal context token: {exc}") from exc

    email = claims.get("email")
    tenant = claims.get("tenant")
    uid = claims.get("uid")
    if not (email and tenant and uid):
        raise InternalTokenError("Internal context token missing email/tenant/uid.")
    return InternalContext(
        tenant=str(tenant),
        email=str(email),
        user_id=str(uid),
        roles=list(claims.get("roles", [])),
        report_ids=[str(x) for x in claims.get("report_ids", [])],
        report_emails=[str(x) for x in claims.get("report_emails", [])],
        effective_views=dict(claims.get("eff_views", {})),
        effective_operations=dict(claims.get("eff_ops", {})),
        matrix_version=int(claims.get("matrix_version", 0)),
        decision=claims.get("decision"),
        method=claims.get("method"),
        path=claims.get("path"),
        operation=claims.get("operation"),
    )
