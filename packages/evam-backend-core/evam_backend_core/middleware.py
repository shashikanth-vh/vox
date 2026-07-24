"""Request-scoped middleware: correlation id, access logging, timing."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from evam_backend_core.logging import get_logger, request_id_ctx, tenant_ctx

log = get_logger("register.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            log.exception(
                "request_failed",
                extra={"method": request.method, "path": request.url.path, "ms": round(elapsed, 1)},
            )
            raise
        finally:
            request_id_ctx.reset(token)

        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = rid
        # Skip access-log spam for health probes.
        if request.url.path not in ("/healthz", "/readyz"):
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round(elapsed, 1),
                    "tenant": tenant_ctx.get(),
                },
            )
        return response
