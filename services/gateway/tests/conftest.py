"""Gateway e2e fixtures — REAL three-service stack.

The Register and Access services run as actual uvicorn subprocesses on localhost (their
own test databases, real migrations + seeds); the gateway app runs in-process pointed at
them. This is a true end-to-end of the agreed architecture, not a mock.
"""

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
ACCESS_DIR = os.path.join(REPO, "services", "access")

REGISTER_PORT = 8101
ACCESS_PORT = 8102

DB_ENV = {
    "DB_HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
    "DB_PORT": os.environ.get("TEST_DB_PORT", "5432"),
    "DB_USER": os.environ.get("TEST_DB_USER", "register"),
    "DB_PASSWORD": os.environ.get("TEST_DB_PASSWORD", "register"),
}

# Gateway settings must be in the environment BEFORE app.config is imported.
os.environ["GATEWAY_REGISTER_URL"] = f"http://127.0.0.1:{REGISTER_PORT}"
os.environ["GATEWAY_ACCESS_URL"] = f"http://127.0.0.1:{ACCESS_PORT}"
os.environ["GATEWAY_ACCESS_API_KEY"] = "test-key"
# The gateway strips the client's X-API-Key and injects the upstream's own scoped key;
# the Register in this stack accepts "test-key", so that's what the gateway must present.
os.environ["GATEWAY_REGISTER_API_KEY"] = "test-key"
os.environ["GATEWAY_CACHE_TTL_S"] = "0"          # always re-resolve → matrix edits are live
os.environ["GATEWAY_GATEWAY_SHARED_SECRET"] = "e2e-secret"
os.environ["GATEWAY_LOG_LEVEL"] = "WARNING"


def _svc_env(prefix: str, db_name: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ}
    env.update({
        f"{prefix}_DB_HOST": DB_ENV["DB_HOST"],
        f"{prefix}_DB_PORT": DB_ENV["DB_PORT"],
        f"{prefix}_DB_USER": DB_ENV["DB_USER"],
        f"{prefix}_DB_PASSWORD": DB_ENV["DB_PASSWORD"],
        f"{prefix}_DB_NAME": db_name,
        f"{prefix}_API_KEYS": "test-key",
        f"{prefix}_LOG_LEVEL": "WARNING",
        f"{prefix}_ENVIRONMENT": "test",
    })
    env.update(extra or {})
    return env


def _run(cmd: list[str], cwd: str, env: dict[str, str]) -> None:
    res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True)
    assert res.returncode == 0, f"{cmd} failed:\n{res.stderr.decode()[-2000:]}"


def _wait_healthy(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"service on :{port} did not become healthy")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Ordering-independence: a test that patches env + rebuilds the settings cache
    (e.g. the require_auth enforcement tests) must never leak its cached Settings into
    the next test — a stale REQUIRE_AUTH=true turns every later proxy test into a 401."""
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def stack() -> Iterator[None]:
    # Pure-ASGI tests (auth gate short-circuits before any upstream) can run without
    # the 3-service stack or a database. CI leaves this unset and keeps the real stack.
    if os.environ.get("GATEWAY_TESTS_NO_STACK"):
        yield
        return
    reg_env = _svc_env("REGISTER", "register_test")
    acc_env = _svc_env("ACCESS", "access_test")

    # Fresh schemas + seeds through the real entrypoint paths.
    _run(["python", "-m", "alembic", "downgrade", "base"], REGISTER_DIR, reg_env)
    _run(["python", "-m", "alembic", "upgrade", "head"], REGISTER_DIR, reg_env)
    _run(["python", "-m", "app.seed.bootstrap"], REGISTER_DIR, reg_env)
    _run(["python", "-m", "alembic", "downgrade", "base"], ACCESS_DIR, acc_env)
    _run(["python", "-m", "alembic", "upgrade", "head"], ACCESS_DIR, acc_env)
    _run(["python", "-m", "app.seed"], ACCESS_DIR, acc_env)

    # The Register must trust ONLY gateway-stamped identity in this stack (CF7).
    reg_env["REGISTER_GATEWAY_SHARED_SECRET"] = "e2e-secret"

    procs: list[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(
            ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(REGISTER_PORT), "--log-level", "warning"],
            cwd=REGISTER_DIR, env=reg_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen(
            ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(ACCESS_PORT), "--log-level", "warning"],
            cwd=ACCESS_DIR, env=acc_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        _wait_healthy(REGISTER_PORT)
        _wait_healthy(ACCESS_PORT)
        yield
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest_asyncio.fixture
async def gw() -> AsyncIterator[httpx.AsyncClient]:
    """The gateway app, in-process, its proxy talking real HTTP to the stack."""
    from httpx import ASGITransport

    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw",
        headers={"X-API-Key": "test-key", "X-Tenant": "EVAM"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def access_direct() -> AsyncIterator[httpx.AsyncClient]:
    """Direct client to the Access service (for admin governance in tests)."""
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{ACCESS_PORT}",
        headers={"X-API-Key": "test-key", "X-Tenant": "EVAM"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def register_direct() -> AsyncIterator[httpx.AsyncClient]:
    """Direct client to the Register — used to prove the bypass wall (CF7)."""
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{REGISTER_PORT}",
        headers={"X-API-Key": "test-key", "X-Tenant": "EVAM"},
    ) as client:
        yield client
