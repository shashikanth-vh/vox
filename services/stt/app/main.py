"""PRISM STT — the dedicated speech-to-text service.

One endpoint that matters: ``POST /v1/audio/transcriptions`` — OpenAI-compatible
multipart (``file``, optional ``model``/``language``), answering
``{text, language, duration, segments, backend}``. VocX's ``api`` STT backend speaks
exactly this, so from VocX's side using this service is pure configuration
(``VOCX_STT_BACKEND=api`` + the endpoint URL and key).

Why a separate service (vs Whisper inside VocX):
  * ONE model instance per process instead of one per VocX worker (~700 MB each);
  * inference is resource-isolated — a long decode can't starve the capture API;
  * the model is baked into the image at BUILD time and the container runs with
    ``HF_HUB_OFFLINE=1`` — no runtime dependency on huggingface.co, deterministic
    deploys, works air-gapped;
  * scales independently (replicas / a GPU pool later) like every PRISM module.

Internal-only: no gateway route, no host port in compose — VocX is the consumer.
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import ORJSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.engine import AudioUndecodable, Engine, ModelUnavailable, build_engine

log = get_logger("stt")


def _authorized(request: Request, settings: Settings) -> bool:
    keys = [k.strip() for k in settings.api_keys.split(",") if k.strip()]
    if not keys:
        return True
    auth = request.headers.get("Authorization", "")
    provided = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") \
        else request.headers.get("X-API-Key", "")
    return any(hmac.compare_digest(provided, k) for k in keys)


def _problem(status: int, kind: str, title: str, detail: str) -> ORJSONResponse:
    return ORJSONResponse(status_code=status,
                          content={"error": {"type": kind, "title": title, "detail": detail}})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = build_engine(settings)
        if settings.preload:
            # Load from the baked image dir now so the FIRST capture isn't slow and a
            # bad model config fails the pod at startup, not mid-request.
            await run_in_threadpool(app.state.engine.warm)
            # cpu_threads is logged because it is the one knob whose WRONG value looks
            # like nothing at all — the service works, it is simply slow, and nobody can
            # tell from the outside whether it took the container's core count or the
            # host's.
            log.info("stt_model_ready", extra={"model": settings.model_size,
                                               "device": settings.device,
                                               "beam_size": settings.beam_size,
                                               "cpu_threads": settings.cpu_threads})
        yield

    app = FastAPI(title="PRISM STT", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    def _engine(request: Request) -> Engine:
        eng = getattr(request.app.state, "engine", None)
        if eng is None:                              # tests skip lifespan
            eng = request.app.state.engine = build_engine(settings)
        return eng

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Any:
        eng = _engine(request)
        loaded = bool(getattr(eng, "loaded", False))
        problem = str(getattr(eng, "load_error", "") or "")
        # Ready means "can transcribe". Answering ok while the model is absent makes an
        # orchestrator route traffic to a container that can only 500.
        if problem:
            return _problem(503, "model_unavailable", "Model not loaded", problem)
        return {"status": "ok" if loaded or not settings.preload else "loading",
                "model_loaded": loaded}

    @app.post("/v1/audio/transcriptions", summary="Transcribe one audio clip")
    async def transcribe(request: Request,
                         file: UploadFile = File(...),
                         model: str = Form(""),          # selects among BAKED sizes; else default
                         language: str | None = Form(None),
                         task: str = Form("transcribe"),
                         prompt: str | None = Form(None)) -> Any:
        if not _authorized(request, settings):
            return _problem(401, "unauthorized", "Unauthorized",
                            "Missing or invalid bearer token.")
        blob = await file.read()
        if not blob:
            return _problem(400, "invalid_request", "Empty audio",
                            "The uploaded file contains no bytes.")
        if len(blob) > settings.max_audio_bytes:
            return _problem(413, "payload_too_large", "Audio too large",
                            f"Limit is {settings.max_audio_bytes} bytes.")
        if task not in ("transcribe", "translate"):
            return _problem(400, "invalid_request", "Bad task",
                            "task must be 'transcribe' or 'translate'.")
        try:
            # A recognised size ("small", "medium", "large-v3") routes to that baked
            # model — the quick-transcript lane; anything else ("whisper-1", "") is
            # the default. Unknown names never fail: OpenAI-compat callers send
            # their own model strings and must keep working.
            result = await run_in_threadpool(_engine(request).transcribe, blob,
                                             language or None, task,
                                             (prompt or "")[:1500] or None,
                                             (model or "").strip() or None)
        except ModelUnavailable as exc:
            # 503, and SAY WHY. This reaches VocX's capture log; a bare 500 there is
            # indistinguishable from a crash and leaves the operator with nothing to fix.
            log.error("stt_model_unavailable", extra={"detail": str(exc)})
            return _problem(503, "model_unavailable", "Model not loaded", str(exc))
        except AudioUndecodable as exc:
            log.warning("stt_audio_undecodable",
                        extra={"bytes": len(blob), "detail": str(exc)})
            return _problem(400, "invalid_request", "Audio could not be transcribed",
                            str(exc))
        log.info("stt_transcribed", extra={
            "bytes": len(blob), "duration": result.get("duration"),
            "language": result.get("language"), "backend": result.get("backend")})
        return result

    return app


app = create_app()
