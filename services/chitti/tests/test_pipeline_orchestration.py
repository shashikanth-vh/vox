from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.evidence import CanonicalRecord, Completeness, RegisterEvidence, RetrievalWindow
from app.executor import ExecutionResult, PlanExecutor
from app.identity import CallerIdentity
from app.model_stages import ModelCallUsage, _usage_sink
from app.pipeline import (
    ChittiPipeline,
    PipelineStageError,
    _aggregate_usage,
    _focused_retrieval_queries,
    _reference_values,
    _stage_progress,
    _stage_progress_description,
)
from app.register_access import RegisterAccess
from app.stage_models import (
    AnswerabilityResult,
    ConversationResolution,
    QueryPlan,
    QuestionInterpretation,
    RetrievalMatch,
    SemanticRetrievalResult,
    StageName,
    StageRecord,
    ValueGroundingResult,
    VerifiedQuestion,
)

IDENTITY = CallerIdentity(
    tenant="EVAM",
    email="admin@evamfinance.com",
    user_id="admin",
    roles=("Admin", "Management"),
    posture="local_debug",
)


def test_mixed_model_usage_is_partial_and_missing_counts_stay_nullable():
    aggregate = _aggregate_usage(
        [
            ModelCallUsage(
                repair=False,
                response_received=True,
                prompt_tokens=10,
                completion_tokens=4,
                total_tokens=14,
            ),
            ModelCallUsage(repair=False, response_received=True),
        ]
    )

    assert aggregate is not None
    assert aggregate.measurement == "partial"
    assert aggregate.attempted_calls == 2
    assert aggregate.responses_missing_usage == 1
    assert aggregate.prompt_tokens is None
    assert aggregate.completion_tokens is None
    assert aggregate.cached_prompt_tokens is None


def test_grounding_usage_records_measured_prompt_and_cached_tokens():
    aggregate = _aggregate_usage(
        [
            ModelCallUsage(
                repair=False,
                response_received=True,
                prompt_tokens=2400,
                completion_tokens=120,
                total_tokens=2520,
                cached_prompt_tokens=1800,
                provider="test-provider",
            )
        ]
    )

    assert aggregate is not None
    assert aggregate.measurement == "measured"
    assert aggregate.prompt_tokens == 2400
    assert aggregate.cached_prompt_tokens == 1800
    assert aggregate.repair_calls == 0


def test_model_validation_errors_are_preserved_in_stage_usage():
    aggregate = _aggregate_usage(
        [
            ModelCallUsage(
                repair=False,
                response_received=True,
                validation_errors=[
                    {"type": "missing", "loc": ["verified_question", "resources"]}
                ],
            ),
            ModelCallUsage(
                repair=True,
                response_received=True,
                validation_errors=[{"type": "value_error", "msg": "still invalid"}],
            ),
        ]
    )

    assert aggregate is not None
    assert aggregate.repair_calls == 1
    assert aggregate.validation_errors == [
        {
            "attempt": 1,
            "repair": False,
            "errors": [
                {"type": "missing", "loc": ["verified_question", "resources"]}
            ],
        },
        {
            "attempt": 2,
            "repair": True,
            "errors": [{"type": "value_error", "msg": "still invalid"}],
        },
    ]


def test_retrieval_trace_capture_is_opt_in_and_preserves_runtime_inputs():
    settings = _settings().model_copy(update={"capture_retrieval_trace": True})
    pipeline = ChittiPipeline(settings, Stages(), retriever=None, access=RegisterAccess(settings))
    now = datetime.now(UTC)
    records = [
        StageRecord(
            stage=StageName.INTERPRETATION,
            status="completed",
            started_at=now,
            input={"resolution": "recorded"},
            output={"standalone_question": "active lending"},
        ),
        StageRecord(
            stage=StageName.RETRIEVAL,
            status="completed",
            started_at=now,
            input={"query": "active lending", "focus_queries": []},
            output={"matches": [], "focus_queries": []},
        ),
    ]

    metadata = pipeline._metadata(
        records,
        IDENTITY,
        "trace-request",
        "ANSWERED",
        Completeness.COMPLETE,
    )

    assert metadata["retrieval_trace"] == {
        "question_interpretation": {
            "input": {"resolution": "recorded"},
            "output": {"standalone_question": "active lending"},
        },
        "semantic_retrieval": {
            "input": {"query": "active lending", "focus_queries": []},
            "output": {"matches": [], "focus_queries": []},
        },
    }


