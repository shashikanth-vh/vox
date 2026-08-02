"""PRISM ATLAS — the live management dashboard service (read-side BFF).

ATLAS never owns data. It reads the Register through the platform SDK, composes the
numbers a management dashboard needs (pipeline by stage, amounts, today's actions,
open intel), and serves them as small JSON payloads a front-end can render directly.
Because it is a pure read-side composer it is stateless, horizontally scalable, and
individually deployable — and a bug in ATLAS can never corrupt the book.

RBAC: ATLAS gates each view (dashboard / today / pipeline …) through the Access
service using the caller's ``X-User-Email`` — the same view matrix admins edit live
(``app/permissions.py``). Data-level scoping stays where it belongs: in the Register.

Endpoints:
    GET /v1/dashboard              — the whole book, summarised per vertical
    GET /v1/today                  — what needs a human today (actions, chases, covenants)
    GET /v1/pipeline/{vertical}    — slim rows for one vertical's board view
    GET /v1/entities/{id}/summary  — one company, composed (dossier passthrough)
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_backend_core.oidc import OidcError, build_verifier
from evam_backend_core.oidc import bearer_token as _bearer
from evam_register_client import AsyncRegisterClient
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import ORJSONResponse

from app import aggregations as agg
from app.config import get_settings
from app.permissions import (
    AccessUnavailableError,
    UserUnknownError,
    ViewDeniedError,
    ViewGate,
)

log = get_logger("atlas")

# vertical → (Register resource, RBAC view name). The single map to extend when a new
# vertical arrives — the pipeline endpoint and the dashboard both read from it.
VERTICALS: dict[str, tuple[str, str]] = {
    "leads": ("leads", "leads"),
    "deals": ("deals", "deals"),
    "lending": ("lending", "lending"),
    "syndication": ("syndication", "syndication"),
    "asset-monetisation": ("asset-monetisation", "asset_monetisation"),
}


def _problem(status: int, title: str, detail: str) -> ORJSONResponse:
    return ORJSONResponse(status_code=status, content={"error": {
        "type": title.lower().replace(" ", "_"), "title": title, "detail": detail}})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.http = httpx.AsyncClient(timeout=10.0)
        app.state.gate = ViewGate(app.state.http, settings.access_url,
                                  settings.access_api_key, settings.permission_cache_ttl_s)
        app.state.oidc = build_verifier(
            app.state.http,
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience or None,
            issuers_spec=settings.oidc_issuers,
            email_claim=settings.oidc_email_claim,
            allowed_domains=settings.oidc_allowed_domains.split(","))
        log.info("atlas_started", extra={"register": settings.register_base_url,
                                         "view_gating": app.state.gate.enabled,
                                         "oidc": app.state.oidc is not None})
        yield
        await app.state.http.aclose()

    app = FastAPI(title="PRISM ATLAS", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    def tenant_of(x_tenant: str | None = Header(default=None, alias="X-Tenant")) -> str:
        return x_tenant or settings.register_tenant

    async def _identity_headers(request: Request, tenant: str,
                                email: str | None) -> dict[str, str]:
        """The CALLER's verified identity + roles, forwarded to the Register so its
        row-level scope applies to ATLAS reads. Without this a scoped user would get
        tenant-wide dashboards (the reviewer's ATLAS finding)."""
        if not email:
            return {}
        gate: ViewGate = request.app.state.gate
        resolved: dict = {}
        if gate.enabled:
            try:
                resolved = await gate.resolve(tenant, email)
            except Exception as exc:  # noqa: BLE001 - identity is best-effort for reads
                log.warning("atlas_identity_resolve_failed", extra={"error": str(exc)})

        # Production channel: a SIGNED internal context — identity + the live effective
        # grant, cryptographically bound. GET-bound so it can NEVER be replayed to a write
        # route. This replaces the plaintext X-User-* headers + shared secret entirely.
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            reports = resolved.get("reports", []) or []
            return {"X-Internal-Context": mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                tenant=tenant, email=email, user_id=str(resolved.get("id") or email),
                roles=list(resolved.get("roles", [])),
                report_ids=[str(r["id"]) for r in reports],
                report_emails=[r["email"] for r in reports],
                effective_views=resolved.get("views", {}),
                effective_operations=resolved.get("operations", {}),
                matrix_version=int(resolved.get("version", 0)),
                method="GET")}

        # Legacy channel (dev / no signing secret): plaintext headers + shared secret.
        headers: dict[str, str] = {"X-User-Email": email}
        if resolved:
            headers["X-User-Id"] = str(resolved.get("id", ""))
            headers["X-User-Roles"] = ",".join(resolved.get("roles", []))
            if resolved.get("reports"):
                headers["X-User-Report-Ids"] = ",".join(
                    str(r["id"]) for r in resolved["reports"])
                headers["X-User-Reports"] = ",".join(
                    r["email"] for r in resolved["reports"])
        if settings.gateway_shared_secret:
            headers["X-Gateway-Auth"] = settings.gateway_shared_secret
        return headers

    async def _client(request: Request, tenant: str,
                      email: str | None) -> AsyncRegisterClient:
        """A Register client carrying the caller's identity, built FRESH per request so a
        role/reporting change takes effect on the next call — a cached identity client
        would keep stale role headers (the reviewer's finding). The identity itself comes
        from the TTL-cached ViewGate.resolve, so this stays cheap. The caller closes it."""
        headers = await _identity_headers(request, tenant, email)
        from evam_register_client.config import RegisterClientConfig

        cfg = RegisterClientConfig(
            base_url=settings.register_base_url, api_key=settings.register_api_key,
            tenant=tenant, actor="atlas", extra_headers=headers)
        return AsyncRegisterClient(config=cfg)

    async def _gate(request: Request, tenant: str, email: str | None,
                    view: str) -> ORJSONResponse | None:
        """Apply view-level RBAC. Returns a ready error response, or None = allowed."""
        gate: ViewGate = request.app.state.gate
        if not gate.enabled:
            return None
        if not email:
            if settings.require_user:
                return _problem(403, "Forbidden", "X-User-Email is required.")
            return None
        try:
            await gate.check(tenant, email, view)
        except ViewDeniedError:
            return _problem(403, "Forbidden",
                            f"Your roles do not grant the '{view}' view.")
        except UserUnknownError:
            return _problem(403, "Forbidden", f"Unknown or inactive user '{email}'.")
        except AccessUnavailableError as exc:
            return _problem(502, "Upstream unavailable", f"Access service: {exc}")
        return None

    async def _rows(client: AsyncRegisterClient, resource: str, **filters: Any) -> list[dict]:
        """Read up to max_pages of a resource — bounded by design so one dashboard
        request can never turn into an unbounded crawl of a huge tenant."""
        rows: list[dict] = []
        cursor: str | None = None
        for _ in range(settings.max_pages_per_resource):
            page = await client.list(resource, limit=200, cursor=cursor, **filters)
            rows.extend(page.items)
            cursor = page.next_cursor
            if not cursor:
                break
        return rows

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        return {"status": "ready", "service": settings.app_name}

    @app.post("/atlas/cache/invalidate", include_in_schema=False)
    async def invalidate(request: Request) -> dict:
        request.app.state.gate.invalidate()
        return {"invalidated": True}

    async def _trusted_email(request: Request) -> str | None:
        """The caller's e-mail. With ATLAS_OIDC_ISSUER(S) set it comes from the VERIFIED
        bearer token (client cannot assert it); otherwise from X-User-Email (dev, or
        behind the authenticated gateway). Raises OidcError on a bad token."""
        verifier = request.app.state.oidc
        if verifier is None:
            return request.headers.get("X-User-Email")
        token = _bearer(request.headers.get("Authorization"))
        if not token:
            return None
        return (await verifier.verify(token)).email

    @app.get("/v1/dashboard", tags=["Dashboard"],
             summary="The whole book summarised: every vertical, amounts, open intel")
    async def dashboard(request: Request, tenant: str = Depends(tenant_of)) -> Any:
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, "Unauthorized", f"Invalid token: {exc}")
        if (denied := await _gate(request, tenant, email, "dashboard")) is not None:
            return denied
        async with await _client(request, tenant, email) as client:
            leads = await _rows(client, "leads")
            deals = await _rows(client, "deals")
            lending = await _rows(client, "lending")
            syndication = await _rows(client, "syndication")
            asset_mon = await _rows(client, "asset-monetisation")
            intel = await _rows(client, "external-intelligence")
        return {
            "tenant": tenant,
            "generated_at": datetime.now(UTC).isoformat(),
            "leads": agg.leads_summary(leads),
            "deals": agg.deals_summary(deals),
            "lending": agg.lending_summary(lending),
            "syndication": agg.syndication_summary(syndication),
            "asset_monetisation": agg.asset_mon_summary(asset_mon),
            "external_intelligence": agg.intel_summary(intel),
        }

    @app.get("/v1/today", tags=["Dashboard"],
             summary="What needs a human today: due actions, stuck stages, chases, covenants")
    async def today_view(request: Request, tenant: str = Depends(tenant_of),
                         horizon_days: int = Query(default=7, ge=0, le=90),
                         amber_days: int | None = Query(default=None, ge=1, le=365),
                         red_days: int | None = Query(default=None, ge=1, le=730)) -> Any:
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, "Unauthorized", f"Invalid token: {exc}")
        if (denied := await _gate(request, tenant, email, "today")) is not None:
            return denied
        today = datetime.now(UTC).date()
        async with await _client(request, tenant, email) as client:
            leads = await _rows(client, "leads", status="Active")
            lenders = await _rows(client, "syndication-lenders")
            monitoring = await _rows(client, "monitoring")
            lending = await _rows(client, "lending")
        # BN-02 "stage stuck": deployment thresholds, narrowable per request.
        bottlenecks = agg.stage_bottlenecks(
            lending, today,
            amber_days=amber_days or settings.stage_amber_days,
            red_days=red_days or settings.stage_red_days)
        return {
            "tenant": tenant,
            "date": today.isoformat(),
            "leads_due": agg.leads_due_today(leads, today),
            "lender_chases": agg.lender_chases(lenders),
            "monitoring_due": agg.monitoring_due(monitoring, today, horizon_days),
            "stage_bottlenecks": bottlenecks,
            "attention_counts": {"red": len(bottlenecks["red"]),
                                 "amber": len(bottlenecks["amber"])},
        }

    @app.get("/v1/pipeline/{vertical}", tags=["Dashboard"],
             summary="Slim rows for one vertical's board (leads / deals / lending / "
                     "syndication / asset-monetisation)")
    async def pipeline(vertical: str, request: Request,
                       tenant: str = Depends(tenant_of)) -> Any:
        if vertical not in VERTICALS:
            return _problem(404, "Not found",
                            f"Unknown vertical '{vertical}'. One of: {', '.join(VERTICALS)}.")
        resource, view = VERTICALS[vertical]
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, "Unauthorized", f"Invalid token: {exc}")
        if (denied := await _gate(request, tenant, email, view)) is not None:
            return denied
        async with await _client(request, tenant, email) as client:
            rows = await _rows(client, resource)
        return {"tenant": tenant, "vertical": vertical, "total": len(rows), "rows": rows}

    @app.get("/v1/entities/{entity_id}/summary", tags=["Dashboard"],
             summary="One company composed — the Register's 360° dossier, passed through")
    async def entity_summary(entity_id: uuid.UUID, request: Request,
                             tenant: str = Depends(tenant_of)) -> Any:
        try:
            email = await _trusted_email(request)
        except OidcError as exc:
            return _problem(401, "Unauthorized", f"Invalid token: {exc}")
        if (denied := await _gate(request, tenant, email, "clients")) is not None:
            return denied
        async with await _client(request, tenant, email) as client:
            return await client.dossier(str(entity_id))

    return app


app = create_app()
