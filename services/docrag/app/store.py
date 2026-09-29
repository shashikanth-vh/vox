"""Document registry, ingestion worker pool, and the bridge to the vector index.

On-disk layout (the volume at ``DOCRAG_DATA_DIR``) — the source of truth::

    {tenant}/{doc_id}/meta.json        status + provenance of the upload
                     /source.{pdf,xlsx} the original bytes
                     /knowledge.json   the canonical KnowledgeDocument
                     /chunks.json      the derived, RAG-ready chunks

Vectors live in Qdrant (``app.rag.vector_index``) and are derived from ``chunks.json``.
At startup every ready document is reconciled against the index: one whose points are
missing (a new or restored Qdrant, a model change) is re-embedded from its saved chunks.
Every file is written atomically (tmp + rename), so a crash never leaves a half-written
record, and work interrupted by a restart is requeued.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evam_backend_core.logging import get_logger

from app.config import Settings
from app.knowledge.chunker import chunks_to_dicts
from app.knowledge.pipeline import IMAGE_SUFFIXES, run_pipeline
from app.rag.embeddings import Embedder
from app.rag.retrieval import RetrievedChunk, fuse
from app.rag.vector_index import VectorIndex

log = get_logger("docrag.store")

SUPPORTED_SUFFIXES = {".pdf", ".xlsx", ".xls", *IMAGE_SUFFIXES}
PENDING = ("queued", "processing")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path: Path, obj: Any) -> None:
    _write_atomic(path, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


class DocumentStore:
    def __init__(self, settings: Settings, embedder: Embedder, index: VectorIndex) -> None:
        self.s = settings
        self.embedder = embedder
        self.index = index
        self.root = Path(settings.data_dir)
        self._docs: dict[str, dict[str, dict]] = {}     # tenant → doc_id → meta
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None

    # ---- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index.ensure_collection()
        self._executor = ThreadPoolExecutor(max_workers=max(1, self.s.ingest_workers),
                                            thread_name_prefix="docrag-ingest")
        pending, ready = self._load_all()
        for tenant, doc_id in pending:
            log.info("docrag_ingest_requeued", extra={"tenant": tenant, "doc_id": doc_id})
            self._submit(self._process_safely, tenant, doc_id)
        for tenant, doc_id in ready:
            self._submit(self._reconcile_safely, tenant, doc_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self.index.close()

    def _load_all(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Load every persisted document; return (interrupted, ready) documents."""
        pending: list[tuple[str, str]] = []
        ready: list[tuple[str, str]] = []
        for tdir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for ddir in sorted(p for p in tdir.iterdir() if p.is_dir()):
                meta_path = ddir / "meta.json"
                if not meta_path.exists():
                    continue
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta["status"] in PENDING:
                    meta["status"] = "queued"
                    pending.append((tdir.name, meta["id"]))
                elif meta["status"] == "ready":
                    ready.append((tdir.name, meta["id"]))
                self._docs.setdefault(tdir.name, {})[meta["id"]] = meta
        return pending, ready

    # ---- helpers ---------------------------------------------------------------

    def _tenant(self, tenant: str) -> dict[str, dict]:
        with self._lock:
            return self._docs.setdefault(tenant, {})

    def _dir(self, tenant: str, doc_id: str) -> Path:
        return self.root / tenant / doc_id

    def _save_meta(self, tenant: str, meta: dict) -> None:
        meta["updated_at"] = _now()
        _write_json(self._dir(tenant, meta["id"]) / "meta.json", meta)

    def _submit(self, fn: Any, tenant: str, doc_id: str) -> None:
        if self._executor is None:
            raise RuntimeError("document store not started")
        self._executor.submit(fn, tenant, doc_id)

    def _read_chunks(self, tenant: str, doc_id: str) -> list[dict]:
        path = self._dir(tenant, doc_id) / "chunks.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _index_chunks(self, tenant: str, doc_id: str, chunks: list[dict]) -> None:
        dense, sparse = self.embedder.embed_passages([c["text"] for c in chunks])
        self.index.upsert_document(tenant, doc_id, chunks, dense, sparse)

    # ---- public API ------------------------------------------------------------

    def create(self, tenant: str, filename: str, data: bytes, actor: str | None,
               use_sarvam: bool) -> tuple[dict, bool]:
        """Store the upload and queue it. Returns ``(meta, created)``; an identical file
        already held by this tenant (and not failed) is returned instead — a retried
        upload never duplicates a document."""
        digest = hashlib.sha256(data).hexdigest()
        docs = self._tenant(tenant)
        with self._lock:
            for meta in docs.values():
                if meta["sha256"] == digest and meta["status"] != "failed":
                    return dict(meta), False
            doc_id = uuid.uuid4().hex
            suffix = Path(filename).suffix.lower()
            ddir = self._dir(tenant, doc_id)
            ddir.mkdir(parents=True)
            _write_atomic(ddir / f"source{suffix}", data)
            now = _now()
            meta = {
                "id": doc_id, "tenant": tenant, "filename": filename, "suffix": suffix,
                "size_bytes": len(data), "sha256": digest, "status": "queued", "error": None,
                "use_sarvam": use_sarvam, "uploaded_by": actor, "created_at": now,
                "updated_at": now, "doc_type": None, "page_count": None, "chunk_count": 0,
                "extraction_engines": [], "warnings": [], "elapsed_s": None,
            }
            self._save_meta(tenant, meta)
            docs[doc_id] = meta
        self._submit(self._process_safely, tenant, doc_id)
        log.info("docrag_document_queued", extra={"tenant": tenant, "doc_id": doc_id,
                                                  "doc_filename": filename, "bytes": len(data)})
        return dict(meta), True

    def _process_safely(self, tenant: str, doc_id: str) -> None:
        try:
            self._process(tenant, doc_id)
        except Exception as exc:  # noqa: BLE001 - recorded on the document, never lost
            log.exception("docrag_ingest_failed", extra={"tenant": tenant, "doc_id": doc_id})
            with self._lock:
                meta = self._tenant(tenant).get(doc_id)
                if meta is None:
                    return
                meta.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                self._save_meta(tenant, meta)

    def _process(self, tenant: str, doc_id: str) -> None:
        docs = self._tenant(tenant)
        with self._lock:
            meta = docs.get(doc_id)
            if meta is None:          # deleted while queued
                return
            meta["status"] = "processing"
            self._save_meta(tenant, meta)
        ddir = self._dir(tenant, doc_id)
        started = datetime.now(UTC)

        result = run_pipeline(ddir / f"source{meta['suffix']}",
                              use_sarvam_for_difficult=meta["use_sarvam"],
                              display_name=meta["filename"])
        chunks = chunks_to_dicts(result.chunks)
        for c in chunks:
            c["doc_id"] = doc_id
        dense, sparse = self.embedder.embed_passages([c["text"] for c in chunks])

        with self._lock:
            if doc_id not in docs:  # deleted mid-ingest: drop the result
                return
            _write_json(ddir / "knowledge.json", asdict(result.document))
            _write_json(ddir / "chunks.json", chunks)
            # Indexed BEFORE the document turns ready: "ready" means searchable.
            self.index.upsert_document(tenant, doc_id, chunks, dense, sparse)
            doc = result.document
            meta.update(
                status="ready", error=None, doc_type=doc.metadata.doc_type,
                page_count=doc.metadata.page_count, chunk_count=len(chunks),
                extraction_engines=doc.metadata.extraction_engines_used,
                warnings=list(doc.warnings),
                elapsed_s=round((datetime.now(UTC) - started).total_seconds(), 2),
                ocr_pages=result.telemetry.get("ocr_pages", 0),
                sarvam=result.telemetry.get("sarvam", {}),
            )
            self._save_meta(tenant, meta)
        sarvam = meta.get("sarvam") or {}
        log.info("docrag_document_ready doc=%s chunks=%d ocr_pages=%s sarvam_jobs=%s "
                 "sarvam_pages=%s seconds=%s", doc_id, len(chunks), meta.get("ocr_pages", 0),
                 sarvam.get("jobs", 0), sarvam.get("pages_submitted", 0), meta["elapsed_s"],
                 extra={"event": "docrag_document_ready",
                        "tenant": tenant, "doc_id": doc_id, "chunks": len(chunks),
                        "doc_type": meta["doc_type"], "warnings": len(meta["warnings"]),
                        "elapsed_s": meta["elapsed_s"], "ocr_pages": meta.get("ocr_pages", 0),
                        "sarvam": sarvam})

    def _reconcile_safely(self, tenant: str, doc_id: str) -> None:
        """Re-embed a ready document whose points are missing from the index."""
        try:
            meta = self._tenant(tenant).get(doc_id)
            if meta is None or meta["status"] != "ready":
                return
            if self.index.count(tenant, doc_id) == meta["chunk_count"]:
                return
            chunks = self._read_chunks(tenant, doc_id)
            self._index_chunks(tenant, doc_id, chunks)
            log.info("docrag_document_reindexed", extra={
                "tenant": tenant, "doc_id": doc_id, "chunks": len(chunks)})
        except Exception:  # noqa: BLE001 - the next start retries; the data is on disk
            log.exception("docrag_reindex_failed", extra={"tenant": tenant, "doc_id": doc_id})

    def list_documents(self, tenant: str) -> list[dict]:
        with self._lock:
            metas = [dict(m) for m in self._tenant(tenant).values()]
        return sorted(metas, key=lambda m: m["created_at"], reverse=True)

    def get(self, tenant: str, doc_id: str) -> dict | None:
        meta = self._tenant(tenant).get(doc_id)
        return dict(meta) if meta else None

    def knowledge(self, tenant: str, doc_id: str) -> dict | None:
        path = self._dir(tenant, doc_id) / "knowledge.json"
        if doc_id not in self._tenant(tenant) or not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def chunks(self, tenant: str, doc_id: str) -> list[dict] | None:
        if doc_id not in self._tenant(tenant):
            return None
        return self._read_chunks(tenant, doc_id)

    def delete(self, tenant: str, doc_id: str) -> bool:
        with self._lock:
            if self._tenant(tenant).pop(doc_id, None) is None:
                return False
            self.index.delete_document(tenant, doc_id)
            shutil.rmtree(self._dir(tenant, doc_id), ignore_errors=True)
        log.info("docrag_document_deleted", extra={"tenant": tenant, "doc_id": doc_id})
        return True

    def indexed_chunks(self, tenant: str) -> int:
        return self.index.count(tenant)

    def search(self, tenant: str, query: str, top_k: int,
               doc_ids: list[str] | None = None) -> list[RetrievedChunk]:
        dense, sparse = self.embedder.embed_query(query)
        dense_hits, sparse_hits = self.index.search(
            tenant, dense, sparse,
            top_k_dense=max(self.s.top_k_vector, top_k),
            top_k_sparse=max(self.s.top_k_bm25, top_k),
            doc_ids=doc_ids)
        return fuse(query, dense_hits, sparse_hits, top_k)
