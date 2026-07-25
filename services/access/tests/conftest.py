"""Access service test fixtures — real PostgreSQL (access_test) via the real migration."""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("ACCESS_DB_HOST", "127.0.0.1")
os.environ.setdefault("ACCESS_DB_PORT", "5432")
os.environ.setdefault("ACCESS_DB_USER", "register")
os.environ.setdefault("ACCESS_DB_PASSWORD", "register")
os.environ.setdefault("ACCESS_DB_NAME", "access_test")
os.environ.setdefault("ACCESS_ENVIRONMENT", "test")
os.environ.setdefault("ACCESS_API_KEYS", "test-key")
os.environ.setdefault("ACCESS_LOG_LEVEL", "WARNING")

from evam_backend_core.db.session import dispose_engine, get_sessionmaker, init_engine  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.matrix import seed_matrix  # noqa: E402
from app.security import clear_tenant_cache  # noqa: E402
from app.seed import ensure_admin_user, ensure_tenant  # noqa: E402

API_HEADERS = {"X-API-Key": "test-key", "X-Tenant": "EVAM", "X-Actor": "pytest"}
_ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    env = {**os.environ}
    subprocess.run(["alembic", "downgrade", "base"], check=False, env=env,
                   capture_output=True, cwd=_ROOT)
    res = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True, cwd=_ROOT)
    assert res.returncode == 0, res.stderr.decode()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    clear_tenant_cache()
    init_engine(get_settings())
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, "EVAM", "Evam Finance")
        await seed_matrix(session, tenant_id)
        await ensure_admin_user(session, tenant_id)
        await session.commit()

    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test",
                               headers=API_HEADERS) as c:
            yield c
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text(
                "TRUNCATE users, user_roles, access_grants, matrix_versions "
                "RESTART IDENTITY CASCADE"
            ))
            await session.commit()
        await dispose_engine()
