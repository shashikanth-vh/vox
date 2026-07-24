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


@lru_cache
def get_settings() -> Settings:
    return Settings()
