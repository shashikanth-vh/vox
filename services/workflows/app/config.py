"""Settings for the workflows service (env prefix: WORKFLOWS_)."""

from __future__ import annotations

from functools import lru_cache

from evam_backend_core.config import BaseServiceSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WORKFLOWS_", extra="ignore")
    app_name: str = "prism-workflows"

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "prism-workflows"

    # Where activities reach the Register (through NGINX in a real deployment).
    register_base_url: str = "http://localhost:8000"
    register_api_key: str = "dev-local-key"
    register_tenant: str = "EVAM"
    register_actor: str = "workflows"

    # The Orchestrator API (python -m app.api) — the HTTP front door that starts
    # workflows / delivers signals. Empty api_keys = open (dev); set in production.
    api_host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    api_port: int = 8000
    api_keys: str = ""

    # Verified identity for approvals. With an OIDC issuer set, approve/reject derive
    # the decider from the bearer TOKEN (not a caller-supplied 'by' field), and the
    # Access service confirms they hold an approver role for the subject's vertical.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_email_claim: str = "email"
    access_url: str = ""             # e.g. http://prism-access — for role checks
    access_api_key: str = "dev-local-key"

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
