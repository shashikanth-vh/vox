from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.register_access import REPORT_UNASSESSABLE_FILTER_FIELDS, REPORT_UNASSESSABLE_FILTER_PASSAGES
from app.semantic import (
    RetrievalModels,
    SemanticDependencyError,
    SemanticRetriever,
    _match_signals,
    _promote_focused_matches,
    _select_need_match,
    expected_manifest,
    expected_point_ids,
    load_passages,
    ontology_content_identity,
    verify_index_integrity,
    verify_model_manifest,
)
from app.semantic_index import (
    ALLOWED_SEMANTIC_FACETS,
    passage_metadata,
    passage_retrieval_text,
)
from app.stage_models import RetrievalFocus, RetrievalMatch


class Dense:
    def query_embed(self, _query):
        yield np.array([0.1, 0.2, 0.3], dtype=np.float32)


class Sparse:
    def query_embed(self, _query):
        yield SimpleNamespace(
            indices=np.array([1, 2], dtype=np.int64),
            values=np.array([0.4, 0.8], dtype=np.float32),
        )


class Reranker:
    def rerank(self, _query, documents):
        assert list(documents) == ["indexed first", "indexed second"]
        return iter([0.1, 0.9])


def _point(point_id, score, content):
    facets = {
        "a": ["relationship", "lifecycle"],
        "b": ["dimension", "metric"],
    }[point_id]
    return SimpleNamespace(
        id=point_id,
        score=score,
        payload={
            "passage_id": point_id,
            "source": "leads",
            "version": "v1",
            "content": content,
            "retrieval_text": f"indexed {content}",
            "metadata": {
                "planning": True,
                "semantic_facets": facets,
                "role": "semantic",
                "audience": ["grounding", "planning"],
            },
        },
    )


class Qdrant:
    def __init__(self):
        self.calls = []

    async def query_points(self, _collection, *, using, **_kwargs):
        self.calls.append(using)
        if using == "dense":
            return SimpleNamespace(points=[_point("a", 0.9, "first"), _point("b", 0.8, "second")])
        return SimpleNamespace(points=[_point("b", 7.0, "second"), _point("a", 6.0, "first")])

    async def close(self):
        return None


class IntegrityQdrant:
    def __init__(self, metadata, point_ids):
        self.metadata = metadata
        self.point_ids = point_ids

    async def get_collection(self, _collection):
        return SimpleNamespace(metadata=self.metadata)

    async def scroll(self, _collection, *, limit, offset, with_payload, with_vectors):
        assert limit == 1000
        assert offset is None
        assert with_payload is False
        assert with_vectors is False
        return [SimpleNamespace(id=point_id) for point_id in self.point_ids], None


def _settings(**overrides) -> Settings:
    values = {
        "dense_model": "dense",
        "dense_model_revision": "dense-rev",
        "sparse_model": "sparse",
        "sparse_model_revision": "sparse-rev",
        "rerank_model": "rerank",
        "rerank_model_revision": "rerank-rev",
        "dense_dimensions": 3,
        "rerank_limit": 2,
    }
    values.update(overrides)
    values.setdefault("grounding_rerank_limit", values.get("rerank_limit", 10))
    values.setdefault("planning_rerank_limit", values.get("rerank_limit", 10))
    return Settings(_env_file=None, **values)


def _passage(passages, parent: str, needle: str | None = None):
    candidates = [item for item in passages.values() if item["id"].startswith(parent + ".")]
    if needle is not None:
        candidates = [item for item in candidates if needle in item["content"]]
    assert candidates, (parent, needle)
    merged = dict(candidates[0])
    merged["content"] = " ".join(item["content"] for item in candidates)
    return merged


def _integrity_source():
    return {
        "version": "test-v1",
        "passages": [
            {
                "id": "semantic.one",
                "content": "A durable semantic claim.",
                "metadata": {
                    "role": "semantic",
                    "audience": ["grounding"],
                    "semantic_facets": ["governance"],
                },
            },
            {
                "id": "invariant.one",
                "content": "An invariant claim.",
                "metadata": {
                    "role": "invariant",
                    "audience": ["grounding"],
                    "semantic_facets": ["governance"],
                },
            },
        ],
    }


