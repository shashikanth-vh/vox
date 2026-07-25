"""PRISM PULSE — the news / adverse-media radar.

What it does, end to end:

1. **Fetch**  — pull raw items from configured providers (RSS / JSON / built-in sample).
2. **Match**  — find which Register entities each item mentions (``app.matching``).
3. **Signal** — classify RED / AMBER / GREEN with auditable keyword rules.
4. **Write**  — file one ``external-intelligence`` row per (item, entity) in the
   Register through the platform SDK. The write is keyed with an Idempotency-Key
   derived from the item URL, so re-running a scan NEVER duplicates intel — the same
   exactly-once pattern VocX uses for touchpoints.
5. **Digest** — ``GET /v1/digest`` summarises the recent intel per entity/signal — the
   payload behind the 7 AM portfolio e-mail.

Stateless and multi-tenant: no database of its own (the Register is the source of
truth; idempotency doubles as dedup), and every endpoint accepts ``X-Tenant`` so one
deployment serves many tenants. Scheduling is external by design — point a Temporal
schedule, Kubernetes CronJob, or plain cron at ``POST /v1/scan``.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_register_client import AsyncRegisterClient
from evam_register_client.errors import RegisterError
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.matching import WatchEntity, classify_signal, match_entities
from app.providers import NewsItem, build_providers

log = get_logger("pulse")


class ItemIn(BaseModel):
    """One news item pushed by an external feeder (scraper, paid API webhook, human).

    Either name the entity explicitly (``entity_id``) or let PULSE match it against the
    tenant's watchlist like a scanned item.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=400)
    summary: str | None = None
    url: str | None = None
    source: str = Field(default="push", max_length=120)
    published_at: str | None = None
    entity_id: uuid.UUID | None = None
    signal: str | None = Field(default=None, max_length=10)  # override RED/AMBER/GREEN


def _tenant_client(app: FastAPI, tenant: str) -> AsyncRegisterClient:
    """One Register client per tenant, created lazily and reused (connection pooling).
    This is the whole multi-tenancy story for PULSE: the tenant only ever travels in
    the ``X-Tenant`` header down to the Register, which scopes every row."""
    clients: dict[str, AsyncRegisterClient] = app.state.register_clients
    if tenant not in clients:
        settings = get_settings()
        clients[tenant] = AsyncRegisterClient(
            base_url=settings.register_base_url,
            api_key=settings.register_api_key,
            tenant=tenant,
            actor="pulse",
        )
    return clients[tenant]


def _require_api_key(settings: Settings, provided: str | None) -> ORJSONResponse | None:
    """PULSE's own front door. Open when PULSE_API_KEYS is empty (dev); constant-time
    comparison when set (prod)."""
    keys = settings.api_key_list()
    if not keys:
        return None
    if provided and any(hmac.compare_digest(provided, k) for k in keys):
        return None
    return ORJSONResponse(status_code=401, content={"error": {
        "type": "unauthorized", "title": "Unauthorized",
        "detail": "Missing or invalid X-API-Key."}})


async def _load_watchlist(client: AsyncRegisterClient, max_entities: int) -> list[WatchEntity]:
    """The tenant's entities, slimmed to what the matcher needs."""
    watchlist: list[WatchEntity] = []
    async for row in client.iterate("entities", page_size=200):
        watchlist.append(WatchEntity(id=row["id"], code=row.get("code") or "",
                                     legal_name=row.get("legal_name") or "",
                                     display_name=row.get("display_name")))
        if len(watchlist) >= max_entities:
            log.warning("pulse_watchlist_capped", extra={"cap": max_entities})
            break
    return watchlist


def _idempotency_key(tenant: str, entity_id: str, item: NewsItem) -> str:
    """Stable identity of one (item, entity) pair. The URL is the natural identity of a
    news item; the title is the fallback for feeds without URLs. Hashing keeps the key
    short and header-safe."""
    identity = item.url or item.title
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"pulse:{tenant}:{entity_id}:{digest}"


