"""PRISM DocRAG — document → knowledge → hybrid retrieval with cited answers.

Flow:

1. ``POST /v1/documents`` (multipart PDF/XLSX) stores the file and answers **202** with a
   document id; a background worker runs the knowledge pipeline — OpenDataLoader +
   PyMuPDF extraction, Sarvam Doc AI for scanned pages, section reconstruction,
   entity tagging, chunking — then embeds the chunks and adds them to the tenant's index.
   Poll ``GET /v1/documents/{id}`` until ``status`` is ``ready`` (or ``failed``).
2. ``POST /v1/query`` runs hybrid retrieval (vector + BM25, reciprocal-rank fused, with an
   entity boost) and answers extractively (cited passages, no LLM) or generatively
   (Sarvam chat, grounded in the retrieved passages only).

Multi-tenant: every endpoint reads ``X-Tenant``; a tenant's documents and index are
isolated on disk and in memory. Reached through the gateway at ``/docrag/...``.
"""

from __future__ import annotations

import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from evam_backend_core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationAppError,
    register_exception_handlers,
)
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from fastapi import Depends, FastAPI, File, Form, Request, Response, Security, UploadFile
from fastapi.responses import ORJSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.extract import Extractor
from app.rag.answer import (
    GenerativeFailedError,
    GenerativeUnavailableError,
    extractive_answer,
    generative_answer,
)
from app.rag.embeddings import build_embedder
from app.rag.vector_index import VectorIndex
from app.store import SUPPORTED_SUFFIXES, DocumentStore

log = get_logger("docrag")

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INDEX_CONNECT_ATTEMPTS = 30
_MAGIC = {".pdf": (b"%PDF",), ".xlsx": (b"PK\x03\x04",),
          ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),     # OLE2 compound file
          ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".png": (b"\x89PNG",),
          ".tif": (b"II*\x00", b"MM\x00*"), ".tiff": (b"II*\x00", b"MM\x00*"),
          ".bmp": (b"BM",)}
# Declared (not just read) so the OpenAPI carries them and /docs gets an Authorize button.
_API_KEY = APIKeyHeader(name="X-API-Key", auto_error=False,
                        description="One of DOCRAG_API_KEYS (injected by the gateway inside PRISM).")
_BEARER = HTTPBearer(auto_error=False, description="Alternative to X-API-Key when calling directly.")


class PayloadTooLargeError(AppError):
    status_code = 413
    error_type = "payload_too_large"
    title = "Upload too large"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    error_type = "unsupported_media_type"
    title = "Unsupported document type"


class BadGatewayError(AppError):
    status_code = 502
    error_type = "bad_gateway"
    title = "Upstream model call failed"


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["extractive", "generative"] = "extractive"
    top_k: int = Field(default=5, ge=1, le=20)
    # Restrict retrieval to these documents (ids from GET /v1/documents). Empty = all.
    doc_ids: list[str] = Field(default_factory=list, max_length=200)


