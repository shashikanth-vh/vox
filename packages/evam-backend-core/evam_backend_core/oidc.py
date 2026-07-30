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
                 allowed_domains: list[str] | None = None,
                 jwks_ttl_s: float = 3600.0) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._client = client
        self._email_claim = email_claim
        self._roles_claim = roles_claim
        # Signature + iss + aud prove the token is GENUINE, not that its subject belongs to
        # this organisation. With a consumer IdP as an accepted issuer (Google), ANY account —
        # including a personal one — mints a structurally valid token; only the downstream user
        # lookup would refuse it, one layer too late. An allowlist rejects at authentication.
        # Empty = no restriction (dev); set it in production.
        self._allowed_domains = [d.strip().lower().lstrip("@")
                                 for d in (allowed_domains or []) if d.strip()]
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
        email = str(email).lower()
        if self._allowed_domains:
            domain = email.rpartition("@")[2]
            if domain not in self._allowed_domains:
                # Deliberately does not echo the address back to the caller.
                log.warning("oidc_domain_rejected",
                            extra={"issuer": self._issuer, "domain": domain})
                raise OidcError(
                    f"identities from '{domain}' are not permitted for this deployment")
        return VerifiedIdentity(email=email, subject=str(claims.get("sub", "")),
                                roles=list(roles), raw=claims)


class MultiIssuerVerifier:
    """Accepts tokens from SEVERAL issuers at once — e.g. Google for people and Dex for CI in the
    same deployment.

    The issuer is chosen from the token's own (UNVERIFIED) ``iss`` claim and then that issuer's
    verifier validates everything properly, so this is a lookup, not a trust decision: an
    unrecognised ``iss`` is rejected outright. Never "try each verifier until one passes" — that
    would let a weaker issuer vouch for a stronger one's audience.
    """

    def __init__(self, verifiers: dict[str, OidcVerifier]) -> None:
        # keyed by the normalised issuer string
        self._by_issuer = {k.rstrip("/"): v for k, v in verifiers.items()}

    @property
    def issuers(self) -> list[str]:
        return sorted(self._by_issuer)

    async def verify(self, token: str) -> VerifiedIdentity:
        import jwt

        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise OidcError(f"malformed token: {exc}") from exc
        iss = str(claims.get("iss") or "").rstrip("/")
        verifier = self._by_issuer.get(iss)
        if verifier is None:
            log.warning("oidc_unknown_issuer",
                        extra={"iss": iss, "configured": self.issuers})
            raise OidcError(f"issuer {iss!r} is not accepted by this deployment")
        return await verifier.verify(token)


# What a service holds after configuration: a single-issuer verifier, a multi-issuer registry,
# or None (no OIDC configured → dev header-trust path).
TokenVerifier = OidcVerifier | MultiIssuerVerifier


def parse_issuer_specs(spec: str) -> list[tuple[str, str | None]]:
    """Parse ``"issuer|audience,issuer2|audience2"`` into pairs; audience optional.

    Lets one deployment accept several IdPs (``GATEWAY_OIDC_ISSUERS``) while the single-issuer
    settings keep working unchanged.
    """
    out: list[tuple[str, str | None]] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        issuer, _, audience = chunk.partition("|")
        issuer = issuer.strip()
        if issuer:
            out.append((issuer, audience.strip() or None))
    return out


def build_verifier(client: httpx.AsyncClient, *, issuer: str = "", audience: str | None = None,
                   issuers_spec: str = "", email_claim: str = "email",
                   allowed_domains: list[str] | None = None):
    """The one place a service turns config into a verifier.

    ``issuers_spec`` (multi-issuer) takes precedence; otherwise the single ``issuer`` is used.
    Returns ``None`` when neither is configured — which is what keeps the dev header-trust path
    available.
    """
    specs = parse_issuer_specs(issuers_spec)
    if not specs and issuer:
        specs = [(issuer, audience)]
    if not specs:
        return None
    verifiers = {
        iss: OidcVerifier(iss, aud, client, email_claim=email_claim,
                          allowed_domains=allowed_domains)
        for iss, aud in specs
    }
    if len(verifiers) == 1:
        return next(iter(verifiers.values()))
    return MultiIssuerVerifier(verifiers)


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <jwt>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
