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
        app.state.register_clients = {}
        app.state.http = httpx.AsyncClient(timeout=10.0)
        app.state.gate = ViewGate(app.state.http, settings.access_url,
                                  settings.access_api_key, settings.permission_cache_ttl_s)
        log.info("atlas_started", extra={"register": settings.register_base_url,
                                         "view_gating": app.state.gate.enabled})
        yield
        for client in app.state.register_clients.values():
            await client.aclose()
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
        headers: dict[str, str] = {"X-User-Email": email}
        if gate.enabled:
            try:
                resolved = await gate.resolve(tenant, email)
                headers["X-User-Id"] = str(resolved.get("id", ""))
                headers["X-User-Roles"] = ",".join(resolved.get("roles", []))
                if resolved.get("reports"):
                    headers["X-User-Report-Ids"] = ",".join(
                        str(r["id"]) for r in resolved["reports"])
                    headers["X-User-Reports"] = ",".join(
                        r["email"] for r in resolved["reports"])
            except Exception as exc:  # noqa: BLE001 - identity is best-effort for reads
                log.warning("atlas_identity_resolve_failed", extra={"error": str(exc)})
        if settings.gateway_shared_secret:
            headers["X-Gateway-Auth"] = settings.gateway_shared_secret
        return headers

    async def _client(request: Request, tenant: str,
                      email: str | None) -> AsyncRegisterClient:
        """A Register client that carries the CALLER's identity, so the Register scopes
        every read to that user (a machine 'atlas' actor would see the whole tenant).
        Cached per (tenant, identity) — identity is bounded (the tenant's users)."""
        headers = await _identity_headers(request, tenant, email)
        key = (tenant, email or "")
        clients: dict = request.app.state.register_clients
        if key not in clients:
            from evam_register_client.config import RegisterClientConfig

            cfg = RegisterClientConfig(
                base_url=settings.register_base_url, api_key=settings.register_api_key,
                tenant=tenant, actor="atlas", extra_headers=headers)
            clients[key] = AsyncRegisterClient(config=cfg)
        return clients[key]

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

    @app.get("/v1/dashboard", tags=["Dashboard"],
             summary="The whole book summarised: every vertical, amounts, open intel")
    async def dashboard(request: Request, tenant: str = Depends(tenant_of),
                        x_user_email: str | None = Header(default=None,
                                                          alias="X-User-Email")) -> Any:
        if (denied := await _gate(request, tenant, x_user_email, "dashboard")) is not None:
            return denied
        client = await _client(request, tenant, x_user_email)
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
             summary="What needs a human today: due actions, lender chases, covenants")
    async def today_view(request: Request, tenant: str = Depends(tenant_of),
                         horizon_days: int = Query(default=7, ge=0, le=90),
                         x_user_email: str | None = Header(default=None,
                                                           alias="X-User-Email")) -> Any:
        if (denied := await _gate(request, tenant, x_user_email, "today")) is not None:
            return denied
        client = await _client(request, tenant, x_user_email)
        today = datetime.now(UTC).date()
        leads = await _rows(client, "leads", status="Active")
        lenders = await _rows(client, "syndication-lenders")
        monitoring = await _rows(client, "monitoring")
        return {
            "tenant": tenant,
            "date": today.isoformat(),
            "leads_due": agg.leads_due_today(leads, today),
            "lender_chases": agg.lender_chases(lenders),
            "monitoring_due": agg.monitoring_due(monitoring, today, horizon_days),
        }

    @app.get("/v1/pipeline/{vertical}", tags=["Dashboard"],
             summary="Slim rows for one vertical's board (leads / deals / lending / "
                     "syndication / asset-monetisation)")
    async def pipeline(vertical: str, request: Request, tenant: str = Depends(tenant_of),
                       x_user_email: str | None = Header(default=None,
                                                         alias="X-User-Email")) -> Any:
        if vertical not in VERTICALS:
            return _problem(404, "Not found",
                            f"Unknown vertical '{vertical}'. One of: {', '.join(VERTICALS)}.")
        resource, view = VERTICALS[vertical]
        if (denied := await _gate(request, tenant, x_user_email, view)) is not None:
            return denied
        client = await _client(request, tenant, x_user_email)
        rows = await _rows(client, resource)
        return {"tenant": tenant, "vertical": vertical, "total": len(rows), "rows": rows}

    @app.get("/v1/entities/{entity_id}/summary", tags=["Dashboard"],
             summary="One company composed — the Register's 360° dossier, passed through")
    async def entity_summary(entity_id: uuid.UUID, request: Request,
                             tenant: str = Depends(tenant_of),
                             x_user_email: str | None = Header(default=None,
                                                               alias="X-User-Email")) -> Any:
        if (denied := await _gate(request, tenant, x_user_email, "clients")) is not None:
            return denied
        client = await _client(request, tenant, x_user_email)
        return await client.dossier(str(entity_id))

    return app


app = create_app()