def _integrity_client(source, point_ids=None):
    return IntegrityQdrant(
        {
            "source_version": source["version"],
            "ontology_identity": ontology_content_identity(source),
        },
        point_ids if point_ids is not None else expected_point_ids(source),
    )


async def test_hybrid_scores_fusion_and_reranking_are_retained():
    client = Qdrant()
    retriever = SemanticRetriever(
        _settings(),
        client=client,
        models=RetrievalModels(dense=Dense(), sparse=Sparse(), reranker=Reranker()),
    )
    result = await retriever.search("active leads")

    assert [match.passage_id for match in result.matches] == ["b", "a"]
    assert result.matches[0].dense_score == 0.8
    assert result.matches[0].sparse_score == 7.0
    assert result.matches[0].rerank_score == 0.9
    assert result.matches[0].fusion_score > 0
    assert result.matches[0].origins == ["primary"]
    assert result.semantic_query_count == 1
    assert result.passage_limit == 2
    assert result.estimated_context_tokens > 0
    assert len(client.calls) == 2


async def test_index_readiness_rejects_content_edit_without_reindex():
    indexed_source = _integrity_source()
    edited_source = deepcopy(indexed_source)
    edited_source["passages"][0]["content"] = "A changed semantic claim."

    with pytest.raises(SemanticDependencyError, match="content identity differs"):
        await verify_index_integrity(
            _integrity_client(indexed_source),
            _settings(qdrant_collection_prefix="test"),
            source=edited_source,
        )


async def test_index_readiness_rejects_missing_indexed_point():
    source = _integrity_source()
    point_ids = expected_point_ids(source)
    point_ids.pop()

    with pytest.raises(SemanticDependencyError, match="points differ.*missing"):
        await verify_index_integrity(
            _integrity_client(source, point_ids),
            _settings(qdrant_collection_prefix="test"),
            source=source,
        )


async def test_index_readiness_accepts_formatting_only_ontology_rewrite():
    source = _integrity_source()
    reformatted = json.loads(json.dumps(source, indent=2, ensure_ascii=False))
    reformatted["passages"].reverse()
    reformatted["passages"][0] = {
        "metadata": reformatted["passages"][0]["metadata"],
        "content": reformatted["passages"][0]["content"],
        "id": reformatted["passages"][0]["id"],
    }

    result = await verify_index_integrity(
        _integrity_client(source),
        _settings(qdrant_collection_prefix="test"),
        source=reformatted,
    )

    assert result["identity"] == ontology_content_identity(source)
    assert result["passage_count"] == 1


async def test_focused_search_is_bounded_to_three_hybrid_queries():
    client = Qdrant()
    retriever = SemanticRetriever(
        _settings(rerank_limit=4),
        client=client,
        models=RetrievalModels(dense=Dense(), sparse=Sparse(), reranker=Reranker()),
    )

    result = await retriever.search(
        "active lending in a region",
        focus_queries=[
            RetrievalFocus(
                responsibility="relationship_dimension",
                needs=[
                    {"facet": "relationship", "query": "assigned lending"},
                    {"facet": "dimension", "query": "region lending"},
                ],
                query="region lending",
            ),
            RetrievalFocus(
                responsibility="lifecycle_metric",
                needs=[
                    {"facet": "lifecycle", "query": "active lending"},
                    {"facet": "metric", "query": "least lending"},
                ],
                query="active lending",
            ),
        ],
        focus_slots=2,
    )

    assert len(client.calls) == 6
    assert result.semantic_query_count == 3
    assert result.passage_limit == 4
    assert [focus.responsibility for focus in result.focus_queries] == [
        "relationship_dimension",
        "lifecycle_metric",
    ]
    assert {origin for match in result.matches for origin in match.origins} == {
        "primary",
        "relationship_dimension:relationship",
        "relationship_dimension:dimension",
        "lifecycle_metric:lifecycle",
        "lifecycle_metric:metric",
    }


