"""mount.py — expose the VOX pipeline on the VocX FastAPI app.

The vendored ``VocxApp.handle`` is deliberately framework-agnostic:
(method, path, query, body) → (status, content-type, bytes). This adapter mounts it
under ``/v1/*`` (reachable through the edge as ``/vocx/v1/*``), runs it in
the threadpool (the pipeline is synchronous: STT, Anthropic call, Register HTTP), and
applies VocX's front-door key — the same one the gateway injects for every /vocx route.

Endpoints (all JSON unless noted):
    POST /v1/capture         transcript (or inline audio_b64) → preview: extraction,
                                  gate decision, approval card, write plan. Never writes.
    POST /v1/capture_audio   raw audio body (?rm=) → STT → same preview
    POST /v1/commit          approved/edited extraction → REAL writes (Register
                                  always; RM's Google Calendar when connected)
    GET  /v1/capabilities    what's available (STT backend, extraction, Google)
    GET  /v1/interactions    search over the register's interaction log
    GET  /v1/facets          facet counts under the same filters
    GET  /v1/entity?code=    one entity + its interactions
    GET  /v1/auth/status|start|callback   per-RM Google OAuth (callback is HTML)
    POST /v1/template_fill   Haiku fills a report template's fields from the transcript

The PoC's panel/PWA asset routes are NOT mounted — PRISM VocX is backend-only.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from app.vocx.core.search import InteractionSearch
from app.vocx.core.server import VocxApp
from app.vocx.loader import build_vox_config
from app.vocx.registry.store import RegisterStoreLoader
from app.vocx.registry.writer import make_writer_factory
from app.vocx.speech.audio_store import build_audio_store


class PrismVocxApp(VocxApp):
    """VocxApp over the live Register: TTL-refreshed store, cache busted on commit."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        config = build_vox_config(settings)
        self.loader = RegisterStoreLoader(
            settings.register_base_url, settings.register_api_key,
            settings.register_tenant, ttl_s=settings.register_cache_ttl_s)
        super().__init__(store=self.loader.store(), config=config,
                         writer_factory=make_writer_factory(settings, settings.tokens_dir))
        self._audio_store = build_audio_store(settings)
        from app.vocx.reports import build_report_store
        self._report_store = build_report_store(settings)

    def audio_store(self):  # noqa: ANN201 — VocxApp hook
        return self._audio_store

    def report_store(self):  # noqa: ANN201 — VocxApp hook
        return self._report_store

    def _load_store(self):  # noqa: ANN202 — VocxApp hook (used by refresh())
        return self.loader.store(force=True)

    _SEARCH_PATHS = ("/v1/interactions", "/v1/facets", "/v1/entity")

    def handle(self, method: str, path: str, query: dict[str, Any],
               body: bytes = b"") -> tuple[int, str, bytes]:
        # Two corpus views (see RegisterStoreLoader): captures resolve against the cheap
        # one; only the search endpoints pay for the interaction log.
        fresh = (self.loader.search_store() if path in self._SEARCH_PATHS
                 else self.loader.store())
        if fresh is not self.store:
            self.store = fresh
            self.search = InteractionSearch(fresh, self.config)
        result = super().handle(method, path, query, body)
        if path == "/v1/commit" and result[0] == 200:
            self.loader.invalidate()          # the next preview resolves against the new rows
        return result


def build_vocx_router(settings: Any) -> APIRouter:
    router = APIRouter(tags=["VOX"])
    state: dict[str, PrismVocxApp] = {}

    def _app() -> PrismVocxApp:
        if "app" not in state:                # lazy: no Register call at import time
            state["app"] = PrismVocxApp(settings)
        return state["app"]

    def _denied(request: Request) -> Response | None:
        keys = [k.strip() for k in settings.api_keys.split(",") if k.strip()]
        if not keys:
            return None
        provided = request.headers.get("X-API-Key", "")
        if any(hmac.compare_digest(provided, k) for k in keys):
            return None
        return Response(status_code=401, media_type="application/json",
                        content=b'{"error": {"type": "unauthorized", "title": "Unauthorized",'
                                b' "detail": "Missing or invalid X-API-Key."}}')

    # Explicit route table — each endpoint is a real OpenAPI operation (it shows up
    # individually in generated collections) and nothing can shadow the service's other
    # /v1 routes (touchpoints). The adapter contract stays (method, path, query, body).
    routes = [
        ("/v1/capture", ["POST"], "Preview a typed/inline-audio capture (never writes)"),
        ("/v1/capture_audio", ["POST"], "Raw audio → archive + STT → preview"),
        ("/v1/commit", ["POST"], "Execute an approved capture (idempotent by capture_id)"),
        ("/v1/capabilities", ["GET"], "What this deployment can do right now"),
        ("/v1/interactions", ["GET"], "Search the interaction log"),
        ("/v1/facets", ["GET"], "Facet counts under the same filters"),
        ("/v1/entity", ["GET"], "One entity + its interactions"),
        ("/v1/interaction_types", ["GET"], "Interaction-type vocabulary"),
        ("/v1/template_fill", ["POST"], "Haiku fills a template's fields from the transcript"),
        ("/v1/audio", ["GET"], "Playback for an archived recording (presigned URL / bytes)"),
        ("/v1/reports", ["GET"], "The RM's report list (drafts → ready → committed)"),
        ("/v1/reports/get", ["GET"], "One report document"),
        ("/v1/reports/save", ["POST"], "Save/update a pending report"),
        ("/v1/reports/delete", ["POST"], "Delete a report"),
        ("/v1/auth/status", ["GET"], "Is this RM's Google connected?"),
        ("/v1/auth/start", ["GET"], "Begin per-RM Google OAuth (browser)"),
        ("/v1/auth/callback", ["GET"], "OAuth redirect target (exempted at the gateway)"),
        ("/v1/calendar/test", ["GET"], "Prove which Google calendar VocX writes to"),
    ]

    def _make_handler(route_path: str):
        async def handler(request: Request) -> Response:
            if (denied := _denied(request)) is not None:
                return denied
            body = await request.body()
            query: dict[str, list[str]] = {}
            for k, v in request.query_params.multi_items():
                query.setdefault(k, []).append(v)
            status, ctype, payload = await run_in_threadpool(
                _app().handle, request.method, route_path, query, body)
            return Response(content=payload, status_code=status, media_type=ctype)
        return handler

    for path, methods, summary in routes:
        router.add_api_route(path, _make_handler(path), methods=methods,
                             summary=summary, tags=["VocX pipeline"])

    return router
