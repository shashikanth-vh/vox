"""Access service configuration (env prefix ``ACCESS_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from evam_backend_core.config import BaseServiceSettings
from pydantic import Field, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACCESS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "prism-access"
    db_name: str = "access"
    db_user: str = "access"
    db_password: str = "access"

    # ---- Security --------------------------------------------------------
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["dev-local-key"])
    require_api_key: bool = True
    default_tenant_code: str = "EVAM"
    # Users' e-mail addresses must belong to this domain (spec: SSO integrity).
    user_email_domain: str = "evamfinance.com"
    # When on, governance writes REQUIRE an Admin user context; off keeps
    # machine-to-machine flows working (requests that DO carry a user are always checked).
    enforce_rbac: bool = False

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