async def test_stage_failure_carries_opted_in_retrieval_trace():
    settings = _settings().model_copy(update={"capture_retrieval_trace": True})
    pipeline = ChittiPipeline(settings, Stages(), retriever=None, access=RegisterAccess(settings))
    now = datetime.now(UTC)
    records = [
        StageRecord(
            stage=StageName.INTERPRETATION,
            status="completed",
            started_at=now,
            input={"resolution": "recorded"},
            output={"standalone_question": "active lending"},
        ),
        StageRecord(
            stage=StageName.RETRIEVAL,
            status="completed",
            started_at=now,
            input={"query": "active lending"},
            output={"matches": []},
        ),
    ]

    async def fail():
        raise ValueError("invalid answerability")

    with pytest.raises(PipelineStageError) as caught:
        await pipeline._stage(records, StageName.ANSWERABILITY, {}, fail)

    assert caught.value.retrieval_trace == {
        "question_interpretation": {
            "input": {"resolution": "recorded"},
            "output": {"standalone_question": "active lending"},
        },
        "semantic_retrieval": {
            "input": {"query": "active lending"},
            "output": {"matches": []},
        },
    }


async def test_stage_records_usage_for_failures_and_none_for_host_only_stages():
    pipeline = ChittiPipeline(_settings(), Stages(), retriever=None, access=RegisterAccess(_settings()))
    records: list[StageRecord] = []

    async def failed_call():
        sink = _usage_sink.get()
        assert sink is not None
        sink.append(
            ModelCallUsage(
                repair=False,
                response_received=True,
                prompt_tokens=5,
                completion_tokens=2,
                total_tokens=7,
            )
        )
        raise ValueError("provider failed after response")

    with pytest.raises(PipelineStageError):
        await pipeline._stage(records, StageName.ANSWER, {}, failed_call, model="answer-model")
    assert records[-1].status == "failed"
    assert records[-1].usage is not None
    assert records[-1].usage.attempted_calls == 1

    host_records: list[StageRecord] = []

    async def host_call():
        return "host result"

    result = await pipeline._stage(
        host_records,
        StageName.RETRIEVAL,
        {},
        host_call,
    )
    assert result == "host result"
    assert host_records[-1].usage is None


async def test_cancelled_stage_records_completed_call_usage_and_reraises():
    pipeline = ChittiPipeline(_settings(), Stages(), retriever=None, access=RegisterAccess(_settings()))
    records: list[StageRecord] = []

    async def cancelled_call():
        sink = _usage_sink.get()
        assert sink is not None
        sink.append(
            ModelCallUsage(
                repair=False,
                response_received=True,
                prompt_tokens=8,
                completion_tokens=3,
                total_tokens=11,
            )
        )
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await pipeline._stage(records, StageName.ANSWER, {}, cancelled_call, model="answer")

    assert records[-1].status == "failed"
    assert records[-1].error == "cancelled"
    assert records[-1].usage is not None
    assert records[-1].usage.total_tokens == 11
    assert _usage_sink.get() is None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        conversation_model="conversation",
        interpretation_model="interpretation",
        grounding_model="grounding",
        answerability_model="answerability",
        planning_model="planning",
        answer_model="answer",
    )