def test_focused_recall_uses_bounded_slots_without_increasing_result_size():
    def match(passage_id: str) -> RetrievalMatch:
        return RetrievalMatch(
            passage_id=passage_id,
            source="test",
            version="v1",
            content=passage_id,
            fusion_score=0.0,
        )

    primary = [match(item) for item in ("a", "b", "c", "d", "e", "f")]
    focused = [match(item) for item in ("dimension", "lifecycle", "extra")]

    result = _promote_focused_matches(primary, focused, slots=2, limit=6)

    assert [item.passage_id for item in result] == ["a", "b", "c", "d", "dimension", "lifecycle"]
    assert len(result) == 6


def test_focused_recall_does_not_scan_past_duplicate_top_results():
    def match(passage_id: str) -> RetrievalMatch:
        return RetrievalMatch(
            passage_id=passage_id,
            source="test",
            version="v1",
            content=passage_id,
            fusion_score=0.0,
        )

    primary = [match(item) for item in ("a", "b", "c", "d", "e", "f")]
    focused = [match(item) for item in ("a", "b", "unrelated", "extra")]

    result = _promote_focused_matches(primary, focused, slots=2, limit=6)

    assert [item.passage_id for item in result] == ["a", "b", "c", "d", "e", "f"]


def test_focused_recall_merges_duplicate_origins_and_keeps_the_limit():
    def match(passage_id: str, origin: str) -> RetrievalMatch:
        return RetrievalMatch(
            passage_id=passage_id,
            source="test",
            version="v1",
            content=passage_id,
            origins=[origin],
            fusion_score=0.0,
        )

    primary = [match(item, "primary") for item in ("a", "b", "c")]
    focused = [
        match("dimension", "relationship_dimension"),
        match("dimension", "lifecycle_metric"),
    ]

    result = _promote_focused_matches(primary, focused, slots=4, limit=3)

    assert [item.passage_id for item in result] == ["a", "b", "dimension"]
    assert result[-1].origins == ["relationship_dimension", "lifecycle_metric"]


def test_need_selection_preserves_rank_for_one_explicit_facet():
    def match(
        passage_id: str,
        facets: list[str] | None = None,
    ) -> RetrievalMatch:
        return RetrievalMatch(
            passage_id=passage_id,
            source="test",
            version="v1",
            content=passage_id,
            metadata={"semantic_facets": facets or []},
            fusion_score=0.0,
        )

    selected = _select_need_match(
        [
            match("join", ["relationship"]),
            match("unrelated", ["governance"]),
            match("geography", ["relationship", "dimension"]),
        ],
        facet="dimension",
    )

    assert selected is not None
    assert selected.passage_id == "geography"


def test_focus_selection_does_not_fall_back_to_an_unrelated_facet():
    unrelated = RetrievalMatch(
        passage_id="governance",
        source="test",
        version="v1",
        content="governance",
        metadata={"semantic_facets": ["governance"]},
        fusion_score=0.0,
    )

    assert _select_need_match(
        [unrelated],
        facet="lifecycle",
    ) is None


def test_index_metadata_requires_explicit_valid_multi_facets():
    passage = {
        "id": "relationships.example",
        "metadata": {
            "planning": True,
            "semantic_facets": ["relationship", "dimension", "relationship"],
            "role": "semantic",
            "audience": ["grounding", "planning"],
        },
    }
    assert passage_metadata(passage) == {
        "planning": True,
        "semantic_facets": ["dimension", "relationship"],
        "role": "semantic",
        "audience": ["grounding", "planning"],
    }
    with pytest.raises(ValueError, match="must declare semantic_facets"):
        passage_metadata({"id": "relationships.missing"})
    with pytest.raises(ValueError, match="unsupported semantic facets"):
        passage_metadata({
            "id": "relationships.invalid",
            "metadata": {"semantic_facets": ["question_specific"]},
        })


