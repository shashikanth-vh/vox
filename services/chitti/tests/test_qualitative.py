from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.evidence import Completeness, RegisterEvidence, RetrievalWindow
from app.executor import ExecutionResult
from app.identity import CallerIdentity
from app.model_stages import ModelStages
from app.pipeline import ChittiPipeline, PipelineStageError, _can_render_validated_fallback
from app.qualitative import (
    QualitativeValidationError,
    build_qualitative_corpus,
    validate_qualitative_findings,
)
from app.stage_models import (
    AnswerabilityResult,
    ConversationResolution,
    QualitativeAnalysisDraft,
    QualitativeAnalysisResult,
    QualitativeStageOutput,
    QueryPlan,
    QuestionInterpretation,
    SemanticRetrievalResult,
    ValueGroundingResult,
    VerifiedQuestion,
)


def test_lender_snapshots_keep_direction_lineage_and_missing_coverage():
    execution = ExecutionResult(
        datasets={"cohort": []}, result_names=["cohort"], result_shapes={"cohort": "rows"},
        contributing_ids=["submission-1"], completeness=Completeness.COMPLETE, windows=[],
        cohort_rows={"cohort": [{
            "fields": {
                "chase": "Requested the remaining documents.", "reply": None,
                "note": "Unrelated manual remark", "forged": "Wrong resource",
            },
            "lineage": ["syndication_lenders:submission-1", "lending:facility-1"],
        }]},
        result_field_sources={"cohort": {
            "chase": [{"resource": "syndication_lenders", "field": "last_chase_note"}],
            "reply": [{"resource": "syndication_lenders", "field": "last_reply_note"}],
            "note": [{"resource": "syndication_lenders", "field": "note"}],
            "forged": [{"resource": "lending", "field": "last_reply_note"}],
        }},
    )
    corpus = build_qualitative_corpus(execution, Settings(_env_file=None))
    assert {r.source_field for r in corpus.records} == {"last_chase_note", "last_reply_note"}
    assert all(r.record_id == "submission-1" for r in corpus.records)
    assert corpus.coverage.text_records == 1
    assert corpus.coverage.missing_records == 1
    assert {r.source_text for r in corpus.records} == {"Requested the remaining documents.", None}


def _execution() -> ExecutionResult:
    long_remark = "Recorded concern " + ("x" * 150)
    return ExecutionResult(
        datasets={"cohort": []},
        result_names=["cohort"],
        result_shapes={"cohort": "rows"},
        contributing_ids=["lend-1", "lend-2"],
        completeness=Completeness.COMPLETE,
        windows=[],
        cohort_rows={
            "cohort": [
                {
                    "fields": {"remarks": long_remark, "private_note": "not authorized"},
                    "lineage": ["lending:lend-1", "entities:entity-1"],
                },
                {
                    "fields": {"remarks": None, "private_note": "not authorized"},
                    "lineage": ["lending:lend-2"],
                },
            ]
        },
        result_field_sources={
            "cohort": {
                "remarks": [{"resource": "lending", "field": "remarks"}],
                "private_note": [{"resource": "lending", "field": "private_note"}],
            }
        },
    )


def _empty_qualitative_execution(
    *,
    authorized: bool = True,
    missing: bool = False,
    empty: bool = False,
    lost_lineage: bool = False,
) -> ExecutionResult:
    field_sources = (
        {"cohort": {"remarks": [{"resource": "lending", "field": "remarks"}]}}
        if authorized
        else {"cohort": {"notes": [{"resource": "lending", "field": "notes"}]}}
    )
    fields = (
        {"remarks": None if missing else "A recorded remark"} if authorized else {"notes": "not authorized"}
    )
    return ExecutionResult(
        datasets={"cohort": []},
        result_names=["cohort"],
        result_shapes={"cohort": "rows"},
        contributing_ids=[],
        completeness=Completeness.COMPLETE,
        windows=[],
        cohort_rows={
            "cohort": []
            if empty
            else [
                {
                    "fields": fields,
                    "lineage": ["deals:deal-1"] if lost_lineage else ["lending:lend-1"],
                }
            ]
        },
        result_field_sources=field_sources,
    )


