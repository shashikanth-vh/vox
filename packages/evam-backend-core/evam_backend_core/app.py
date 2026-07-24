"""FastAPI application factory — wires the whole production-grade stack in one call.

A service calls :func:`create_service_app` with its settings and routers and gets, for
free and identically across services: structured logging, request-id/tenant/actor
correlation middleware, the RFC-9457 error contract, transient-retry configuration, CORS,
an engine lifespan, and liveness/readiness probes.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from evam_backend_core.db.session import dispose_engine, init_engine, register_settings_provider
from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.health import build_health_router
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_backend_core.retry import configure_retry

log = get_logger(__name__)


def create_service_app(
    *,
    settings: Any,
    routers: Iterable[APIRouter] = (),
    title: str = "PRISM Service",
    version: str = "0.1.0",
    description: str = "",
    include_health: bool = True,
) -> FastAPI:
    configure_logging(settings.log_level, json_logs=settings.log_json and not settings.is_local)
    configure_retry(settings.db_retry_max_attempts, settings.db_retry_base_delay_ms / 1000)
    register_settings_provider(lambda: settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        init_engine(settings)
        log.info("service_started", extra={"environment": settings.environment})
        yield
        await dispose_engine()
        log.info("service_stopped")

    app = FastAPI(
        title=title, version=version, description=description,
        default_response_class=ORJSONResponse, root_path=settings.root_path, lifespan=lifespan,
        docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
            allow_methods=["*"], allow_headers=["*"],
            expose_headers=["ETag", "X-Request-ID", "Idempotency-Replay"],
        )
    register_exception_handlers(app)

    if include_health:
        app.include_router(build_health_router(settings.app_name))
    for r in routers:
        app.include_router(r)
    return app
