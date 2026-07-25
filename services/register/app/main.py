"""FastAPI application factory for the PRISM Register.

Assembled from ``evam_backend_core`` (the shared backend platform) — logging, error
contract, request correlation, transient-retry, DB lifespan and health probes come from
there; this module supplies only the Register's routers and identity.
"""

from __future__ import annotations

from evam_backend_core.app import create_service_app
from fastapi import FastAPI

from app.api.custom import router as custom_router
from app.api.export import router as export_router
from app.api.imports import router as import_router
from app.api.resources import build_resource_router
from app.api.tenants import router as tenants_router
from app.api.users import router as users_router
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

DESCRIPTION = """
The **Register** is PRISM's single source of truth — every entity, deal, financial,
contract, touchpoint, signal and behavioural record. It is entity-centric, tenant-aware,
product-aware and versioned.

Auth: send an `X-API-Key` header. Bind a tenant with `X-Tenant` (default `EVAM`).
Optimistic concurrency: pass `If-Match: "<version>"` (or `expected_version`) on writes.
Idempotent creates: pass an `Idempotency-Key` header.
""".strip()


def create_app() -> FastAPI:
    settings = get_settings()
    app = create_service_app(
        settings=settings,
        routers=[
            tenants_router,
            users_router,
            export_router,
            import_router,
            custom_router,
            build_resource_router(),
        ],
        title="PRISM Register",
        version="0.1.0",
        description=DESCRIPTION,
    )

    @app.get("/", tags=["Health"], include_in_schema=False)
    async def root() -> dict:
        return {"service": settings.app_name, "docs": "/docs", "health": "/healthz"}

    return app


app = create_app()
