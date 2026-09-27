"""The Qdrant index: one collection, every tenant, dense + sparse vectors per chunk.

Multi-tenancy uses Qdrant's recommended pattern — a single collection with a
tenant-partitioned payload index (``is_tenant``) — and EVERY read, count and delete here
carries a tenant filter; there is no code path that queries across tenants.

The index is derived data. The canonical documents and their chunks live on the DocRAG
volume; point ids are deterministic (tenant, document, chunk), so re-indexing a document
overwrites its points instead of duplicating them, and a lost or new collection is rebuilt
from the saved chunks at startup (see ``DocumentStore.reconcile``).

The collection name carries a hash of the embedding models + revisions: changing a model
starts a NEW collection that is rebuilt from the chunks, instead of mixing vectors from two
models in one space.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from contextlib import AbstractContextManager, nullcontext

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import Settings
from app.rag.embeddings import Sparse

DENSE = "dense"
SPARSE = "sparse"
_UPSERT_BATCH = 128


def collection_name(settings: Settings) -> str:
    identity = "|".join([settings.dense_model, settings.dense_model_revision,
                         str(settings.dense_dimensions),
                         settings.sparse_model, settings.sparse_model_revision])
    return f"{settings.qdrant_collection_prefix}_chunks_{hashlib.sha256(identity.encode()).hexdigest()[:10]}"


def point_id(tenant: str, doc_id: str, chunk_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"docrag:{tenant}:{doc_id}:{chunk_id}"))


def _sparse(s: Sparse) -> qm.SparseVector:
    return qm.SparseVector(indices=s.indices, values=s.values)


def _scope(tenant: str, doc_ids: list[str] | None = None) -> qm.Filter:
    must: list[qm.Condition] = [qm.FieldCondition(key="tenant", match=qm.MatchValue(value=tenant))]
    if doc_ids:
        must.append(qm.FieldCondition(key="doc_id", match=qm.MatchAny(any=list(doc_ids))))
    return qm.Filter(must=must)


def _chunk(payload: dict | None) -> dict:
    chunk = dict(payload or {})
    chunk.pop("tenant", None)
    return chunk


class VectorIndex:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.name = collection_name(settings)
        self.in_memory = settings.qdrant_url == ":memory:"
        self.client = (QdrantClient(location=":memory:") if self.in_memory else
                       QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None,
                                    timeout=int(settings.qdrant_timeout_seconds)))
        # The in-process engine is not thread-safe; a server client is.
        self._lock: AbstractContextManager = threading.Lock() if self.in_memory else nullcontext()

    def ensure_collection(self) -> None:
        with self._lock:
            if self.client.collection_exists(self.name):
                return
            self.client.create_collection(
                self.name,
                vectors_config={DENSE: qm.VectorParams(size=self.s.dense_dimensions,
                                                       distance=qm.Distance.COSINE)},
                sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
                metadata={"dense_model": self.s.dense_model,
                          "dense_model_revision": self.s.dense_model_revision,
                          "sparse_model": self.s.sparse_model,
                          "sparse_model_revision": self.s.sparse_model_revision},
            )
            if not self.in_memory:  # payload indexes are a server feature
                self.client.create_payload_index(
                    self.name, "tenant",
                    qm.KeywordIndexParams(type=qm.KeywordIndexType.KEYWORD, is_tenant=True))
                self.client.create_payload_index(self.name, "doc_id",
                                                 qm.PayloadSchemaType.KEYWORD)

    def ping(self) -> None:
        with self._lock:
            self.client.get_collection(self.name)

    def upsert_document(self, tenant: str, doc_id: str, chunks: list[dict],
                        dense: np.ndarray, sparse: list[Sparse]) -> None:
        points = [
            qm.PointStruct(
                id=point_id(tenant, doc_id, c["chunk_id"]),
                vector={DENSE: d.tolist(), SPARSE: _sparse(s)},
                payload={**c, "tenant": tenant, "doc_id": doc_id},
            )
            for c, d, s in zip(chunks, dense, sparse, strict=True)
        ]
        with self._lock:
            # A re-index may have FEWER chunks than before: clear the document first.
            self.client.delete(self.name, points_selector=qm.FilterSelector(
                filter=_scope(tenant, [doc_id])), wait=True)
            for i in range(0, len(points), _UPSERT_BATCH):
                self.client.upsert(self.name, points=points[i:i + _UPSERT_BATCH], wait=True)

    def delete_document(self, tenant: str, doc_id: str) -> None:
        with self._lock:
            self.client.delete(self.name, points_selector=qm.FilterSelector(
                filter=_scope(tenant, [doc_id])), wait=True)

    def count(self, tenant: str, doc_id: str | None = None) -> int:
        with self._lock:
            return self.client.count(self.name, count_filter=_scope(
                tenant, [doc_id] if doc_id else None), exact=True).count

    def search(self, tenant: str, dense: np.ndarray, sparse: Sparse, *, top_k_dense: int,
               top_k_sparse: int, doc_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
        """Dense and sparse candidates, each in rank order (payload = the chunk)."""
        scope = _scope(tenant, doc_ids)
        with self._lock:
            dense_hits = self.client.query_points(
                self.name, query=dense.tolist(), using=DENSE, query_filter=scope,
                limit=top_k_dense, with_payload=True).points
            sparse_hits = (self.client.query_points(
                self.name, query=_sparse(sparse), using=SPARSE, query_filter=scope,
                limit=top_k_sparse, with_payload=True).points if sparse.indices else [])
        return [_chunk(p.payload) for p in dense_hits], [_chunk(p.payload) for p in sparse_hits]

    def close(self) -> None:
        self.client.close()
