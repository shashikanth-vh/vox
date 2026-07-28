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

    # Upstreams (in-cluster service names in real deployments).
    register_url: str = "http://register:8000"
    access_url: str = "http://access:8000"

    # The API key + tenant the gateway uses when calling the ACCESS service itself
    # (client API keys are passed through to the Register untouched).
    access_api_key: str = "dev-local-key"
    default_tenant_code: str = "EVAM"

    # Facts cache: how long a /resolve answer is reused before re-fetching. 0 = always
    # re-resolve (tests). The gateway serves last-known-good if Access is briefly down.
    cache_ttl_s: float = 60.0

    # Shared secret stamped on forwarded identity headers so the Register can verify
    # they came from the gateway. Empty = dev mode (Register trusts headers as sent).
    gateway_shared_secret: str = ""

    # Verified identity (opt-in). Set the issuer to REQUIRE a valid bearer token and
    # derive the caller's e-mail from it instead of trusting X-User-Email — the
    # impersonation fix. Empty issuer = header-trust (dev / trusted mesh).
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_email_claim: str = "email"

    # Proxy behaviour.
    upstream_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
