"""ATLAS e2e fixtures — the Register runs as a real uvicorn server; ATLAS in-process."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REGISTER_DIR = os.path.join(REPO, "services", "register")
REGISTER_PORT = 8105

os.environ["ATLAS_REGISTER_BASE_URL"] = f"http://127.0.0.1:{REGISTER_PORT}"
os.environ["ATLAS_REGISTER_API_KEY"] = "test-key"
os.environ["ATLAS_REGISTER_TENANT"] = "EVAM"
os.environ["ATLAS_LOG_LEVEL"] = "WARNING"
os.environ["ATLAS_ACCESS_URL"] = ""  # view gating off in the base fixtures


def _wait_healthy(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"register on :{port} did not become healthy")


@pytest.fixture(scope="session", autouse=True)
def register_server() -> Iterator[None]:
    env = {**os.environ,
           "REGISTER_DB_HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
           "REGISTER_DB_PORT": os.environ.get("TEST_DB_PORT", "5432"),
           "REGISTER_DB_USER": os.environ.get("TEST_DB_USER", "register"),
           "REGISTER_DB_PASSWORD": os.environ.get("TEST_DB_PASSWORD", "register"),
           "REGISTER_DB_NAME": "register_test",
           "REGISTER_API_KEYS": "test-key",
           "REGISTER_LOG_LEVEL": "WARNING",
           "REGISTER_ENVIRONMENT": "test"}
    for cmd in (["python", "-m", "alembic", "upgrade", "head"],
                ["python", "-m", "app.seed.bootstrap"]):
        res = subprocess.run(cmd, cwd=REGISTER_DIR, env=env, capture_output=True)
        assert res.returncode == 0, res.stderr.decode()[-1500:]
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(REGISTER_PORT), "--log-level", "warning"],
        cwd=REGISTER_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_healthy(REGISTER_PORT)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture
async def atlas() -> AsyncIterator[httpx.AsyncClient]:
    from httpx import ASGITransport

    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://atlas",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def register_direct() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{REGISTER_PORT}",
        headers={"X-API-Key": "test-key", "X-Tenant": "EVAM"},
    ) as client:
        yield client