def test_retrieval_text_uses_governed_metadata_but_not_source_paths():
    passage = {
        "id": "relationships.company_region",
        "source": "relationships",
        "content": "Company region is held on the related company master.",
        "metadata": {
            "resource": "companies",
            "field": "region",
            "semantic_facets": ["relationship", "dimension"],
            "role": "semantic",
            "audience": ["grounding"],
            "sources": ["private/source/path.py"],
        },
    }

    text = passage_retrieval_text(passage)

    assert "relationships company region" in text
    assert "Semantic facets: dimension, relationship" in text
    assert "Register resource: companies" in text
    assert "Governed field: region" in text
    assert "private/source/path.py" not in text


def test_offline_manifest_must_exist_and_match_exact_revisions(tmp_path):
    settings = _settings(model_cache_dir=str(tmp_path), model_offline=True)
    with pytest.raises(SemanticDependencyError, match="Missing model manifest"):
        verify_model_manifest(settings)

    (tmp_path / "manifest.json").write_text(json.dumps({"models": expected_manifest(settings)}))
    verify_model_manifest(settings)

    wrong = expected_manifest(settings)
    wrong["dense"]["revision"] = "floating"
    (tmp_path / "manifest.json").write_text(json.dumps({"models": wrong}))
    with pytest.raises(SemanticDependencyError, match="do not match"):
        verify_model_manifest(settings)


def test_exact_and_normalized_keyword_signals_are_retained_separately():
    assert _match_signals("Solar - EPC", "Sector value Solar - EPC is current") == (True, True)
    assert _match_signals("solar/epc", "Sector value Solar - EPC is current") == (False, True)
    assert _match_signals("wind", "Sector value Solar - EPC is current") == (False, False)


def test_versioned_ontology_defines_book_boundaries_and_ambiguity_policy():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}

    assert ontology["version"] == "ontology-v2"
    required = {
        "books.overview",
        "books.boundaries",
        "books.partner",
        "books.ambiguity",
        "books.qualifiers",
    }
    assert all(any(item["id"].startswith(parent + ".") for item in passages.values()) for parent in required)
    assert "four distinct business books" in _passage(passages, "books.overview")["content"]
    assert "not a fifth business book" in _passage(passages, "books.partner")["content"]
    assert "ask the user which book" in _passage(passages, "books.ambiguity")["content"]
    assert "throughout the same question" in _passage(passages, "books.qualifiers")["content"]
    assert "parent ticket or ask value" in _passage(passages, "resources.syndication")["content"]
    assert "null-amount population" in _passage(passages, "resources.syndication")["content"]
    lending = _passage(passages, "resources.lending")["content"]
    assert "tracker_no, stage and pending_with" in lending
    assert "lending.rm is the relationship-manager assignment" in lending
    assert "lending.analyst is the assigned analyst" in lending
    assert "canonical People.name values" in lending
    assert "not interchangeable" in lending
    assert "entities.legal_name" in _passage(passages, "resources.asset_monetisation")["content"]


def test_current_lifecycles_do_not_classify_closed_or_unknown_lenders_as_active():
    passages = {p["id"]: p for p in load_passages()["passages"]}
    activity = _passage(passages, "metrics.lender_activity")["content"]
    assert "Sanctioned and Disbursed are approved/won" in activity
    assert "Declined and Dropped are distinct terminal lost" in activity
    assert "On Hold remains open but paused" in activity
    assert "Unknown or blank statuses are unassessable" in activity
    assert "every other populated" not in activity
    lending = _passage(passages, "lifecycle.lending")["content"]
    assert "CP/CS Completed needs separate assessment" in lending
    assert "Ready for Disbursement" in passages["lifecycle.lending.sanctioned_family"]["content"]
    assert "Closed Lost and Dropped are terminal/excluded" in _passage(passages, "lifecycle.deals")["content"]