def test_empty_qualitative_cohort_is_complete_without_model_input():
    corpus = build_qualitative_corpus(
        _empty_qualitative_execution(empty=True),
        _settings(),
    )
    assert corpus.coverage.completeness == "COMPLETE"
    assert corpus.coverage.total_records == 0
    assert corpus.authorized_cohort_row_count == 0


def test_populated_cohort_with_lost_authorized_lineage_fails_loudly():
    with pytest.raises(QualitativeValidationError, match="no authorized source records"):
        build_qualitative_corpus(_empty_qualitative_execution(lost_lineage=True), _settings())


def test_all_missing_qualitative_text_is_partial():
    corpus = build_qualitative_corpus(_empty_qualitative_execution(missing=True), _settings())
    assert corpus.coverage.text_records == 0
    assert corpus.coverage.completeness == "PARTIAL"
    assert "no authorized source text" in (corpus.coverage.limitation or "")


async def test_zero_authorized_records_failure_is_attributed_to_qualitative_stage():
    pipeline = ChittiPipeline(_settings(), object(), retriever=None, access=object())
    records = []

    async def build_invalid_corpus():
        return build_qualitative_corpus(_empty_qualitative_execution(lost_lineage=True), _settings())

    with pytest.raises(PipelineStageError) as caught:
        await pipeline._stage(records, "qualitative_analysis", {}, build_invalid_corpus)

    assert caught.value.stage == "qualitative_analysis"
    assert records[-1].stage == "qualitative_analysis"


@pytest.mark.parametrize(
    "execution",
    [
        _empty_qualitative_execution(empty=True),
        _empty_qualitative_execution(missing=True),
    ],
)
def test_validated_fallback_rejects_zero_text_corpora(execution):
    corpus = build_qualitative_corpus(execution, _settings())
    qualitative = QualitativeAnalysisResult(coverage=corpus.coverage)

    assert _can_render_validated_fallback(Completeness.COMPLETE, corpus, qualitative) is False


def test_qualitative_stage_output_serializes_coverage_without_authorized_text():
    corpus = build_qualitative_corpus(_execution(), _settings())
    output = QualitativeStageOutput.from_run(
        corpus,
        # The analysis is not relevant to this serialization boundary.
        QualitativeAnalysisResult(coverage=corpus.coverage),
    )
    serialized = output.model_dump_json()
    assert "source_text" not in serialized
    assert "Recorded concern" not in serialized
    assert output.corpus.records


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        qualitative_max_records=overrides.pop("qualitative_max_records", 500),
        qualitative_max_chars_per_field=overrides.pop("qualitative_max_chars_per_field", 4000),
        qualitative_max_total_chars=overrides.pop("qualitative_max_total_chars", 100000),
        **overrides,
    )


def test_corpus_contains_only_governed_fields_and_explicit_missing_records():
    corpus = build_qualitative_corpus(_execution(), _settings())

    assert [(record.record_id, record.source_field) for record in corpus.records] == [
        ("lend-1", "remarks"),
        ("lend-2", "remarks"),
    ]
    assert [record.evidence_ref for record in corpus.records] == ["qref_0001", "qref_0002"]
    assert corpus.records[1].missing is True
    assert "private_note" not in corpus.model_dump_json()
    assert corpus.coverage.model_dump() == {
        "total_records": 2,
        "assessed_records": 2,
        "text_records": 1,
        "missing_records": 1,
        "completeness": "COMPLETE",
        "limitation": None,
    }


def test_corpus_bounds_mark_partial_and_retain_exact_total():
    corpus = build_qualitative_corpus(
        _execution(),
        _settings(qualitative_max_records=1, qualitative_max_chars_per_field=100),
    )

    assert corpus.coverage.total_records == 2
    assert corpus.coverage.assessed_records == 1
    assert corpus.coverage.completeness == "PARTIAL"
    assert corpus.records[0].truncated is True
    assert "record limit reached" in (corpus.coverage.limitation or "")


