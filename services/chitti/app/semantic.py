"""FastEmbed hybrid retrieval, reciprocal-rank fusion, and cross-encoder reranking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastembed import SparseTextEmbedding, TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, SparseVector

from app.config import Settings
from app.contracts import AUDIENCE_GROUNDING, AUDIENCE_PLANNING, ROLE_INVARIANT, ROLE_SEMANTIC
from app.stage_models import RetrievalFocus, RetrievalMatch, SemanticRetrievalResult

PASSAGES_FILE = Path(__file__).parent / "ontology" / "passages.json"
MANIFEST_FILE = "manifest.json"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
FOCUS_MATCHES_PER_QUERY = 2


class SemanticDependencyError(RuntimeError):
    pass


@dataclass(slots=True)
class RetrievalModels:
    dense: Any
    sparse: Any
    reranker: Any


def collection_name(settings: Settings) -> str:
    version = re.sub(r"[^a-zA-Z0-9_]+", "_", str(load_passages()["version"]))
    return f"{settings.qdrant_collection_prefix}_ontology_{version}"


@lru_cache(maxsize=1)
def load_passages() -> dict[str, Any]:
    return json.loads(PASSAGES_FILE.read_text())


def ontology_content_identity(source: dict[str, Any] | None = None) -> str:
    """Return a stable identity for meaning-bearing ontology content.

    JSON formatting and object key order are intentionally excluded.  The identity
    covers every field that can affect retrieval or passage interpretation.
    """
    source = source or load_passages()
    passages = []
    for passage in source.get("passages", []):
        metadata = dict(passage.get("metadata") or {})
        passages.append({
            "id": passage["id"],
            "content": passage["content"],
            "role": metadata.get("role"),
            "audience": sorted(metadata.get("audience") or []),
            "metadata": {
                key: value for key, value in sorted(metadata.items())
                if key not in {"sources"}
            },
        })
    canonical = json.dumps(
        {"version": source.get("version"), "passages": sorted(passages, key=lambda item: item["id"])},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def expected_point_ids(source: dict[str, Any] | None = None) -> set[str]:
    source = source or load_passages()
    version = str(source["version"])
    return {
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"chitti:{version}:{passage['id']}"))
        for passage in source.get("passages", [])
        if (passage.get("metadata") or {}).get("role") != ROLE_INVARIANT
    }


def collection_metadata(info: Any) -> dict[str, Any]:
    """Read Qdrant collection metadata across client response versions."""
    direct = getattr(info, "metadata", None)
    if isinstance(direct, dict):
        return direct
    config = getattr(info, "config", None)
    nested = getattr(config, "metadata", None)
    return nested if isinstance(nested, dict) else {}


async def verify_index_integrity(
    client: AsyncQdrantClient,
    settings: Settings,
    *,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require that the served collection was built from exactly this ontology."""
    source = source or load_passages()
    info = await client.get_collection(collection_name(settings))
    metadata = collection_metadata(info)
    expected_identity = ontology_content_identity(source)
    if metadata.get("source_version") != source.get("version"):
        raise SemanticDependencyError(
            "Ontology index is stale: source version differs "
            f"(index={metadata.get('source_version')!r}, file={source.get('version')!r})."
        )
    if metadata.get("ontology_identity") != expected_identity:
        raise SemanticDependencyError(
            "Ontology index is stale: content identity differs "
            f"(index={metadata.get('ontology_identity')!r}, file={expected_identity!r})."
        )
    indexed: set[str] = set()
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name(settings),
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        indexed.update(str(point.id) for point in points)
        if offset is None:
            break
    expected = expected_point_ids(source)
    if indexed != expected:
        missing = sorted(expected - indexed)
        extra = sorted(indexed - expected)
        raise SemanticDependencyError(
            "Ontology index points differ from the served ontology "
            f"(missing={missing[:3]}, extra={extra[:3]})."
        )
    return {
        "version": source["version"],
        "passage_count": len(expected),
        "identity": expected_identity,
    }


def expected_manifest(settings: Settings) -> dict[str, dict[str, str]]:
    return {
        "dense": {"model": settings.dense_model, "revision": settings.dense_model_revision},
        "sparse": {"model": settings.sparse_model, "revision": settings.sparse_model_revision},
        "reranker": {"model": settings.rerank_model, "revision": settings.rerank_model_revision},
    }


def verify_model_manifest(settings: Settings) -> None:
    path = Path(settings.model_cache_dir) / MANIFEST_FILE
    if not path.is_file():
        raise SemanticDependencyError(f"Missing model manifest at {path}.")
    try:
        actual = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SemanticDependencyError(f"Invalid model manifest at {path}: {exc}") from exc
    expected = expected_manifest(settings)
    if actual.get("models") != expected:
        raise SemanticDependencyError(
            "Cached model ids/revisions do not match configured retrieval models."
        )


