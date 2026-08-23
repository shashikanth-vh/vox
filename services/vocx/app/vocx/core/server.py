"""
core.server — the VOX web panel backend (step 5).

Read endpoints over the register plus the panel page. The routing core
(`VoxApp.handle`) is framework-agnostic — (method, path, query, body) -> (status,
content_type, bytes) — so it drops into the existing atlas_serve.py, and a stdlib
http.server adapter is included for standalone dev use. Logging reuses the
service JSON-stdout logging configuration (no file handlers in containers),
per the "reuse, don't rebuild" note.

Endpoints:
  GET  /                         -> the panel (vox_panel.html)
  GET  /health                   -> {ok:true}
  GET  /v1/interactions     -> search (company,user,from,to,type,ref_type,q,limit,offset,sort)
  GET  /v1/facets           -> facet counts under the same filters
  GET  /v1/entity?code=CODE -> one entity + its interactions
  GET  /v1/interaction_types-> enum for the panel dropdown
  POST /v1/capture          -> run process_capture on {transcript, rm, capture_ts?}
                                    (plan-only preview; never auto-writes here)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.vocx.core import gate as vocx_gate
from app.vocx.core.atlas import AtlasStore
from app.vocx.core.resolve import load_config
from app.vocx.core.search import InteractionSearch

HERE = os.path.dirname(os.path.abspath(__file__))

# Input bounds: a 30-minute meeting is ~4-6k words; these caps are generous while keeping
# a malformed client (or an attack) from parking megabytes in the extraction path.
MAX_TRANSCRIPT_CHARS = 40_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _gps_meta(get) -> dict:
    """Capture-side facts (GPS fix, spoken-language hint) → _meta → the interaction's
    structured columns. `get` maps a key to a raw string/None; bad numbers are dropped
    (a capture must never fail because the phone sent a junk coordinate)."""
    out: dict = {}
    for key in ("gps_lat", "gps_lng"):
        raw = get(key)
        if raw not in (None, ""):
            try:
                v = float(raw)
                if abs(v) <= (90 if key == "gps_lat" else 180):
                    out[key] = v
            except (TypeError, ValueError):
                pass
    loc = get("location")
    if loc:
        out["location"] = str(loc)[:200]
    lang = get("language")
    if lang:
        out["language"] = str(lang)[:20]
    return out


def _stamp_capture_id(extraction, supplied) -> None:
    if not isinstance(extraction, dict):
        return
    import uuid as _uuid
    meta = extraction.setdefault("_meta", {})
    meta["capture_id"] = str(supplied or meta.get("capture_id") or _uuid.uuid4())


def _logger(config: dict[str, Any]) -> logging.Logger:
    # Twelve-factor: the service configures JSON logging to stdout at startup and this
    # logger inherits it. The PoC's rotating vox.log file is gone — containers must not
    # log to local files (it also leaked a stray vox.log into the working directory).
    return logging.getLogger("vocx")


class VocxApp:
    def __init__(self, store: AtlasStore | None = None, config: dict[str, Any] | None = None,
                 transcriber: Any = None, writer_factory: Any = None):
        self.config = config or load_config()
        self.store = store or self._load_store()
        self.search = InteractionSearch(self.store, self.config)
        self.log = _logger(self.config)
        self._transcriber = transcriber          # injected in tests; else built lazily
        self.writer_factory = writer_factory      # (rm, store, config) -> writer; None -> MockWriter
        # The VOX pipeline (spec build): a runner per process, in-flight ids tracked so a
        # double-tap spawns one worker, not two. Built lazily; injectable for tests.
        self._vox_runner = None
        self._vox_inflight: set[str] = set()
        self._vox_lock = __import__("threading").Lock()

    def transcriber(self):
        if self._transcriber is None:
            from app.vocx.speech import stt as vocx_stt
            self._transcriber = vocx_stt.build_transcriber(self.config)
        return self._transcriber

    def stt_prompt(self) -> str | None:
        """Prime Whisper with OUR vocabulary: finance/climate terms from config plus the
        live client & lead names from the Register corpus. Whisper reads only the LAST
        ~224 tokens of the prompt, so the names — the highest-value words — go last."""
        if not (self.config.get("intelligence", {}) or {}).get("stt_priming", True):
            return None
        stt_cfg = self.config.get("stt", {}) or {}
        terms = [t for t in (stt_cfg.get("vocabulary") or []) if t]
        names: list[str] = []
        try:
            for c in (getattr(self.store, "clients", {}) or {}).values():
                n = (c.get("name") or c.get("display_name") or c.get("legal_name") or "").strip()
                if n:
                    names.append(n)
            for ld in (getattr(self.store, "leads", []) or []):
                n = (ld.get("company") or "").strip()
                if n:
                    names.append(n)
        except Exception:  # noqa: BLE001 — priming is best-effort, never fatal
            pass
        seen: set[str] = set()
        uniq = [n for n in names if not (n.lower() in seen or seen.add(n.lower()))]
        parts = terms + uniq[:60]
        return (", ".join(parts))[:1500] or None

    # ---- recorded-audio playback -------------------------------------------
    def _audio(self, query):
        """Playback for an archived recording: {"url": presigned} for MinIO refs, raw
        audio bytes for volume refs. Refs outside our bucket/archive are refused."""
        ref = _one(query, "ref")
        if not ref:
            return 400, "application/json", _j({"ok": False, "error": "ref required"})
        store = self.audio_store()
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "audio_store_off"})
        got = store.playback(ref)
        if got is None:
            return 404, "application/json", _j({"ok": False, "error": "not found"})
        kind, payload = got
        if kind == "url":
            return 200, "application/json", _j({"ok": True, "url": payload})
        # The clip's own magic bytes name the type. It was hardcoded "audio/wav" while
        # the browser records webm/opus — Chromium sniffed past the lie, Edge trusted it
        # and played silence. Sniffing (not the stored extension) also heals every clip
        # archived under the old `.wav` name.
        from app.vocx.speech.audio_store import sniff_audio_type
        return 200, sniff_audio_type(payload), payload

    # ---- server-side report list (drafts → ready → committed) ---------------
    def _reports_list(self, query):
        rm = _one(query, "rm")
        store = self.report_store()
        if not rm:
            return 400, "application/json", _j({"ok": False, "error": "rm required"})
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        return 200, "application/json", _j({"ok": True, "reports": store.list(rm)})

    def _reports_get(self, query):
        from app.vocx import reports as reports_mod
        rm, cid = _one(query, "rm"), _one(query, "id")
        store = self.report_store()
        if not rm or not reports_mod.valid_id(cid or ""):
            return 400, "application/json", _j({"ok": False, "error": "rm and valid id required"})
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        doc = store.get(rm, cid)
        if doc is None:
            return 404, "application/json", _j({"ok": False, "error": "not found"})
        return 200, "application/json", _j({"ok": True, "report": doc})

    def _reports_print(self, query):
        """The report as print-ready HTML — open in a browser tab, Ctrl+P → PDF.
        Same lookup rules as /v1/reports/get; renders whatever status the doc is in."""
        from app.vocx import reports as reports_mod
        rm, cid = _one(query, "rm"), _one(query, "id")
        store = self.report_store()
        if not rm or not reports_mod.valid_id(cid or ""):
            return 400, "application/json", _j({"ok": False, "error": "rm and valid id required"})
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        doc = store.get(rm, cid)
        if doc is None:
            return 404, "application/json", _j({"ok": False, "error": "not found"})
        html = reports_mod.render_print_html(doc, self.config)
        return 200, "text/html; charset=utf-8", html.encode("utf-8")

    def _reports_pdf(self, query):
        """The report as an actual PDF file — the Download button saves this straight
        to the device. Same lookup rules as /v1/reports/print."""
        from app.vocx import reports as reports_mod
        rm, cid = _one(query, "rm"), _one(query, "id")
        store = self.report_store()
        if not rm or not reports_mod.valid_id(cid or ""):
            return 400, "application/json", _j({"ok": False, "error": "rm and valid id required"})
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        doc = store.get(rm, cid)
        if doc is None:
            return 404, "application/json", _j({"ok": False, "error": "not found"})
        return 200, "application/pdf", reports_mod.render_pdf(doc, self.config)

    def _reports_save(self, body):
        from app.vocx import reports as reports_mod
        if len(body) > reports_mod.MAX_REPORT_BYTES:
            return 413, "application/json", _j({"ok": False, "error": "report too large"})
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON body"})
        rm, cid = data.get("rm"), data.get("capture_id")
        if not rm or not reports_mod.valid_id(cid or ""):
            return 400, "application/json", _j({"ok": False, "error": "rm and valid capture_id required"})
        store = self.report_store()
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        existing = store.get(rm, cid) or {}
        if existing.get("status") == "committed":
            return 409, "application/json", _j({"ok": False, "error": "already committed"})
        doc = reports_mod.make_doc(rm, cid, data.get("status") or "ready",
                                   data.get("report") or existing.get("report") or {})
        ok = store.save(rm, cid, doc)
        return ((200, "application/json", _j({"ok": True, "report": reports_mod._summary_of(doc)}))
                if ok else
                (502, "application/json", _j({"ok": False, "error": "save failed — retry"})))

    def _reports_delete(self, body):
        from app.vocx import reports as reports_mod
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON body"})
        rm, cid = data.get("rm"), data.get("capture_id")
        if not rm or not reports_mod.valid_id(cid or ""):
            return 400, "application/json", _j({"ok": False, "error": "rm and valid capture_id required"})
        store = self.report_store()
        if store is None:
            return 404, "application/json", _j({"ok": False, "error": "report_store_off"})
        ok = store.delete(rm, cid)
        return ((200, "application/json", _j({"ok": True}))
                if ok else (502, "application/json", _j({"ok": False, "error": "delete failed"})))

    def _autosave_draft(self, result: dict[str, Any], rm: str) -> None:
        """Every preview persists as a DRAFT — a dead phone loses nothing. Best-effort:
        a store hiccup must not fail the capture (the preview still returns)."""
        store = self.report_store()
        if store is None:
            return
        from app.vocx import reports as reports_mod
        ext = result.get("extraction") or {}
        cid = (ext.get("_meta") or {}).get("capture_id")
        if not reports_mod.valid_id(cid or ""):
            return
        existing = store.get(rm, cid) or {}
        if existing.get("status") in ("ready", "committed"):
            return                               # never downgrade an edited/committed report
        report = {"extraction": ext, "decision": result.get("decision"),
                  "approval_card": result.get("approval_card"),
                  "write_plan": result.get("write_plan"),
                  "summary": (result.get("extraction") or {}).get("report", {}).get("summary") or ""}
        store.save(rm, cid, reports_mod.make_doc(rm, cid, "draft", report))

    def _log_preview(self, result: dict[str, Any], rm: str) -> None:
        """One structured line per preview: everything needed to see WHY a capture
        resolved (or didn't) without replaying it. DEBUG adds the resolver's scoring."""
        ext = result.get("extraction") or {}
        em = ext.get("entity_match") or {}
        dec = result.get("decision") or {}
        self.log.info(
            "vocx capture=%s rm=%s company=%r -> entity=%s score=%s new_lead=%s "
            "needs_approval=%s ref=%s",
            (ext.get("_meta") or {}).get("capture_id"), rm, ext.get("company_mentioned"),
            em.get("code"), em.get("match_score"), em.get("is_new_lead"),
            dec.get("needs_approval"), (ext.get("_meta") or {}).get("transcript_ref"))
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("vocx capture=%s alternatives=%s gate=%s",
                           (ext.get("_meta") or {}).get("capture_id"),
                           em.get("alternatives"), dec.get("fields"))

    def audio_store(self):
        """Where recorded captures are archived (None = no archiving). PRISM overrides
        this with the MinIO/S3-backed store; the reference it returns becomes the
        capture's transcript_ref and rides into the committed interaction."""
        return None

    def report_store(self):
        """Server-side persistence for pending captures (None = stateless previews).
        PRISM overrides this with the MinIO/S3-backed store, making the RM's report
        list a backend fact instead of browser localStorage."""
        return None

    def _load_store(self) -> AtlasStore:
        path = self.config.get("register_store")
        if path and not os.path.isabs(path):
            path = os.path.join(HERE, path)
        return AtlasStore.from_file(path) if path and os.path.exists(path) else AtlasStore.default()

    def refresh(self) -> None:
        """Reload the register (call after external writes)."""
        self.store = self._load_store()
        self.search = InteractionSearch(self.store, self.config)

    # ---- routing core (framework-agnostic) ---------------------------------
    def handle(self, method: str, path: str, query: dict[str, Any],
               body: bytes = b"") -> tuple[int, str, bytes]:
        try:
            status, ctype, payload = self._route(method, path, query, body)
        except Exception as e:  # noqa: BLE001
            self.log.exception("VOX %s %s failed", method, path)
            return 500, "application/json", _j({"ok": False, "error": str(e)})
        self.log.info("VOX %s %s -> %s", method, path, status)
        return status, ctype, payload

    def _route(self, method, path, query, body):
        if method == "GET" and path in ("/", "/vox", "/index.html"):
            return 200, "text/html; charset=utf-8", _panel_html()
        if method == "GET" and path == "/health":
            return 200, "application/json", _j({"ok": True, "interactions": len(self.store.interactions)})
        if method == "GET" and path == "/v1/interaction_types":
            return 200, "application/json", _j({"types": self.store.interaction_types})
        if method == "GET" and path == "/v1/capabilities":
            return 200, "application/json", _j(self._capabilities())
        if method == "POST" and path == "/v1/vox/process":
            return self._vox_process(body)
        if method == "POST" and path == "/v1/vox/capture":
            return self._vox_capture(query, body)
        if method == "GET" and path == "/v1/spec":
            # The registry-driven contract (Phase 0): the review renderer draws its
            # blocks from THIS, so adding a field needs zero renderer changes.
            from ..spec import RegistryError, latest_prompt_version, load_registry
            v = _one(query, "version") or None
            try:
                reg = load_registry(v)
            except RegistryError as exc:
                return 404, "application/json", _j({"ok": False, "error": str(exc)})
            return 200, "application/json", _j({
                "registry": reg,
                "registry_version": reg["registry_version"],
                "prompt_version": latest_prompt_version(),
            })
        if method == "GET" and path == "/v1/interactions":
            return 200, "application/json", _j(self.search.search(**_search_args(query)))
        if method == "GET" and path == "/v1/facets":
            args = _search_args(query)
            args.pop("limit", None); args.pop("offset", None); args.pop("sort", None)
            return 200, "application/json", _j(self.search.facets(**args))
        if method == "GET" and path == "/v1/suggest":
            return self._suggest(query)
        if method == "GET" and path == "/v1/entity":
            code = _one(query, "code")
            if not code:
                return 400, "application/json", _j({"ok": False, "error": "code required"})
            return 200, "application/json", _j(self.search.entity(code))
        if method == "POST" and path == "/v1/capture":
            return self._capture(body)
        # --- mobile (step 6) ---
        if method == "GET" and path in ("/app", "/app/"):
            return 200, "text/html; charset=utf-8", _asset("index.html", b"<h1>VOX app missing</h1>")
        if method == "GET" and path == "/manifest.webmanifest":
            return 200, "application/manifest+json", _asset("manifest.webmanifest")
        if method == "GET" and path == "/sw.js":
            return 200, "text/javascript", _asset("sw.js")
        if method == "GET" and path == "/app/icon.svg":
            return 200, "image/svg+xml", _asset("icon.svg")
        if method == "GET" and path == "/v1/auth/status":
            return self._auth_status(query)
        if method == "GET" and path == "/v1/auth/start":
            return self._auth_start(query)
        if method == "GET" and path == "/v1/auth/callback":
            return self._auth_callback(query)
        if method == "POST" and path == "/v1/capture_audio":
            return self._capture_audio(query, body)
        if method == "GET" and path == "/v1/capture_status":
            return self._capture_status(query)
        if method == "POST" and path == "/v1/commit":
            return self._commit(body)
        if method == "GET" and path == "/v1/calendar/test":
            return self._calendar_test(query)
        if method == "GET" and path == "/v1/audio":
            return self._audio(query)
        if method == "GET" and path == "/v1/reports":
            return self._reports_list(query)
        if method == "GET" and path == "/v1/reports/get":
            return self._reports_get(query)
        if method == "GET" and path == "/v1/reports/print":
            return self._reports_print(query)
        if method == "GET" and path == "/v1/reports/pdf":
            return self._reports_pdf(query)
        if method == "POST" and path == "/v1/reports/save":
            return self._reports_save(body)
        if method == "POST" and path == "/v1/reports/delete":
            return self._reports_delete(body)
        if method == "POST" and path == "/v1/template_fill":
            return self._template_fill(body)
        return 404, "application/json", _j({"ok": False, "error": "not found", "path": path})

    def _suggest(self, query):
        """Company typeahead for the capture UI: rank the live corpus (entities + open
        leads) against what the user has typed so far. `new_company` says the best hit
        is not even a weak match — the UI offers "create <q> as a new company" instead
        of a wrong link. Same scorer as capture-time resolution, so what the typeahead
        suggests is exactly what a commit would resolve to."""
        from app.vocx.core.resolve import EntityResolver
        q = (_one(query, "q") or "").strip()
        if len(q) < 2:
            return 400, "application/json", _j({"ok": False, "error": "q (min 2 chars) required"})
        try:
            limit = max(1, min(int(_one(query, "limit") or 8), 20))
        except ValueError:
            limit = 8
        rm = (_one(query, "rm") or "").strip()
        resolver = EntityResolver(self.store, self.config)
        scored = []
        for cand in resolver._cands:                     # noqa: SLF001 — same-package core
            got = resolver._score_one([q], cand)         # noqa: SLF001
            if got["base"] <= 0.0:
                continue
            score = got["base"]
            if rm and cand.rm and cand.rm.strip().lower() == rm.lower():
                score = min(1.0, score + resolver.boost)
            scored.append({"code": cand.ref_id, "name": cand.name, "kind": cand.kind,
                           "ref_type": cand.ref_type, "rm": cand.rm,
                           # enough context to pick the right line without opening it:
                           # sector/lens for companies, temperature for leads
                           "sector": cand.sector or "",
                           "lens": cand.lens or "",
                           "temperature": (cand.raw or {}).get("temp") or "",
                           "score": round(score, 4), "match_type": got["how"]})
        scored.sort(key=lambda x: x["score"], reverse=True)
        matches = scored[:limit]
        new_company = not matches or matches[0]["score"] < resolver.match_min
        return 200, "application/json", _j({
            "ok": True, "q": q, "matches": matches, "new_company": new_company})

    # --------------------------------------------------------- the VOX pipeline

    def vox_runner(self):
        """The spec-build pipeline runner, wired to the real register, the local
        transcriber and Anthropic — built once, injectable for tests via
        ``self._vox_runner``."""
        if self._vox_runner is None:
            import os

            from ..pipeline import PipelineRunner
            from ..pipeline.register_client import RegisterClient

            register = RegisterClient(
                base_url=os.environ.get("VOCX_REGISTER_BASE_URL", "http://register:8000"),
                api_key=os.environ.get("VOCX_REGISTER_API_KEY", "dev-local-key"),
                tenant=os.environ.get("VOCX_REGISTER_TENANT", "EVAM"),
            )

            def transcribe(audio_ref: str) -> dict:
                # A local-archive ref IS a file path — hand it over as one (so test
                # fixtures' .txt sidecars work too). Anything else (S3 keys) goes
                # through the store's playback, which yields ("bytes"|"url", data).
                if os.path.isfile(audio_ref):
                    audio: Any = audio_ref
                else:
                    store = self.audio_store()
                    playback = store.playback(audio_ref) if store else None
                    if playback is None:
                        raise RuntimeError(f"audio {audio_ref!r} is not in the store")
                    audio = playback[1] if isinstance(playback, tuple) else playback
                result = self.transcriber().transcribe(audio, prompt=self.stt_prompt())
                if isinstance(result, str):
                    return {"text": result, "segments": [{"text": result}], "language": None}
                segments = result.get("segments") or [{"text": result.get("text", "")}]
                return {"text": result.get("text", ""), "segments": segments,
                        "language": result.get("language")}

            def ask_model(model: str, system: str, user: str, schema: dict | None = None) -> str:
                # Eval/dev fixture: a canned contract object instead of a live model.
                # Set ONLY in test harnesses — never in a deployed environment.
                stub = os.environ.get("VOCX_MODEL_STUB_FILE")
                if stub:
                    with open(stub, encoding="utf-8") as fh:
                        return fh.read()
                import anthropic  # lazy: offline paths never import the SDK
                client = anthropic.Anthropic(
                    api_key=os.environ[os.environ.get("VOCX_ANTHROPIC_KEY_ENV",
                                                      "ANTHROPIC_API_KEY")])
                kwargs: dict = dict(model=model, max_tokens=8000, system=system,
                                    messages=[{"role": "user", "content": user}])

                def _create(**extra):
                    try:
                        # deterministic extraction: temperature 0 removes sampling
                        # drift; an SDK line without the keyword still works
                        return client.messages.create(temperature=0.0, **kwargs, **extra)
                    except TypeError:
                        return client.messages.create(**kwargs, **extra)

                if schema is not None:
                    # The outer wall (same pattern as the legacy extraction path):
                    # a FORCED tool call — the API validates the model's answer
                    # against the contract schema before we ever see it. Any
                    # SDK/feature failure degrades to the text path below.
                    try:
                        msg = _create(
                            tools=[{"name": "file_report",
                                    "description": "File the structured conversation report.",
                                    "input_schema": schema}],
                            tool_choice={"type": "tool", "name": "file_report"})
                        for blk in msg.content:
                            if getattr(blk, "type", "") == "tool_use" and isinstance(
                                    getattr(blk, "input", None), dict):
                                return json.dumps(blk.input)
                    except Exception:  # noqa: BLE001 — degrade, never fail the take here
                        self.log.warning("VOX structured tool call unavailable; text fallback")
                msg = _create()
                return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

            def known_names() -> str | None:
                # Lender roster + the tenant's live company names (entities and
                # open leads), so structuring can repair STT-mangled spellings.
                from ..pipeline.glossary import build_known_names_block
                names = [c.name for c in self.store.candidates() if c.name]
                return build_known_names_block(names)

            self._vox_runner = PipelineRunner(register, transcribe, ask_model,
                                              alert=lambda m: self.log.error("ADMIN ALERT: %s", m),
                                              known_names=known_names)
        return self._vox_runner

    def _vox_capture(self, query, body: bytes):
        """The new-flow capture door, one POST from the panel: store the audio
        locally, create (or REPLAY, by capture_id) the register conversation row,
        and kick the pipeline. The client can vanish the moment this returns —
        everything after is server-side."""
        cap_id = _one(query, "capture_id") or ""
        mode = _one(query, "mode") or "post_meeting"
        if mode not in ("post_meeting", "live"):
            return 400, "application/json", _j({"ok": False, "error": "mode must be post_meeting|live"})
        if not body:
            return 400, "application/json", _j({"ok": False, "error": "no audio payload"})
        rm = _one(query, "rm") or "unknown"
        astore = self.audio_store()
        if astore is None:
            # Dev/base posture without a configured store: a local archive under the
            # configured directory. PRISM's subclass supplies the MinIO-backed store.
            from ..speech.audio_store import LocalAudioStore
            directory = ((self.config.get("audio") or {}).get("dir")
                         or os.path.join(os.path.expanduser("~"), ".vocx", "audio"))
            astore = self._local_audio = getattr(self, "_local_audio", None) or \
                LocalAudioStore(directory)
        ref = astore.save(bytes(body), _one(query, "ts") or "", rm,
                          _one(query, "content_type") or "")
        if not ref:
            return 500, "application/json", _j({"ok": False, "error": "audio could not be stored"})
        try:
            row = self.vox_runner().register.create(
                recording_mode=mode,
                recorder_email=_one(query, "email") or None,
                recorder_name=rm if rm != "unknown" else None,
                capture_id=cap_id or None,
                audio_ref=ref,
                duration_seconds=int(_one(query, "duration") or 0) or None,
                latitude=float(_one(query, "lat")) if _one(query, "lat") else None,
                longitude=float(_one(query, "lng")) if _one(query, "lng") else None,
                consent_id=_one(query, "consent_id") or None,
            )
        except Exception as e:  # noqa: BLE001 — the audio is SAFE; say so honestly
            self.log.exception("VOX capture: register create failed")
            return 502, "application/json", _j({
                "ok": False, "stored_audio": ref,
                "error": f"the register did not accept the conversation: {e}"})
        cid = row.get("id")
        if row.get("replayed") and row.get("status") in ("ready", "submitted"):
            return 200, "application/json", _j({"ok": True, "conversation_id": cid,
                                                "replayed": True, "status": row.get("status")})
        code, ctype, payload = self._vox_process(_j({"conversation_id": cid}))
        out = json.loads(payload)
        return 202, "application/json", _j({"ok": True, "conversation_id": cid,
                                            "status": row.get("status"),
                                            "processing": out.get("ok", False)})

    def _vox_process(self, body: bytes):
        """Kick (or resume) processing for a conversation and return AT ONCE —
        the pipeline continues server-side; the panel polls the register row.
        Idempotent: a ready row is untouched, an in-flight id spawns nothing."""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON"})
        cid = str(payload.get("conversation_id") or "").strip()
        if not cid:
            return 400, "application/json", _j({"ok": False, "error": "conversation_id required"})
        with self._vox_lock:
            if cid in self._vox_inflight:
                return 202, "application/json", _j({"ok": True, "conversation_id": cid,
                                                    "already_running": True})
            self._vox_inflight.add(cid)

        def _work():
            try:
                self.vox_runner().process(cid)
            except Exception:  # noqa: BLE001 — the runner logs; the set must clear
                self.log.exception("VOX pipeline run for %s crashed", cid)
            finally:
                with self._vox_lock:
                    self._vox_inflight.discard(cid)

        import threading
        threading.Thread(target=_work, daemon=True, name=f"vox-run-{cid[:8]}").start()
        return 202, "application/json", _j({"ok": True, "conversation_id": cid})

    def _capabilities(self) -> dict[str, Any]:
        """What this server can do right now, so the app can adapt the UI
        (e.g. open straight to typing when voice STT isn't installed)."""
        import importlib.util
        stt_cfg = self.config.get("stt", {}) or {}
        backend = stt_cfg.get("backend", "faster_whisper")
        stt = False
        try:
            if backend == "stub":
                stt = True
            elif backend == "api":
                stt = bool(os.environ.get((stt_cfg.get("api", {}) or {}).get("endpoint_env", "VOX_STT_API_URL")))
            else:
                stt = importlib.util.find_spec("faster_whisper") is not None
        except Exception:  # noqa: BLE001
            stt = False
        gcfg = self.config.get("google", {}) or {}
        secret = gcfg.get("client_secret_file", "client_secret.json")
        # An EMPTY path means "Google off" — os.path.join(HERE, "") is the package
        # directory, which exists, so the unguarded check would report configured=True.
        google_configured = bool(secret) and (os.path.exists(secret) or (
            not os.path.isabs(secret) and os.path.exists(os.path.join(HERE, secret))))
        extraction = "haiku" if os.environ.get(self.config.get("anthropic_api_key_env", "ANTHROPIC_API_KEY")) else "offline_stub"
        astore = self.audio_store()
        return {"ok": True, "stt": stt, "stt_backend": backend,
                "audio_store": getattr(astore, "kind", None) or "off",
                "google_configured": google_configured, "extraction": extraction,
                "calendar_enabled": gcfg.get("calendar_enabled", True),
                "drive_enabled": gcfg.get("drive_enabled", False),
                "report_templates": self.config.get("report_templates", []),
                # What a report must carry before it may be filed, and what merely
                # helps. Served rather than compiled into a client so the bar can be
                # raised in config without shipping a new UI — the same rule the
                # templates already follow.
                "completeness": self.config.get("completeness", [])}

    def _store_for(self, data: dict[str, Any]) -> AtlasStore:
        """Resolve against the live register the browser posted (its S), if given;
        otherwise the server's own register file."""
        ents = data.get("entities")
        if ents:
            from app.vocx.core.store import MutableAtlasStore
            return MutableAtlasStore.from_entities(ents)
        return self.store

    def _capture(self, body: bytes):
        from app.vocx.core import pipeline as vocx_pipeline
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON body"})
        rm = (data.get("rm") or "").strip()
        transcript = (data.get("transcript") or "").strip()
        # in-ATLAS path can send audio inline (base64) so one endpoint serves both
        if not transcript and data.get("audio_b64"):
            import base64
            if len(data["audio_b64"]) > MAX_AUDIO_BYTES * 4 // 3 + 4:
                return 413, "application/json", _j({"ok": False, "error": "audio too large"})
            audio = base64.b64decode(data["audio_b64"])
            store = self.audio_store()
            if store is not None:
                data["_transcript_ref"] = store.save(
                    audio, data.get("capture_ts") or "", rm)
            tr = self.transcriber().transcribe(audio, language=data.get("language"),
                                               prompt=self.stt_prompt())
            transcript = tr.get("text", "").strip()
            data["_transcription"] = tr
        if not transcript or not rm:
            return 400, "application/json", _j({"ok": False, "error": "transcript and rm required"})
        if len(rm) > 120 or len(transcript) > MAX_TRANSCRIPT_CHARS:
            return 413, "application/json", _j({"ok": False, "error": "rm or transcript too long"})
        meta_extra = _gps_meta(data.get)
        if data.get("_transcription", {}).get("language"):
            meta_extra.setdefault("language", data["_transcription"]["language"])
        result = vocx_pipeline.process_capture(
            transcript, rm=rm, capture_ts=data.get("capture_ts"),
            transcript_ref=data.get("_transcript_ref"),
            store=self._store_for(data), config=self.config,
            offline=bool(data.get("offline")), execute=False,  # preview only
            meta_extra=meta_extra)
        # A capture id rides in _meta from preview to commit, making the commit's Register
        # writes idempotent. Client-supplied (offline queue replay) or minted here.
        _stamp_capture_id(result.get("extraction"), data.get("capture_id"))
        if data.get("_transcription"):
            result["transcription"] = data["_transcription"]
        self._preserve_audio_ref(result, rm)
        self._autosave_draft(result, rm)
        self._log_preview(result, rm)
        return 200, "application/json", _j({"ok": True, **result})

    def _preserve_audio_ref(self, result: dict[str, Any], rm: str) -> None:
        """A RE-ANALYSED capture keeps its recording. The typed lane rebuilds the
        extraction from text alone, so the new extraction carries no transcript_ref —
        and the draft then autosaves OVER the version that had one, losing the
        report's audio for good. If the stored draft knows the ref and the fresh
        extraction doesn't, it carries over."""
        ext = result.get("extraction") or {}
        meta = ext.setdefault("_meta", {})
        if meta.get("transcript_ref"):
            return
        cid = meta.get("capture_id")
        store = self.report_store()
        if not cid or store is None:
            return
        from app.vocx import reports as reports_mod
        if not reports_mod.valid_id(str(cid)):
            return
        old = ((store.get(rm, cid) or {}).get("report") or {}).get("extraction") or {}
        ref = (old.get("_meta") or {}).get("transcript_ref")
        if ref:
            meta["transcript_ref"] = ref

    # ---- mobile: audio capture -> pipeline preview -------------------------
    def _capture_audio(self, query, body):
        from app.vocx.core import pipeline as vocx_pipeline
        from app.vocx.speech import stt as vocx_stt
        rm = (_one(query, "rm") or "").strip()
        if not rm or len(rm) > 120:
            return 400, "application/json", _j({"ok": False, "error": "rm required"})
        if not body:
            return 400, "application/json", _j({"ok": False, "error": "empty audio body"})
        if len(body) > MAX_AUDIO_BYTES:
            return 413, "application/json", _j({"ok": False, "error": "audio too large"})
        cid = (_one(query, "capture_id") or "").strip()[:80]
        _set_capture_stage(cid, "received")
        ref = None
        astore = self.audio_store()
        if astore is not None:
            ref = astore.save(bytes(body), _one(query, "ts") or "", rm,
                              content_type=_one(query, "ct") or "")
        try:
            result = vocx_pipeline.process_audio_capture(
                bytes(body), rm=rm, capture_ts=_one(query, "ts"),
                transcript_ref=ref, content_type=_one(query, "ct") or "",
                transcriber=self.transcriber(), store=self.store, config=self.config,
                execute=False,  # preview; the RM confirms via /commit
                stt_prompt=self.stt_prompt(),
                on_stage=lambda s: _set_capture_stage(cid, s),
                meta_extra=_gps_meta(lambda k: _one(query, k)))
        except vocx_stt.SttTimeoutError as e:
            # ANSWER, rather than leave the caller to time out on us. The decode ran past
            # the budget this capture was given, which is a fact about how long the clip
            # needs — not a fault anyone can retry into working. Said plainly here it
            # reaches the recorder as a sentence; left unsaid, the browser aborts on its
            # own clock and reports "VocX did not answer in time", which names nothing.
            # `ref` goes with it: the audio IS stored, and the reference proves it.
            return 504, "application/json", _j({"ok": False, "error": str(e), "ref": ref})
        _stamp_capture_id(result.get("extraction"), _one(query, "capture_id"))
        self._autosave_draft(result, rm)
        self._log_preview(result, rm)
        _set_capture_stage(cid, "done")
        return 200, "application/json", _j({"ok": True, **result})

    def _capture_status(self, query):
        """Where an in-flight capture is in the pipeline — polled by the recorder UI
        while /v1/capture_audio runs. Only a stage word leaks; unknown ids simply
        answer 'unknown' (the poll starts before the upload lands)."""
        cid = (_one(query, "capture_id") or "").strip()[:80]
        entry = _CAPTURE_STAGES.get(cid) if cid else None
        return 200, "application/json", _j(
            {"ok": True, "stage": entry[0] if entry else "unknown"})

    # ---- mobile: commit an approved (possibly edited) capture --------------
    def _commit(self, body):
        from app.vocx.core import pipeline as vocx_pipeline
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON body"})
        ext = data.get("extraction")
        rm = data.get("rm")
        if not ext or not rm:
            return 400, "application/json", _j({"ok": False, "error": "extraction and rm required"})

        store = self._store_for(data)   # live register from ATLAS, or the server's
        vocx_gate.override_entity(ext, store, code=data.get("chosen_code"),
                                 new_lead=bool(data.get("new_lead")), company=data.get("company"))
        vocx_gate.apply_edits(ext, data.get("edits"))
        ext.setdefault("_meta", {})["rm"] = rm

        summary = data.get("summary") or vocx_pipeline._fallback_summary(ext)
        decision = vocx_gate.gate(ext, self.config)
        plan = vocx_gate.plan_writes(ext, store, self.config, summary=summary)

        capture_id = (data.get("capture_id")
                      or (ext.get("_meta") or {}).get("capture_id") or None)
        # Explicit "Log To": route the interaction at a chosen product line / subject.
        log_to = data.get("log_to")
        if log_to is not None:
            import uuid as _uuid
            ok_types = {"Lead", "Deal", "Entity", "Lending", "Syndication", "AssetMonetisation"}
            try:
                st, sid = log_to.get("subject_type"), str(_uuid.UUID(str(log_to.get("subject_id"))))
            except (ValueError, AttributeError, TypeError):
                st, sid = None, None
            if st not in ok_types or not sid:
                return 400, "application/json", _j(
                    {"ok": False, "error": "log_to needs subject_type in "
                     + "/".join(sorted(ok_types)) + " and a UUID subject_id"})
            log_to = {"subject_type": st, "subject_id": sid}
        writer = None
        writer_error = None
        if self.writer_factory:
            try:
                writer = self.writer_factory(rm, store, self.config, capture_id=capture_id)
            except Exception as e:  # noqa: BLE001 — no token, missing libs, refresh failure…
                writer_error = f"{type(e).__name__}: {e}"
                self.log.warning("writer_factory failed for %s: %s", rm, writer_error)
        writer = writer or vocx_gate.MockWriter()
        writes = writer.execute(plan, {"extraction": ext, "transcript": data.get("transcript", ""),
                                       "summary": summary, "log_to": log_to})
        if writes.get("ok") and self.report_store() is not None and capture_id:
            from app.vocx import reports as reports_mod
            if reports_mod.valid_id(capture_id):
                self.report_store().save(rm, capture_id, reports_mod.make_doc(
                    rm, capture_id, "committed",
                    {"extraction": ext, "summary": summary}, writes=writes))
        ops = {r.get("op"): r.get("status") for r in (writes.get("results") or [])}
        self.log.info("vocx commit capture=%s rm=%s committed=%s ops=%s writer_error=%s",
                      capture_id, rm, bool(writes.get("ok")), ops, writer_error)
        return 200, "application/json", _j({"ok": True, "committed": bool(writes.get("ok")),
                                            "decision": decision, "write_plan": plan, "writes": writes,
                                            "writer_error": writer_error})

    # ---- AI auto-fill for a template's extra fields ------------------------
    def _template_fill(self, body):
        """Given the transcript and a template's fields, ask Haiku to fill just
        those fields from what was said. Returns {values: {key: value}}."""
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, "application/json", _j({"ok": False, "error": "invalid JSON"})
        transcript = (data.get("transcript") or "").strip()
        fields = [f for f in (data.get("fields") or []) if f.get("key")]
        if not transcript or not fields:
            return 200, "application/json", _j({"ok": True, "values": {}})
        key_env = self.config.get("anthropic_api_key_env", "ANTHROPIC_API_KEY")
        if not os.environ.get(key_env):
            return 200, "application/json", _j({"ok": False, "error": "no_api_key", "values": {}})
        try:
            import anthropic

            from app.vocx.core import extract as vocx_extract
            client = anthropic.Anthropic(api_key=os.environ[key_env])
            model = self.config.get("model", "claude-haiku-4-5-20251001")
            spec = "\n".join("- {} (json key: {})".format(f.get("label") or f["key"], f["key"]) for f in fields)
            sys_prompt = ("You extract specific fields from a meeting transcript for a lending/finance CRM. "
                          "Return ONE JSON object mapping each json key to a short string value, or null if the "
                          "field is not clearly stated. NEVER guess. No prose, no markdown fences — the first "
                          "character must be { and the last }.\n\nFields to fill:\n" + spec)
            resp = client.messages.create(
                model=model, max_tokens=900, system=sys_prompt,
                messages=[{"role": "user", "content": "Transcript:\n\"\"\"\n" + transcript + "\n\"\"\""}])
            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            parsed = vocx_extract._parse_json_lenient(text) or {}
            keys = {f["key"] for f in fields}
            values = {k: str(v).strip() for k, v in parsed.items() if k in keys and v not in (None, "", [])}
            return 200, "application/json", _j({"ok": True, "values": values})
        except Exception as e:  # noqa: BLE001
            return 200, "application/json", _j({"ok": False, "error": str(e), "values": {}})

    # ---- diagnostic: prove which calendar VOX actually writes to -----------
    def _calendar_test(self, query):
        """Create a real test event NOW and report which Google account it landed
        on — so 'I can't see my follow-up' is answered definitively (usually the
        event is on a different account than the one being viewed)."""
        import datetime as _dt
        rm = _one(query, "rm") or "RM"
        if not self.writer_factory:
            return 200, "application/json", _j({"ok": False,
                "error": "Google writes are OFF on the server (started with --no-google)."})
        try:
            writer = self.writer_factory(rm, self.store, self.config)
        except Exception as e:  # noqa: BLE001 — no token / no fallback
            return 200, "application/json", _j({"ok": False,
                "error": f"No Google token for {rm} (and demo fallback found none). Connect Google first. [{e}]"})
        try:
            cal = getattr(writer, "cal", None)
            if cal is None:
                return 200, "application/json", _j({"ok": False, "error": "writer has no calendar (register-only)."})
            email = None
            try:
                prim = cal.calendar.calendars().get(calendarId="primary").execute()
                email = prim.get("id")
            except Exception:  # noqa: BLE001
                pass
            d = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
            r = cal.create_event("VOX calendar test ✓", d, "16:00", "video",
                                 "If you can see this at 4pm tomorrow, VOX is writing to THIS calendar.")
            return 200, "application/json", _j({"ok": True, "link": r.get("link"),
                                                "calendar_email": email, "date": d, "rm": rm})
        except Exception as e:  # noqa: BLE001
            return 200, "application/json", _j({"ok": False, "error": f"Calendar write failed: {e}"})

    # ---- PKCE persistence (tokens volume; entries expire after 15 min) ------
    def _pkce_path(self) -> str:
        d = self.config.get("google", {}).get("tokens_dir", "vocx_tokens")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "vocx_pkce.json")

    def _pkce_save(self, rm: str, verifier: str | None) -> None:
        import time as _t
        path = self._pkce_path()
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                data = {}
        now = _t.time()
        data = {k: v for k, v in data.items() if now - v.get("ts", 0) < 900}
        data[rm] = {"verifier": verifier, "ts": now}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _pkce_pop(self, rm: str) -> str | None:
        path = self._pkce_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        entry = data.pop(rm, None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return entry.get("verifier") if entry else None

    # ---- mobile: per-user Google auth --------------------------------------
    def _token_store(self):
        from app.vocx.google.oauth import TokenStore
        return TokenStore(self.config.get("google", {}).get("tokens_dir", "vocx_tokens"))

    def _auth_status(self, query):
        rm = _one(query, "rm")
        if not rm:
            return 400, "application/json", _j({"ok": False, "error": "rm required"})
        return 200, "application/json", _j({"ok": True, "rm": rm, "connected": self._token_store().has(rm)})

    def _auth_start(self, query):
        rm = _one(query, "rm")
        if not rm:
            return 400, "application/json", _j({"ok": False, "error": "rm required"})
        try:
            from app.vocx.google.oauth import authorization_url, build_flow
            flow = build_flow(self.config)
            url = authorization_url(flow, rm)
            # Remember the PKCE verifier this flow generated, so the callback can complete
            # the exchange — persisted on the tokens volume, not in process memory, so a
            # restart or a second replica between start and callback cannot lose it.
            self._pkce_save(rm, getattr(flow, "code_verifier", None))
            # ?go=1 -> redirect the browser straight to Google, so you can connect
            # a specific RM by visiting /v1/auth/start?rm=Shubh&go=1
            if _one(query, "go"):
                page = ("<meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<script>location.replace({json.dumps(url)})</script>"
                        "<p style='font:16px system-ui;padding:24px'>Redirecting to Google…</p>"
                        )
                return 200, "text/html; charset=utf-8", page.encode("utf-8")
            return 200, "application/json", _j({"ok": True, "url": url})
        except FileNotFoundError:
            return 503, "application/json", _j({"ok": False, "error": "google_not_configured",
                "hint": "Add client_secret.json and set google.redirect_uri in atlas_config.json."})
        except Exception as e:  # noqa: BLE001
            return 503, "application/json", _j({"ok": False, "error": str(e)})

    def _auth_callback(self, query):
        code = _one(query, "code")
        rm = _one(query, "state")
        if not code or not rm:
            return 400, "text/html", b"<h1>Missing code/state</h1>"
        try:
            from app.vocx.google.oauth import build_flow, exchange_code
            flow = build_flow(self.config)
            cv = self._pkce_pop(rm)                # restore the verifier from /auth/start
            if cv is not None:
                flow.code_verifier = cv
            creds = exchange_code(flow, code)
            self._token_store().save_credentials(rm, creds)
            return 200, "text/html; charset=utf-8", (
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<body style='font:16px system-ui;padding:40px;text-align:center'>"
                f"<h2>✓ {rm} connected</h2>"
                "<p>Google Calendar is linked. You can close this tab and return to VOM — "
                "it will notice automatically.</p>"
                "<script>try{window.close()}catch(e){}"
                "setTimeout(function(){document.body.insertAdjacentHTML('beforeend',"
                "'<p style=\"color:#888\">(If this tab didn\\'t close, just switch back to the VOM tab.)</p>')},800);"
                "</script></body>"
            ).encode()
        except Exception as e:  # noqa: BLE001
            return 500, "text/html", (f"<h1>Auth failed</h1><p>{e}</p>").encode()


# --- helpers ------------------------------------------------------------------
def _j(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# In-flight capture stages, keyed by the client-generated capture_id. In-memory on
# purpose: the stage only matters while the /v1/capture_audio request is running in
# this same process, and a restart mid-capture loses the request anyway. Entries
# self-prune so an abandoned poll can never grow the map.
_CAPTURE_STAGES: dict[str, tuple[str, float]] = {}
_CAPTURE_STAGE_TTL_S = 900.0


def _set_capture_stage(capture_id: str, stage: str) -> None:
    if not capture_id:
        return
    import time as _time

    now = _time.time()
    for k in [k for k, (_, at) in _CAPTURE_STAGES.items()
              if now - at > _CAPTURE_STAGE_TTL_S]:
        _CAPTURE_STAGES.pop(k, None)
    _CAPTURE_STAGES[capture_id] = (stage, now)


def _one(query: dict[str, Any], key: str, default=None):
    v = query.get(key)
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def _search_args(query: dict[str, Any]) -> dict[str, Any]:
    g = lambda k, d=None: _one(query, k, d)  # noqa: E731
    args = {
        "company": g("company"), "user": g("user"),
        "date_from": g("from") or g("date_from"), "date_to": g("to") or g("date_to"),
        "itype": g("type") or g("itype"), "ref_type": g("ref_type"),
        "q": g("q"), "sort": g("sort", "desc"),
    }
    limit = g("limit"); offset = g("offset")
    args["limit"] = int(limit) if (limit not in (None, "")) else 50
    args["offset"] = int(offset) if (offset not in (None, "")) else 0
    return {k: v for k, v in args.items() if v is not None}


def _panel_html() -> bytes:
    path = os.path.join(HERE, "vox_panel.html")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    return b"<h1>VOX</h1><p>vox_panel.html not found.</p>"


def _asset(name: str, fallback: bytes = b"not found") -> bytes:
    path = os.path.join(HERE, "vox_mobile", name)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    return fallback


# --- stdlib http.server adapter (standalone dev) ------------------------------
def serve(host: str = "127.0.0.1", port: int = 8765,
          store: AtlasStore | None = None, config: dict[str, Any] | None = None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    app = VocxApp(store, config)

    class Handler(BaseHTTPRequestHandler):
        def _do(self, method):
            parsed = urlparse(self.path)
            query = {k: v for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            status, ctype, payload = app.handle(method, parsed.path, query, body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def log_message(self, *a):
            pass  # requests are logged by VoxApp

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"VOX panel on http://{host}:{port}/  ({len(app.store.interactions)} interactions)")
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="VOX web panel server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    serve(a.host, a.port)