class Stages:
    async def resolve_conversation(self, value):
        reply = value.messages[-1]["content"]
        if reply == "the commercial funnel":
            return ConversationResolution(
                standalone_question="Show active commercial deals",
                used_prior_context=True,
            )
        return ConversationResolution(standalone_question=reply)

    async def interpret(self, resolution):
        return QuestionInterpretation(
            standalone_question=resolution.standalone_question,
            terms=[
                {"text": "deals", "role": "subject", "source_text": "deals"},
                {"text": "active", "role": "filter", "source_text": "active"},
            ],
            requested_shape="list",
        )

    async def ground(self, *_args):
        return ValueGroundingResult(
            status="RESOLVED",
            established_meaning={"resources": ["deals"]},
        )

    async def answerability(self, interpretation, *_args):
        if interpretation.standalone_question == "Show active deals":
            return AnswerabilityResult(
                outcome="CLARIFICATION_REQUIRED",
                reason="Deal is ambiguous across Ledger books.",
                clarification_question=(
                    "Do you mean the commercial deal funnel, own-book lending, "
                    "syndication, or asset monetisation?"
                ),
            )
        return AnswerabilityResult(
            outcome="ANSWERABLE",
            reason="The user selected the commercial funnel.",
            verified_question=VerifiedQuestion(
                standalone_question=interpretation.standalone_question,
                resources=["deals"],
                answer_shape="list",
                filters=[
                    {
                        "field": "stage",
                        "operator": "in",
                        "values": ["New Inquiry", "In Screening", "In Pipeline", "On Hold"],
                    }
                ],
                evidence_obligations=[
                    {
                        "kind": "answer_shape",
                        "description": "List active commercial deals",
                        "provenance": "user",
                        "source_text": "Show active commercial deals",
                    }
                ],
            ),
        )

    async def plan(self, *_args):
        return QueryPlan.model_validate(
            {
                "reads": [{"name": "deals", "resource": "deals"}],
                "operations": [
                    {
                        "name": "live",
                        "operation": "filter",
                        "input": "deals",
                        "output": "live_deals",
                        "arguments": {
                            "field": "stage",
                            "operator": "in",
                            "values": ["New Inquiry", "In Screening", "In Pipeline", "On Hold"],
                        },
                    }
                ],
                "result_names": ["live_deals"],
            }
        )

    async def answer(self, *_args):
        return "The commercial funnel has one active deal."


async def test_pipeline_run_without_on_stage_preserves_diagnostic_response():
    settings = _settings().model_copy(update={"pipeline_stop_after": "conversation_resolution"})
    pipeline = ChittiPipeline(settings, Stages(), retriever=None, access=RegisterAccess(settings))
    response = await pipeline.run(
        [{"role": "user", "content": "Show active deals"}],
        identity=IDENTITY,
        request_id="request-id",
        on_stage=None,
    )
    assert response.content.startswith("Local Chitti diagnostic stopped after")
    assert response.metadata["last_completed_stage"] == "conversation_resolution"


async def test_pipeline_emits_started_and_completed_progress_and_resets_callback():
    settings = _settings().model_copy(update={"pipeline_stop_after": "conversation_resolution"})
    pipeline = ChittiPipeline(settings, Stages(), retriever=None, access=RegisterAccess(settings))
    progress = []

    await pipeline.run(
        [{"role": "user", "content": "Show active deals"}],
        identity=IDENTITY,
        request_id="request-id",
        on_stage=progress.append,
    )

    assert [event["status"] for event in progress] == ["started", "completed"]
    assert all(event["stage"] == "conversation_resolution" for event in progress)
    assert progress[0]["elapsed_ms"] == 0.0
    assert progress[0]["description"] == (
        "Understanding your request and relevant conversation context…"
    )
    assert progress[1]["elapsed_ms"] >= 0
    assert progress[1]["description"] == "Understood your request as: Show active deals"
    assert _stage_progress.get() is None


def test_progress_summaries_explain_grounding_decisions_without_exposing_ids():
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["deals"],
                "semantic_bindings": [
                    {
                        "user_term": "active",
                        "binding_kind": "lifecycle",
                        "resource": "deals",
                        "field": "stage",
                        "canonical_values": ["In Pipeline"],
                        "passage_ids": ["lifecycle.active"],
                    }
                ],
                "grounded_values": [
                    {
                        "field": "rm",
                        "user_term": "CM",
                        "canonical_value": "person-internal-name",
                        "candidate_id": "person-secret-id",
                        "candidate_label": "Chetan Mehta",
                    }
                ],
            },
        }
    )

    description = _stage_progress_description(StageName.GROUNDING, "completed", grounding)

    assert description == "Resolved 1 business meaning. Matched CM → Chetan Mehta."
    assert "person-secret-id" not in description
    assert "person-internal-name" not in description


