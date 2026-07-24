#!/usr/bin/env python3
"""Scaffold a new PRISM service on evam-backend-core.

    python scripts/new_service.py cipher
    make new-service NAME=cipher

Creates services/<name>/ with a runnable app (config + health + example resource),
pyproject, README and a smoke test — everything cross-cutting is inherited from the
platform, so you just add models/schemas/resources. See BACKEND_STANDARDS.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  + {path.relative_to(REPO_ROOT)}")


def scaffold(name: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name):
        _die("service name must be lowercase letters/digits/hyphens, e.g. 'cipher'")
    root = REPO_ROOT / "services" / name
    if root.exists():
        _die(f"{root.relative_to(REPO_ROOT)} already exists")

    env_prefix = name.upper().replace("-", "_") + "_"
    pkg = name.replace("-", "_")

    _write(root / "pyproject.toml", f"""\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "prism-{name}"
version = "0.1.0"
description = "PRISM {name} service."
requires-python = ">=3.11"
dependencies = [
    "evam-backend-core",
    "evam-register-client",   # to read/write the Register
    "fastapi",
    "uvicorn[standard]",
    "gunicorn",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "httpx", "ruff", "mypy"]

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.pytest.ini_options]
asyncio_mode = "auto"
""")

    _write(root / "app" / "__init__.py", "")

    _write(root / "app" / "config.py", f'''\
"""Settings for the {name} service (env prefix: {env_prefix})."""

from __future__ import annotations

from functools import lru_cache

from evam_backend_core.config import BaseServiceSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="{env_prefix}", extra="ignore")
    app_name: str = "prism-{name}"
    db_name: str = "{pkg}"
    # Where this service reaches the Register:
    register_base_url: str = "http://localhost:8000"
    register_api_key: str = "dev-local-key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
''')

    _write(root / "app" / "main.py", f'''\
"""FastAPI app for the {name} service — assembled from evam-backend-core."""

from __future__ import annotations

from evam_backend_core.app import create_service_app
from evam_backend_core.router import api_router
from fastapi import FastAPI

from app.config import get_settings

router = api_router(prefix="/v1", tags=["{name}"])


@router.get("/ping")
async def ping() -> dict:
    return {{"service": "{name}", "ok": True}}


def create_app() -> FastAPI:
    return create_service_app(
        settings=get_settings(),
        routers=[router],
        title="PRISM {name}",
        description="PRISM {name} service (scaffolded on evam-backend-core).",
    )


app = create_app()
''')

    _write(root / "tests" / "test_smoke.py", f'''\
"""Smoke test — the scaffolded app builds and answers."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

pytestmark = pytest.mark.asyncio


async def test_ping():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/ping")
    assert r.status_code == 200 and r.json()["service"] == "{name}"
''')

    _write(root / "README.md", f"""\
# PRISM {name}

Scaffolded on **evam-backend-core**. Add your models (`app/models.py`, inheriting
`RecordBase`), schemas and resources; everything cross-cutting (logging, errors, DB pool +
timeouts + retry, health, pagination) is inherited. Use **evam-register-client** to talk to
the Register.

```bash
pip install -e packages/evam-backend-core -e packages/evam-register-client
pip install -e "services/{name}[dev]"
uvicorn app.main:app --reload      # from services/{name}
```

See [`BACKEND_STANDARDS.md`](../../BACKEND_STANDARDS.md).
""")

    print(f"\nScaffolded services/{name}. Next:")
    print(f"  pip install -e services/{name}[dev]")
    print(f"  cd services/{name} && uvicorn app.main:app --reload")


def main() -> None:
    if len(sys.argv) != 2:
        _die("usage: python scripts/new_service.py <name>")
    scaffold(sys.argv[1])


if __name__ == "__main__":
    main()
