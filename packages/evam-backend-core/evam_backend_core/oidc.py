"""Verified identity — OIDC / JWT validation shared by the gateway and orchestrator.

The platform's trust story before this module: whoever reached the gateway could set
``X-User-Email`` to any known user and be believed. That is fine behind a trusted mesh,
but not as the sole control. This module lets a service **derive identity from a
cryptographically verified bearer token** instead:

* ``OidcVerifier`` fetches the IdP's JWKS (Dex, Auth0, Entra, Keycloak, …), caches the
  signing keys, and validates a JWT's signature, ``exp``/``nbf``, ``iss`` and ``aud``.
* The verified ``email`` (and optional ``roles``/``groups`` claim) become the identity —
  no longer client-assertable.

It is **opt-in**: a service with no ``*_OIDC_ISSUER`` configured keeps the header-trust
path (dev / trusted-mesh). When an issuer IS set, a valid ``Authorization: Bearer <jwt>``
is required and its claims win over any header. This keeps local development friction-free
while making production impersonation-proof — the reviewer's core auth gap.

Dependencies: ``pyjwt[crypto]`` (RS256/ES256 via the IdP's public keys). Network calls use
``httpx`` (already a platform dependency).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from evam_backend_core.logging import get_logger

log = get_logger("oidc")


class OidcError(Exception):
    """Token missing, malformed, expired, or signed by an unknown key."""


@dataclass
class VerifiedIdentity:
    """The trustworthy identity extracted from a validated token."""

    email: str
    subject: str
    roles: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class OidcVerifier:
    """Validates bearer tokens against an OIDC issuer's JWKS. One instance per process;
    the JWKS is cached and refreshed on an unknown ``kid`` (key rotation) or TTL."""

    def __init__(self, issuer: str, audience: str | None, client: httpx.AsyncClient,
                 *, email_claim: str = "email", roles_claim: str = "roles",
                 jwks_ttl_s: float = 3600.0) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._client = client
        self._email_claim = email_claim
        self._roles_claim = roles_claim
        self._jwks_ttl_s = jwks_ttl_s
        self._jwks_uri: str | None = None
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    async def _discover(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        url = f"{self._issuer}/.well-known/openid-configuration"
        resp = await self._client.get(url, timeout=10.0)
        resp.raise_for_status()
        jwks_uri: str = resp.json()["jwks_uri"]
        self._jwks_uri = jwks_uri
        return jwks_uri

    async def _load_keys(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._keys and (now - self._fetched_at) < self._jwks_ttl_s:
            return
        import jwt  # pyjwt

        jwks_uri = await self._discover()
        resp = await self._client.get(jwks_uri, timeout=10.0)
        resp.raise_for_status()
        keys = {}
        for jwk in resp.json().get("keys", []):
            kid = jwk.get("kid")
            if kid:
                keys[kid] = jwt.PyJWK(jwk).key
        self._keys = keys
        self._fetched_at = now

    async def verify(self, token: str) -> VerifiedIdentity:
        """Validate a JWT and return the identity, or raise ``OidcError``."""
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:  # pragma: no cover - malformed token
            raise OidcError(f"malformed token: {exc}") from exc
        kid = header.get("kid")
        await self._load_keys()
        key = self._keys.get(kid) if kid else None
        if key is None:  # unknown kid → the IdP may have rotated; refetch once.
            await self._load_keys(force=True)
            key = self._keys.get(kid) if kid else None
        if key is None:
            raise OidcError("token signed by an unknown key")
        try:
            claims = jwt.decode(
                token, key=key, algorithms=["RS256", "ES256"],
                audience=self._audience, issuer=self._issuer,
                options={"require": ["exp", "iss"],
                         "verify_aud": self._audience is not None},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"token rejected: {exc}") from exc
        email = claims.get(self._email_claim)
        if not email:
            raise OidcError(f"token has no '{self._email_claim}' claim")
        roles = claims.get(self._roles_claim) or claims.get("groups") or []
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",") if r.strip()]
        return VerifiedIdentity(email=str(email).lower(), subject=str(claims.get("sub", "")),
                                roles=list(roles), raw=claims)


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <jwt>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
