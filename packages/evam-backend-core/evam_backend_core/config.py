"""Reusable settings base for PRISM backend services.

A service subclasses :class:`BaseServiceSettings`, sets its own ``env_prefix`` and adds
service-specific fields (API keys, tenancy, feature flags). Everything cross-cutting —
service identity, HTTP, the bounded DB pool, hard timeouts, transient-retry and
pagination — is inherited, so every service is tuned and named consistently.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- Service identity ------------------------------------------------
    app_name: str = "prism-service"
    environment: str = Field(default="local", description="local|dev|staging|prod")
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # ---- HTTP server -----------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - binds inside a container, fronted by an ingress
    port: int = 8000
    root_path: str = ""
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- Database --------------------------------------------------------
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "prism"
    db_user: str = "prism"
    db_password: str = "prism"
    database_url: PostgresDsn | None = None

    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 30
    db_pool_recycle_s: int = 1800
    db_pool_pre_ping: bool = True
    db_echo: bool = False

    db_statement_timeout_ms: int = 30_000
    db_lock_timeout_ms: int = 10_000
    db_idle_in_txn_timeout_ms: int = 60_000

    # Transparent retry of transient DB failures (deadlock / serialization / dropped conn).
    db_retry_max_attempts: int = 3
    db_retry_base_delay_ms: int = 50

    # ---- Pagination ------------------------------------------------------
    max_page_size: int = 200
    default_page_size: int = 50

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def sqlalchemy_dsn(self) -> str:
        """Async DSN for SQLAlchemy/asyncpg."""
        if self.database_url is not None:
            dsn = str(self.database_url)
            return dsn.replace("postgresql+psycopg", "postgresql").replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        # URL-encode the credentials: a password containing '@' (or ':', '/', '%')
        # otherwise corrupts the URL — the parser reads everything after the first '@'
        # as the HOST and dies with "Name or service not known", which reads like a
        # network problem and cost a real deployment an evening.
        from urllib.parse import quote
        return (
            f"postgresql+asyncpg://{quote(self.db_user, safe='')}:"
            f"{quote(self.db_password, safe='')}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def alembic_dsn(self) -> str:
        """Sync DSN for Alembic migrations (psycopg driver)."""
        return self.sqlalchemy_dsn.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)

    @property
    def is_local(self) -> bool:
        return self.environment.lower() in {"local", "test"}
