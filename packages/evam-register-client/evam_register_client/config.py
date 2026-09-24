"""Client configuration (env-driven, 12-factor). Prefix: ``REGISTER_CLIENT_``."""

from __future__ import annotations

import ssl

from pydantic_settings import BaseSettings, SettingsConfigDict


class RegisterClientConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REGISTER_CLIENT_", extra="ignore")

    base_url: str = "http://localhost:8000"
    ca_file: str = ""
    api_key: str = "dev-local-key"
    tenant: str = "EVAM"
    # Identifies the calling vertical in the Register's audit trail (X-Actor).
    actor: str = "service"

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0

    # Transient-failure retry (network errors, timeouts, 429/502/503/504).
    retry_max_attempts: int = 3
    retry_base_delay_s: float = 0.1
    retry_max_delay_s: float = 5.0

    # Connection pool.
    max_connections: int = 100
    max_keepalive_connections: int = 20

    # Auto-attach an Idempotency-Key to create calls so a retried POST never duplicates.
    auto_idempotency: bool = True

    # Extra headers merged into every request — e.g. a BFF (ATLAS) forwarding the
    # CALLER's verified identity (X-User-Email / X-User-Roles / X-Gateway-Auth) so the
    # Register's row-level scope applies to that user, not the service actor.
    extra_headers: dict[str, str] = {}

    def tls_verify(self) -> ssl.SSLContext | bool:
        """Add a private CA without replacing the system trust store."""
        if not self.ca_file:
            return True
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=self.ca_file)
        return context