def test_ontology_defines_reusable_cross_book_and_lender_semantics():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}
    required = {
        "governance.unsupported_dimensions",
        "metrics.cross_book_clients",
        "metrics.lender_activity",
        "metrics.lender_decisions",
        "metrics.lender_approach",
        "metrics.lender_im_response",
        "relationships.entity_dimensions",
        "relationships.live_book_overlap",
    }

    assert all(any(item["id"].startswith(parent + ".") for item in passages.values()) for parent in required)
    assert "entity_id values" in _passage(passages, "relationships.live_book_overlap")["content"]
    assert "Data Awaited" in _passage(passages, "relationships.live_book_overlap")["content"]
    assert "other than Dropped" in _passage(passages, "relationships.live_book_overlap")["content"]
    assert "entities.sector" in _passage(passages, "relationships.entity_dimensions")["content"]
    assert "Lead, Deal, LendingTracker" in _passage(passages, "relationships.entity_lines")["content"]
    assert "status is not Converted" in _passage(passages, "resources.leads")["content"]
    assert "preserve every tie" in _passage(passages, "metrics.lender_activity")["content"]
    assert "is_existing=false" in _passage(passages, "metrics.lender_activity")["content"]
    assert "status=Sanctioned" in _passage(passages, "metrics.lender_decisions")["content"]
    assert "minus the distinct recorded lender_name" in _passage(
        passages, "metrics.lender_approach"
    )["content"]
    assert "response_date is null" in _passage(passages, "metrics.lender_im_response")["content"]
    assert "UAT" not in " ".join(_passage(passages, item)["content"] for item in required)

    repository_root = Path(__file__).resolve().parents[3]
    for passage_id in required:
        sources = _passage(passages, passage_id)["metadata"]["sources"]
        assert sources
        for source in sources:
            assert (repository_root / source).is_file(), (passage_id, source)


def test_ontology_governs_qualitative_and_mixed_evidence_semantics():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}

    lending = _passage(passages, "qualitative.lending_remarks")
    assert lending["metadata"]["qualitative"] is True
    assert lending["metadata"]["field"] == "remarks"
    assert "recorded explanation" in lending["content"]
    assert "not independently verified facts" in lending["content"]
    assert "host code" in lending["content"]

    mandate = _passage(passages, "semantics.syndication_mandate")["content"]
    assert "composite recorded value" in mandate
    assert "invented Executed status" in mandate
    assert "SyndicationLender rows" in mandate

    auxiliary_passage = _passage(passages, "governance.auxiliary_and_bottleneck_evidence")
    auxiliary = auxiliary_passage["content"]
    unavailable = _passage(passages, "governance.unavailable_business_criterion")["content"]
    assert "never treated as passed, failed, clear, or zero" in unavailable
    assert "qualifies" in unavailable
    assert "elapsed-time ordering is an unavailable auxiliary measure" in auxiliary

    scope = _passage(passages, "governance.caller_visible_scope")
    assert "do not assert" in scope["content"]


def test_grounding_semantics_define_roles_grain_and_live_value_matching_generically():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}

    assert "generic word deal alone does not select" in _passage(passages, "resources.deals")["content"]
    assert "relationship manager responsible" in _passage(passages, "resources.deals")["content"]
    assert "each named book's own record" in _passage(passages, "books.overview")["content"]
    assert "Deal.rm" in _passage(passages, "metrics.cross_book_clients")["content"]
    assert "each tracker's analyst field" in _passage(passages, "metrics.cross_book_clients")["content"]
    assert "does not itself select a business book" in _passage(
        passages, "metrics.cross_book_clients"
    )["content"]
    assert "explicitly named lender" in _passage(passages, "metrics.lender_decisions")["content"]
    assert "do not introduce own-book" in _passage(passages, "metrics.lender_decisions")["content"]
    assert "Sector belongs to Entity" in _passage(passages, "terms.solar")["content"]
    assert "through its related Entity" in _passage(passages, "terms.solar")["content"]
    assert "caller-visible Register Sector reference values" in _passage(passages, "terms.solar")["content"]

    parent_status = passages["lifecycle.syndication.syndication_status_distinct"]
    assert "SyndicationTracker.status" in parent_status["content"]
    assert parent_status["metadata"]["resource"] == "syndication"
    assert parent_status["metadata"]["field"] == "status"
    overlap_status = passages["relationships.live_book_overlap.live_parent_syndication"]
    assert "belongs specifically to SyndicationTracker.status" in overlap_status["content"]
    assert overlap_status["metadata"]["resource"] == "syndication"
    assert overlap_status["metadata"]["field"] == "status"

    lender_status = _passage(passages, "lifecycle.syndication_lenders")
    assert "SyndicationLender.status" in lender_status["content"]
    decisions = passages["metrics.lender_activity.sanctioned_approved_won"]["content"]
    assert "SyndicationTracker.status must not substitute" in decisions

    entity_names = passages["resources.entities.canonical_names"]
    assert "no authoritative promoter-person field" in entity_names["content"]
    assert "must not be joined to People" in entity_names["content"]
    assert entity_names["metadata"]["relationship"] is True
    assert entity_names["metadata"]["governance"] is True

    conversion = passages["resources.leads.unconverted_lead_pre"]["content"]
    assert "not by itself a lead-to-deal conversion-rate denominator" in conversion
    assert "derived rate is unavailable" in conversion

    for passage in ontology["passages"]:
        facets = set(passage["metadata"]["semantic_facets"])
        assert facets
        assert facets <= ALLOWED_SEMANTIC_FACETS
        assert passage_retrieval_text(passage).endswith(passage["content"])

    runtime_ontology = json.dumps(ontology, ensure_ascii=False).casefold()
    for evaluation_specific_term in (
        "maharashtra",
        "axis bank",
        "zeon",
        "shubh",
        "prateek",
        "q05",
        "q12",
        "q20",
        "q29",
    ):
        assert evaluation_specific_term not in runtime_ontology


