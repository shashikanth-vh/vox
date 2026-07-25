"""Test fixtures.

Tests run against a real PostgreSQL database (``register_test``) built with the actual
Alembic migration — so triggers, partial-unique indexes and constraints are exercised
exactly as in production, not a simplified ``create_all`` schema.

asyncpg connections are bound to the event loop that created them, and pytest-asyncio
gives each test its own loop. So the engine is created and disposed *per test*, keeping
every connection on the test's own loop — the concurrency tests still share one pool
within their test, which is exactly what they need to exercise.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Point the app at the test database BEFORE app modules read settings.
os.environ.setdefault("REGISTER_DB_HOST", "127.0.0.1")
os.environ.setdefault("REGISTER_DB_PORT", "5433")
os.environ.setdefault("REGISTER_DB_USER", "register")
os.environ.setdefault("REGISTER_DB_PASSWORD", "register")
os.environ.setdefault("REGISTER_DB_NAME", "register_test")
os.environ.setdefault("REGISTER_ENVIRONMENT", "test")
os.environ.setdefault("REGISTER_API_KEYS", "test-key")
os.environ.setdefault("REGISTER_LOG_LEVEL", "WARNING")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import clear_tenant_cache  # noqa: E402
from app.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.loader import ensure_tenant  # noqa: E402

API_HEADERS = {"X-API-Key": "test-key", "X-Tenant": "EVAM", "X-Actor": "pytest"}
_ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    """Build the schema once per session using the real migration."""
    env = {**os.environ}
    subprocess.run(["alembic", "downgrade", "base"], check=False, env=env,
                   capture_output=True, cwd=_ROOT)
    res = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True, cwd=_ROOT)
    assert res.returncode == 0, res.stderr.decode()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Fresh engine on this test's event loop.
    clear_tenant_cache()
    init_engine(get_settings())
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()

    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=API_HEADERS
        ) as c:
            yield c
    finally:
        # Clean business rows between tests (keep tenant + ref), then drop the engine.
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text(
                "TRUNCATE entities, deals, leads, lending_tracker, syndication_tracker, "
                "syndication_lenders, asset_monetisation, financials, contracts_assets, "
                "interactions, external_intelligence, monitoring_reporting, counterparties, "
                "people, documents, document_checklist, audit_log, idempotency_keys "
                "RESTART IDENTITY CASCADE"
            ))
            await session.commit()
        await dispose_engine()
