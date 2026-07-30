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
from evam_backend_core.internal_token import mint_internal_context
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_backend_core.oidc import (
    OidcError,
    TokenVerifier,
    bearer_token,
    build_verifier,
)
from fastapi import FastAPI, Request, Response
from fastapi.responses import ORJSONResponse

from app.config import get_settings
from app.resolver import AccessUnavailableError, Resolver, UserDeniedError
from app.routes_map import operation_for

log = get_logger("gateway")

# Hop-by-hop / recomputed headers never forwarded in either direction — PLUS every
# internal identity/authorization header. These are server-derived only: a client that
# injects X-Authz-Decision / X-Gateway-Auth / X-User-Roles must never have them
# survive the hop (with the gateway then stamping its valid secret on the forgery).
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "keep-alive",
                         "transfer-encoding", "upgrade", "expect",
                         "x-authz-decision", "x-gateway-auth", "x-user-email",
                         "x-user-id", "x-user-roles", "x-user-report-ids",
                         "x-user-reports", "x-internal-context",
                         # The client never presents a backend data-plane key. Strip whatever
                         # X-API-Key it sent so the gateway injects the correct scoped upstream
                         # credential itself. (The OIDC bearer in Authorization is preserved —
                         # ATLAS / orchestrator verify it themselves for defence in depth.)
                         "x-api-key",
                         # The tenant-admin credential is injected by the gateway for a
                         # verified Admin only — a client can never present its own.
                         "x-admin-key"}
_SKIP_RESPONSE_HEADERS = {"content-length", "connection", "keep-alive",
                          "transfer-encoding", "server", "date"}


def _is_tenant_admin_route(method: str, path: str) -> bool:
    """A tenant-administration route. The Register gates ALL of these — reads (GET/list)
    AND writes — on the admin credential, so the gateway injects it for a verified Admin on
    every method (a missing key on GET /v1/tenants was breaking production tenant reads)."""
    return method in ("GET", "POST", "PATCH", "DELETE") and (
        path == "/v1/tenants" or path.startswith("/v1/tenants/"))


