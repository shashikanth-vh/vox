"""PRISM Gateway — the REST-API service.

Day 1: the cached binary RBAC gate + reverse proxy to the Register, forwarding verified
identity headers. The deliberate seam for future client-specific logic (composition,
per-client shaping, API versioning) so core services never fork per client.

Decision ladder per request (the agreed design):
    NONE   → 403 right here (never reaches the data plane)
    FULL   → forward + ``X-Authz-Decision: FULL``
    SCOPED → forward + ``X-Authz-Decision: SCOPED`` (the Register checks the assignment)
No ``X-User-Email``  → forward unchanged (machine caller; Register applies its own mode).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from fastapi import FastAPI, Request, Response
from fastapi.responses import ORJSONResponse

from app.config import get_settings
from app.resolver import AccessUnavailableError, Resolver, UserDeniedError
from app.routes_map import operation_for

log = get_logger("gateway")

# Hop-by-hop / recomputed headers never forwarded in either direction.
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "keep-alive",
                         "transfer-encoding", "upgrade", "expect"}
_SKIP_RESPONSE_HEADERS = {"content-length", "connection", "keep-alive",
                          "transfer-encoding", "server", "date"}


def _problem(status: int, detail: str) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=status,
        content={"error": {"type": "forbidden" if status == 403 else "bad_gateway",
                           "title": "Forbidden" if status == 403 else "Upstream unavailable",
                           "detail": detail}},
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.client = httpx.AsyncClient(timeout=settings.upstream_timeout_s)
        app.state.resolver = Resolver(app.state.client)
        log.info("gateway_started", extra={"register": settings.register_url,
                                           "access": settings.access_url})
        yield
        await app.state.client.aclose()

    app = FastAPI(title="PRISM Gateway", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        return {"status": "ready", "service": settings.app_name}

    @app.post("/gateway/cache/invalidate", include_in_schema=False)
    async def invalidate_cache(request: Request) -> dict:
        request.app.state.resolver.invalidate()
        return {"invalidated": True}

    @app.get("/v1/me", tags=["Composition"],
             summary="Identity + effective permissions + active assignments (composed)")
    async def me(request: Request) -> Response:
        """The composition pattern in miniature: identity facts from the Access service +
        assignments from the Register, one response for the UI."""
        email = request.headers.get("X-User-Email")
        tenant = request.headers.get("X-Tenant", settings.default_tenant_code)
        if not email:
            return _problem(403, "X-User-Email is required.")
        try:
            user = await request.app.state.resolver.resolve(tenant, email)
        except UserDeniedError:
            return _problem(403, f"Unknown or inactive user '{email}'.")
        except AccessUnavailableError as exc:
            return _problem(502, f"Access service unavailable: {exc}")
        fwd_headers = _forward_headers(request, user, decision=None)
        resp = await request.app.state.client.get(
            f"{settings.register_url}/v1/assignments",
            params={"user_id": user.id},
            headers=fwd_headers,
        )
        assignments = resp.json() if resp.status_code == 200 else []
        return ORJSONResponse({
            "id": user.id, "email": user.email, "roles": user.roles,
            "views": user.views, "operations": user.operations,
            "matrix_version": user.version, "assignments": assignments,
        })

    def _forward_headers(request: Request, user, decision: str | None) -> dict[str, str]:  # noqa: ANN001
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _SKIP_REQUEST_HEADERS}
        if user is not None:
            headers["X-User-Email"] = user.email
            headers["X-User-Id"] = str(user.id)
            headers["X-User-Roles"] = ",".join(user.roles)
            if user.reports:
                headers["X-User-Report-Ids"] = ",".join(str(r["id"]) for r in user.reports)
                headers["X-User-Reports"] = ",".join(r["email"] for r in user.reports)
        if decision is not None:
            headers["X-Authz-Decision"] = decision
        if settings.gateway_shared_secret:
            headers["X-Gateway-Auth"] = settings.gateway_shared_secret
        return headers

    @app.api_route("/{path:path}",
                   methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                   include_in_schema=False)
    async def proxy(path: str, request: Request) -> Response:
        method = request.method
        full_path = "/" + path
        email = request.headers.get("X-User-Email")
        tenant = request.headers.get("X-Tenant", settings.default_tenant_code)

        user = None
        decision: str | None = None
        if email:
            try:
                user = await request.app.state.resolver.resolve(tenant, email)
            except UserDeniedError:
                return _problem(403, f"Unknown or inactive user '{email}'.")
            except AccessUnavailableError as exc:
                return _problem(502, f"Access service unavailable: {exc}")

            operation = operation_for(method, full_path)
            if operation is not None:
                granted = user.operations.get(operation, "NONE")
                if granted == "NONE":
                    return _problem(
                        403,
                        f"Role(s) {user.roles} may not perform '{operation}'. "
                        f"(decided at the gateway)",
                    )
                decision = "FULL" if granted in ("FULL", "APPROVE") else "SCOPED"

        upstream = f"{settings.register_url}{full_path}"
        body = await request.body()
        try:
            resp = await request.app.state.client.request(
                method, upstream, content=body,
                params=request.query_params,
                headers=_forward_headers(request, user, decision),
            )
        except httpx.HTTPError as exc:
            return _problem(502, f"Register unreachable: {exc}")
        out_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() not in _SKIP_RESPONSE_HEADERS}
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=out_headers)

    return app


app = create_app()