def test_progress_summaries_show_business_clarification_instead_of_internal_reason():
    result = AnswerabilityResult(
        outcome="CLARIFICATION_REQUIRED",
        reason="internal validation detail",
        clarification_question="Do you mean platform or partner syndication?",
    )

    description = _stage_progress_description(StageName.ANSWERABILITY, "completed", result)

    assert description == (
        "I need one clarification: Do you mean platform or partner syndication?"
    )
    assert "internal validation detail" not in description


class Retriever:
    async def search(self, query, **_kwargs):
        return SemanticRetrievalResult(query=query, matches=[])


def test_focused_retrieval_queries_preserve_independent_surface_needs_and_bounds():
    interpretation = QuestionInterpretation(
        standalone_question=(
            "Which open partner-book facilities for this adviser are in the northern region?"
        ),
        terms=[
            {"text": "facilities", "role": "subject", "source_text": "facilities"},
            {"text": "this adviser", "role": "actor", "source_text": "this adviser"},
            {"text": "assigned to", "role": "relationship", "source_text": "assigned to"},
            {"text": "open", "role": "lifecycle", "source_text": "open"},
            {
                "text": "partner book",
                "role": "book_scope",
                "source_text": "partner-book",
            },
            {
                "text": "northern region",
                "role": "dimension",
                "source_text": "northern region",
            },
        ],
        requested_shape="list",
    )

    queries, slots = _focused_retrieval_queries(interpretation)

    assert [item.model_dump() for item in queries] == [
        {
            "responsibility": "relationship_dimension",
            "needs": [
                {"facet": "relationship", "query": "assigned to facilities"},
                {"facet": "dimension", "query": "northern region facilities"},
            ],
            "query": "assigned to facilities northern region facilities",
        },
        {
            "responsibility": "lifecycle_metric",
            "needs": [
                {"facet": "book_scope", "query": "partner-book facilities"},
                {"facet": "lifecycle", "query": "open facilities"},
            ],
            "query": "partner-book facilities open facilities",
        },
    ]
    assert slots == 2


def test_generic_scalar_language_does_not_create_a_metric_focus_query():
    interpretation = QuestionInterpretation(
        standalone_question="How many facilities are there?",
        terms=[
            {"text": "facilities", "role": "subject", "source_text": "facilities"},
            {"text": "how many", "role": "measure", "source_text": "How many"},
        ],
        requested_shape="scalar",
    )

    queries, slots = _focused_retrieval_queries(interpretation)

    assert [item.model_dump() for item in queries] == [
        {
            "responsibility": "book_scope",
            "needs": [
                {"facet": "book_scope", "query": "facilities"},
                {"facet": "resource", "query": "facilities"},
            ],
            "query": "facilities",
        }
    ]
    assert slots == 1


def test_general_filter_does_not_claim_lifecycle_coverage():
    interpretation = QuestionInterpretation(
        standalone_question="List facilities above a stated threshold.",
        terms=[
            {"text": "facilities", "role": "subject", "source_text": "facilities"},
            {
                "text": "above a stated threshold",
                "role": "filter",
                "source_text": "above a stated threshold",
            },
        ],
        requested_shape="list",
    )

    queries, _ = _focused_retrieval_queries(interpretation)

    assert all(need.facet != "lifecycle" for focus in queries for need in focus.needs)


class Access:
    async def read(self, read, **_kwargs):
        return RegisterEvidence(
            read=read,
            records=[],
            window=RetrievalWindow(
                resource=read.resource,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                pages_retrieved=1,
                records_retrieved=0,
                completeness=Completeness.COMPLETE,
            ),
        )

    async def reference_values(self, **_kwargs):
        return {"Deal Funnel Stage": ["New Inquiry", "In Screening", "In Pipeline"]}


