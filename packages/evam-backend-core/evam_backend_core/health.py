"""Liveness + readiness probe factory (used by Docker healthchecks and Kubernetes)."""

from __future__ import annotations

from fastapi.responses import ORJSONResponse
from sqlalchemy import text

from evam_backend_core.db.session import get_sessionmaker
from evam_backend_core.router import api_router


def build_health_router(app_name: str = "prism-service"):  # noqa: ANN201
    router = api_router(tags=["Health"])

    @router.get("/healthz", summary="Liveness — is the process up?")
    async def healthz() -> dict:
        return {"status": "ok", "service": app_name}

    @router.get("/readyz", summary="Readiness — can we serve traffic (DB reachable)?")
    async def readyz() -> ORJSONResponse:
        sm = get_sessionmaker()
        try:
            async with sm() as session:
                await session.execute(text("SELECT 1"))
            return ORJSONResponse({"status": "ready"})
        except Exception as exc:  # pragma: no cover - only on a real outage
            return ORJSONResponse({"status": "not_ready", "detail": str(exc)}, status_code=503)

    return router
