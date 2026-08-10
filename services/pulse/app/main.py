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

import asyncio
import contextlib
import hashlib
import hmac
import re
import time
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
from app.news import schedules as sched
from app.news.mailer import digest_html, send_email
from app.news.search import NewsSearch
from app.news.triage import POLICY_THEMES
from app.providers import NewsItem, build_providers

log = get_logger("pulse")


class SweepIn(BaseModel):
    """A whole sweep in one request: every term the desk wants scanned.

    The browser used to send these one at a time — one HTTP round trip per firm per
    watch term, strictly sequential, because a `for` loop that awaits cannot overlap.
    Four hundred firms is then four hundred serial crossings of the gateway.
    """

    model_config = ConfigDict(extra="forbid")

    terms: list[str] = Field(min_length=1, max_length=1200)
    date_from: str = Field(default="", max_length=10)
    date_to: str = Field(default="", max_length=10)
    limit: int = Field(default=40, ge=1, le=100)
    # The terms belonging to companies we already have money out to. Polarity depends on
    # the relationship and only the CALLER holds the book, so it says which is which —
    # "raises fresh debt" is a win for a prospect and a review flag for a borrower.
    live_terms: list[str] = Field(default_factory=list, max_length=1200)


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
        # One search engine per process: it owns the shared cache, the coalescing map and
        # the bounded upstream pool, so those are shared across requests rather than
        # rebuilt per call.
        app.state.search = NewsSearch(
            disable_gdelt=settings.disable_gdelt, timeout_s=settings.search_timeout_s,
            cache_ttl_s=settings.search_cache_ttl_s, cache_max=settings.search_cache_max,
            upstream_concurrency=settings.upstream_concurrency)
        app.state.schedules = sched.ScheduleStore(settings.schedule_file)
        app.state.scheduler_task = None
        if settings.scheduler_enabled:
            app.state.scheduler_task = asyncio.create_task(
                sched.scheduler_loop(app.state.schedules, _run_one_schedule,
                                     tick_seconds=settings.scheduler_tick_s))
        log.info("pulse_started", extra={
            "register": settings.register_base_url,
            "sources": [s.get("name") for s in settings.source_list()],
            "email": settings.smtp().ready, "gdelt": not settings.disable_gdelt,
            "schedules": len(app.state.schedules.all())})
        yield
        task = app.state.scheduler_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for client in app.state.register_clients.values():
            await client.aclose()
        # The engine's own upstream pool holds keep-alive sockets to the news sources.
        await app.state.search.aclose()

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

    # ================= News Radar: search, email, schedules =================
    # The desk-facing half of PULSE. /v1/scan above files intel into the Register on a
    # watchlist; these routes answer "what does the web say about THIS name, right now",
    # email it, and do it on a cadence. Ported from the desk's atlas_serve.py.

    def _recipients(value: Any) -> list[str]:
        """Recipients arrive as a list OR as the one-line string the dialog collects."""
        if isinstance(value, str):
            return [r.strip() for r in re.split(r"[,\n;]", value) if r.strip()]
        return [str(r).strip() for r in (value or []) if str(r).strip()]

    async def _run_one_schedule(schedule: dict) -> None:
        """The scheduler's callback — searches, then emails. Bound here so it closes over
        this app's engine and settings rather than reaching for a global."""
        await sched.run_schedule(
            schedule,
            search=lambda term, dfrom, dto: app.state.search.search(term, dfrom, dto),
            send=lambda to, subject, body: send_email(settings.smtp(), to, subject, body),
            digest=digest_html)

    @app.get("/v1/news/search", tags=["News Radar"],
             summary="Search the news for a term (Google News + GDELT + Bing, merged)")
    async def news_search(request: Request, q: str = Query(default="", max_length=200),
                          date_from: str = Query(default="", alias="from"),
                          date_to: str = Query(default="", alias="to"),
                          limit: int = Query(default=40, ge=1, le=100),
                          exposure: str = Query(
                              default="",
                              description="'live' when we already have money out to this "
                                          "name — flips good-looking news to a review flag"),
                          x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        term = (q or "").strip()
        if not term:
            return {"articles": []}
        # Polarity depends on our relationship with the firm, and the CALLER is what knows
        # it — the sweep holds the book. Anything other than "live" is a name we are
        # chasing, for which fresh funding really is good news.
        live = (exposure or "").strip().lower() in {"live", "borrower", "true", "1"}
        t0 = time.time()
        try:
            articles, sources = await request.app.state.search.search_detail(
                term, date_from, date_to, limit, live)
        except Exception as exc:  # noqa: BLE001 - a search must fail as a message
            log.exception("pulse_search_failed", extra={"term": term})
            return ORJSONResponse(status_code=502, content={
                "articles": [], "error": f"search failed: {exc}"})
        log.info("pulse_search", extra={"term": term, "count": len(articles),
                                        "seconds": round(time.time() - t0, 1)})
        # `sources` is the difference between "this firm is not in the news" and "this
        # container could not reach the news". Zero articles is a legitimate answer to
        # the first and a fault to be fixed in the second, and only the server knows
        # which happened — so it says, rather than leaving the screen to guess.
        down = [s for s in sources if not s["ok"]]
        body: dict[str, Any] = {"articles": articles, "count": len(articles),
                                "sources": sources}
        if down and not articles:
            body["error"] = ("no news source could be reached — "
                             + "; ".join(f"{s['name']}: {s['error']}" for s in down))
        return body

    @app.post("/v1/news/sweep", tags=["News Radar"],
              summary="Search MANY terms in one request (the all-firms sweep)")
    async def news_sweep(request: Request, payload: SweepIn,
                         x_api_key: str | None = Header(default=None,
                                                        alias="X-API-Key")) -> Any:
        """The sweep, done where the upstreams are.

        Sending one request per firm made the browser the bottleneck: every term waited
        for the one before it, and each paid its own TLS handshakes to the same three
        hosts. Here the terms overlap, share one connection pool, and share the cache
        and coalescing — two firms watching the same promoter cost one fetch.

        One row per term, in the order given, each with its own sources: a term that
        found nothing has to be distinguishable from a term whose sources were down,
        per term, or a sweep of four hundred reports "0 items" and says nothing about
        which half of that is a fault.
        """
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        t0 = time.time()
        try:
            rows = await request.app.state.search.sweep(
                payload.terms, dfrom=payload.date_from, dto=payload.date_to,
                limit=payload.limit, live_terms=set(payload.live_terms))
        except Exception as exc:  # noqa: BLE001 - a sweep must fail as a message
            log.exception("pulse_sweep_failed", extra={"terms": len(payload.terms)})
            return ORJSONResponse(status_code=502, content={
                "results": [], "error": f"sweep failed: {exc}"})
        failed = [r for r in rows if r.get("error")]
        body: dict[str, Any] = {
            "results": rows, "terms": len(rows),
            "articles": sum(r["count"] for r in rows),
            "failed_terms": len(failed),
            "seconds": round(time.time() - t0, 1)}
        # EVERY term failing is one fault, not four hundred: say it once, in the words
        # the first term's sources used, so the screen shows a cause and not a tally.
        if failed and len(failed) == len(rows):
            body["error"] = str(failed[0].get("error") or "every term failed")
        return body

    @app.get("/v1/news/diagnostics", tags=["News Radar"],
             summary="Can this container reach the news? (per-source probe)")
    async def news_diagnostics(request: Request,
                               x_api_key: str | None = Header(default=None,
                                                              alias="X-API-Key")) -> Any:
        """Answers the question a search cannot.

        "No articles" reads the same whether the desk picked a quiet company or this
        container has no outbound HTTPS — and on a locked-down host it is always the
        second. That is a firewall rule somebody can add in five minutes once they
        know, and a fortnight of blaming the radar until they do.

        Probes each source with a fixed, always-newsworthy term, bypassing the cache,
        and times them separately.
        """
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        out = await request.app.state.search.probe()
        return ORJSONResponse(status_code=200 if out["ok"] else 503, content=out)

    @app.get("/v1/news/config", tags=["News Radar"],
             summary="What the radar can do here (is email configured, is GDELT on)")
    async def news_config(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        smtp = settings.smtp()
        # The sender is shown so a desk can see WHICH address a digest will come from
        # before sending one; the password is never echoed.
        return {"email": smtp.ready, "from": smtp.sender if smtp.ready else "",
                "gdelt": not settings.disable_gdelt, "scheduler": settings.scheduler_enabled,
                # The screen composes a policy sweep from these; publishing them keeps one
                # list of themes rather than a copy per client that drifts.
                "policy_themes": POLICY_THEMES}

    @app.post("/v1/news/email", tags=["News Radar"],
              summary="Search a term and email the digest")
    async def news_email(payload: dict, request: Request,
                         x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        term = (payload.get("q") or "").strip()
        recipients = _recipients(payload.get("recipients"))
        dfrom, dto = payload.get("from", ""), payload.get("to", "")
        articles = await request.app.state.search.search(term, dfrom, dto) if term else []
        subject = payload.get("subject") or f"PRISM news — {term}"
        ok, msg = await asyncio.to_thread(send_email, settings.smtp(), recipients, subject,
                                          digest_html(term, articles, dfrom, dto))
        return ORJSONResponse(status_code=200 if ok else 400,
                              content={"ok": ok, "message": msg, "count": len(articles)})

    @app.post("/v1/news/email-digest", tags=["News Radar"],
              summary="Email a digest the caller already assembled (an all-firms sweep)")
    async def news_email_digest(payload: dict,
                                x_api_key: str | None = Header(default=None,
                                                               alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        recipients = _recipients(payload.get("recipients"))
        adverse_only = bool(payload.get("adverse_only"))
        parts, total = [], 0
        for group in payload.get("groups") or []:
            arts = group.get("articles") or []
            if adverse_only:
                arts = [a for a in arts if a.get("severity") in ("UGLY", "BAD")]
            if arts:                      # a firm with nothing to report is left out
                parts.append(digest_html(group.get("term", ""), arts))
                total += len(arts)
        head = ('<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px;'
                f'margin:8px auto;color:#5F6E76;font-size:13px">{len(parts)} firm(s) '
                f'&middot; {total} articles</div>')
        subject = payload.get("subject") or f"PRISM news digest — {len(parts)} firms"
        body = head + "<br>".join(parts) if parts else "<p>No news to send.</p>"
        ok, msg = await asyncio.to_thread(send_email, settings.smtp(), recipients,
                                          subject, body)
        return ORJSONResponse(status_code=200 if ok else 400,
                              content={"ok": ok, "message": msg, "firms": len(parts),
                                       "count": total})

    @app.post("/v1/news/email-test", tags=["News Radar"],
              summary="Send a test email, to prove the SMTP setup before relying on it")
    async def news_email_test(payload: dict,
                              x_api_key: str | None = Header(default=None,
                                                             alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        smtp = settings.smtp()
        body = ('<div style="font-family:Segoe UI,Arial,sans-serif">'
                '<h2 style="color:#1B2A4A">PRISM email is working</h2>'
                '<p>This is a test from the PRISM News Radar. Digests and scheduled '
                f'reports will be sent from <b>{smtp.sender or "(unset)"}</b>.</p></div>')
        ok, msg = await asyncio.to_thread(send_email, smtp, _recipients(payload.get("recipients")),
                                          "PRISM — test email", body)
        return ORJSONResponse(status_code=200 if ok else 400,
                              content={"ok": ok, "message": msg, "from": smtp.sender})

    @app.get("/v1/news/schedules", tags=["News Radar"],
             summary="The recurring digests on file for this tenant")
    async def list_schedules(request: Request, tenant: str = Depends(tenant_of),
                             x_api_key: str | None = Header(default=None,
                                                            alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        return {"schedules": request.app.state.schedules.all(tenant),
                "smtp": settings.smtp().ready}

    @app.post("/v1/news/schedules", tags=["News Radar"],
              summary="Create a recurring digest (daily or weekly)")
    async def create_schedule(payload: dict, request: Request,
                              tenant: str = Depends(tenant_of),
                              x_api_key: str | None = Header(default=None,
                                                             alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        recipients = _recipients(payload.get("recipients"))
        term = (payload.get("q") or "").strip()
        if not term or not recipients:
            return ORJSONResponse(status_code=400, content={
                "ok": False,
                "message": "Need a search term and at least one recipient."})
        cadence = "weekly" if payload.get("cadence") == "weekly" else "daily"
        hour = max(0, min(23, int(payload.get("hour", 8) or 0)))
        weekday = max(0, min(6, int(payload.get("weekday", 0) or 0)))
        schedule = {
            "id": "S" + str(int(time.time() * 1000)), "tenant": tenant, "q": term,
            "recipients": recipients, "cadence": cadence, "weekday": weekday, "hour": hour,
            "window_days": max(1, min(90, int(payload.get("window_days", 7) or 7))),
            "subject": (payload.get("subject") or "").strip(),
            "adverse_only": bool(payload.get("adverse_only")),
            "scope": (payload.get("scope") or "").strip(), "active": True, "last_run": 0,
            "next_run": sched.next_run(cadence, hour, weekday)}
        request.app.state.schedules.add(schedule)
        log.info("pulse_schedule_created", extra={"id": schedule["id"], "tenant": tenant,
                                                  "cadence": cadence, "hour": hour,
                                                  "recipients": len(recipients)})
        return {"ok": True, "schedule": schedule}

    @app.post("/v1/news/schedules/delete", tags=["News Radar"],
              summary="Delete a recurring digest")
    async def delete_schedule(payload: dict, request: Request,
                              tenant: str = Depends(tenant_of),
                              x_api_key: str | None = Header(default=None,
                                                             alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        removed = request.app.state.schedules.remove(str(payload.get("id") or ""), tenant)
        return {"ok": removed}

    @app.post("/v1/news/schedules/run", tags=["News Radar"],
              summary="Run a schedule now (send its digest immediately)")
    async def run_schedule_now(payload: dict, request: Request,
                               tenant: str = Depends(tenant_of),
                               x_api_key: str | None = Header(default=None,
                                                              alias="X-API-Key")) -> Any:
        if (denied := _require_api_key(settings, x_api_key)) is not None:
            return denied
        schedule = request.app.state.schedules.get(str(payload.get("id") or ""), tenant)
        if schedule is None:
            return ORJSONResponse(status_code=404,
                                  content={"ok": False, "message": "No such schedule."})
        ok, msg = await sched.run_schedule(
            schedule,
            search=lambda term, dfrom, dto: request.app.state.search.search(term, dfrom, dto),
            send=lambda to, subject, body: send_email(settings.smtp(), to, subject, body),
            digest=digest_html)
        return ORJSONResponse(status_code=200 if ok else 400,
                              content={"ok": ok, "message": msg})

    return app



app = create_app()