class ReferenceCaptureStages(Stages):
    candidates = None

    async def ground(self, _interpretation, _retrieval, candidates):
        self.candidates = candidates
        return ValueGroundingResult(status="RESOLVED", established_meaning={"resources": ["deals"]})


class ReferenceMapAccess(Access):
    async def reference_values(self, **_kwargs):
        return {
            "Deal Funnel Stage": ["New Inquiry", "In Screening", "In Pipeline"],
            "State": ["Maharashtra", "Gujarat"],
            "Unrelated": ["must remain available to the executor"],
        }


class CompositeValueAccess(Access):
    async def read(self, read, **kwargs):
        evidence = await super().read(read, **kwargs)
        if read.resource != "syndication":
            return evidence
        return evidence.model_copy(
            update={
                "records": [
                    CanonicalRecord(
                        resource="syndication",
                        record_id="syn-1",
                        fields={
                            "id": "syn-1",
                            "mandate_status": (
                                "Sent - Mandate Signed | "
                                "[Syndication: Yes, Partnership: No]"
                            ),
                        },
                    )
                ]
            }
        )


async def test_grounding_keeps_full_reference_map_for_executor_and_trims_model_candidates():
    stages = ReferenceCaptureStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=ReferenceMapAccess(), executor=Executor()
    )
    interpretation = QuestionInterpretation(
        standalone_question="Show active deals",
        terms=[{"text": "deals", "role": "subject", "source_text": "deals"}],
        requested_shape="list",
    )
    resolution = ConversationResolution(standalone_question="Show active deals")

    await pipeline._ground(
        interpretation,
        SemanticRetrievalResult(query="deals", matches=[]),
        resolution=resolution,
        identity=IDENTITY,
        request_id="reference-map",
    )

    assert _reference_values.get() is not None
    assert "Unrelated" in _reference_values.get()
    assert stages.candidates is not None
    assert "Unrelated" not in stages.candidates["reference_values"]


async def test_grounding_collects_retrieved_governed_composite_values_as_candidates():
    stages = ReferenceCaptureStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=CompositeValueAccess(), executor=Executor()
    )
    interpretation = QuestionInterpretation(
        standalone_question="List mandates sent and signed for syndication.",
        terms=[
            {
                "text": "sent and signed for syndication",
                "role": "filter",
                "source_text": "sent and signed for syndication",
            }
        ],
        requested_shape="list",
    )
    retrieval = SemanticRetrievalResult(
        query="mandates sent signed",
        matches=[
            RetrievalMatch(
                passage_id="semantics.syndication_mandate.syndicationtracker_mandate_status",
                source="syndication",
                version="1",
                content="Preserve the caller-visible composite mandate status.",
                fusion_score=1.0,
                metadata={
                    "resource": "syndication",
                    "field": "mandate_status",
                    "audience": ["grounding"],
                },
            )
        ],
    )

    await pipeline._ground(
        interpretation,
        retrieval,
        resolution=ConversationResolution(
            standalone_question="List mandates sent and signed for syndication."
        ),
        identity=IDENTITY,
        request_id="composite-values",
    )

    assert stages.candidates is not None
    assert stages.candidates["governed_composite_values"] == [
        {
            "id": stages.candidates["governed_composite_values"][0]["id"],
            "resource": "syndication",
            "field": "mandate_status",
            "value": "Sent - Mandate Signed | [Syndication: Yes, Partnership: No]",
        }
    ]
    assert stages.candidates["governed_composite_values"][0]["id"].startswith(
        "composite-"
    )


class Executor:
    calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        return ExecutionResult(
            datasets={"live_deals": [{"id": "deal-1", "stage": "In Pipeline"}]},
            result_names=["live_deals"],
            result_shapes={"live_deals": "rows"},
            contributing_ids=["deal-1"],
            completeness=Completeness.COMPLETE,
            windows=[],
        )

    def validate_plan(self, _plan):
        return None