def _safe_filename(name: str | None) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:200] or "document"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.extractor = Extractor(settings)
        if not settings.index_enabled:
            app.state.embedder = app.state.store = None
            log.info("docrag_started", extra={
                "mode": "extract-only", "data_dir": settings.data_dir,
                "odl": bool(settings.use_odl and settings.odl_binary()),
                "sarvam": settings.sarvam_configured()})
            yield
            return
        embedder = build_embedder(settings)
        if settings.preload:
            try:
                await run_in_threadpool(embedder.warm)
                log.info("docrag_models_ready", extra={
                    "dense": settings.dense_model, "sparse": settings.sparse_model})
            except Exception:  # noqa: BLE001 - surfaced on /readyz with the reason
                log.exception("docrag_model_unavailable")
        index = VectorIndex(settings)
        store = DocumentStore(settings, embedder, index)
        # Qdrant may still be starting (it is a separate container/pod): wait a bounded
        # time for it instead of crash-looping, then fail loudly with the reason.
        for attempt in range(1, _INDEX_CONNECT_ATTEMPTS + 1):
            try:
                await run_in_threadpool(store.start)
                break
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                if attempt == _INDEX_CONNECT_ATTEMPTS:
                    log.error("docrag_index_unreachable", extra={
                        "qdrant": settings.qdrant_url, "detail": str(exc)})
                    raise
                await asyncio.sleep(2)
        app.state.embedder, app.state.store = embedder, store
        log.info("docrag_started", extra={
            "data_dir": settings.data_dir, "collection": index.name,
            "odl": bool(settings.use_odl and settings.odl_binary()),
            "sarvam": settings.sarvam_configured()})
        yield
        store.shutdown()

    app = FastAPI(title="PRISM DocRAG", version="0.2.0",
                  description="Document → knowledge → hybrid retrieval with cited answers.",
                  default_response_class=ORJSONResponse, lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origin_list():
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list(),
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"],
                           expose_headers=["X-Request-ID"], max_age=3600)
    register_exception_handlers(app)

    def authorized(api_key: str | None = Security(_API_KEY),
                   bearer: HTTPAuthorizationCredentials | None = Security(_BEARER)) -> None:
        keys = settings.api_key_list()
        if not keys:
            return
        # EITHER credential may carry the key. Behind the gateway the Authorization header
        # holds the USER's OIDC token (forwarded) while X-API-Key holds this service's key
        # (injected) — preferring the bearer would refuse every signed-in request.
        offered = [c for c in (api_key, bearer.credentials if bearer else None) if c]
        if not any(hmac.compare_digest(c, k) for c in offered for k in keys):
            raise UnauthorizedError("Missing or invalid X-API-Key.")

    def tenant_of(request: Request) -> str:
        tenant = request.headers.get("X-Tenant") or settings.default_tenant
        if not _TENANT_RE.match(tenant):
            raise ValidationAppError(f"Invalid X-Tenant {tenant!r}.")
        return tenant

    def store_of(request: Request) -> DocumentStore:
        store = getattr(request.app.state, "store", None)
        if store is None:
            raise ServiceUnavailableError(
                "The document index is disabled on this deployment (DOCRAG_INDEX_ENABLED="
                "false); POST /v1/extract is available.")
        return store

    def doc_or_404(store: DocumentStore, tenant: str, doc_id: str) -> dict:
        meta = store.get(tenant, doc_id) if _DOC_ID_RE.match(doc_id) else None
        if meta is None:
            raise NotFoundError(f"Document {doc_id!r} not found.")
        return meta

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Any:
        if not settings.index_enabled:
            return {"status": "ok", "mode": "extract-only"}
        embedder = getattr(request.app.state, "embedder", None)
        if embedder is None:
            return ORJSONResponse(status_code=503, content={"status": "starting"})
        # Ready means "can embed AND can index/search". Either missing would accept
        # uploads that then fail, or answer queries with nothing.
        if embedder.load_error:
            return ORJSONResponse(status_code=503, content={
                "status": "model_unavailable", "detail": embedder.load_error})
        try:
            await run_in_threadpool(request.app.state.store.index.ping)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return ORJSONResponse(status_code=503, content={
                "status": "index_unavailable", "detail": f"Qdrant: {exc}"})
        return {"status": "ok", "model_loaded": bool(embedder.loaded)}

    secured = [Depends(authorized)]

    @app.get("/v1/status", dependencies=secured, tags=["DocRAG"],
             summary="What this deployment can do (engines, keys) + the tenant's index size")
    async def status(request: Request, tenant: str = Depends(tenant_of)) -> dict:
        store = getattr(request.app.state, "store", None)
        docs = store.list_documents(tenant) if store else []
        return {
            "tenant": tenant,
            "mode": "full" if store else "extract-only",
            "documents": len(docs),
            "documents_ready": sum(d["status"] == "ready" for d in docs),
            "chunks_indexed": store.indexed_chunks(tenant) if store else 0,
            "vector_index": {
                "engine": "qdrant", "collection": store.index.name,
                "dense_model": "stub" if settings.embedder == "stub" else settings.dense_model,
                "sparse_model": "stub" if settings.embedder == "stub" else settings.sparse_model,
            } if store else None,
            "opendataloader": bool(settings.use_odl and settings.odl_binary()),
            "sarvam_configured": settings.sarvam_configured(),
            "supported_types": sorted(SUPPORTED_SUFFIXES),
            "max_upload_bytes": settings.max_upload_bytes,
        }

    async def read_upload(file: UploadFile) -> tuple[str, bytes]:
        filename = _safe_filename(file.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise UnsupportedMediaTypeError(
                f"{filename!r}: supported types are {', '.join(sorted(SUPPORTED_SUFFIXES))}.")
        data = await file.read(settings.max_upload_bytes + 1)
        if not data:
            raise ValidationAppError("The uploaded file is empty.")
        if len(data) > settings.max_upload_bytes:
            raise PayloadTooLargeError(f"Limit is {settings.max_upload_bytes} bytes.")
        if not data.startswith(_MAGIC[suffix]):
            raise UnsupportedMediaTypeError(f"{filename!r} does not look like a {suffix} file.")
        return filename, data

    @app.post("/v1/extract", dependencies=secured, tags=["DocRAG"],
              summary="Extract one PDF/XLSX/image to structured Markdown (synchronous, cached)")
    async def extract(request: Request,
                      file: UploadFile = File(...),
                      use_sarvam: bool = Form(True),
                      tenant: str = Depends(tenant_of)) -> dict:
        """The document's content as Markdown — headings, real tables, ``[page N]``
        markers — plus the engines used and every warning (a scanned page with no OCR
        is named, never silently empty). Nothing is indexed. Identical files are served
        from cache; a scan that needs Sarvam can take minutes the first time."""
        filename, data = await read_upload(file)
        return await run_in_threadpool(request.app.state.extractor.extract,
                                       tenant, filename, data, use_sarvam)

    @app.post("/v1/documents", dependencies=secured, tags=["DocRAG"], status_code=202,
              summary="Upload a PDF/XLSX/image; it is processed in the background")
    async def upload(request: Request, response: Response,
                     file: UploadFile = File(...),
                     use_sarvam: bool = Form(True),
                     tenant: str = Depends(tenant_of)) -> dict:
        filename, data = await read_upload(file)
        meta, created = await run_in_threadpool(
            store_of(request).create, tenant, filename, data,
            request.headers.get("X-User-Email"), use_sarvam)
        if not created:
            response.status_code = 200
        return {**meta, "duplicate": not created}

    @app.get("/v1/documents", dependencies=secured, tags=["DocRAG"],
             summary="The tenant's documents, newest first")
    async def list_documents(request: Request, tenant: str = Depends(tenant_of)) -> dict:
        items = store_of(request).list_documents(tenant)
        return {"items": items, "total": len(items)}

    @app.get("/v1/documents/{doc_id}", dependencies=secured, tags=["DocRAG"],
             summary="One document's status; with ?include=knowledge, its reconstructed sections")
    async def get_document(request: Request, doc_id: str, include: str = "",
                           tenant: str = Depends(tenant_of)) -> dict:
        store = store_of(request)
        meta = doc_or_404(store, tenant, doc_id)
        body: dict[str, Any] = dict(meta)
        if "knowledge" in include.split(","):
            body["knowledge"] = await run_in_threadpool(store.knowledge, tenant, doc_id)
        return body

    @app.get("/v1/documents/{doc_id}/chunks", dependencies=secured, tags=["DocRAG"],
             summary="The document's RAG-ready knowledge chunks (with provenance)")
    async def get_chunks(request: Request, doc_id: str, tenant: str = Depends(tenant_of)) -> dict:
        store = store_of(request)
        doc_or_404(store, tenant, doc_id)
        items: list[dict] = store.chunks(tenant, doc_id) or []
        return {"items": items, "total": len(items)}

    @app.delete("/v1/documents/{doc_id}", dependencies=secured, tags=["DocRAG"],
                status_code=204, summary="Remove a document and its chunks from the index")
    async def delete_document(request: Request, doc_id: str,
                              tenant: str = Depends(tenant_of)) -> Response:
        store = store_of(request)
        doc_or_404(store, tenant, doc_id)
        await run_in_threadpool(store.delete, tenant, doc_id)
        return Response(status_code=204)

    @app.post("/v1/query", dependencies=secured, tags=["DocRAG"],
              summary="Hybrid retrieval + an extractive or generative (Sarvam) cited answer")
    async def query(request: Request, body: QueryIn, tenant: str = Depends(tenant_of)) -> dict:
        store = store_of(request)
        retrieved = await run_in_threadpool(store.search, tenant, body.query, body.top_k,
                                            body.doc_ids or None)
        try:
            if body.mode == "generative":
                answer = await run_in_threadpool(generative_answer, body.query, retrieved)
            else:
                answer = extractive_answer(body.query, retrieved)
        except GenerativeUnavailableError as exc:
            raise ConflictError(str(exc)) from exc
        except GenerativeFailedError as exc:
            log.warning("docrag_generative_failed", extra={"detail": str(exc)})
            raise BadGatewayError(str(exc)) from exc
        return {
            "query": body.query,
            "mode": answer.mode,
            "answer": answer.text,
            "citations": [asdict(c) for c in answer.citations],
            "results": [
                {"rank": i + 1, "fused_score": round(r.fused_score, 6),
                 "vector_rank": r.vector_rank, "bm25_rank": r.bm25_rank, "chunk": r.chunk}
                for i, r in enumerate(retrieved)
            ],
        }

    if settings.dev_ui:
        # TEMPORARY browser test console: upload / explore / query over this API.
        # Off by default; the prod-posture compose overlay pins it off.
        page_path = Path(__file__).parent / "dev_ui.html"

        @app.get("/v1/dev-ui", dependencies=secured, include_in_schema=False)
        async def dev_ui() -> Response:
            return Response(content=page_path.read_bytes(), media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    return app


app = create_app()
