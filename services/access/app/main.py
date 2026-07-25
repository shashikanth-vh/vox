"""FastAPI application factory for the PRISM Access service."""

from __future__ import annotations

from evam_backend_core.app import create_service_app
from fastapi import FastAPI

from app.api import router
from app.config import get_settings

DESCRIPTION = """
The **Access** service owns PRISM's identity & authorization facts: users (the Employees
governance table), stacked roles, and the **access matrix as admin-editable data**
(seeded from the ATLAS RBAC v3.1 spec; guardrail cells immutable). Gateways call
`/v1/resolve` to fill their caches — never per request.
""".strip()


def create_app() -> FastAPI:
    settings = get_settings()
    app = create_service_app(
        settings=settings,
        routers=[router],
        title="PRISM Access",
        version="0.1.0",
        description=DESCRIPTION,
    )

    @app.get("/", tags=["Health"], include_in_schema=False)
    async def root() -> dict:
        return {"service": settings.app_name, "docs": "/docs", "health": "/healthz"}

    return app


app = create_app()