@pytest.mark.parametrize(
    ("support", "message"),
    [
        (
            {"evidence_ref": "invented", "source_field": "remarks", "excerpt": "concern"},
            "unauthorized record or field",
        ),
        (
            {"evidence_ref": "VALID", "source_field": "private_note", "excerpt": "not"},
            "unauthorized record or field",
        ),
        (
            {"evidence_ref": "VALID", "source_field": "remarks", "excerpt": "invented text"},
            "not an exact source substring",
        ),
    ],
)
def test_validation_rejects_invented_ids_fields_and_excerpts(support, message):
    corpus = build_qualitative_corpus(_execution(), _settings())
    support = {
        **support,
        "evidence_ref": (
            corpus.records[0].evidence_ref if support["evidence_ref"] == "VALID" else support["evidence_ref"]
        ),
    }
    draft = QualitativeAnalysisDraft.model_validate(
        {
            "findings": [
                {
                    "label": "Concern",
                    "summary": "A recorded concern.",
                    "supports": [support],
                }
            ]
        }
    )

    with pytest.raises(QualitativeValidationError, match=message):
        validate_qualitative_findings(draft, corpus)


def test_validation_rejects_duplicate_support_and_host_computes_unique_count():
    corpus = build_qualitative_corpus(_execution(), _settings())
    support = {
        "evidence_ref": corpus.records[0].evidence_ref,
        "source_field": "remarks",
        "excerpt": "Recorded concern",
    }
    duplicate = QualitativeAnalysisDraft.model_validate(
        {
            "findings": [
                {
                    "label": "Concern",
                    "summary": "Recorded concern",
                    "supports": [support, support],
                }
            ]
        }
    )
    with pytest.raises(QualitativeValidationError, match="Duplicate qualitative support"):
        validate_qualitative_findings(duplicate, corpus)

    valid = duplicate.model_copy(
        update={
            "findings": [
                duplicate.findings[0].model_copy(update={"supports": [duplicate.findings[0].supports[0]]})
            ]
        }
    )
    result = validate_qualitative_findings(valid, corpus)
    assert result.findings[0].support_count == 1
    assert result.findings[0].supporting_record_ids == ["lend-1"]
    assert result.coverage == corpus.coverage


class FakeCompletions:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(self.payloads))))]
        )


async def test_qualitative_stage_repairs_invalid_provenance_once():
    settings = _settings(qualitative_model="qualitative-model")
    corpus = build_qualitative_corpus(_execution(), settings)
    finding = {
        "label": "Concern",
        "summary": "A concern was recorded.",
        "supports": [
            {
                "evidence_ref": corpus.records[0].evidence_ref,
                "source_field": "remarks",
                "excerpt": "Recorded concern",
            }
        ],
    }
    completions = FakeCompletions(
        [
            {
                "findings": [
                    {
                        **finding,
                        "supports": [
                            {
                                "evidence_ref": "invented",
                                "source_field": "remarks",
                                "excerpt": "Recorded concern",
                            }
                        ],
                    }
                ]
            },
            {"findings": [finding]},
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), settings)

    result = await stages.qualitative(corpus)

    assert len(completions.calls) == 2
    assert all(call["model"] == "qualitative-model" for call in completions.calls)
    repair_payload = completions.calls[1]["messages"][1]["content"]
    assert "character-for-character" in repair_payload
    assert "may occur only once" in repair_payload
    assert result.findings[0].support_count == 1


async def test_empty_qualitative_corpus_makes_no_model_call():
    completions = FakeCompletions([])
    stages = ModelStages(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        _settings(qualitative_model="qualitative-model"),
    )
    corpus = build_qualitative_corpus(_empty_qualitative_execution(empty=True), _settings())

    result = await stages.qualitative(corpus)

    assert result.findings == []
    assert result.coverage.completeness == "COMPLETE"
    assert completions.calls == []


async def test_all_missing_qualitative_corpus_makes_no_model_call():
    completions = FakeCompletions([])
    settings = _settings(qualitative_model="qualitative-model")
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), settings)
    corpus = build_qualitative_corpus(_empty_qualitative_execution(missing=True), settings)

    result = await stages.qualitative(corpus)

    assert corpus.records
    assert result.findings == []
    assert result.coverage.completeness == "PARTIAL"
    assert completions.calls == []


def test_confidence_extra_is_accepted_but_not_serialized():
    draft = QualitativeAnalysisDraft.model_validate(
        {
            "findings": [
                {
                    "label": "Concern",
                    "summary": "Recorded concern",
                    "confidence": 0.8,
                    "supports": [
                        {
                            "evidence_ref": "qref-example",
                            "source_field": "remarks",
                            "excerpt": "Recorded concern",
                        }
                    ],
                }
            ]
        }
    )
    assert "confidence" not in draft.model_dump_json()


