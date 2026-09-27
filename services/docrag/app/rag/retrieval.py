"""Hybrid retrieval: dense (semantic) and sparse (BM25) candidates from Qdrant, fused
by reciprocal rank fusion, with an entity-match boost for exact-value queries (PAN
numbers, amounts, dates) that the chunker already tagged per chunk.

The fusion happens here rather than inside Qdrant so each result keeps its rank in BOTH
lists — the API reports them, which is what makes a surprising ranking explainable.
"""

from __future__ import annotations

from dataclasses import dataclass

RRF_K = 60  # standard reciprocal-rank-fusion constant


@dataclass
class RetrievedChunk:
    chunk: dict
    fused_score: float
    vector_rank: int | None
    bm25_rank: int | None


def _entity_boost(query: str, chunk: dict) -> float:
    query_lower = query.lower()
    boost = 0.0
    for entity in chunk.get("entities", []):
        value = entity.split(":", 1)[-1]
        if value and value.lower() in query_lower:
            boost += 0.05
    return boost


def _chunk_key(c: dict) -> tuple:
    return (c["doc_id"], c["chunk_id"])


def fuse(query: str, dense_hits: list[dict], sparse_hits: list[dict],
         top_k_final: int) -> list[RetrievedChunk]:
    vector_rank = {_chunk_key(c): i for i, c in enumerate(dense_hits)}
    bm25_rank = {_chunk_key(c): i for i, c in enumerate(sparse_hits)}

    chunk_by_key = {_chunk_key(c): c for c in dense_hits}
    chunk_by_key.update({_chunk_key(c): c for c in sparse_hits})

    scored: list[RetrievedChunk] = []
    for key, chunk in chunk_by_key.items():
        rrf_score = 0.0
        if key in vector_rank:
            rrf_score += 1.0 / (RRF_K + vector_rank[key] + 1)
        if key in bm25_rank:
            rrf_score += 1.0 / (RRF_K + bm25_rank[key] + 1)
        rrf_score += _entity_boost(query, chunk)
        scored.append(RetrievedChunk(chunk=chunk, fused_score=rrf_score,
                                     vector_rank=vector_rank.get(key),
                                     bm25_rank=bm25_rank.get(key)))

    scored.sort(key=lambda r: -r.fused_score)
    return scored[:top_k_final]
