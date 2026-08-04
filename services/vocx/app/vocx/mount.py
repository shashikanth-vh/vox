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


def _merged_reports(app: Any, keys: list[str]) -> Response:
    """One report list across a person's storage keys, newest first, de-duplicated."""
    import json as _json

    seen: set[str] = set()
    rows: list[dict] = []
    for key in keys:
        status, ctype, payload = app.handle("GET", "/v1/reports", {"rm": [key]}, b"")
        if status != 200:
            continue
        try:
            got = _json.loads(payload).get("reports") or []
        except (ValueError, UnicodeDecodeError):
            continue
        for row in got:
            cid = str(row.get("capture_id") or "")
            if cid and cid not in seen:
                seen.add(cid)
                rows.append(row)
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return Response(content=_json.dumps({"ok": True, "reports": rows}).encode("utf-8"),
                    status_code=200, media_type="application/json")


def _first_hit(app: Any, method: str, path: str, query: dict[str, Any], body: bytes,
               keys: list[str]) -> Response:
    """Open a document under whichever of a person's keys actually holds it.

    Without this a draft written by the older build would LIST (see _merged_reports) and
    then 404 on open — a row in your own list you cannot touch, which is worse than not
    showing it at all.
    """
    last: tuple[int, str, bytes] = (404, "application/json", b'{"ok": false}')
    for key in keys:
        last = app.handle(method, path, {**query, "rm": [key]}, body)
        if last[0] == 200:                              # a miss is a 404, never an empty 200
            break
    status, ctype, payload = last
    return Response(content=payload, status_code=status, media_type=ctype)


def _delete_everywhere(app: Any, query: dict[str, Any], body: bytes,
                       keys: list[str]) -> Response:
    """Delete under every key this person's captures may sit under — otherwise a draft
    deleted from the list reappears on the next refresh, filed under the older key."""
    last = None
    for key in keys:
        last = app.handle("POST", "/v1/reports/delete", {**query, "rm": [key]},
                          _rebind_body(body, key))
    status, ctype, payload = last
    return Response(content=payload, status_code=status, media_type=ctype)


def _rebind_body(body: bytes, rm: str) -> bytes:
    """Set a JSON body's `rm` to the verified caller's. Non-JSON bodies (the raw audio a
    capture posts) pass through untouched.

    SET, not replace-if-present: these routes act on your own captures, so a body that
    omits `rm` is not a request to act on nobody — it is a client that (rightly) stopped
    sending an identity the server decides.
    """
    import json as _json

    try:
        data = _json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    data["rm"] = rm
    return _json.dumps(data).encode("utf-8")


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

    # Routes whose subject is a PERSON's own captures. On these the RM is not a parameter
    # the caller chooses: it is the verified caller. `/v1/auth/callback` is absent on
    # purpose — Google calls it back with no ATLAS identity, carrying the rm in `state`.
    _OWNED = {
        "/v1/capture", "/v1/capture_audio", "/v1/commit",
        "/v1/reports", "/v1/reports/get", "/v1/reports/print",
        "/v1/reports/save", "/v1/reports/delete",
        "/v1/audio", "/v1/auth/status", "/v1/auth/start", "/v1/calendar/test",
        "/v1/suggest", "/v1/template_fill",
    }

    def _unauthenticated() -> Response:
        return Response(status_code=401, media_type="application/json",
                        content=b'{"error": {"type": "unauthorized", "title": "Unauthorized",'
                                b' "detail": "This route acts on your own captures and needs'
                                b' a verified user identity."}}')

    def _forbidden(detail: bytes) -> Response:
        return Response(status_code=403, media_type="application/json",
                        content=b'{"error": {"type": "forbidden", "title": "Forbidden",'
                                b' "detail": "' + detail + b'"}}')


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
        ("/v1/suggest", ["GET"], "Company typeahead: ranked matches or new-company signal"),
        ("/v1/interaction_types", ["GET"], "Interaction-type vocabulary"),
        ("/v1/template_fill", ["POST"], "Haiku fills a template's fields from the transcript"),
        ("/v1/audio", ["GET"], "Playback for an archived recording (presigned URL / bytes)"),
        ("/v1/reports", ["GET"], "The RM's report list (drafts → ready → committed)"),
        ("/v1/reports/get", ["GET"], "One report document"),
        ("/v1/reports/print", ["GET"], "Print-ready HTML of a report (browser print → PDF)"),
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

            if route_path in _OWNED:
                # BIND the RM to the verified caller. Previously this came from `?rm=`,
                # so any signed-in user could list, open, edit, delete and play back
                # anyone else's captures by changing one parameter. A draft belongs to
                # whoever recorded it; there is no supervisor view, by design.
                from app.vocx import identity

                email = (request.headers.get("X-User-Email") or "").strip()
                if not email:
                    return _unauthenticated()
                # Carried to the register writer so a lead or company created from this
                # capture lands in the RECORDING RM's own book, not the service's.
                identity.caller_email.set(email)
                who = await run_in_threadpool(identity.rm_for, email, settings)
                if not who:
                    return _unauthenticated()
                query["rm"] = [who]
                # Captures filed by an earlier build sit under a second key (see
                # identity.aliases_for). Every route that reads or removes one spans both,
                # or a person's own past work is either invisible or — worse — listed and
                # then unopenable.
                if route_path in ("/v1/reports", "/v1/reports/get", "/v1/reports/print",
                                  "/v1/reports/delete"):
                    keys = await run_in_threadpool(identity.aliases_for, email, settings)
                    if len(keys) > 1:
                        app = _app()
                        if route_path == "/v1/reports":
                            return await run_in_threadpool(_merged_reports, app, keys)
                        if route_path == "/v1/reports/delete":
                            return await run_in_threadpool(
                                _delete_everywhere, app, query, body, keys)
                        return await run_in_threadpool(
                            _first_hit, app, request.method, route_path, query, body, keys)
                # The audio ref is opaque but not unguessable, and its payload is the raw
                # meeting recording — so it is checked on its own terms, not by trusting
                # that the caller asked with the right rm.
                if route_path == "/v1/audio":
                    ref = (query.get("ref") or [""])[0]
                    if ref and not identity.owns_audio_ref(ref, who):
                        return _forbidden(b"That recording belongs to another user.")
                # Body-carried rm (capture / commit / reports.save) is rewritten too — a
                # forged one there would file a capture under someone else\'s name.
                if body:
                    body = _rebind_body(body, who)

            status, ctype, payload = await run_in_threadpool(
                _app().handle, request.method, route_path, query, body)
            return Response(content=payload, status_code=status, media_type=ctype)
        return handler

    for path, methods, summary in routes:
        router.add_api_route(path, _make_handler(path), methods=methods,
                             summary=summary, tags=["VocX pipeline"])

    # --- TEMPORARY dev test console (delete this block + dev_ui.html to remove) -----
    # A single self-contained page to exercise the pipeline from a browser:
    # record → preview → edit → commit → reports, through the edge at /vocx/v1/dev-ui.
    # Served ONLY when VOCX_DEV_UI=true; hidden from OpenAPI so it never lands in
    # generated Postman collections. The front-door key check applies like every route.
    if getattr(settings, "dev_ui", False):
        from pathlib import Path as _Path

        async def _dev_ui(request: Request) -> Response:
            if (denied := _denied(request)) is not None:
                return denied
            page = (_Path(__file__).parent / "dev_ui.html").read_bytes()
            return Response(content=page, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

        router.add_api_route("/v1/dev-ui", _dev_ui, methods=["GET"],
                             include_in_schema=False)

    return router