async def test_answerability_preserves_non_blocking_unavailable_obligation():
    completions = FakeCompletions(
        [
            {
                "outcome": "ANSWERABLE",
                "reason": "The requested cohort is supported; the auxiliary check is unavailable.",
                "verified_question": {
                    "standalone_question": "List ready mandates and check the external screening clearance",
                    "resources": ["syndication"],
                    "answer_shape": "list",
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "List the request-defined ready cohort",
                            "provenance": "user",
                            "source_text": "List ready mandates",
                        },
                        {
                            "kind": "unavailable",
                            "description": "External screening clearance",
                            "provenance": "register",
                            "availability": "UNAVAILABLE",
                            "unavailable_reason": "No governed clearance field is available",
                            "prevents_main_answer": False,
                        },
                    ],
                },
            }
        ]
    )
    settings = _settings(answerability_model="answerability-model")
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), settings)
    interpretation = QuestionInterpretation(
        standalone_question="List ready mandates and check the external screening clearance",
        requested_shape="list",
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["syndication"],
                "unavailable_obligations": [
                    {
                        "description": "External screening clearance",
                        "reason": "No governed clearance field is available",
                        "prevents_main_answer": False,
                    }
                ],
            },
        }
    )

    result = await stages.answerability(
        interpretation,
        grounding,
        SemanticRetrievalResult(query="ready mandates", matches=[]),
    )

    assert result.outcome == "ANSWERABLE"
    assert result.verified_question is not None
    unavailable = result.verified_question.evidence_obligations[1]
    assert unavailable.availability == "UNAVAILABLE"
    assert unavailable.prevents_main_answer is False


async def test_answerability_normalizes_qualitative_text_filter_into_coverage_obligations():
    obligations = [
        {
            "kind": "qualitative",
            "description": "Interpret the authorized notes",
            "resource": "lending",
            "field": "remarks",
            "provenance": "ontology",
            "passage_ids": ["synthetic.qualitative"],
        },
        {
            "kind": "completeness",
            "description": "Report authorized-note coverage",
            "resource": "lending",
            "field": "remarks",
            "provenance": "ontology",
            "passage_ids": ["synthetic.qualitative"],
        },
    ]

    def answer(filters):
        return {
            "outcome": "ANSWERABLE",
            "reason": "The exact cohort and authorized notes are available.",
            "verified_question": {
                "standalone_question": "Interpret the authorized notes",
                "resources": ["lending"],
                "answer_shape": "qualitative",
                "qualitative_analysis": True,
                "filters": filters,
                "missing_data_requirements": [
                    {
                        "resource": "lending",
                        "field": "remarks",
                        "usage": "qualitative_source",
                    }
                ],
                "evidence_obligations": obligations,
            },
        }

    completions = FakeCompletions(
        [
            answer([{"resource": "lending", "field": "remarks", "operator": "icontains", "value": "x"}]),
            answer([]),
        ]
    )
    stages = ModelStages(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        _settings(answerability_model="answerability-model"),
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["lending"],
                "semantic_bindings": [
                    {
                        "user_term": "authorized notes",
                        "binding_kind": "field",
                        "resource": "lending",
                        "field": "remarks",
                        "passage_ids": ["synthetic.qualitative"],
                        "details": {"missing_policy": "report_unassessable"},
                    }
                ],
            },
        }
    )

    result = await stages.answerability(
        QuestionInterpretation(
            standalone_question="Interpret the authorized notes",
            requested_shape="qualitative",
            qualitative_analysis=True,
        ),
        grounding,
        SemanticRetrievalResult(query="authorized notes", matches=[]),
    )

    assert len(completions.calls) == 1
    assert result.verified_question is not None
    assert result.verified_question.filters == []
    assert result.verified_question.filters == []