class CapturingStages(Stages):
    evidence = None

    async def answer(self, _question, evidence):
        self.evidence = evidence
        return "One active deal is visible; one amount is missing."


class CompletenessExecutor(Executor):
    async def execute(self, *_args, **_kwargs):
        return ExecutionResult(
            datasets={"live_deals": [{"id": "deal-1", "amount_cr": None}]},
            result_names=["live_deals"],
            result_shapes={"live_deals": "rows"},
            contributing_ids=["deal-1"],
            contributing_records=[{"resource": "deals", "id": "deal-1"}],
            completeness=Completeness.COMPLETE,
            windows=[
                {
                    "resource": "deals",
                    "pages_retrieved": 1,
                    "records_retrieved": 1,
                    "completeness": "COMPLETE",
                    "controlled_value_issues": {"sector": 1},
                }
            ],
            metric_completeness={"live_deals": {"amount_cr": {"assessed_count": 0, "missing_count": 1}}},
        )


async def test_list_evidence_carries_one_row_table_lineage_windows_and_null_caveat():
    stages = CapturingStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=Access(), executor=CompletenessExecutor()
    )

    result = await pipeline.run(
        [{"role": "user", "content": "Show active commercial deals"}],
        identity=IDENTITY,
        request_id="complete-evidence",
    )

    assert result.metadata["result_table"] == [
        {
            "result": "live_deals",
            "id": "deal-1",
            "amount_cr": None,
        }
    ]
    assert stages.evidence.facts == {"live_deals_row_count": 1}
    assert stages.evidence.contributing_records == [{"resource": "deals", "id": "deal-1"}]
    assert stages.evidence.retrieval_windows[0]["completeness"] == "COMPLETE"
    assert stages.evidence.metric_completeness["live_deals"]["amount_cr"] == {
        "assessed_count": 0,
        "missing_count": 1,
    }
    assert any("1 rows had no value" in caveat for caveat in stages.evidence.caveats)
    assert not any("reference label" in caveat for caveat in stages.evidence.caveats)


async def test_recorded_stage_date_caveat_reaches_answer_evidence():
    class LendingStages(CapturingStages):
        async def plan(self, *_args):
            return QueryPlan.model_validate({
                "reads": [{"name": "facilities", "resource": "lending"}],
                "operations": [], "result_names": ["facilities"],
            })

    class StageDateExecutor(Executor):
        async def execute(self, *_args, **_kwargs):
            return ExecutionResult(
                datasets={"facilities": [{"tracker_no": "LN-EXAMPLE", "stage_updated_at": None}]},
                result_names=["facilities"], result_shapes={"facilities": "rows"},
                contributing_ids=[], completeness=Completeness.COMPLETE, windows=[],
                result_field_sources={"facilities": {
                    "stage_updated_at": [{"resource": "lending", "field": "stage_updated_at"}],
                }},
            )

    stages = LendingStages()
    pipeline = ChittiPipeline(_settings(), stages, Retriever(), access=Access(), executor=StageDateExecutor())
    await pipeline.run(
        [{"role": "user", "content": "Show recorded lending stage dates"}],
        identity=IDENTITY, request_id="stage-date-provenance",
    )
    assert any("not a verified duration" in caveat for caveat in stages.evidence.caveats)
    assert stages.evidence.rows[0]["stage_updated_at"] is None


class MixedShapeExecutor(Executor):
    async def execute(self, *_args, **_kwargs):
        matching_rows = [{"id": f"deal-{index}"} for index in range(18)]
        return ExecutionResult(
            datasets={
                "matching_deal_count": [{"matching_deal_count": 18}],
                "unassessable_sector_count": [{"unassessable_sector_count": 0}],
                "matching_deals": matching_rows,
            },
            result_names=[
                "matching_deal_count",
                "unassessable_sector_count",
                "matching_deals",
            ],
            result_shapes={
                "matching_deal_count": "scalar",
                "unassessable_sector_count": "scalar",
                "matching_deals": "rows",
            },
            contributing_ids=[row["id"] for row in matching_rows],
            completeness=Completeness.COMPLETE,
            windows=[],
        )