def load_models(settings: Settings) -> RetrievalModels:
    if settings.model_offline:
        verify_model_manifest(settings)
    return RetrievalModels(
        dense=TextEmbedding(
            settings.dense_model,
            cache_dir=settings.model_cache_dir,
            local_files_only=settings.model_offline,
        ),
        sparse=SparseTextEmbedding(
            settings.sparse_model,
            cache_dir=settings.model_cache_dir,
            local_files_only=settings.model_offline,
        ),
        reranker=TextCrossEncoder(
            settings.rerank_model,
            cache_dir=settings.model_cache_dir,
            local_files_only=settings.model_offline,
        ),
    )


class SemanticRetriever:
    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncQdrantClient | None = None,
        models: RetrievalModels | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=math.ceil(settings.register_timeout_seconds),
        )
        self.models = models or load_models(settings)

    async def close(self) -> None:
        await self.client.close()

    async def search(
        self,
        query: str,
        *,
        focus_queries: list[RetrievalFocus] | None = None,
        focus_slots: int = 0,
        audience: str = AUDIENCE_GROUNDING,
    ) -> SemanticRetrievalResult:
        limit = self._limit_for(audience)
        candidate_pool = await self._hybrid_candidates(query)
        primary = await self._rerank_matches(
            query,
            _filter_audience_candidates(candidate_pool, audience),
            origin="primary",
            limit=limit,
        )
        planning_matches = []
        if audience == AUDIENCE_GROUNDING:
            planning_matches = await self._rerank_matches(
                query,
                _filter_audience_candidates(candidate_pool, AUDIENCE_PLANNING),
                origin="planning_shared_pool",
                limit=self._limit_for(AUDIENCE_PLANNING),
            )
        if not focus_queries or focus_slots <= 0:
            return _retrieval_result(
                query,
                primary,
                focus_queries=[],
                passage_limit=limit,
                planning_matches=planning_matches,
            )

        focused_groups = await asyncio.gather(
            *(
                self._search_focused_matches(focus, audience=audience, limit=limit)
                for focus in focus_queries[:focus_slots]
            )
        )
        focused = [
            group[index]
            for index in range(FOCUS_MATCHES_PER_QUERY)
            for group in focused_groups
            if index < len(group)
        ]
        matches = _promote_focused_matches(
            primary,
            focused,
            slots=focus_slots * FOCUS_MATCHES_PER_QUERY,
            limit=limit,
        )
        return _retrieval_result(
            query,
            matches,
            focus_queries=focus_queries[:focus_slots],
            passage_limit=limit,
            planning_matches=planning_matches,
        )

    def _limit_for(self, audience: str) -> int:
        if audience == AUDIENCE_PLANNING and self.settings.planning_rerank_limit:
            return self.settings.planning_rerank_limit
        if audience == AUDIENCE_GROUNDING and self.settings.grounding_rerank_limit:
            return self.settings.grounding_rerank_limit
        return self.settings.rerank_limit

    async def _search_focused_matches(
        self,
        focus: RetrievalFocus,
        *,
        audience: str,
        limit: int,
    ) -> list[RetrievalMatch]:
        """Rerank one shared focus pool independently for every surface need."""
        candidates = await self._hybrid_candidates(focus.query, audience=audience)
        selected: list[RetrievalMatch] = []
        for need in focus.needs[:FOCUS_MATCHES_PER_QUERY]:
            matches = await self._rerank_matches(
                need.query,
                candidates,
                origin=f"{focus.responsibility}:{need.facet}",
                limit=self.settings.fused_candidates,
            )
            match = _select_need_match(matches, facet=need.facet)
            if match is not None:
                selected.append(match)
        return selected

    async def _hybrid_candidates(self, query: str, *, audience: str | None = None) -> list[dict[str, Any]]:
        dense_vector, sparse_vector = await asyncio.gather(
            asyncio.to_thread(lambda: list(self.models.dense.query_embed(query))[0]),
            asyncio.to_thread(lambda: list(self.models.sparse.query_embed(query))[0]),
        )
        role_filter: list[Any] = [
            FieldCondition(key="metadata.role", match=MatchValue(value=ROLE_SEMANTIC))
        ]
        if audience is not None:
            role_filter.append(FieldCondition(key="metadata.audience", match=MatchValue(value=audience)))
        query_filter = Filter(must=role_filter)
        dense_response, sparse_response = await asyncio.gather(
            self.client.query_points(
                collection_name(self.settings),
                query=dense_vector.tolist(),
                using=DENSE_VECTOR,
                limit=self.settings.dense_candidates,
                with_payload=True,
                query_filter=query_filter,
            ),
            self.client.query_points(
                collection_name(self.settings),
                query=SparseVector(
                    indices=sparse_vector.indices.tolist(),
                    values=sparse_vector.values.tolist(),
                ),
                using=SPARSE_VECTOR,
                limit=self.settings.sparse_candidates,
                with_payload=True,
                query_filter=query_filter,
            ),
        )
        fused = _fuse(dense_response.points, sparse_response.points)
        return fused[: self.settings.fused_candidates]

    async def _rerank_matches(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        *,
        origin: str,
        limit: int,
    ) -> list[RetrievalMatch]:
        ranked = [dict(item) for item in candidates]
        documents = [
            str(
                item["payload"].get("retrieval_text")
                or item["payload"].get("content")
                or ""
            )
            for item in ranked
        ]
        rerank_scores = await asyncio.to_thread(
            lambda: list(self.models.reranker.rerank(query, documents))
        )
        for item, score in zip(ranked, rerank_scores, strict=True):
            item["rerank_score"] = float(score)
            content = str(item["payload"].get("content") or "")
            item["exact_match"], item["normalized_match"] = _match_signals(query, content)
        ranked.sort(
            key=lambda item: (
                item["exact_match"], item["normalized_match"], item["rerank_score"]
            ),
            reverse=True,
        )
        matches = []
        for item in ranked[:limit]:
            payload = item["payload"]
            matches.append(RetrievalMatch(
                passage_id=str(payload["passage_id"]),
                source=str(payload["source"]),
                version=str(payload["version"]),
                content=str(payload["content"]),
                metadata=dict(payload.get("metadata") or {}),
                dense_score=item.get("dense_score"),
                sparse_score=item.get("sparse_score"),
                fusion_score=item["fusion_score"],
                rerank_score=item["rerank_score"],
                exact_match=item["exact_match"],
                normalized_match=item["normalized_match"],
                origins=[origin],
            ))
        return matches