def test_missing_value_passage_mapping_is_bidirectionally_governed():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}
    assert set(REPORT_UNASSESSABLE_FILTER_PASSAGES) == set(REPORT_UNASSESSABLE_FILTER_FIELDS)
    for pair, passage_ids in REPORT_UNASSESSABLE_FILTER_PASSAGES.items():
        assert passage_ids
        for passage_id in passage_ids:
            assert passage_id in passages
            content = passages[passage_id]["content"]
            resource, field = pair
            assert f"{resource}.{field}" in content
            assert "unassessable" in content


def test_claims_manifest_covers_each_current_passage_and_baseline_source():
    ontology = load_passages()
    passages = {passage["id"]: passage for passage in ontology["passages"]}
    manifest = ontology["claims_manifest"]
    assert manifest["source_version"] == "ontology-v1"
    manifest_ids = set()
    for entries in manifest["parents"].values():
        for entry in entries:
            passage_id = entry["child_id"]
            assert passage_id in passages
            manifest_ids.add(passage_id)
            for claim in entry["claims"]:
                assert claim["text"] in passages[passage_id]["content"]
                assert claim["source_id"]
                assert claim["claim_key"]
    assert manifest_ids == set(passages)

    baseline = json.loads(
        (Path(__file__).parent / "fixtures" / "ontology-v1-passages.json").read_text()
    )
    assert baseline["version"] == "ontology-v1"
    assert baseline["source_commit"] == "b8e027c"
    baseline_content = {item["id"]: item["content"] for item in baseline["passages"]}
    for entries in manifest["parents"].values():
        for entry in entries:
            for claim in entry["claims"]:
                if claim.get("source_text"):
                    origin_parent = entry["origin_parent"]
                    assert origin_parent in baseline_content, (
                        f"Claim {claim['claim_key']} declares source_text but origin_parent "
                        f"{origin_parent!r} is not a ontology-v1 parent."
                    )
                    assert claim["source_text"] in baseline_content.get(origin_parent, "")
    current_text = " ".join(item["content"] for item in ontology["passages"])
    removals = {
        (item["source_parent"], item["source_text"])
        for item in manifest["removals"]
    }
    reworded_sources = {
        claim["source_text"]
        for entries in manifest["parents"].values()
        for entry in entries
        for claim in entry["claims"]
        if claim.get("source_text")
    }
    unresolved = []
    for passage in baseline["passages"]:
        sentences = re.split(r"(?<=[.!?])\s+", passage["content"].strip())
        for sentence in sentences:
            if (
                sentence
                and sentence not in current_text
                and (passage["id"], sentence) not in removals
                and sentence not in reworded_sources
            ):
                unresolved.append(f"{passage['id']}: {sentence}")
    assert unresolved == []