async def test_mixed_result_shapes_preserve_zero_scalar_and_row_dataset():
    stages = CapturingStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=Access(), executor=MixedShapeExecutor()
    )

    result = await pipeline.run(
        [{"role": "user", "content": "Show active commercial deals"}],
        identity=IDENTITY,
        request_id="mixed-result-shapes",
    )

    assert stages.evidence.facts == {
        "matching_deal_count": {"matching_deal_count": 18},
        "unassessable_sector_count": {"unassessable_sector_count": 0},
        "matching_deals_row_count": 18,
    }
    assert len(stages.evidence.rows) == 18
    assert "deal-0" in result.content


class PartialZeroExecutor(Executor):
    async def execute(self, *_args, **_kwargs):
        return ExecutionResult(
            datasets={"live_deals": []},
            result_names=["live_deals"],
            result_shapes={"live_deals": "rows"},
            contributing_ids=[],
            completeness=Completeness.PARTIAL_LIMIT,
            windows=[
                {
                    "resource": "deals",
                    "pages_retrieved": 1,
                    "records_retrieved": 0,
                    "completeness": "PARTIAL_LIMIT",
                }
            ],
        )


class PartialZeroStages(Stages):
    evidence = None

    async def answer(self, _question, evidence):
        self.evidence = evidence
        return "No matching rows were observed within the partial traversal."


async def test_partial_zero_is_reported_as_partial_not_as_a_complete_zero():
    stages = PartialZeroStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=Access(), executor=PartialZeroExecutor()
    )

    result = await pipeline.run(
        [{"role": "user", "content": "Show active commercial deals"}],
        identity=IDENTITY,
        request_id="partial-zero",
    )

    assert result.metadata["outcome"] == "PARTIAL_RESULT"
    assert result.metadata["completeness"] == "PARTIAL_LIMIT"
    assert stages.evidence.completeness == Completeness.PARTIAL_LIMIT
    assert stages.evidence.facts == {"live_deals_row_count": 0}
    assert any("traversal limit" in caveat for caveat in stages.evidence.caveats)


class GroupedShapeExecutor(Executor):
    async def execute(self, *_args, **_kwargs):
        grouped_rows = [
            {"stage": "In Pipeline", "deal_count": 3},
            {"stage": "On Hold", "deal_count": 2},
        ]
        return ExecutionResult(
            datasets={"deals_by_stage": grouped_rows},
            result_names=["deals_by_stage"],
            result_shapes={"deals_by_stage": "grouped_rows"},
            contributing_ids=[],
            completeness=Completeness.COMPLETE,
            windows=[],
        )


async def test_grouped_result_shape_remains_row_evidence():
    stages = CapturingStages()
    pipeline = ChittiPipeline(
        _settings(), stages, Retriever(), access=Access(), executor=GroupedShapeExecutor()
    )

    await pipeline.run(
        [{"role": "user", "content": "Show active commercial deals"}],
        identity=IDENTITY,
        request_id="grouped-result-shape",
    )

    assert stages.evidence.facts == {"deals_by_stage_row_count": 2}
    assert stages.evidence.rows == [
        {"result": "deals_by_stage", "stage": "In Pipeline", "deal_count": 3},
        {"result": "deals_by_stage", "stage": "On Hold", "deal_count": 2},
    ]


def test_query_plan_declares_structural_scalar_rows_and_grouped_shapes():
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "deals", "resource": "deals"}],
            "operations": [
                {
                    "name": "count",
                    "operation": "count",
                    "input": "deals",
                    "output": "deal_count",
                },
                {
                    "name": "group",
                    "operation": "group",
                    "input": "deals",
                    "output": "by_stage",
                    "arguments": {
                        "by": ["stage"],
                        "aggregations": [{"operation": "count", "as": "deal_count"}],
                    },
                },
            ],
            "result_names": ["deal_count", "deals", "by_stage"],
        }
    )

    assert plan.result_shapes == {
        "deal_count": "scalar",
        "deals": "rows",
        "by_stage": "grouped_rows",
    }


