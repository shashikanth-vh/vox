"""PRISM Chitti service lifecycle and dependency readiness."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_register_client.config import RegisterClientConfig
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from openai import AsyncOpenAI

from app import __version__
from app.api import build_router
from app.config import get_settings
from app.model_stages import ModelStages
from app.pipeline import ChittiPipeline
from app.semantic import SemanticRetriever, collection_name, verify_index_integrity

log = get_logger("chitti")


def runtime_source_fingerprint(source_root: Path | None = None) -> str:
    """Fingerprint the Python/ontology bytes copied into the running image."""
    root = source_root or Path(__file__).resolve().parent
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def create_app() -> FastAPI:
    settings = get_settings()
    source_sha256 = runtime_source_fingerprint()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pipeline = None
        app.state.readiness_error = None
        app.state.ontology_index = None
        app.state.retriever = None
        llm: AsyncOpenAI | None = None
        retriever: SemanticRetriever | None = None
        if settings.pipeline_enabled:
            try:
                llm = AsyncOpenAI(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=settings.llm_max_retries,
                )
                retriever = SemanticRetriever(settings)
                app.state.retriever = retriever
                app.state.ontology_index = await verify_index_integrity(
                    retriever.client, settings
                )
                app.state.pipeline = ChittiPipeline(
                    settings, ModelStages(llm, settings), retriever
                )
            except Exception as exc:
                app.state.readiness_error = str(exc)
                log.exception("chitti_dependency_not_ready")
        log.info(
            "chitti_started",
            extra={
                "environment": settings.environment,
                "public_model_id": settings.public_model_id,
                "pipeline_status": (
                    "CONNECTED_TO_REGISTER" if app.state.pipeline else "NOT_READY"
                ) if settings.pipeline_enabled else "NOT_CONNECTED_TO_REGISTER",
            },
        )
        try:
            yield
        finally:
            if retriever is not None:
                await retriever.close()
            if llm is not None:
                await llm.close()

    app = FastAPI(
        title="PRISM Chitti",
        version=__version__,
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(build_router(settings))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():  # noqa: ANN202
        if settings.pipeline_enabled and app.state.pipeline is None:
            return ORJSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "service": settings.app_name,
                    "pipeline_status": "DEPENDENCY_NOT_READY",
                    "detail": app.state.readiness_error,
                },
            )
        if settings.pipeline_enabled:
            try:
                async with asyncio.timeout(3):
                    await app.state.retriever.client.get_collection(collection_name(settings))
                    async with httpx.AsyncClient(
                        timeout=2,
                        verify=RegisterClientConfig(ca_file=settings.register_ca_file).tls_verify(),
                    ) as client:
                        response = await client.get(f"{settings.register_base_url.rstrip('/')}/readyz")
                        response.raise_for_status()
            except Exception:
                return ORJSONResponse(status_code=503, content={
                    "status": "not_ready", "service": settings.app_name,
                    "pipeline_status": "DEPENDENCY_NOT_READY",
                })
        return {
            "status": "ready",
            "service": settings.app_name,
            "pipeline_status": (
                "CONNECTED_TO_REGISTER"
                if settings.pipeline_enabled
                else "NOT_CONNECTED_TO_REGISTER"
            ),
            "runtime": {
                "service_app_sha256": source_sha256,
                "ontology_collection": collection_name(settings),
                "ontology_version": (getattr(app.state, "ontology_index", None) or {}).get("version"),
                "ontology_passage_count": (
                    getattr(app.state, "ontology_index", None) or {}
                ).get("passage_count"),
                "ontology_identity": (getattr(app.state, "ontology_index", None) or {}).get("identity"),
                "dense_candidates": settings.dense_candidates,
                "sparse_candidates": settings.sparse_candidates,
                "fused_candidates": settings.fused_candidates,
                "rerank_limit": settings.rerank_limit,
                "grounding_rerank_limit": settings.grounding_rerank_limit,
                "planning_rerank_limit": settings.planning_rerank_limit,
                "capture_retrieval_trace": settings.capture_retrieval_trace,
                "dense_model": settings.dense_model,
                "dense_model_revision": settings.dense_model_revision,
                "sparse_model": settings.sparse_model,
                "sparse_model_revision": settings.sparse_model_revision,
                "rerank_model": settings.rerank_model,
                "rerank_model_revision": settings.rerank_model_revision,
            },
        }

    return app


app = create_app()
