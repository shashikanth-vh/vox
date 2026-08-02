"""ATLAS configuration (env prefix ``ATLAS_``). Stateless — no database.

Same pattern as every PRISM service: everything is an environment variable, a local
``.env`` works for development, and ``get_settings()`` is cached so the whole process
shares one validated Settings object.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-atlas"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # The Register — ATLAS is a read-side composer over it.
    register_base_url: str = "http://register:8000"
    register_api_key: str = "dev-local-key"
    # Default tenant when the caller does not send X-Tenant (ATLAS is multi-tenant).
    register_tenant: str = "EVAM"

    # The Access service — used to gate ATLAS views by the caller's RBAC view access
    # (dashboard, today, leads, ...). Empty = gating off (dev / behind-the-gateway).
    access_url: str = ""
    access_api_key: str = "dev-local-key"
    # When True, a caller WITHOUT X-User-Email is refused on view endpoints.
    # Keep False in dev; set True in production so dashboards are always attributable.
    require_user: bool = False
    # How long resolved permissions are cached before re-asking the Access service.
    permission_cache_ttl_s: float = 30.0

    # Today-view attention thresholds (the prototype's BN-02 "stage stuck" rule):
    # a working lending line becomes AMBER after this many days in one stage, RED
    # after red. Tune per deployment; a request may narrow with query params.
    stage_amber_days: int = 14
    stage_red_days: int = 30

    # ATLAS forwards the CALLER's verified identity to the Register so row-level scope
    # applies to reads (not just the view gate). The shared secret must match the
    # Register's REGISTER_GATEWAY_SHARED_SECRET (same trust as the gateway). Empty =
    # dev: the Register trusts forwarded headers as sent.
    gateway_shared_secret: str = ""

    # Signed internal context — the production identity channel. When set, ATLAS mints a
    # short-lived signed token (GET-bound, so it can never be replayed to a write route)
    # carrying the caller's identity + live effective grant, and STOPS sending plaintext
    # X-User-* headers + the shared secret. Must equal the Register's signing secret.
    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"

    # Verified identity (opt-in). Set the issuer to derive the caller's e-mail from a
    # bearer token instead of trusting X-User-Email. In production either set this OR
    # front ATLAS with the authenticated gateway.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_email_claim: str = "email"
    # Accept SEVERAL issuers at once — "issuer|audience,issuer2|audience2". Takes precedence
    # over the single pair above; a token is verified only by the issuer that matches its
    # `iss`, never by trying each in turn.
    oidc_issuers: str = ""
    # Organisation e-mail domains an identity may come from (comma-separated). Empty = no
    # restriction (dev). Set it whenever a PUBLIC issuer is accepted: Google will happily
    # mint a valid token for any consumer account, so the domain is the membership test.
    oidc_allowed_domains: str = ""

    # How many rows per vertical the aggregations read, page-cap. 10 pages x 200 rows
    # covers the ATLAS book comfortably; raise deliberately for very large tenants.
    max_pages_per_resource: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