def _route(settings, full_path: str) -> tuple[str, str, str]:  # noqa: ANN001
    """Map an inbound path to ``(upstream_base_url, injected_api_key, downstream_path)``.

    The gateway is the single trust boundary: it fronts EVERY service and routes by path
    prefix, stripping the prefix so each sub-service sees its own paths. A prefix whose URL
    is unconfigured (empty) falls through to the Register, so a partial deployment still
    works and nothing is reachable around the gateway."""
    prefixes = (
        # Access is the identity/RBAC service. Routing it here makes the WHOLE platform reachable
        # through the one public door (the ATLAS UI must manage users), while keeping the rule that
        # a client never presents a backend key — the gateway injects Access's own. Access still
        # enforces its Admin-only governance writes against the forwarded identity.
        ("/access", settings.access_url, settings.access_api_key),
        ("/atlas", settings.atlas_url, settings.atlas_api_key),
        ("/vocx", settings.vocx_url, settings.vocx_api_key),
        ("/pulse", settings.pulse_url, settings.pulse_api_key),
        ("/orchestrator", settings.orchestrator_url, settings.orchestrator_api_key),
    )
    for prefix, url, key in prefixes:
        if url and (full_path == prefix or full_path.startswith(prefix + "/")):
            return url, key, full_path[len(prefix):] or "/"
    return settings.register_url, settings.register_api_key, full_path


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
        app.state.oidc = (
            build_verifier(
                app.state.client, issuer=settings.oidc_issuer,
                audience=settings.oidc_audience or None,
                issuers_spec=settings.oidc_issuers,
                email_claim=settings.oidc_email_claim,
                allowed_domains=settings.oidc_allowed_domains.split(",")))
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
        tenant = request.headers.get("X-Tenant", settings.default_tenant_code)
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, f"Invalid bearer token: {exc}")
        if not email:
            return _problem(403, "Authentication required (bearer token or X-User-Email).")
        try:
            user = await request.app.state.resolver.resolve(tenant, email)
        except UserDeniedError:
            return _problem(403, f"Unknown or inactive user '{email}'.")
        except AccessUnavailableError as exc:
            return _problem(502, f"Access service unavailable: {exc}")
        fwd_headers = _forward_headers(request, user, decision=None,
                                       method="GET", path="/v1/assignments",
                                       api_key=settings.register_api_key)
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

    async def _trusted_email(request: Request) -> str | None:
        """The caller's e-mail. When OIDC is configured (or ``require_auth`` is on) it
        comes ONLY from the VERIFIED bearer token — X-User-Email is never trusted, so a
        client cannot assert an identity. Header-trust applies ONLY in pure dev mode
        (no OIDC, no require_auth: a trusted mesh)."""
        verifier: TokenVerifier | None = request.app.state.oidc
        if verifier is None:
            if settings.require_auth:
                return None  # no token source configured but anonymous is refused
            return request.headers.get("X-User-Email")
        token = bearer_token(request.headers.get("Authorization"))
        if not token:
            return None
        ident = await verifier.verify(token)
        return ident.email

    def _forward_headers(request: Request, user, decision: str | None,  # noqa: ANN001
                         *, method: str | None = None, path: str | None = None,
                         operation: str | None = None,
                         api_key: str | None = None,
                         admin_key: str | None = None) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _SKIP_REQUEST_HEADERS}
        # Inject the SCOPED service credential for the chosen upstream. The client's own
        # key was stripped above (_SKIP_REQUEST_HEADERS); each backend accepts only its own
        # key, so a leaked edge token can never be replayed against the data plane.
        if api_key:
            headers["X-API-Key"] = api_key
        # Inject the tenant-admin credential ONLY for a verified Admin on an admin route —
        # the browser never holds it (the client's own X-Admin-Key was stripped above).
        if admin_key:
            headers["X-Admin-Key"] = admin_key
        if user is not None:
            # Production channel: a SIGNED internal context carrying identity + the LIVE
            # effective matrix, BOUND to the downstream method + path so it cannot be
            # replayed against another route. The Register verifies the signature and
            # enforces from it — no static-secret forgery, no stale-matrix divergence.
            if settings.internal_signing_secret:
                headers["X-Internal-Context"] = mint_internal_context(
                    signing_key=settings.internal_signing_secret,
                    algorithm=settings.internal_signing_algorithm,
                    ttl_seconds=settings.internal_token_ttl_seconds,
                    tenant=request.headers.get("X-Tenant", settings.default_tenant_code),
                    email=user.email, user_id=str(user.id), roles=list(user.roles),
                    report_ids=[str(r["id"]) for r in user.reports],
                    report_emails=[r["email"] for r in user.reports],
                    effective_views=user.views, effective_operations=user.operations,
                    matrix_version=user.version, decision=decision,
                    method=method or request.method,
                    path=path or request.url.path, operation=operation)
            # Legacy header propagation (kept for dev / mixed rollout). When the signed
            # context is on, these are advisory only — the Register prefers the token.
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
        tenant = request.headers.get("X-Tenant", settings.default_tenant_code)
        verifier: TokenVerifier | None = request.app.state.oidc
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, f"Invalid bearer token: {exc}")
        # OIDC on (or require_auth) → no verified identity means 401, NOT a silent
        # machine-caller passthrough that would let an anonymous request reach the data.
        # Exception: the explicit exempt list (OAuth redirects arrive bearer-less).
        exempt = full_path in {p.strip() for p in settings.auth_exempt_paths.split(",")
                               if p.strip()}
        if (verifier is not None or settings.require_auth) and not email and not exempt:
            return _problem(401, "Authentication required (Bearer token).")

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

        # Route by prefix to the fronted service, strip the prefix, and inject that
        # upstream's scoped credential. The signed internal context is bound to the
        # DOWNSTREAM (stripped) method + path so it can't be replayed on another route.
        base_url, api_key, downstream_path = _route(settings, full_path)
        upstream = f"{base_url}{downstream_path}"
        # Inject the tenant-admin credential only for a verified Admin on an admin route,
        # so the browser never possesses it (the internal trust boundary stays internal).
        admin_key = None
        if (settings.register_admin_api_key
                and _is_tenant_admin_route(method, downstream_path)
                and user is not None and "Admin" in set(user.roles)):
            admin_key = settings.register_admin_api_key
        body = await request.body()
        try:
            resp = await request.app.state.client.request(
                method, upstream, content=body,
                params=request.query_params,
                headers=_forward_headers(request, user, decision,
                                         method=method, path=downstream_path,
                                         api_key=api_key, admin_key=admin_key),
            )
        except httpx.HTTPError as exc:
            return _problem(502, f"Upstream unreachable: {exc}")
        out_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() not in _SKIP_RESPONSE_HEADERS}
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=out_headers)

    return app


app = create_app()
