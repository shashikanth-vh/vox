"""VocX configuration (env prefix ``VOCX_``). Stateless — no database."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOCX_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    app_name: str = "prism-vocx"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container
    port: int = 8000

    # The Register (direct in-cluster, or via the gateway if preferred).
    register_base_url: str = "http://register:8000"
    register_api_key: str = "dev-local-key"
    register_tenant: str = "EVAM"

    # The Orchestrator (workflow plane). When set, captures that carry a company_name
    # (instead of a resolved subject id) run as durable VoxTouchpointWorkflows —
    # resolve-or-create company + lead, full-fidelity interaction, follow-up. Captures
    # with an explicit subject keep the direct fast path below.
    orchestrator_url: str = ""
    orchestrator_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