async def test_answerability_derives_qualitative_plumbing_without_repair():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The authorized notes are available.",
        "verified_question": {
            "standalone_question": "Interpret the authorized notes",
            "resources": ["lending"],
            "answer_shape": "qualitative",
            "evidence_obligations": [
                {
                    "kind": "qualitative",
                    "description": "Interpret the authorized notes",
                    "resource": "lending",
                    "field": "remarks",
                    "provenance": "ontology",
                    "passage_ids": ["synthetic.qualitative"],
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        _settings(answerability_model="answerability-model"),
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["lending"],
                "semantic_bindings": [
                    {
                        "user_term": "authorized notes",
                        "binding_kind": "field",
                        "resource": "lending",
                        "field": "remarks",
                        "passage_ids": ["synthetic.qualitative"],
                    }
                ],
            },
        }
    )

    result = await stages.answerability(
        QuestionInterpretation(
            standalone_question="Interpret the authorized notes",
            requested_shape="qualitative",
            qualitative_analysis=True,
        ),
        grounding,
        SemanticRetrievalResult(query="authorized notes", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.qualitative_analysis is True
    assert [item.usage for item in result.verified_question.missing_data_requirements] == [
        "qualitative_source"
    ]
    assert any(
        obligation.kind == "completeness"
        and obligation.resource == "lending"
        and obligation.field == "remarks"
        for obligation in result.verified_question.evidence_obligations
    )
    assert len(completions.calls) == 1


def test_non_clarification_outcomes_reject_clarification_questions():
    with pytest.raises(ValueError, match="Only CLARIFICATION_REQUIRED"):
        AnswerabilityResult(
            outcome="OUT_OF_SCOPE",
            reason="The requested authority is unavailable.",
            clarification_question="Which internal field should be used?",
        )


class PipelineStages:
    async def resolve_conversation(self, value):
        return ConversationResolution(standalone_question=value.messages[-1]["content"])

    async def interpret(self, resolution):
        return QuestionInterpretation(
            standalone_question=resolution.standalone_question,
            requested_shape="qualitative",
            qualitative_analysis=True,
        )

    async def ground(self, *_args):
        return ValueGroundingResult(
            status="RESOLVED",
            established_meaning={
                "resources": ["lending"],
                "unavailable_obligations": [
                    {
                        "description": "Auxiliary screening clearance",
                        "reason": "No governed field is available",
                        "prevents_main_answer": False,
                    }
                ],
            },
        )

    async def answerability(self, interpretation, *_args):
        return AnswerabilityResult(
            outcome="ANSWERABLE",
            reason="The main cohort and remarks are available.",
            verified_question=VerifiedQuestion(
                standalone_question=interpretation.standalone_question,
                resources=["lending"],
                answer_shape="qualitative",
                qualitative_analysis=True,
                evidence_obligations=[
                    {
                        "kind": "qualitative",
                        "description": "Analyze recorded remarks",
                        "resource": "lending",
                        "field": "remarks",
                        "provenance": "register",
                    },
                    {
                        "kind": "unavailable",
                        "description": "Auxiliary screening clearance",
                        "provenance": "register",
                        "availability": "UNAVAILABLE",
                        "unavailable_reason": "No governed field is available",
                        "prevents_main_answer": False,
                    },
                ],
            ),
        )

    async def plan(self, *_args):
        return QueryPlan.model_validate(
            {
                "reads": [{"name": "rejected", "resource": "lending", "filters": {"stage": "Rejected"}}],
                "operations": [],
                "result_names": ["rejected"],
            }
        )

    async def qualitative(self, corpus):
        return validate_qualitative_findings(
            QualitativeAnalysisDraft.model_validate(
                {
                    "findings": [
                        {
                            "label": "Recorded concern",
                            "summary": "A concern appears in the recorded case note.",
                            "supports": [
                                {
                                    "evidence_ref": corpus.records[0].evidence_ref,
                                    "source_field": "remarks",
                                    "excerpt": "Recorded concern",
                                }
                            ],
                        }
                    ]
                }
            ),
            corpus,
        )

    async def answer(self, *_args):
        raise RuntimeError("prose provider unavailable")


class PipelineRetriever:
    async def search(self, query, **_kwargs):
        return SemanticRetrievalResult(query=query, matches=[])


class PipelineAccess:
    async def read(self, read, **_kwargs):
        return RegisterEvidence(
            read=read,
            records=[],
            window=RetrievalWindow(
                resource=read.resource,
                started_at="2026-08-17T00:00:00Z",
                completed_at="2026-08-17T00:00:01Z",
                pages_retrieved=1,
                records_retrieved=0,
                completeness=Completeness.COMPLETE,
            ),
        )

    async def reference_values(self, **_kwargs):
        return {}


class PipelineExecutor:
    def validate_plan(self, _plan):
        return None

    async def execute(self, *_args, **_kwargs):
        return ExecutionResult(
            datasets={"rejected": [{"id": "lend-1", "remarks": "Recorded concern"}]},
            result_names=["rejected"],
            result_shapes={"rejected": "rows"},
            contributing_ids=["lend-1"],
            contributing_records=[{"resource": "lending", "id": "lend-1"}],
            completeness=Completeness.COMPLETE,
            windows=[],
            cohort_rows={
                "rejected": [
                    {
                        "fields": {"id": "lend-1", "remarks": "Recorded concern"},
                        "lineage": ["lending:lend-1"],
                    }
                ]
            },
            result_field_sources={
                "rejected": {
                    "id": [{"resource": "lending", "field": "id"}],
                    "remarks": [{"resource": "lending", "field": "remarks"}],
                }
            },
        )


async def test_pipeline_uses_validated_fallback_without_exposing_raw_remarks():
    settings = _settings(
        conversation_model="conversation",
        interpretation_model="interpretation",
        grounding_model="grounding",
        answerability_model="answerability",
        planning_model="planning",
        qualitative_model="qualitative",
        answer_model="answer",
    )
    pipeline = ChittiPipeline(
        settings,
        PipelineStages(),
        PipelineRetriever(),
        access=PipelineAccess(),
        executor=PipelineExecutor(),
    )
    identity = CallerIdentity(
        tenant="EVAM",
        email="user@example.com",
        user_id="user",
        roles=("Management",),
        posture="local_debug",
    )

    result = await pipeline.run(
        [{"role": "user", "content": "Summarize the recorded reasons"}],
        identity=identity,
        request_id="qualitative-fallback",
    )

    assert result.metadata["outcome"] == "ANSWERED"
    assert result.metadata["answer_generation_fallback"] is True
    assert result.metadata["failed_stage"] == "answer_generation"
    assert result.metadata["last_completed_stage"] == "evidence_construction"
    assert "Recorded theme — Recorded concern (1 supporting records)" in result.content
    assert "Auxiliary screening clearance" in result.content
    assert 'Recorded concern"' not in result.content
    assert result.metadata["result_table"] == [{"result": "rejected", "id": "lend-1"}]
    assert "remarks" not in result.content


async def test_all_missing_qualitative_turn_is_partial_and_explains_the_caveat():
    class MissingTextExecutor(PipelineExecutor):
        async def execute(self, *_args, **_kwargs):
            result = await super().execute(*_args, **_kwargs)
            result.cohort_rows = {
                "rejected": [
                    {
                        "fields": {"id": "lend-1", "remarks": None},
                        "lineage": ["lending:lend-1"],
                    }
                ]
            }
            return result

    class MissingTextStages(PipelineStages):
        async def qualitative(self, corpus):
            return QualitativeAnalysisResult(coverage=corpus.coverage)

        async def answer(self, *_args):
            return "The requested cohort has no authorized source text."

    settings = _settings(
        conversation_model="conversation",
        interpretation_model="interpretation",
        grounding_model="grounding",
        answerability_model="answerability",
        planning_model="planning",
        qualitative_model="qualitative",
        answer_model="answer",
    )
    pipeline = ChittiPipeline(
        settings,
        MissingTextStages(),
        PipelineRetriever(),
        access=PipelineAccess(),
        executor=MissingTextExecutor(),
    )
    result = await pipeline.run(
        [{"role": "user", "content": "Summarize the recorded reasons"}],
        identity=CallerIdentity(
            tenant="EVAM",
            email="user@example.com",
            user_id="user",
            roles=("Management",),
            posture="local_debug",
        ),
        request_id="all-missing-qualitative",
    )

    assert result.metadata["outcome"] == "PARTIAL_RESULT"
    assert "no authorized source text" in result.content
    assert any(
        stage["stage"] == "qualitative_analysis" and stage["status"] == "completed"
        for stage in result.metadata["stages"]
    )