def _retrieval_result(
    query: str,
    matches: list[RetrievalMatch],
    *,
    focus_queries: list[RetrievalFocus],
    passage_limit: int,
    planning_matches: list[RetrievalMatch] | None = None,
) -> SemanticRetrievalResult:
    estimated_tokens = sum(max(1, len(match.content) // 4) for match in matches)
    return SemanticRetrievalResult(
        query=query,
        matches=matches,
        planning_matches=planning_matches or [],
        focus_queries=focus_queries,
        semantic_query_count=1 + len(focus_queries),
        passage_limit=passage_limit,
        estimated_context_tokens=estimated_tokens,
    )


def _filter_audience_candidates(
    candidates: list[dict[str, Any]], audience: str
) -> list[dict[str, Any]]:
    """Apply the audience and role boundary before cross-encoder ranking."""
    return [
        candidate for candidate in candidates
        if audience in (candidate.get("payload", {}).get("metadata", {}).get("audience") or [])
        and candidate.get("payload", {}).get("metadata", {}).get("role") == ROLE_SEMANTIC
    ]


def _select_need_match(
    matches: list[RetrievalMatch],
    *,
    facet: str,
) -> RetrievalMatch | None:
    """Return the highest-ranked passage that explicitly serves one surface need."""
    return next(
        (
            match
            for match in matches
            if facet in (match.metadata.get("semantic_facets") or [])
        ),
        None,
    )


def _promote_focused_matches(
    primary: list[RetrievalMatch],
    focused: list[RetrievalMatch],
    *,
    slots: int,
    limit: int,
) -> list[RetrievalMatch]:
    """Reserve bounded result slots for passages recalled by a narrower surface facet."""
    selected = list(primary[:limit])
    selected_indexes = {match.passage_id: index for index, match in enumerate(selected)}
    promoted: list[RetrievalMatch] = []
    promoted_indexes: dict[str, int] = {}
    for match in focused[: min(slots, limit)]:
        if match.passage_id in selected_indexes:
            index = selected_indexes[match.passage_id]
            selected[index] = selected[index].model_copy(update={
                "origins": list(dict.fromkeys([
                    *selected[index].origins,
                    *match.origins,
                ])),
            })
            continue
        if match.passage_id in promoted_indexes:
            index = promoted_indexes[match.passage_id]
            promoted[index] = promoted[index].model_copy(update={
                "origins": list(dict.fromkeys([
                    *promoted[index].origins,
                    *match.origins,
                ])),
            })
            continue
        promoted_indexes[match.passage_id] = len(promoted)
        promoted.append(match)
    if not promoted:
        return selected
    retained = selected[: max(0, limit - len(promoted))]
    return [*retained, *promoted]


def _fuse(dense: list[Any], sparse: list[Any], *, rank_constant: int = 60) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for channel, points in (("dense", dense), ("sparse", sparse)):
        for rank, point in enumerate(points, start=1):
            key = str(point.id)
            item = by_id.setdefault(key, {
                "id": key,
                "payload": dict(point.payload or {}),
                "fusion_score": 0.0,
            })
            item[f"{channel}_score"] = float(point.score)
            item["fusion_score"] += 1.0 / (rank_constant + rank)
    return sorted(by_id.values(), key=lambda item: (-item["fusion_score"], item["id"]))


def _match_signals(query: str, content: str) -> tuple[bool, bool]:
    exact_query = " ".join(query.casefold().split())
    exact_content = " ".join(content.casefold().split())
    exact = bool(exact_query) and exact_query in exact_content
    normalized_query = re.sub(r"[^a-z0-9]+", " ", exact_query).strip()
    normalized_content = re.sub(r"[^a-z0-9]+", " ", exact_content).strip()
    normalized = bool(normalized_query) and normalized_query in normalized_content
    return exact, normalized
