"""Async database engine and session management (service-agnostic).

Concurrency & stability design (the core of "no deadlocks / no race conditions"):

* A single **bounded** pool (``pool_size`` + ``max_overflow``) caps server connections so
  a burst of parallel requests can never exhaust PostgreSQL and wedge the box.
* ``pool_pre_ping`` transparently discards dead connections (e.g. after a failover).
* Every new connection gets hard ``statement_timeout`` / ``lock_timeout`` /
  ``idle_in_transaction_session_timeout`` so a slow query or forgotten transaction
  self-terminates instead of holding locks into a deadlock.
* Each request runs in exactly one short transaction (``session_scope``); we never hold a
  transaction open across an ``await`` that waits on the network.
* Writes use optimistic concurrency (``version`` column), not long-held row locks.

Settings are injected: call :func:`register_settings_provider` once at startup (or pass a
settings object to :func:`init_engine`). The settings object must expose the attributes
read in :func:`_build_engine`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from evam_backend_core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_settings_provider: Callable[[], Any] | None = None


def register_settings_provider(provider: Callable[[], Any]) -> None:
    """Supply a zero-arg callable returning the service settings (e.g. ``get_settings``)."""
    global _settings_provider
    _settings_provider = provider


def _resolve_settings(settings: Any | None) -> Any:
    if settings is not None:
        return settings
    if _settings_provider is not None:
        return _settings_provider()
    raise RuntimeError(
        "evam_backend_core.db: no settings — call init_engine(settings) or "
        "register_settings_provider(...) before using the engine."
    )


def _build_engine(settings: Any) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_dsn,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=settings.db_pool_recycle_s,
        pool_pre_ping=settings.db_pool_pre_ping,
        connect_args={
            "server_settings": {
                "application_name": settings.app_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
                "idle_in_transaction_session_timeout": str(settings.db_idle_in_txn_timeout_ms),
            },
        },
    )


def init_engine(settings: Any | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    settings = _resolve_settings(settings)
    if _engine is None:
        _engine = _build_engine(settings)
        _sessionmaker = async_sessionmaker(
            _engine, expire_on_commit=False, autoflush=False, class_=AsyncSession
        )
        log.info(
            "db_engine_initialised",
            extra={"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow},
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        log.info("db_engine_disposed")
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope(tenant_id: str | None = None) -> AsyncIterator[AsyncSession]:
    """One request → one transaction. Commits on success, rolls back on error.

    When ``tenant_id`` is provided it is set as a session-local GUC so PostgreSQL
    row-level-security policies can scope every read/write to the caller's tenant.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            if tenant_id is not None:
                await session.execute(
                    text("SELECT set_config('app.current_tenant', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session (no tenant GUC)."""
    async with session_scope() as session:
        yield session