async def _file_intel(client: AsyncRegisterClient, tenant: str, entity: WatchEntity,
                      item: NewsItem, signal: str) -> dict:
    """One intel row in the Register. Idempotent: replaying the same item is a no-op."""
    return await client.create(
        "external-intelligence",
        {
            "entity_id": entity.id,
            "intel_type": "News",
            "source": f"PULSE:{item.source}",
            "signal": signal,
            "title": item.title[:400],
            "summary": item.summary or None,
            "url": item.url or None,
            "observed_at": item.published_at,
            "payload": {"provider": item.source, "matched_on": entity.code or entity.legal_name},
        },
        idempotency_key=_idempotency_key(tenant, entity.id, item),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.register_clients = {}
        log.info("pulse_started", extra={"register": settings.register_base_url,
                                         "sources": [s.get("name") for s in settings.source_list()]})
        yield
        for client in app.state.register_clients.values():
            await client.aclose()

    app = FastAPI(title="PRISM PULSE", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    def tenant_of(x_tenant: str | None = Header(default=None, alias="X-Tenant")) -> str:
        return x_tenant or settings.register_tenant

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        return {"status": "ready", "service": settings.app_name}

    @app.post("/v1/scan", tags=["Radar"],
              summary="Run a scan now: fetch every source, match, file intel")
    async def scan(request: Request, tenant: str = Depends(tenant_of),
                   x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        client = _tenant_client(request.app, tenant)
        watchlist = await _load_watchlist(client, settings.watchlist_max_entities)
        red, green = settings.red_word_list(), settings.green_word_list()

        stats: list[dict] = []
        filed: list[dict] = []
        for provider in build_providers(settings.source_list(), settings.fetch_timeout_s):
            try:
                items = await provider.fetch()
            except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the scan
                log.warning("pulse_provider_failed",
                            extra={"provider": provider.name, "error": str(exc)})
                stats.append({"provider": provider.name, "error": str(exc)})
                continue
            matched_count = 0
            for item in items:
                for entity in match_entities(item, watchlist):
                    signal = classify_signal(item, red, green)
                    try:
                        intel = await _file_intel(client, tenant, entity, item, signal)
                    except RegisterError as exc:
                        log.warning("pulse_intel_write_failed",
                                    extra={"entity": entity.id, "error": str(exc)})
                        continue
                    matched_count += 1
                    filed.append({"intel_id": intel["id"], "entity_id": entity.id,
                                  "signal": signal, "title": item.title})
            stats.append({"provider": provider.name, "items": len(items),
                          "matched": matched_count})
        return {"tenant": tenant, "watchlist_size": len(watchlist),
                "providers": stats, "filed": filed}

    @app.post("/v1/items", status_code=201, tags=["Radar"],
              summary="Push one news item (the door for scrapers / webhooks)")
    async def push_item(payload: ItemIn, request: Request, tenant: str = Depends(tenant_of),
                        x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        client = _tenant_client(request.app, tenant)
        item = NewsItem(source=payload.source, title=payload.title,
                        summary=payload.summary or "", url=payload.url or "",
                        published_at=payload.published_at)
        red, green = settings.red_word_list(), settings.green_word_list()
        signal = payload.signal or classify_signal(item, red, green)

        if payload.entity_id is not None:
            entity = WatchEntity(id=str(payload.entity_id), code="", legal_name="")
            targets = [entity]
        else:
            watchlist = await _load_watchlist(client, settings.watchlist_max_entities)
            targets = match_entities(item, watchlist)
        filed = []
        for entity in targets:
            intel = await _file_intel(client, tenant, entity, item, signal)
            filed.append({"intel_id": intel["id"], "entity_id": entity.id, "signal": signal})
        return ORJSONResponse(status_code=201, content={
            "tenant": tenant, "matched": len(filed), "filed": filed})

    @app.get("/v1/digest", tags=["Radar"],
             summary="The portfolio digest: recent intel grouped by signal and entity")
    async def digest(request: Request, tenant: str = Depends(tenant_of),
                     hours: int = Query(default=24, ge=1, le=24 * 14),
                     x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        client = _tenant_client(request.app, tenant)
        since = datetime.now(UTC) - timedelta(hours=hours)
        by_signal: dict[str, list[dict]] = {"RED": [], "AMBER": [], "GREEN": []}
        page = await client.list("external-intelligence", limit=200, intel_type="News")
        for row in page.items:
            created = row.get("created_at")
            if created and datetime.fromisoformat(created) < since:
                continue
            if row.get("is_dismissed"):
                continue
            by_signal.setdefault(row.get("signal") or "AMBER", []).append({
                "intel_id": row["id"], "entity_id": row.get("entity_id"),
                "title": row.get("title"), "url": row.get("url"),
                "source": row.get("source"), "created_at": created,
                "acknowledged": bool(row.get("acknowledged_at")),
            })
        return {"tenant": tenant, "window_hours": hours,
                "counts": {k: len(v) for k, v in by_signal.items()},
                "items": by_signal}

    return app


app = create_app()
