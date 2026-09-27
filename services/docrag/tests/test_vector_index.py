"""The Qdrant layer on its own: tenant isolation at the index, re-index hygiene, and the
model identity baked into the collection name."""

from __future__ import annotations

from app.config import Settings
from app.rag.embeddings import StubEmbedder
from app.rag.vector_index import VectorIndex, collection_name


def _index() -> tuple[VectorIndex, StubEmbedder]:
    idx = VectorIndex(Settings(qdrant_url=":memory:", embedder="stub"))
    idx.ensure_collection()
    return idx, StubEmbedder()


def _chunks(doc_id: str, texts: list[str]) -> list[dict]:
    return [{"doc_id": doc_id, "chunk_id": i, "doc": f"{doc_id}.pdf", "text": t,
             "section_path": "s", "pages": [1], "entities": []} for i, t in enumerate(texts)]


def _put(idx: VectorIndex, emb: StubEmbedder, tenant: str, doc_id: str, texts: list[str]) -> None:
    chunks = _chunks(doc_id, texts)
    dense, sparse = emb.embed_passages(texts)
    idx.upsert_document(tenant, doc_id, chunks, dense, sparse)


def test_every_read_is_tenant_scoped():
    idx, emb = _index()
    _put(idx, emb, "ALPHA", "a1", ["tenor 36 months"])
    _put(idx, emb, "BETA", "b1", ["tenor 36 months"])
    dense, sparse = emb.embed_query("tenor")
    d, s = idx.search("ALPHA", dense, sparse, top_k_dense=10, top_k_sparse=10)
    assert {c["doc_id"] for c in d + s} == {"a1"}
    assert all("tenant" not in c for c in d + s)       # payload plumbing never leaks out
    assert idx.count("ALPHA") == 1 and idx.count("BETA") == 1
    # A doc id from ANOTHER tenant is not reachable by naming it.
    d, s = idx.search("ALPHA", dense, sparse, top_k_dense=10, top_k_sparse=10, doc_ids=["b1"])
    assert d == [] and s == []


def test_reindex_replaces_points_instead_of_accumulating():
    idx, emb = _index()
    _put(idx, emb, "T", "d", ["one", "two", "three"])
    _put(idx, emb, "T", "d", ["one"])                   # fewer chunks the second time
    assert idx.count("T", "d") == 1
    idx.delete_document("T", "d")
    assert idx.count("T") == 0


def test_model_change_means_a_new_collection():
    base = Settings(qdrant_url=":memory:")
    moved = Settings(qdrant_url=":memory:", dense_model_revision="0" * 40)
    assert collection_name(base) != collection_name(moved)
    assert collection_name(base) == collection_name(Settings(qdrant_url=":memory:"))