def test_query_plan_overwrites_result_shape_conflicting_with_structure():
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "deals", "resource": "deals"}],
            "operations": [
                {
                    "name": "count",
                    "operation": "count",
                    "input": "deals",
                    "output": "deal_count",
                }
            ],
            "result_names": ["deal_count"],
            "result_shapes": {"deal_count": "rows", "intermediate": "grouped_rows"},
        }
    )

    assert plan.result_shapes == {"deal_count": "scalar"}


class InvalidPlanStages(Stages):
    async def plan(self, *_args):
        return QueryPlan.model_validate(
            {
                "reads": [{"name": "deals", "resource": "deals"}],
                "operations": [
                    {
                        "name": "filter",
                        "operation": "filter",
                        "input": "deals",
                        "output": "selected",
                        "arguments": {"field": "stage", "operator": "eq", "value": "In Pipeline"},
                    },
                    {
                        "name": "project",
                        "operation": "project",
                        "input": "selected",
                        "output": "selected",
                        "arguments": {"fields": ["id"]},
                    },
                ],
                "result_names": ["missing_result"],
            }
        )


class MissingResultPlanStages(Stages):
    async def plan(self, *_args):
        return QueryPlan.model_validate(
            {
                "reads": [{"name": "deals", "resource": "deals"}],
                "operations": [
                    {
                        "name": "count",
                        "operation": "count",
                        "input": "deals",
                        "output": "deal_count",
                        "arguments": {},
                    }
                ],
                "result_names": ["conversion_rate"],
            }
        )


class TrackingPlanExecutor(PlanExecutor):
    def __init__(self, settings, access):
        super().__init__(settings, access)
        self.execute_calls = 0

    async def execute(self, *args, **kwargs):
        self.execute_calls += 1
        return await super().execute(*args, **kwargs)


async def test_clarification_skips_planning_then_history_resumes_the_pipeline():
    executor = Executor()
    pipeline = ChittiPipeline(_settings(), Stages(), Retriever(), access=Access(), executor=executor)
    first = await pipeline.run(
        [{"role": "user", "content": "Show active deals"}],
        identity=IDENTITY,
        request_id="clarify",
    )
    assert first.metadata["outcome"] == "CLARIFICATION_REQUIRED"
    assert first.metadata["last_completed_stage"] == "answerability"
    assert executor.calls == 0

    second = await pipeline.run(
        [
            {"role": "user", "content": "Show active deals"},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": "the commercial funnel"},
        ],
        identity=IDENTITY,
        request_id="resume",
    )
    assert second.metadata["outcome"] == "ANSWERED"
    assert second.metadata["last_completed_stage"] == "answer_generation"
    assert executor.calls == 1


async def test_malformed_plan_fails_inside_planning_before_execution():
    settings = _settings()
    access = Access()
    executor = TrackingPlanExecutor(settings, access)
    pipeline = ChittiPipeline(settings, InvalidPlanStages(), Retriever(), access=access, executor=executor)

    with pytest.raises(PipelineStageError) as caught:
        await pipeline.run(
            [{"role": "user", "content": "Show active commercial deals"}],
            identity=IDENTITY,
            request_id="invalid-plan",
        )

    assert caught.value.stage == "query_planning"
    assert "Duplicate dataset name 'selected'" in str(caught.value)
    assert executor.execute_calls == 0


async def test_missing_result_fails_inside_planning_before_execution():
    settings = _settings()
    access = Access()
    executor = TrackingPlanExecutor(settings, access)
    pipeline = ChittiPipeline(
        settings, MissingResultPlanStages(), Retriever(), access=access, executor=executor
    )

    with pytest.raises(PipelineStageError) as caught:
        await pipeline.run(
            [{"role": "user", "content": "Count commercial deals"}],
            identity=IDENTITY,
            request_id="missing-result",
        )

    assert caught.value.stage == "query_planning"
    assert "Unknown result dataset(s): conversion_rate" in str(caught.value)
    assert executor.execute_calls == 0
