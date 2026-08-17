"""Gateway configuration (env prefix ``GATEWAY_``). No database — the gateway is stateless."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "prism-gateway"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container, fronted by NGINX
    port: int = 8000

    # Upstreams (in-cluster service names in real deployments). The gateway is the SINGLE
    # trust boundary: it fronts every service and routes by path prefix, so nothing is
    # reached around it. A prefix with an empty URL is disabled (routes fall through to
    # the Register).
    register_url: str = "http://register:8000"
    access_url: str = "http://access:8000"
    atlas_url: str = ""
    vocx_url: str = ""
    pulse_url: str = ""
    orchestrator_url: str = ""

    # Per-upstream SERVICE credential the gateway INJECTS (having stripped the client's own
    # X-API-Key). A client authenticates to the edge with a bearer token; it never presents
    # a backend data-plane key. Each backend accepts only its own scoped key.
    register_api_key: str = "dev-local-key"
    atlas_api_key: str = ""
    vocx_api_key: str = ""
    pulse_api_key: str = ""
    orchestrator_api_key: str = ""

    # The API key + tenant the gateway uses when calling the ACCESS service itself.
    access_api_key: str = "dev-local-key"
    default_tenant_code: str = "EVAM"

    # The tenant-administration credential the gateway INJECTS on tenant-admin routes for a
    # verified Admin — so the browser never holds it (it stays inside the trust boundary).
    # The client's own X-Admin-Key is always stripped. Empty = don't inject (dev).
    register_admin_api_key: str = ""

    # Facts cache: how long a /resolve answer is reused before re-fetching. 0 = always
    # re-resolve (tests). The gateway serves last-known-good if Access is briefly down.
    cache_ttl_s: float = 60.0
    # HARD staleness bound for the last-known-good answer: past this age the gateway FAILS
    # CLOSED (503) instead of serving ever-staler grants through an Access outage. Together
    # with the token TTL this defines the platform's deliberate revocation window.
    cache_max_stale_s: float = 300.0

    # Shared secret stamped on forwarded identity headers so the Register can verify
    # they came from the gateway. Empty = dev mode (Register trusts headers as sent).
    # (Legacy propagation; prefer the SIGNED internal context below for production.)
    gateway_shared_secret: str = ""

    # Signed internal context — the production identity-propagation channel. When set, the
    # gateway mints a short-lived signed token (X-Internal-Context) carrying the caller's
    # identity + LIVE effective permissions; the Register verifies the signature and
    # enforces from it. HS256 uses this value as the shared secret; RS256 uses it as the
    # PEM private key. Empty = fall back to the legacy header propagation above.
    internal_signing_secret: str = ""
    internal_signing_algorithm: str = "HS256"
    internal_token_ttl_seconds: int = 120
    # Signing-key id stamped into each token's header — names WHICH key signed it, so keys
    # can be rotated (verifiers log/pin the kid) without ambiguity.
    internal_signing_kid: str = "primary"

    # Verified identity (opt-in). Set the issuer to REQUIRE a valid bearer token and
    # derive the caller's e-mail from it instead of trusting X-User-Email — the
    # impersonation fix. Empty issuer = header-trust (dev / trusted mesh).
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_email_claim: str = "email"
    # Accept SEVERAL issuers in one deployment: "issuer|audience,issuer2|audience2".
    # Takes precedence over oidc_issuer/oidc_audience above; empty = single-issuer behaviour.
    # e.g. Google for people AND Dex for CI against the same staging environment.
    oidc_issuers: str = ""
    # Comma-separated e-mail domains permitted to authenticate. Empty = no restriction (dev).
    # REQUIRED in production once a consumer IdP (Google) is an accepted issuer: a valid token
    # proves the account is real, not that it belongs to your organisation.
    oidc_allowed_domains: str = ""
    # Refuse any proxied request without a verified identity (401), even when OIDC is
    # off — a trusted-mesh that still wants no-anonymous. Implied true when OIDC is on.
    require_auth: bool = False
    # Exact paths that stay reachable WITHOUT a bearer even under require_auth —
    # Google's OAuth redirect arrives from the BROWSER with no Authorization header.
    # Safe because completing the exchange needs the PKCE verifier persisted by an
    # AUTHENTICATED /auth/start; an attacker cannot land a token in someone's slot.
    # Comma-separated, exact match only.
    auth_exempt_paths: str = "/vocx/v1/auth/callback"

    # Proxy behaviour.
    upstream_timeout_s: float = 60.0
    # For the few paths that are slow BY DESIGN — a speech capture is transcribed
    # synchronously (a multi-minute clip on CPU outlasts the ordinary budget), and a
    # full CAM generation reads the template + prompt + documents and streams an
    # 11-section memo back, which honestly runs past five minutes. Scoped in
    # _SLOW_PATHS so nothing else inherits a minutes-long window. Keep it >= the STT
    # client's own 300s, and the whole CAM chain (UI 620s, edge 625s) above this.
    slow_upstream_timeout_s: float = 600.0

    # CORS — for a UI served from a DIFFERENT origin than this edge (a separate static
    # host, a local dev server). Comma-separated origins, e.g.
    # "https://ui.example.com,http://localhost:5173"; "*" allows any origin. Empty =
    # no CORS headers (the default: the UI is served same-origin from the edge NGINX,
    # where the browser needs none). Auth is a bearer HEADER, never a cookie, so
    # allowing an origin here grants nothing by itself — every request still needs a
    # valid token; credentials-mode CORS is deliberately not offered.
    cors_origins: str = ""

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
