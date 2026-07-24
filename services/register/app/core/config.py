"""Register service configuration.

Inherits every cross-cutting knob (identity, HTTP, DB pool, timeouts, retry, pagination)
from ``evam_backend_core.config.BaseServiceSettings`` and adds only the Register-specific
settings (API keys, tenancy, idempotency, RLS). Values come from ``REGISTER_``-prefixed
environment variables (12-factor); nothing secret is hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from evam_backend_core.config import BaseServiceSettings
from pydantic import Field, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="REGISTER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Register-specific identity/DB defaults.
    app_name: str = "prism-register"
    db_name: str = "register"
    db_user: str = "register"
    db_password: str = "register"

    # ---- Security --------------------------------------------------------
    # Comma-separated API keys accepted by the service (X-API-Key header).
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["dev-local-key"])
    require_api_key: bool = True
    default_tenant_code: str = "EVAM"
    # Enforce PostgreSQL row-level security using the request tenant. Off by default for
    # the single-tenant local build; turn on in multi-tenant deployments.
    enforce_rls: bool = False

    # ---- Behaviour -------------------------------------------------------
    idempotency_ttl_hours: int = 48

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
