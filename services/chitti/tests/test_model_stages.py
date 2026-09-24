from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.evidence import Completeness
from app.model_stages import (
    PROMPTS,
    ModelStages,
    _canonicalize_grounding_controlled_values,
    _usage_sink,
    _validate_visible_list_results,
)
from app.stage_models import (
    AnswerabilityResult,
    ConversationInput,
    QueryPlan,
    QuestionInterpretation,
    ResultEvidence,
    RetrievalMatch,
    SemanticRetrievalResult,
    ValueGroundingResult,
    VerifiedQuestion,
)


class FakeCompletions:
    def __init__(self, payloads, *, usage=None):
        self.payloads = iter(payloads)
        self.calls = []
        self.usage = usage

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = next(self.payloads)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        if self.usage is not None:
            response.usage = self.usage
        return response


class RaisingCompletions(FakeCompletions):
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("provider unavailable")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        conversation_model="conversation-model",
        interpretation_model="interpretation-model",
        grounding_model="grounding-model",
        answerability_model="answerability-model",
        planning_model="planning-model",
        answer_model="answer-model",
    )


def test_answerability_prompt_has_no_orphaned_grounding_fragment():
    assert "established by Grounding. Ontology obligations" not in PROMPTS["answerability"]


def test_list_result_rejects_opaque_only_identity_projection():
    opaque = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "lending", "resource": "lending"},
                {"name": "syndication", "resource": "syndication"},
            ],
            "operations": [
                {
                    "name": "overlap",
                    "operation": "set_intersection",
                    "input": "lending",
                    "output": "overlap",
                    "arguments": {"others": ["syndication"], "key_fields": ["entity_id"]},
                }
            ],
            "result_names": ["overlap"],
        }
    )
    with pytest.raises(ValueError, match="no reader-visible columns"):
        _validate_visible_list_results(opaque, "list")

    visible = QueryPlan.model_validate(
        {
            "reads": [{"name": "entities", "resource": "entities"}],
            "operations": [
                {
                    "name": "display",
                    "operation": "project",
                    "input": "entities",
                    "output": "display",
                    "arguments": {"fields": ["id", "legal_name"]},
                }
            ],
            "result_names": ["display"],
        }
    )
    _validate_visible_list_results(visible, "list")


async def test_grounding_mechanically_canonicalizes_deal_temperature_without_repair():
    grounded = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["deals"],
            "semantic_bindings": [
                {
                    "user_term": "hot deals",
                    "binding_kind": "field",
                    "resource": "deals",
                    "field": "temperature",
                    "canonical_values": [" HOT "],
                    "passage_ids": ["deals.temperature"],
                }
            ],
            "grounded_values": [
                {
                    "field": "temperature",
                    "user_term": "hot",
                    "canonical_value": "hot",
                }
            ],
        },
    }
    completions = FakeCompletions([grounded])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.ground(
        QuestionInterpretation.model_validate(
            {"standalone_question": "List hot deals.", "requested_shape": "list"}
        ),
        SemanticRetrievalResult(
            query="hot deals",
            matches=[
                RetrievalMatch(
                    passage_id="deals.temperature",
                    source="deals",
                    version="1",
                    content="Deal temperature is a governed dimension.",
                    fusion_score=1.0,
                )
            ],
        ),
        {"reference_values": {"Temperature": ["Hot", "Warm", "Cold"]}},
    )

    meaning = result.established_meaning
    assert meaning.grounded_values[0].canonical_value == "Hot"
    assert meaning.grounded_values[0].candidate_label == "Hot"
    assert meaning.grounded_values[0].normalized_match is True
    assert meaning.semantic_bindings[0].canonical_values == ["Hot"]
    assert len(completions.calls) == 1


async def test_hot_follow_up_preserves_canonical_value_through_planning():
    completions = FakeCompletions(
        [
            {
                "standalone_question": "List Hot deals.",
                "used_prior_context": True,
                "intent_ledger": [
                    {
                        "kind": "status",
                        "source_message_index": 0,
                        "source_text": "Hot deals",
                        "resolved_text": "Hot deals",
                    },
                    {
                        "kind": "status",
                        "source_message_index": 2,
                        "source_text": "Show these deals again.",
                        "resolved_text": "List Hot deals.",
                    }
                ],
                "introduced_constraints": [],
            },
            {
                "standalone_question": "List Hot deals.",
                "terms": [
                    {"text": "deals", "role": "subject", "source_text": "deals"},
                    {"text": "Hot", "role": "filter", "source_text": "Hot"},
                ],
                "requested_shape": "list",
            },
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["deals"],
                    "semantic_bindings": [
                        {
                            "user_term": "Hot",
                            "binding_kind": "field",
                            "resource": "deals",
                            "field": "temperature",
                            "canonical_values": ["hot"],
                            "passage_ids": ["deals.temperature"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "temperature",
                            "user_term": "Hot",
                            "canonical_value": "hot",
                        }
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "Deal temperature is available.",
                "verified_question": {
                    "standalone_question": "List Hot deals.",
                    "resources": ["deals"],
                    "answer_shape": "list",
                    "filters": [
                        {
                            "resource": "deals",
                            "field": "temperature",
                            "operator": "eq",
                            "value": "hot",
                        }
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "List Hot deals.",
                            "provenance": "user",
                            "source_text": "List Hot deals.",
                        }
                    ],
                },
            },
            {
                "reads": [
                    {
                        "name": "hot_deals",
                        "resource": "deals",
                        "filters": {"temperature": "hot"},
                    }
                ],
                "operations": [],
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    resolution = await stages.resolve_conversation(
        ConversationInput(
            messages=[
                {"role": "user", "content": "List Hot deals."},
                {"role": "assistant", "content": "Here are the matching deals."},
                {"role": "user", "content": "Show these deals again."},
            ]
        )
    )
    interpretation = await stages.interpret(resolution)
    retrieval = SemanticRetrievalResult(
        query="Hot deals",
        matches=[
            RetrievalMatch(
                passage_id="deals.temperature",
                source="deals",
                version="1",
                content="Deal temperature is a governed dimension.",
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
        ],
    )

    grounding = await stages.ground(
        interpretation,
        retrieval,
        {"reference_values": {"Temperature": ["Hot", "Warm", "Cold"]}},
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    plan = await stages.plan(answerability.verified_question, retrieval)

    assert resolution.used_prior_context is True
    assert grounding.established_meaning.grounded_values[0].canonical_value == "Hot"
    assert answerability.verified_question.filters[0]["value"] == "Hot"
    assert plan.reads[0].filters == {"temperature": "Hot"}
    assert len(completions.calls) == 5


def test_grounding_rejects_cross_resource_status_category_collision():
    result = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["syndication", "asset_monetisation"],
                "semantic_bindings": [
                    {
                        "user_term": "status",
                        "binding_kind": "field",
                        "resource": "syndication",
                        "field": "status",
                        "canonical_values": ["On Hold"],
                        "passage_ids": ["syndication.status"],
                    },
                    {
                        "user_term": "status",
                        "binding_kind": "field",
                        "resource": "asset_monetisation",
                        "field": "status",
                        "canonical_values": ["Closed"],
                        "passage_ids": ["asset.status"],
                    },
                ],
                "grounded_values": [
                    {"field": "status", "user_term": "status", "canonical_value": "closed"}
                ],
            },
        }
    )

    errors = _canonicalize_grounding_controlled_values(
        result,
        {
            "Status of Proposal": ["On Hold"],
            "Asset Mon Status": ["Closed"],
        },
    )

    assert errors == []
    assert result.established_meaning.grounded_values[0].canonical_value == "Closed"


def test_grounding_fails_when_shared_status_has_no_unique_binding():
    result = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["syndication", "asset_monetisation"],
                "grounded_values": [
                    {"field": "status", "user_term": "closed", "canonical_value": "Closed"}
                ],
            },
        }
    )

    errors = _canonicalize_grounding_controlled_values(
        result,
        {"Status of Proposal": ["Closed"], "Asset Mon Status": ["Closed"]},
    )

    assert "exactly one resource-qualified semantic binding" in errors[0]


def test_grounding_uses_explicit_resource_to_disambiguate_shared_status():
    result = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["syndication", "asset_monetisation"],
                "semantic_bindings": [
                    {
                        "user_term": "status",
                        "binding_kind": "field",
                        "resource": "syndication",
                        "field": "status",
                        "canonical_values": ["Closed"],
                        "passage_ids": ["syndication.status"],
                    },
                    {
                        "user_term": "status",
                        "binding_kind": "field",
                        "resource": "asset_monetisation",
                        "field": "status",
                        "canonical_values": ["Closed"],
                        "passage_ids": ["asset.status"],
                    },
                ],
                "grounded_values": [
                    {
                        "resource": "syndication",
                        "field": "status",
                        "user_term": "closed",
                        "canonical_value": "Closed",
                    }
                ],
            },
        }
    )

    errors = _canonicalize_grounding_controlled_values(
        result,
        {"Status of Proposal": ["Closed"], "Asset Mon Status": ["Closed"]},
    )

    assert errors == []
    assert result.established_meaning.grounded_values[0].resource == "syndication"


def test_grounding_rejects_empty_controlled_lifecycle_expansion():
    result = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["syndication"],
                "semantic_bindings": [
                    {
                        "user_term": "live",
                        "binding_kind": "lifecycle",
                        "resource": "syndication",
                        "field": "status",
                        "canonical_values": [],
                        "passage_ids": ["lifecycle.syndication"],
                    }
                ],
            },
        }
    )

    errors = _canonicalize_grounding_controlled_values(
        result, {"Status of Proposal": ["Docs Pending"]}
    )

    assert "non-empty caller-visible canonical expansion" in errors[0]


def test_verified_question_normalizes_filter_missing_policy() -> None:
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "How many companies are in Maharashtra?",
            "resources": ["entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                    "missing_policy": "report_unassessable",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "metric",
                    "description": "Count matching companies.",
                    "provenance": "user",
                    "source_text": "How many companies are in Maharashtra?",
                }
            ],
        }
    )

    assert "missing_policy" not in question.filters[0]
    assert [item.model_dump() for item in question.missing_data_requirements] == [
        {
            "resource": "entities",
            "field": "state",
            "policy": "report_unassessable",
            "usage": "predicate",
        }
    ]


def test_verified_question_does_not_duplicate_normalized_missing_requirement() -> None:
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "How many companies are in Maharashtra?",
            "resources": ["entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "missing_policy": "report_unassessable",
                }
            ],
            "missing_data_requirements": [{"resource": "entities", "field": "state", "usage": "predicate"}],
            "evidence_obligations": [
                {
                    "kind": "metric",
                    "description": "Count matching companies.",
                    "provenance": "user",
                    "source_text": "How many companies are in Maharashtra?",
                }
            ],
        }
    )

    assert len(question.missing_data_requirements) == 1


def test_grounding_prompt_uses_complete_meaning_feasibility_without_uat_examples():
    prompt = PROMPTS["value_grounding"]
    assert "cannot bind every explicit actor, role, predicate, relationship" in prompt
    assert "requested record grain" in prompt
    assert "required qualifier is absent" in prompt
    assert "resource, cohort, or metric over a display preference" in prompt
    assert "Person identity resolves a canonical value, not a relationship" in prompt
    assert "explicit relationship wording wins, otherwise clarify" in prompt
    assert "API filter availability never makes relationship fields interchangeable" in prompt
    for evaluation_specific_term in ("Axis Bank", "Maharashtra", "Zeon", "Shubh", "Q20"):
        assert evaluation_specific_term not in prompt


def test_planning_prompt_keeps_report_unassessable_fields_out_of_read_filters():
    instruction = PROMPTS["query_planning"]
    assert "never put that field in any read's filters" in instruction


def test_interpretation_prompt_marks_free_text_analysis_independently_of_shape():
    prompt = PROMPTS["question_interpretation"]
    assert "qualitative_analysis=true" in prompt
    assert "reasons, remarks, notes, concerns, explanations, or themes" in prompt
    assert "even when requested_shape is ranked or list" in prompt


async def test_model_call_usage_sink_records_clean_and_repair_attempts():
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    completions = FakeCompletions(
        [
            {},
            {
                "standalone_question": "How many active leads are there?",
                "intent_ledger": [
                    {
                        "kind": "metric",
                        "source_message_index": 0,
                        "resolved_text": "How many active leads are there?",
                    }
                ],
                "introduced_constraints": [],
            },
        ],
        usage=usage,
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    sink = []
    token = _usage_sink.set(sink)
    try:
        await stages.resolve_conversation(
            ConversationInput(messages=[{"role": "user", "content": "How many active leads are there?"}])
        )
    finally:
        _usage_sink.reset(token)
    assert len(sink) == 2
    assert sink[0].repair is False
    assert sink[1].repair is True
    assert sink[0].prompt_tokens == 11
    assert sink[0].cached_prompt_tokens == 3
    assert sink[0].validation_errors is not None
    assert sink[0].validation_errors[0]["loc"] == ["standalone_question"]
    assert sink[1].validation_errors is None


async def test_model_call_without_usage_keeps_counts_nullable():
    completions = FakeCompletions(
        [
            {
                "standalone_question": "How many active leads are there?",
                "intent_ledger": [
                    {
                        "kind": "metric",
                        "source_message_index": 0,
                        "resolved_text": "How many active leads are there?",
                    }
                ],
                "introduced_constraints": [],
            }
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    sink = []
    token = _usage_sink.set(sink)
    try:
        await stages.resolve_conversation(
            ConversationInput(messages=[{"role": "user", "content": "How many active leads are there?"}])
        )
    finally:
        _usage_sink.reset(token)
    assert len(sink) == 1
    assert sink[0].response_received is True
    assert sink[0].prompt_tokens is None
    assert sink[0].completion_tokens is None
    assert sink[0].total_tokens is None


async def test_provider_exception_records_an_unanswered_attempt():
    completions = RaisingCompletions([])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    sink = []
    token = _usage_sink.set(sink)
    try:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await stages.resolve_conversation(
                ConversationInput(messages=[{"role": "user", "content": "How many active leads are there?"}])
            )
    finally:
        _usage_sink.reset(token)
    assert len(sink) == 1
    assert sink[0].response_received is False
    assert sink[0].prompt_tokens is None


async def test_each_reasoning_stage_uses_its_own_call_and_model():
    completions = FakeCompletions(
        [
            {
                "standalone_question": "How many active leads does Chetan have?",
                "intent_ledger": [
                    {
                        "kind": "metric",
                        "source_message_index": 0,
                        "resolved_text": "How many active leads does Chetan have?",
                    }
                ],
                "introduced_constraints": [],
            },
            {
                "standalone_question": "How many active leads does Chetan have?",
                "terms": [
                    {"text": "active leads", "role": "subject", "source_text": "active leads"},
                    {"text": "Chetan", "role": "actor", "source_text": "Chetan"},
                ],
                "literals": [],
                "requested_shape": "scalar",
            },
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["leads"],
                    "semantic_bindings": [
                        {
                            "user_term": "active leads",
                            "binding_kind": "lifecycle",
                            "resource": "leads",
                            "field": "status",
                            "canonical_values": ["Active"],
                            "definition": "Active is a Lead lifecycle status.",
                            "passage_ids": ["leads.lifecycle"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "status",
                            "user_term": "active",
                            "canonical_value": "Active",
                        },
                        {
                            "field": "rm",
                            "user_term": "Chetan",
                            "canonical_value": "CM",
                            "candidate_id": "person-1",
                        }
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "The resource and values are available.",
                "verified_question": {
                    "standalone_question": "How many active leads does Chetan have?",
                    "resources": ["leads"],
                    "answer_shape": "scalar",
                    "metrics": [{"operation": "count"}],
                    "filters": [
                        {"field": "status", "operator": "eq", "value": "Active"},
                        {"field": "rm", "operator": "eq", "value": "CM"},
                    ],
                    "grounded_values": [
                        {
                            "field": "rm",
                            "user_term": "Chetan",
                            "canonical_value": "CM",
                            "candidate_id": "person-1",
                        }
                    ],
                    "semantic_bindings": [
                        {
                            "user_term": "active leads",
                            "binding_kind": "lifecycle",
                            "resource": "leads",
                            "field": "status",
                            "canonical_values": ["Active"],
                            "passage_ids": ["leads.lifecycle"],
                        }
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "metric",
                            "description": "Count active leads",
                            "resource": "leads",
                            "provenance": "ontology",
                            "passage_ids": ["leads.lifecycle"],
                        }
                    ],
                },
            },
            {
                "reads": [
                    {
                        "name": "active_leads",
                        "resource": "leads",
                        "filters": {"status": "Active", "rm": "CM"},
                    }
                ],
                "operations": [
                    {
                        "name": "count_active",
                        "operation": "count",
                        "input": "active_leads",
                        "output": "active_count",
                        "arguments": {},
                    }
                ],
            },
            {"answer": "There are 107 active leads for Chetan."},
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    stages = ModelStages(client, _settings())
    retrieval = SemanticRetrievalResult(
        query="active leads Chetan",
        matches=[
            RetrievalMatch(
                passage_id="leads.lifecycle",
                source="leads",
                version="1",
                content="Active is a Lead.status lifecycle value.",
                metadata={
                    "planning": True,
                    "role": "semantic",
                    "audience": ["grounding", "planning"],
                },
                fusion_score=1.0,
            )
        ],
    )
    resolution = await stages.resolve_conversation(
        ConversationInput(
            messages=[
                {
                    "role": "user",
                    "content": "How many active leads does Chetan have?",
                }
            ]
        )
    )
    interpretation = await stages.interpret(resolution)
    grounding = await stages.ground(
        interpretation,
        retrieval,
        {
            "people": [{"id": "person-1", "name": "CM", "full_name": "Chetan"}],
            "reference_values": {"Lead Status": ["Active", "Converted", "Dropped"]},
        },
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    plan = await stages.plan(answerability.verified_question, retrieval)
    answer = await stages.answer(
        answerability.verified_question,
        ResultEvidence(
            verified_question=answerability.verified_question,
            facts={"count": 107},
            row_presentation={"total_count": 0, "displayed_count": 0, "truncated": False},
            completeness=Completeness.COMPLETE,
            scope="FULL caller-visible data",
            retrieved_at=datetime.now(UTC),
        ),
    )
    assert [call["model"] for call in completions.calls] == [
        "conversation-model",
        "interpretation-model",
        "grounding-model",
        "answerability-model",
        "planning-model",
        "answer-model",
    ]
    assert all(call["response_format"]["type"] == "json_schema" for call in completions.calls)
    assert all(call["response_format"]["json_schema"]["strict"] for call in completions.calls)
    assert all(call["max_completion_tokens"] == 32768 for call in completions.calls)
    assert all(call["extra_body"] == {"reasoning": {"enabled": True}} for call in completions.calls)
    assert all("JSON" in call["messages"][0]["content"] for call in completions.calls)
    assert '"canonical_value_source": "name"' in completions.calls[2]["messages"][1]["content"]
    assert '"analyst": {' in completions.calls[2]["messages"][1]["content"]
    grounding_prompt = completions.calls[2]["messages"][0]["content"]
    assert "complete question" in grounding_prompt
    assert "do not ground isolated words independently" in grounding_prompt
    assert "complete interpretations" in grounding_prompt
    assert "evidence_obligations" in completions.calls[3]["messages"][0]["content"]
    answerability_payload = completions.calls[3]["messages"][1]["content"]
    assert "semantic_material" not in answerability_payload
    assert "semantic_bindings" in answerability_payload
    interpretation_payload = completions.calls[1]["messages"][1]["content"]
    assert "ledger_resource_catalog" not in interpretation_payload
    assert "canonical_resource_phrases" not in interpretation_payload
    assert "Preserve the user's wording" in interpretation_payload
    assert "Do not select or name Ledger resources" in completions.calls[1]["messages"][0]["content"]
    planning_payload = completions.calls[4]["messages"][1]["content"]
    assert "relationship_required=true" in planning_payload
    assert '"operation_contracts"' not in planning_payload
    planning_schema = completions.calls[4]["response_format"]["json_schema"]["schema"]
    assert "SetUnionOperation" in planning_schema["$defs"]
    assert "DistinctCountOperation" in planning_schema["$defs"]
    assert "Use rank rather than sort plus limit" in planning_payload
    assert "many matching items there are" in completions.calls[5]["messages"][0]["content"]
    assert plan.reads[0].resource == "leads"
    assert answer == "There are 107 active leads for Chetan."


async def test_answer_payload_is_bounded_and_keeps_only_the_verified_question():
    completions = FakeCompletions([{"answer": "The result is complete."}])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List the qualifying deals.",
            "resources": ["deals"],
            "answer_shape": "list",
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Return the qualifying deal rows.",
                    "provenance": "user",
                    "source_text": "List the qualifying deals.",
                }
            ],
        }
    )
    rows = [{"deal_no": f"D-{index:03d}", "amount_cr": index} for index in range(200)]
    evidence = ResultEvidence(
        verified_question=question,
        facts={"row_count": 200},
        rows=rows,
        row_presentation={"total_count": 200, "displayed_count": 50, "truncated": True},
        contributing_ids=[f"id-{index}" for index in range(200)],
        contributing_records=[{"resource": "deals", "id": f"id-{index}"} for index in range(200)],
        completeness=Completeness.COMPLETE,
        scope="FULL caller-visible data",
        retrieved_at=datetime.now(UTC),
    )

    await stages.answer(question, evidence)

    request_payload = json.loads(completions.calls[0]["messages"][1]["content"])
    payload = request_payload["input"]
    assert list(payload) == ["verified_question", "result_evidence", "instruction"]
    result_evidence = payload["result_evidence"]
    assert result_evidence["rows"] == {
        "count": 200,
        "columns": ["deal_no", "amount_cr"],
        "sample": rows[:5],
    }
    assert "row_presentation" not in result_evidence
    assert "scope" not in result_evidence
    assert result_evidence["contributing_record_count"] == 200
    assert "contributing_ids" not in result_evidence
    assert "contributing_records" not in result_evidence


async def test_answer_payload_carries_business_result_counts():
    completions = FakeCompletions([{"answer": "There are 88 matching records."}])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List the records.",
            "resources": ["deals"],
            "answer_shape": "list",
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List records.",
                    "provenance": "user",
                    "source_text": "List the records.",
                }
            ],
        }
    )
    evidence = ResultEvidence(
        verified_question=question,
        facts={"records_row_count": 88},
        rows=[{"id": index} for index in range(88)],
        row_presentation={"total_count": 88, "displayed_count": 50, "truncated": True},
        completeness=Completeness.COMPLETE,
        scope="FULL caller-visible data",
        retrieved_at=datetime.now(UTC),
    )

    await stages.answer(question, evidence)

    request_payload = json.loads(completions.calls[0]["messages"][1]["content"])["input"]
    assert request_payload["result_evidence"]["rows"]["count"] == 88
    assert "row_presentation" not in request_payload["result_evidence"]
    assert "business-facing prose" in request_payload["instruction"]
    assert "Do not transcribe sample rows" in request_payload["instruction"]


async def test_scalar_answer_has_no_empty_table_or_internal_scope_metadata():
    completions = FakeCompletions([{"answer": "There are 326 leads."}])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion(
        standalone_question="How many leads are there?", resources=["leads"], answer_shape="scalar",
        evidence_obligations=[{"kind": "metric", "description": "Count leads.",
                               "provenance": "user", "source_text": "How many leads are there?"}],
    )
    evidence = ResultEvidence(
        verified_question=question, facts={"lead_count": 326},
        row_presentation={"total_count": 0, "displayed_count": 0, "truncated": False},
        completeness=Completeness.COMPLETE, scope="FULL caller-visible data",
        retrieved_at=datetime.now(UTC),
    )
    await stages.answer(question, evidence)
    payload = json.loads(completions.calls[0]["messages"][1]["content"])["input"]["result_evidence"]
    assert payload["facts"] == {"lead_count": 326}
    assert not {"rows", "row_presentation", "scope"} & payload.keys()


async def test_invalid_structured_output_gets_one_same_stage_schema_repair():
    completions = FakeCompletions(
        [
            {},
            {
                "standalone_question": "How many active leads are there?",
                "intent_ledger": [
                    {
                        "kind": "metric",
                        "source_message_index": 0,
                        "resolved_text": "How many active leads are there?",
                    }
                ],
                "introduced_constraints": [],
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(
        ConversationInput(
            messages=[
                {
                    "role": "user",
                    "content": "How many active leads are there?",
                }
            ]
        )
    )
    assert result.standalone_question == "How many active leads are there?"
    assert [call["model"] for call in completions.calls] == [
        "conversation-model",
        "conversation-model",
    ]
    repair_system = completions.calls[1]["messages"][0]["content"]
    repair_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "Change only what the validation errors require" in repair_system
    assert json.loads(repair_payload["invalid_output"]) == {}
    assert repair_payload["validation_errors"][0]["loc"] == ["standalone_question"]
    assert "required_output_schema" not in repair_payload
    assert completions.calls[1]["response_format"] == completions.calls[0]["response_format"]
    assert (
        repair_payload["repair_context"]["conversation_input"]["messages"][0]["content"]
        == "How many active leads are there?"
    )


async def test_reasoning_wrapped_json_is_postprocessed_without_repair():
    output = {
        "standalone_question": "How many active leads are there?",
        "intent_ledger": [
            {
                "kind": "metric",
                "source_message_index": 0,
                "resolved_text": "How many active leads are there?",
            }
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions(
        [f'<reasoning>Consider {{"an_example": true}} first.</reasoning>\n```json\n{json.dumps(output)}\n```']
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.resolve_conversation(
        ConversationInput(messages=[{"role": "user", "content": output["standalone_question"]}])
    )

    assert result.standalone_question == output["standalone_question"]
    assert len(completions.calls) == 1


async def test_reasoning_wrapped_repair_json_is_postprocessed():
    repaired = {
        "standalone_question": "How many active leads are there?",
        "intent_ledger": [
            {
                "kind": "metric",
                "source_message_index": 0,
                "resolved_text": "How many active leads are there?",
            }
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions(
        [
            {},
            f"<think>Repair the object.</think>\n{json.dumps(repaired)}",
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.resolve_conversation(
        ConversationInput(messages=[{"role": "user", "content": repaired["standalone_question"]}])
    )

    assert result.standalone_question == repaired["standalone_question"]
    assert len(completions.calls) == 2


async def test_conversation_resolution_repairs_an_introduced_restriction_once():
    messages = [
        {"role": "user", "content": "How many deals do we have?"},
        {"role": "assistant", "content": "Which business book?"},
        {"role": "user", "content": "In Maharashtra, including missing states."},
    ]
    invalid = {
        "standalone_question": (
            "How many current commercial deals are in Maharashtra, including missing states?"
        ),
        "used_prior_context": True,
        "intent_ledger": [
            {
                "kind": "metric",
                "source_message_index": 0,
                "resolved_text": "How many current commercial deals",
            },
            {
                "kind": "scope",
                "source_message_index": 2,
                "resolved_text": "in Maharashtra",
            },
            {
                "kind": "inclusion",
                "source_message_index": 2,
                "resolved_text": "including missing states",
            },
        ],
        "introduced_constraints": ["current"],
    }
    repaired = {
        **invalid,
        "standalone_question": ("How many commercial deals are in Maharashtra, including missing states?"),
        "intent_ledger": [
            {**invalid["intent_ledger"][0], "resolved_text": "How many commercial deals"},
            *invalid["intent_ledger"][1:],
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(ConversationInput(messages=messages))
    assert "current" not in result.standalone_question
    assert "including missing states" in result.standalone_question
    assert len(completions.calls) == 2


async def test_conversation_resolution_omits_assistant_result_table_without_reindexing():
    messages = [
        {"role": "user", "content": "List our hot deals."},
        {
            "role": "assistant",
            "content": (
                "There are 72 hot deals.\n\n"
                "| code | temperature |\n"
                "| --- | --- |\n"
                "| A | Hot |\n"
                "| B | Hot |"
            ),
        },
        {"role": "user", "content": "How many of these are handled by Chetan?"},
    ]
    output = {
        "standalone_question": "How many hot deals are handled by Chetan?",
        "intent_ledger": [
            {
                "kind": "scope",
                "source_message_index": 0,
                "resolved_text": "hot deals",
            },
            {
                "kind": "metric",
                "source_message_index": 2,
                "resolved_text": "count handled by Chetan",
            },
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions([output])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.resolve_conversation(ConversationInput(messages=messages))

    assert result.used_prior_context is True
    request = json.loads(completions.calls[0]["messages"][1]["content"])["input"]
    assert len(request["messages"]) == 3
    assert request["messages"][0] == messages[0]
    assert request["messages"][2] == messages[2]
    assert request["messages"][1]["content"] == (
        "There are 72 hot deals.\n\n"
        "[Host-rendered result table omitted from conversation history.]"
    )


async def test_conversation_resolution_keeps_model_output_without_semantic_repair():
    messages = [
        {"role": "user", "content": "How many deals do we have with Zeon?"},
        {"role": "assistant", "content": "Which Zeon and which business books?"},
        {
            "role": "user",
            "content": (
                "Use Zeon Charging for own-book and syndication, and keep any unconverted "
                "Zeon entity lead separate."
            ),
        },
    ]
    ledger = [
        {
            "kind": "entity",
            "source_message_index": 2,
            "resolved_text": "Zeon Charging",
        },
        {
            "kind": "scope",
            "source_message_index": 2,
            "resolved_text": "own-book and syndication",
        },
        {
            "kind": "separate_cohort",
            "source_message_index": 2,
            "resolved_text": "unconverted Zeon entity lead separately",
        },
    ]
    invalid = {
        "standalone_question": "Count Zeon Charging own-book and syndication deals.",
        "used_prior_context": False,
        "intent_ledger": ledger,
        "introduced_constraints": [],
    }
    completions = FakeCompletions([invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(ConversationInput(messages=messages))
    assert result.standalone_question == invalid["standalone_question"]
    assert len(completions.calls) == 1


async def test_conversation_resolution_keeps_only_source_index_without_a_quote():
    messages = [
        {"role": "user", "content": "Which lenders have we never approached?"},
    ]
    invalid = {
        "standalone_question": "Which lenders have we never approached?",
        "intent_ledger": [
            {
                "kind": "relationship",
                "source_message_index": 0,
                "resolved_text": "we never approached",
            }
        ],
    }
    completions = FakeCompletions([invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(ConversationInput(messages=messages))
    assert result.intent_ledger[0].source_message_index == 0
    assert "source_message" not in result.intent_ledger[0].model_dump()
    assert len(completions.calls) == 1


async def test_conversation_resolution_accepts_supported_ellipsis_fragments():
    messages = [
        {
            "role": "user",
            "content": "Check our current exposure for Example Energy and keep Example Green separate.",
        }
    ]
    output = {
        "standalone_question": (
            "Check our current own-book and syndication exposure for Example Energy, "
            "keeping Example Green separate."
        ),
        "used_prior_context": False,
        "intent_ledger": [
            {
                "kind": "metric",
                "source_message_index": 0,
                "resolved_text": "our ... exposure for Example Energy",
            },
            {
                "kind": "separate_cohort",
                "source_message_index": 0,
                "resolved_text": "keeping Example Green separate",
            },
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions([output])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(ConversationInput(messages=messages))
    assert result.standalone_question == output["standalone_question"]
    assert len(completions.calls) == 1


async def test_conversation_resolution_accepts_rephrased_non_cohort_intent():
    output = {
        "standalone_question": "How many leads are active?",
        "intent_ledger": [
            {
                "kind": "entity",
                "source_message_index": 0,
                "resolved_text": "Axis Bank",
            }
        ],
        "introduced_constraints": [],
    }
    completions = FakeCompletions([output])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    result = await stages.resolve_conversation(
        ConversationInput(
            messages=[{"role": "user", "content": "How many active leads does Axis Bank have?"}]
        )
    )
    assert result.standalone_question == output["standalone_question"]
    assert len(completions.calls) == 1


async def test_invalid_schema_repair_still_fails_after_one_attempt():
    completions = FakeCompletions(
        [
            {"used_prior_context": False},
            {"used_prior_context": True},
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    sink = []
    token = _usage_sink.set(sink)
    try:
        with pytest.raises(ValueError, match="standalone_question"):
            await stages.resolve_conversation(ConversationInput(messages=[]))
    finally:
        _usage_sink.reset(token)
    assert len(completions.calls) == 2
    assert len(sink) == 2
    assert all(call.validation_errors for call in sink)
    assert all(
        call.validation_errors is not None and call.validation_errors[0]["loc"] == ["standalone_question"]
        for call in sink
    )


async def test_planning_repairs_non_iso_ageing_date_once():
    invalid = {
        "reads": [{"name": "leads", "resource": "leads"}],
        "operations": [
            {
                "name": "stale",
                "operation": "filter",
                "input": "leads",
                "output": "stale",
                "arguments": {
                    "field": "last_interaction_date",
                    "operator": "older_than_days",
                    "days": 21,
                    "as_of": "14 August",
                },
            }
        ],
        "result_names": ["stale"],
    }
    repaired = {
        **invalid,
        "operations": [
            {
                **invalid["operations"][0],
                "arguments": {
                    **invalid["operations"][0]["arguments"],
                    "as_of": "2026-08-14",
                },
            }
        ],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "Which leads were stale on 14 August 2026?",
            "resources": ["leads"],
            "answer_shape": "list",
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List stale leads",
                    "provenance": "user",
                    "source_text": "Which leads",
                }
            ],
        }
    )
    plan = await stages.plan(question, SemanticRetrievalResult(query="stale leads", matches=[]))
    assert plan.operations[0].arguments["as_of"] == "2026-08-14"
    assert len(completions.calls) == 2
    repair_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "YYYY-MM-DD" in repair_payload["validation_errors"][0]["msg"]


async def test_planning_repairs_an_omitted_verified_filter_once():
    invalid = {
        "reads": [{"name": "entities", "resource": "entities"}],
        "operations": [],
        "result_names": ["entities"],
    }
    repaired = {
        **invalid,
        "reads": [
            {
                "name": "entities",
                "resource": "entities",
                "filters": {"sector": "EV Mobility"},
            }
        ],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List companies in the selected sector.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "equals",
                    "value": "EV Mobility",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List qualifying companies",
                    "provenance": "user",
                    "source_text": "List companies",
                }
            ],
        }
    )
    plan = await stages.plan(question, SemanticRetrievalResult(query="sector", matches=[]))
    assert plan.reads[0].filters == {"sector": "EV Mobility"}
    assert len(completions.calls) == 2
    repair_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "verified filter id(s): f1" in repair_payload["validation_errors"][0]["msg"]


async def test_answerability_derives_typed_missing_related_value_requirement():
    meaning = {
        "resources": ["deals", "entities"],
        "grounded_values": [
            {
                "field": "state",
                "user_term": "Maharashtra",
                "canonical_value": "Maharashtra",
                "exact_match": True,
            }
        ],
        "semantic_bindings": [
            {
                "user_term": "Maharashtra",
                "binding_kind": "field",
                "resource": "entities",
                "field": "state",
                "canonical_values": ["Maharashtra"],
                "passage_ids": ["relationships.entity_dimensions"],
            },
            {
                "user_term": "Maharashtra",
                "binding_kind": "relationship",
                "resource": "deals",
                "field": "entity_id",
                "details": {
                    "related_resource": "entities",
                    "related_field": "entities.state",
                },
                "passage_ids": ["relationships.entity_dimensions"],
            }
        ],
    }
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The related state is filterable.",
        "verified_question": {
            "standalone_question": "How many commercial deals are in Maharashtra?",
            "resources": ["deals", "entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Return the count.",
                    "provenance": "user",
                    "source_text": "How many",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "How many commercial deals are in Maharashtra?",
            "terms": [{"text": "Maharashtra", "source_text": "Maharashtra"}],
            "requested_shape": "scalar",
        }
    )
    result = await stages.answerability(
        interpretation,
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": meaning,
            }
        ),
        SemanticRetrievalResult(query="state", matches=[]),
    )
    assert result.verified_question is not None
    requirement = result.verified_question.missing_data_requirements[0]
    assert (requirement.resource, requirement.field, requirement.usage) == (
        "entities",
        "state",
        "predicate",
    )
    assert len(completions.calls) == 1


async def test_answerability_discards_ungoverned_filter_missing_requirement():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The deal stage is filterable.",
        "verified_question": {
            "standalone_question": "List rejected deals.",
            "resources": ["deals"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "deals",
                    "field": "stage",
                    "operator": "equals",
                    "value": "Rejected",
                    "missing_policy": "report_unassessable",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List rejected deals.",
                    "provenance": "user",
                    "source_text": "List rejected deals.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {"standalone_question": "List rejected deals.", "requested_shape": "list"}
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["deals"],
                    "semantic_bindings": [
                        {
                            "user_term": "rejected",
                            "binding_kind": "lifecycle",
                            "resource": "deals",
                            "field": "stage",
                            "canonical_values": ["Rejected"],
                            "passage_ids": ["deals.lifecycle"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "stage",
                            "user_term": "rejected",
                            "canonical_value": "Rejected",
                        }
                    ],
                },
            }
        ),
        SemanticRetrievalResult(query="rejected deals", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.missing_data_requirements == []
    assert "missing_policy" not in result.verified_question.filters[0]
    assert len(completions.calls) == 1


async def test_answerability_overwrites_wrong_missing_requirement_usage():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The governed amount can be summed.",
        "verified_question": {
            "standalone_question": "What is the total lending amount?",
            "resources": ["lending"],
            "answer_shape": "scalar",
            "metrics": [{"resource": "lending", "field": "amount_cr", "operation": "sum"}],
            "missing_data_requirements": [
                {"resource": "lending", "field": "amount_cr", "usage": "predicate"}
            ],
            "evidence_obligations": [
                {
                    "kind": "metric",
                    "description": "Return the total lending amount.",
                    "provenance": "user",
                    "source_text": "total lending amount",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "What is the total lending amount?",
                "requested_shape": "scalar",
            }
        ),
        ValueGroundingResult.model_validate(
            {"status": "RESOLVED", "established_meaning": {"resources": ["lending"]}}
        ),
        SemanticRetrievalResult(query="total lending amount", matches=[]),
    )

    assert result.verified_question is not None
    assert [item.usage for item in result.verified_question.missing_data_requirements] == ["measure"]
    assert len(completions.calls) == 1


async def test_answerability_accepts_malformed_model_missing_requirements_before_host_derivation():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The governed state is filterable.",
        "verified_question": {
            "standalone_question": "List companies in Maharashtra.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "missing_data_requirements": [
                {"resource": ["entities"], "field": "state", "usage": "predicate"},
                {"resource": "entities", "field": ["state"], "usage": "measure"},
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List companies in Maharashtra.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List companies in Maharashtra.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["entities"],
                    "semantic_bindings": [
                        {
                            "user_term": "Maharashtra",
                            "binding_kind": "field",
                            "resource": "entities",
                            "field": "state",
                            "canonical_values": ["Maharashtra"],
                            "passage_ids": ["entities.state"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "state",
                            "user_term": "Maharashtra",
                            "canonical_value": "Maharashtra",
                            "exact_match": True,
                        }
                    ],
                },
            }
        ),
        SemanticRetrievalResult(query="companies in Maharashtra", matches=[]),
    )

    assert result.verified_question is not None
    assert [item.model_dump() for item in result.verified_question.missing_data_requirements] == [
        {
            "resource": "entities",
            "field": "state",
            "policy": "report_unassessable",
            "usage": "predicate",
        }
    ]
    assert len(completions.calls) == 1


async def test_answerability_does_not_classify_display_metric_as_measure():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The value is displayed on each matching row.",
        "verified_question": {
            "standalone_question": "List opportunities above 250 Cr.",
            "resources": ["asset_monetisation"],
            "answer_shape": "list",
            "metrics": [
                {
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "name": "indicative value",
                    "unit": "Cr",
                }
            ],
            "filters": [
                {
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "operator": "gt",
                    "value": 250,
                }
            ],
            "missing_data_requirements": [
                {
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "usage": "measure",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching opportunities.",
                    "provenance": "user",
                    "source_text": "List opportunities above 250 Cr.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List opportunities above 250 Cr.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {"resources": ["asset_monetisation"]},
            }
        ),
        SemanticRetrievalResult(query="opportunities above 250 Cr", matches=[]),
    )

    assert result.verified_question is not None
    assert [item.usage for item in result.verified_question.missing_data_requirements] == ["predicate"]
    assert len(completions.calls) == 1


async def test_answerability_compiles_grounded_qualitative_filter_to_evidence_obligation():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "Recorded case reasons can be analyzed qualitatively.",
        "verified_question": {
            "standalone_question": "Which rejected cases were loss-making?",
            "resources": ["lending"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "lending",
                    "field": "stage",
                    "operator": "equals",
                    "value": "Rejected",
                },
                {
                    "resource": "lending",
                    "field": "remarks",
                    "operator": "equals",
                    "value": "loss-making",
                },
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching cases.",
                    "provenance": "user",
                    "source_text": "Which rejected cases were loss-making?",
                }
            ],
        },
    }
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["lending"],
                "semantic_bindings": [
                    {
                        "user_term": "rejected",
                        "binding_kind": "lifecycle",
                        "resource": "lending",
                        "field": "stage",
                        "canonical_values": ["Rejected"],
                        "passage_ids": ["lending.lifecycle"],
                    },
                    {
                        "user_term": "loss-making reasons",
                        "binding_kind": "relationship",
                        "resource": "lending",
                        "field": "remarks",
                        "passage_ids": ["qualitative.lending_remarks.qualitative_meanings_analyzed"],
                    }
                ],
                "grounded_values": [
                    {
                        "field": "stage",
                        "user_term": "rejected",
                        "canonical_value": "Rejected",
                    }
                ],
            },
        }
    )
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "Which rejected cases were loss-making?",
                "requested_shape": "list",
            }
        ),
        grounding,
        SemanticRetrievalResult(query="loss-making reasons", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.filters == [
        {
            "resource": "lending",
            "field": "stage",
            "operator": "equals",
            "value": "Rejected",
        }
    ]
    assert result.verified_question.qualitative_analysis is True
    assert any(
        obligation.kind == "qualitative"
        and obligation.resource == "lending"
        and obligation.field == "remarks"
        for obligation in result.verified_question.evidence_obligations
    )
    assert [item.usage for item in result.verified_question.missing_data_requirements] == [
        "qualitative_source"
    ]
    assert len(completions.calls) == 1


async def test_answerability_rejects_unproven_governed_composite_value_before_planning():
    invalid = {
        "outcome": "ANSWERABLE",
        "reason": "The mandate status is filterable.",
        "verified_question": {
            "standalone_question": "List mandates sent and signed for syndication.",
            "resources": ["syndication"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "syndication",
                    "field": "mandate_status",
                    "operator": "equals",
                    "value": "sent and signed-for-syndication",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching mandates.",
                    "provenance": "user",
                    "source_text": "List mandates sent and signed for syndication.",
                }
            ],
        },
    }
    repaired = {
        "outcome": "OUT_OF_SCOPE",
        "reason": "No caller-visible canonical composite value establishes that predicate.",
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List mandates sent and signed for syndication.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {"status": "RESOLVED", "established_meaning": {"resources": ["syndication"]}}
        ),
        SemanticRetrievalResult(query="mandates sent signed", matches=[]),
    )

    assert result.outcome == "OUT_OF_SCOPE"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert (
        "syndication.mandate_status=sent and signed-for-syndication lacks "
        "host-validated executable provenance"
    ) in repair["validation_errors"][0]["msg"]


async def test_answerability_accepts_all_members_of_grounded_lifecycle_family():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The sanctioned lending lifecycle family is grounded.",
        "verified_question": {
            "standalone_question": "Show sanctioned lending cases.",
            "resources": ["lending"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "lending",
                    "field": "stage",
                    "operator": "in",
                    "values": ["Sanctioned", "CP/CS Completed", "Disbursed"],
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List sanctioned lending cases.",
                    "provenance": "user",
                    "source_text": "Show sanctioned lending cases.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["lending"],
                "semantic_bindings": [
                    {
                        "user_term": "sanctioned",
                        "binding_kind": "lifecycle",
                        "resource": "lending",
                        "field": "stage",
                        "canonical_values": ["Sanctioned", "CP/CS Completed", "Disbursed"],
                        "passage_ids": ["lifecycle.lending"],
                    }
                ],
                "grounded_values": [
                    {
                        "resource": "lending",
                        "field": "stage",
                        "user_term": "sanctioned",
                        "canonical_value": "Sanctioned",
                    }
                ],
            },
        }
    )

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {"standalone_question": "Show sanctioned lending cases.", "requested_shape": "list"}
        ),
        grounding,
        SemanticRetrievalResult(query="sanctioned lending", matches=[]),
    )

    assert result.outcome == "ANSWERABLE"
    assert result.verified_question is not None
    assert result.verified_question.filters[0]["values"] == [
        "Sanctioned",
        "CP/CS Completed",
        "Disbursed",
    ]
    assert len(completions.calls) == 1


async def test_lifecycle_family_survives_grounding_answerability_and_planning():
    family = ["Sanctioned", "CP/CS Completed", "Disbursed"]
    completions = FakeCompletions(
        [
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["lending"],
                    "semantic_bindings": [
                        {
                            "user_term": "sanctioned",
                            "binding_kind": "lifecycle",
                            "resource": "lending",
                            "field": "stage",
                            "canonical_values": ["sanctioned", "cp/cs completed", "disbursed"],
                            "passage_ids": ["lifecycle.lending"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "resource": "lending",
                            "field": "stage",
                            "user_term": "sanctioned",
                            "canonical_value": "sanctioned",
                        }
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "The lending lifecycle family is established.",
                "verified_question": {
                    "standalone_question": "Show sanctioned lending cases.",
                    "resources": ["lending"],
                    "answer_shape": "list",
                    "filters": [
                        {
                            "resource": "lending",
                            "field": "stage",
                            "operator": "in",
                            "values": family,
                        }
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "List sanctioned lending cases.",
                            "provenance": "user",
                            "source_text": "Show sanctioned lending cases.",
                        }
                    ],
                },
            },
            {
                "reads": [{"name": "lending_cases", "resource": "lending"}],
                "operations": [
                    {
                        "name": "sanctioned_family",
                        "operation": "filter",
                        "input": "lending_cases",
                        "output": "sanctioned_family",
                        "arguments": {
                            "field": "stage",
                            "operator": "in",
                            "values": family,
                        },
                    }
                ],
                "result_names": ["sanctioned_family"],
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {"standalone_question": "Show sanctioned lending cases.", "requested_shape": "list"}
    )
    retrieval = SemanticRetrievalResult(
        query="sanctioned lending",
        matches=[
            RetrievalMatch(
                passage_id="lifecycle.lending",
                source="lending",
                version="1",
                content="Sanctioned lending includes three governed stages.",
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
        ],
    )

    grounding = await stages.ground(
        interpretation,
        retrieval,
        {"reference_values": {"Lending Stage": family}},
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    plan = await stages.plan(answerability.verified_question, retrieval)

    assert grounding.established_meaning.semantic_bindings[0].canonical_values == family
    assert answerability.verified_question.filters[0]["values"] == family
    assert plan.reads[0].filters == {}
    assert plan.operations[0].arguments["values"] == family
    assert plan.operations[0].arguments["verified_filter_ids"] == ["f1"]
    assert len(completions.calls) == 3


async def test_person_relationship_meaning_survives_grounding_and_wrong_field_plan_repair():
    person_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    passage_id = "resources.lending.human_readable_lending"
    completions = FakeCompletions(
        [
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["lending"],
                    "semantic_bindings": [
                        {
                            "user_term": "Asha's own-book cases",
                            "binding_kind": "relationship",
                            "resource": "lending",
                            "field": "analyst",
                            "canonical_values": ["AR"],
                            "definition": "The assigned analyst on the lending case.",
                            "passage_ids": [passage_id],
                        }
                    ],
                    "grounded_values": [
                        {
                            "resource": "lending",
                            "field": "analyst",
                            "user_term": "Asha",
                            "canonical_value": "AR",
                            "candidate_id": person_id,
                            "candidate_label": "Asha Rao",
                            "exact_match": True,
                            "normalized_match": True,
                        }
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "The assigned-analyst relationship is grounded.",
                "verified_question": {
                    "standalone_question": "Count Asha's active own-book lending cases.",
                    "resources": ["lending"],
                    "answer_shape": "scalar",
                    "filters": [
                        {
                            "resource": "lending",
                            "field": "analyst",
                            "operator": "equals",
                            "value": "AR",
                        }
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "Count the assigned analyst's cases.",
                            "provenance": "user",
                            "source_text": "Count",
                        }
                    ],
                },
            },
            {
                "reads": [
                    {
                        "name": "lending_cases",
                        "resource": "lending",
                        "filters": {"rm": "AR"},
                    }
                ],
                "operations": [
                    {
                        "name": "case_count",
                        "operation": "count",
                        "input": "lending_cases",
                        "output": "case_count",
                        "arguments": {"as": "case_count"},
                    }
                ],
                "result_names": ["case_count"],
            },
            {
                "reads": [{"name": "lending_cases", "resource": "lending"}],
                "operations": [
                    {
                        "name": "assigned_analyst",
                        "operation": "filter",
                        "input": "lending_cases",
                        "output": "assigned_analyst",
                        "arguments": {
                            "field": "analyst",
                            "operator": "eq",
                            "value": "AR",
                        },
                    },
                    {
                        "name": "case_count",
                        "operation": "count",
                        "input": "assigned_analyst",
                        "output": "case_count",
                        "arguments": {"as": "case_count"},
                    },
                ],
                "result_names": ["case_count"],
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Count Asha's active own-book lending cases.",
            "terms": [
                {"text": "Asha", "role": "actor", "source_text": "Asha"},
                {
                    "text": "Asha's cases",
                    "role": "relationship",
                    "source_text": "Asha's cases",
                },
            ],
            "requested_shape": "scalar",
            "relationship_terms": ["Asha's cases"],
        }
    )
    retrieval = SemanticRetrievalResult(
        query="Asha active own-book lending cases",
        matches=[
            RetrievalMatch(
                passage_id=passage_id,
                source="lending",
                version="ontology-v2",
                content=(
                    "lending.rm is the relationship-manager assignment and lending.analyst is "
                    "the assigned analyst; they are not interchangeable."
                ),
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
        ],
    )

    grounding = await stages.ground(
        interpretation,
        retrieval,
        {
            "people": [
                {
                    "id": person_id,
                    "name": "AR",
                    "full_name": "Asha Rao",
                    "role": "Deal Analyst",
                    "inactive": False,
                }
            ]
        },
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    plan = await stages.plan(answerability.verified_question, retrieval)

    relationship = grounding.established_meaning.semantic_bindings[0]
    assert (relationship.resource, relationship.field, relationship.passage_ids) == (
        "lending",
        "analyst",
        [passage_id],
    )
    assert answerability.verified_question.filters[0].model_dump()["field"] == "analyst"
    assert plan.reads[0].filters == {}
    assert plan.operations[0].arguments == {
        "field": "analyst",
        "operator": "eq",
        "value": "AR",
        "verified_filter_ids": ["f1"],
    }
    assert len(completions.calls) == 4
    repair = json.loads(completions.calls[3]["messages"][1]["content"])
    assert "omitted verified filter id(s): f1" in repair["validation_errors"][0]["msg"]


@pytest.mark.parametrize(
    ("question", "relationship_term", "directory_role", "field", "pushdown"),
    [
        (
            "Count own-book cases where Asha is the relationship manager.",
            "relationship manager",
            "Deal Analyst",
            "rm",
            True,
        ),
        (
            "Count own-book cases where Asha is the assigned analyst.",
            "assigned analyst",
            "Relationship Manager",
            "analyst",
            False,
        ),
        (
            "Count Asha's own-book lending cases.",
            "Asha's cases",
            "Deal Analyst",
            "analyst",
            False,
        ),
    ],
    ids=["explicit-rm-over-role", "explicit-analyst-over-role", "role-supported-possessive"],
)
async def test_lending_relationship_assignment_matrix(
    question: str,
    relationship_term: str,
    directory_role: str,
    field: str,
    pushdown: bool,
):
    person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    passage_id = "resources.lending.human_readable_lending"
    definition = (
        "The relationship-manager assignment on the lending case."
        if field == "rm"
        else "The assigned analyst on the lending case."
    )
    plan = {
        "reads": [
            {
                "name": "lending_cases",
                "resource": "lending",
                **({"filters": {field: "AR"}} if pushdown else {}),
            }
        ],
        "operations": [
            *(
                []
                if pushdown
                else [
                    {
                        "name": "assigned_person",
                        "operation": "filter",
                        "input": "lending_cases",
                        "output": "assigned_person",
                        "arguments": {"field": field, "operator": "eq", "value": "AR"},
                    }
                ]
            ),
            {
                "name": "case_count",
                "operation": "count",
                "input": "lending_cases" if pushdown else "assigned_person",
                "output": "case_count",
                "arguments": {"as": "case_count"},
            },
        ],
        "result_names": ["case_count"],
    }
    completions = FakeCompletions(
        [
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["lending"],
                    "semantic_bindings": [
                        {
                            "user_term": relationship_term,
                            "binding_kind": "relationship",
                            "resource": "lending",
                            "field": field,
                            "canonical_values": ["AR"],
                            "definition": definition,
                            "passage_ids": [passage_id],
                        }
                    ],
                    "grounded_values": [
                        {
                            "resource": "lending",
                            "field": field,
                            "user_term": "Asha",
                            "canonical_value": "AR",
                            "candidate_id": person_id,
                            "candidate_label": "Asha Rao",
                            "exact_match": True,
                            "normalized_match": True,
                        }
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "The lending assignment relationship is grounded.",
                "verified_question": {
                    "standalone_question": question,
                    "resources": ["lending"],
                    "answer_shape": "scalar",
                    "filters": [
                        {
                            "resource": "lending",
                            "field": field,
                            "operator": "equals",
                            "value": "AR",
                        }
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "Count the person's assigned lending cases.",
                            "provenance": "user",
                            "source_text": "Count",
                        }
                    ],
                },
            },
            plan,
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": question,
            "terms": [
                {"text": "Asha", "role": "actor", "source_text": "Asha"},
                {
                    "text": relationship_term,
                    "role": "relationship",
                    "source_text": relationship_term,
                },
            ],
            "requested_shape": "scalar",
            "relationship_terms": [relationship_term],
        }
    )
    retrieval = SemanticRetrievalResult(
        query=question,
        matches=[
            RetrievalMatch(
                passage_id=passage_id,
                source="lending",
                version="ontology-v2",
                content=(
                    "lending.rm is the relationship-manager assignment and lending.analyst is "
                    "the assigned analyst; they are not interchangeable."
                ),
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
        ],
    )

    grounding = await stages.ground(
        interpretation,
        retrieval,
        {
            "people": [
                {
                    "id": person_id,
                    "name": "AR",
                    "full_name": "Asha Rao",
                    "role": directory_role,
                    "inactive": False,
                }
            ]
        },
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    planned = await stages.plan(answerability.verified_question, retrieval)

    binding = grounding.established_meaning.semantic_bindings[0]
    assert (binding.resource, binding.field, binding.passage_ids) == (
        "lending",
        field,
        [passage_id],
    )
    assert grounding.established_meaning.grounded_values[0].canonical_value == "AR"
    assert answerability.verified_question.filters[0].field == field
    grounding_payload = json.loads(completions.calls[0]["messages"][1]["content"])["input"]
    assert grounding_payload["caller_visible_candidates"]["people"][0]["role"] == directory_role
    if pushdown:
        assert planned.reads[0].filters[field] == "AR"
        assert planned.reads[0].filters.root[0].verified_filter_ids == ["f1"]
        assert [step.operation for step in planned.operations] == ["count"]
    else:
        assert planned.reads[0].filters == {}
        assert planned.operations[0].arguments == {
            "field": field,
            "operator": "eq",
            "value": "AR",
            "verified_filter_ids": ["f1"],
        }


async def test_informal_lending_assignment_with_no_clear_role_remains_ambiguous():
    person_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    passage_id = "resources.lending.human_readable_lending"

    def alternative(label: str, field: str, definition: str) -> dict:
        return {
            "label": label,
            "meaning": {
                "resources": ["lending"],
                "semantic_bindings": [
                    {
                        "user_term": "Asha's cases",
                        "binding_kind": "relationship",
                        "resource": "lending",
                        "field": field,
                        "canonical_values": ["AR"],
                        "definition": definition,
                        "passage_ids": [passage_id],
                    }
                ],
                "grounded_values": [
                    {
                        "resource": "lending",
                        "field": field,
                        "user_term": "Asha",
                        "canonical_value": "AR",
                        "candidate_id": person_id,
                        "candidate_label": "Asha Rao",
                        "exact_match": True,
                        "normalized_match": True,
                    }
                ],
            },
        }

    completions = FakeCompletions(
        [
            {
                "status": "NEEDS_CLARIFICATION",
                "established_meaning": {"resources": ["lending"]},
                "issue": {
                    "term": "Asha's cases",
                    "kind": "AMBIGUOUS",
                    "reason": "Relationship-manager and analyst assignments remain plausible.",
                    "alternatives": [
                        alternative("relationship-managed cases", "rm", "Relationship manager."),
                        alternative("analyst-assigned cases", "analyst", "Assigned analyst."),
                    ],
                },
            },
            {
                "outcome": "CLARIFICATION_REQUIRED",
                "reason": "The assignment relationship is unresolved.",
                "clarification_question": (
                    "Do you mean cases Asha relationship-manages or cases assigned to her as analyst?"
                ),
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Count Asha's own-book lending cases.",
            "terms": [
                {"text": "Asha", "role": "actor", "source_text": "Asha"},
                {
                    "text": "Asha's cases",
                    "role": "relationship",
                    "source_text": "Asha's cases",
                },
            ],
            "requested_shape": "scalar",
            "relationship_terms": ["Asha's cases"],
        }
    )
    retrieval = SemanticRetrievalResult(
        query="Asha's own-book lending cases",
        matches=[
            RetrievalMatch(
                passage_id=passage_id,
                source="lending",
                version="ontology-v2",
                content=(
                    "lending.rm is the relationship-manager assignment and lending.analyst is "
                    "the assigned analyst; they are not interchangeable."
                ),
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
        ],
    )

    grounding = await stages.ground(
        interpretation,
        retrieval,
        {
            "people": [
                {
                    "id": person_id,
                    "name": "AR",
                    "full_name": "Asha Rao",
                    "role": "Management",
                    "inactive": False,
                }
            ]
        },
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)

    assert grounding.status == "NEEDS_CLARIFICATION"
    assert grounding.issue is not None
    assert [item.meaning.semantic_bindings[0].field for item in grounding.issue.alternatives] == [
        "rm",
        "analyst",
    ]
    assert all(
        item.meaning.semantic_bindings[0].passage_ids == [passage_id]
        for item in grounding.issue.alternatives
    )
    assert answerability.outcome == "CLARIFICATION_REQUIRED"
    assert answerability.clarification_question is not None
    assert len(completions.calls) == 2


async def test_shared_status_ownership_survives_all_model_contracts():
    completions = FakeCompletions(
        [
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["syndication", "asset_monetisation"],
                    "semantic_bindings": [
                        {
                            "user_term": "on hold syndication asks",
                            "binding_kind": "lifecycle",
                            "resource": "syndication",
                            "field": "status",
                            "canonical_values": ["on hold"],
                            "passage_ids": ["lifecycle.syndication"],
                        },
                        {
                            "user_term": "on hold asset mandates",
                            "binding_kind": "lifecycle",
                            "resource": "asset_monetisation",
                            "field": "status",
                            "canonical_values": ["on hold"],
                            "passage_ids": ["lifecycle.asset_monetisation"],
                        },
                    ],
                    "grounded_values": [
                        {
                            "resource": "syndication",
                            "field": "status",
                            "user_term": "on hold syndication asks",
                            "canonical_value": "on hold",
                        },
                        {
                            "resource": "asset_monetisation",
                            "field": "status",
                            "user_term": "on hold asset mandates",
                            "canonical_value": "on hold",
                        },
                    ],
                },
            },
            {
                "outcome": "ANSWERABLE",
                "reason": "Each status is owned by its named business book.",
                "verified_question": {
                    "standalone_question": (
                        "List on-hold syndication asks and on-hold asset monetisation mandates."
                    ),
                    "resources": ["syndication", "asset_monetisation"],
                    "answer_shape": "list",
                    "filters": [
                        {
                            "resource": "syndication",
                            "field": "status",
                            "operator": "eq",
                            "value": "On Hold",
                        },
                        {
                            "resource": "asset_monetisation",
                            "field": "status",
                            "operator": "eq",
                            "value": "On Hold",
                        },
                    ],
                    "evidence_obligations": [
                        {
                            "kind": "answer_shape",
                            "description": "List each book separately.",
                            "provenance": "user",
                            "source_text": (
                                "List on-hold syndication asks and on-hold asset monetisation mandates."
                            ),
                        }
                    ],
                },
            },
            {
                "reads": [
                    {
                        "name": "syndication_asks",
                        "resource": "syndication",
                        "filters": {"status": "on hold"},
                    },
                    {
                        "name": "asset_mandates",
                        "resource": "asset_monetisation",
                        "filters": {"status": "on hold"},
                    },
                ],
                "operations": [],
                "result_names": ["syndication_asks", "asset_mandates"],
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = "List on-hold syndication asks and on-hold asset monetisation mandates."
    interpretation = QuestionInterpretation.model_validate(
        {"standalone_question": question, "requested_shape": "list"}
    )
    retrieval = SemanticRetrievalResult(
        query="on hold status by business book",
        matches=[
            RetrievalMatch(
                passage_id=passage_id,
                source=source,
                version="1",
                content="On Hold is a governed status in this business book.",
                metadata={"audience": ["grounding", "planning"]},
                fusion_score=1.0,
            )
            for passage_id, source in (
                ("lifecycle.syndication", "syndication"),
                ("lifecycle.asset_monetisation", "asset_monetisation"),
            )
        ],
    )
    references = {
        "Status of Proposal": ["On Hold"],
        "Asset Mon Status": ["On Hold"],
    }

    grounding = await stages.ground(
        interpretation, retrieval, {"reference_values": references}
    )
    answerability = await stages.answerability(interpretation, grounding, retrieval)
    assert answerability.verified_question is not None
    plan = await stages.plan(answerability.verified_question, retrieval)

    assert [item.resource for item in grounding.established_meaning.grounded_values] == [
        "syndication",
        "asset_monetisation",
    ]
    assert [item["resource"] for item in answerability.verified_question.filters] == [
        "syndication",
        "asset_monetisation",
    ]
    assert [read.filters["status"] for read in plan.reads] == ["On Hold", "On Hold"]
    assert [read.filters.root[0].verified_filter_ids for read in plan.reads] == [["f1"], ["f2"]]
    assert len(completions.calls) == 3


async def test_answerability_falls_back_to_unsupported_after_repeated_invented_filter():
    invented = {
        "outcome": "ANSWERABLE",
        "reason": "Promoter is an entity type.",
        "verified_question": {
            "standalone_question": "Show promoter entities.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "entity_type",
                    "operator": "eq",
                    "value": "Promoter",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List promoter entities.",
                    "provenance": "user",
                    "source_text": "Show promoter entities.",
                }
            ],
        },
    }
    completions = FakeCompletions([invented, invented])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {"standalone_question": "Show promoter entities.", "requested_shape": "list"}
        ),
        ValueGroundingResult.model_validate(
            {"status": "RESOLVED", "established_meaning": {"resources": ["entities"]}}
        ),
        SemanticRetrievalResult(query="promoter entities", matches=[]),
    )

    assert result.outcome == "OUT_OF_SCOPE"
    assert result.verified_question is None
    assert "cannot apply it as a filter" in result.reason
    assert len(completions.calls) == 2


async def test_grounding_repairs_governed_composite_to_caller_visible_canonical_value():
    candidate_id = "composite-visible"
    stored_value = "Sent - Mandate Signed | [Syndication: Yes, Partnership: No]"
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["syndication"],
            "grounded_values": [
                {
                    "field": "mandate_status",
                    "user_term": "sent and signed for syndication",
                    "canonical_value": "sent and signed-for-syndication",
                    "candidate_id": candidate_id,
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [
                {
                    **invalid["established_meaning"]["grounded_values"][0],
                    "canonical_value": stored_value,
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.ground(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List mandates sent and signed for syndication.",
                "requested_shape": "list",
            }
        ),
        SemanticRetrievalResult(query="mandates sent signed", matches=[]),
        {
            "governed_composite_values": [
                {
                    "id": candidate_id,
                    "resource": "syndication",
                    "field": "mandate_status",
                    "value": stored_value,
                }
            ]
        },
    )

    assert result.established_meaning.grounded_values[0].canonical_value == stored_value
    assert len(completions.calls) == 2
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "must preserve caller-visible canonical value" in repair["validation_errors"][0]["msg"]


async def test_grounding_repairs_invented_controlled_value_to_visible_reference():
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["entities"],
            "semantic_bindings": [
                {
                    "user_term": "EV Mobility",
                    "binding_kind": "field",
                    "resource": "entities",
                    "field": "sector",
                    "canonical_values": ["EV Mobility"],
                    "passage_ids": ["entities.sector"],
                }
            ],
            "grounded_values": [
                {
                    "field": "sector",
                    "user_term": "EV Mobility",
                    "canonical_value": "Invented EV Category",
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [
                {
                    **invalid["established_meaning"]["grounded_values"][0],
                    "canonical_value": "EV Mobility",
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.ground(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List EV Mobility companies.",
                "requested_shape": "list",
            }
        ),
        SemanticRetrievalResult(
            query="EV Mobility companies",
            matches=[
                RetrievalMatch(
                    passage_id="entities.sector",
                    source="entities",
                    version="1",
                    content="Sector is a governed Entity field.",
                    fusion_score=1.0,
                )
            ],
        ),
        {"reference_values": {"Sector": [{"value": "EV Mobility"}]}},
    )

    assert result.established_meaning.grounded_values[0].canonical_value == "EV Mobility"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "must use a unique caller-visible canonical value" in repair["validation_errors"][0]["msg"]


async def test_planning_relocates_canonical_entity_lifecycle_filter():
    draft = {
        "reads": [
            {
                "name": "dormant_entities",
                "resource": "entities",
                "filters": {"lifecycle": "dormant"},
            }
        ],
        "operations": [],
    }
    stages = ModelStages(
        SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([draft]))),
        _settings(),
    )
    verified = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List dormant entities.",
            "resources": ["entities"],
            "answer_shape": "list",
            "semantic_bindings": [
                {
                    "user_term": "dormant",
                    "binding_kind": "lifecycle",
                    "resource": "entities",
                    "field": "lifecycle",
                    "canonical_values": ["Dormant"],
                    "passage_ids": ["entities.lifecycle"],
                }
            ],
            "grounded_values": [
                {
                    "field": "lifecycle",
                    "user_term": "dormant",
                    "canonical_value": "Dormant",
                }
            ],
            "filters": [
                {
                    "resource": "entities",
                    "field": "lifecycle",
                    "operator": "eq",
                    "value": "Dormant",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List dormant entities.",
                    "provenance": "user",
                    "source_text": "List dormant entities.",
                }
            ],
        }
    )

    plan = await stages.plan(
        verified,
        SemanticRetrievalResult(query="dormant entities", matches=[]),
    )

    assert plan.reads[0].filters == {}
    assert plan.operations[0].operation == "filter"
    assert plan.operations[0].arguments["value"] == "Dormant"


async def test_answerability_accepts_typed_missing_measure_requirement_without_repair():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The governed amount can be summed over known values.",
        "verified_question": {
            "standalone_question": "What is the total lending amount?",
            "resources": ["lending"],
            "answer_shape": "scalar",
            "metrics": [{"resource": "lending", "field": "amount_cr", "operation": "sum"}],
            "missing_data_requirements": [{"resource": "lending", "field": "amount_cr", "usage": "measure"}],
            "evidence_obligations": [
                {
                    "kind": "metric",
                    "description": "Return the total lending amount and missing coverage.",
                    "resource": "lending",
                    "field": "amount_cr",
                    "provenance": "user",
                    "source_text": "total lending amount",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "What is the total lending amount?",
                "terms": [{"text": "total lending amount", "source_text": "total lending amount"}],
                "requested_shape": "scalar",
            }
        ),
        ValueGroundingResult.model_validate(
            {"status": "RESOLVED", "established_meaning": {"resources": ["lending"]}}
        ),
        SemanticRetrievalResult(query="total lending amount", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.missing_data_requirements[0].usage == "measure"
    assert len(completions.calls) == 1


async def test_measure_missing_requirement_plans_known_aggregate_without_repair():
    plan_payload = {
        "reads": [{"name": "lending", "resource": "lending"}],
        "operations": [
            {
                "name": "total_amount",
                "operation": "sum",
                "input": "lending",
                "output": "total_amount",
                "arguments": {"field": "amount_cr", "as": "total_amount_cr"},
            }
        ],
        "result_names": ["total_amount"],
        "result_shapes": {"total_amount": "scalar"},
    }
    completions = FakeCompletions([plan_payload])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "What is the total lending amount?",
            "resources": ["lending"],
            "answer_shape": "scalar",
            "metrics": [{"resource": "lending", "field": "amount_cr", "operation": "sum"}],
            "missing_data_requirements": [{"resource": "lending", "field": "amount_cr", "usage": "measure"}],
            "evidence_obligations": [
                {
                    "kind": "metric",
                    "description": "Return the total lending amount and missing coverage.",
                    "resource": "lending",
                    "field": "amount_cr",
                    "provenance": "user",
                    "source_text": "total lending amount",
                }
            ],
        }
    )

    result = await stages.plan(
        question,
        SemanticRetrievalResult(query="total lending amount", matches=[]),
    )

    assert result.result_shapes == {"total_amount": "scalar"}
    assert len(completions.calls) == 1


async def test_planning_assigns_predicate_missing_policy_and_removes_unexpected_marker():
    plan_payload = {
        "reads": [{"name": "entities", "resource": "entities"}],
        "operations": [
            {
                "name": "maharashtra",
                "operation": "filter",
                "input": "entities",
                "output": "maharashtra",
                "arguments": {
                    "field": "state",
                    "operator": "eq",
                    "value": "Maharashtra",
                },
            },
            {
                "name": "active",
                "operation": "filter",
                "input": "maharashtra",
                "output": "active",
                "arguments": {
                    "field": "legal_name",
                    "operator": "eq",
                    "value": "Example Energy",
                    "missing_policy": "report_unassessable",
                },
            },
        ],
        "result_names": ["active"],
    }
    completions = FakeCompletions([plan_payload])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List active companies in Maharashtra.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                },
                {
                    "resource": "entities",
                    "field": "legal_name",
                    "operator": "equals",
                    "value": "Example Energy",
                },
            ],
            "missing_data_requirements": [{"resource": "entities", "field": "state", "usage": "predicate"}],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List active companies in Maharashtra.",
                }
            ],
        }
    )

    result = await stages.plan(question, SemanticRetrievalResult(query="companies", matches=[]))

    assert result.operations[0].arguments["missing_policy"] == "report_unassessable"
    assert "missing_policy" not in result.operations[1].arguments
    assert len(completions.calls) == 1


async def test_planning_repairs_related_filter_that_discards_unassessable_rows():
    invalid = {
        "reads": [
            {"name": "deals", "resource": "deals"},
            {
                "name": "entities",
                "resource": "entities",
                "filters": {"state": "Maharashtra"},
            },
        ],
        "operations": [
            {
                "name": "matches",
                "operation": "join",
                "input": "deals",
                "output": "matches",
                "arguments": {
                    "right": "entities",
                    "left_key": "entity_id",
                    "right_key": "id",
                    "how": "inner",
                    "relationship_required": True,
                },
            }
        ],
        "result_names": ["matches"],
    }
    repaired = {
        "reads": [
            {"name": "deals", "resource": "deals"},
            {"name": "entities", "resource": "entities"},
        ],
        "operations": [
            {
                "name": "with_entity",
                "operation": "join",
                "input": "deals",
                "output": "with_entity",
                "arguments": {
                    "right": "entities",
                    "left_key": "entity_id",
                    "right_key": "id",
                    "how": "left",
                    "right_prefix": "entity_",
                },
            },
            {
                "name": "missing_state",
                "operation": "missing_count",
                "input": "with_entity",
                "output": "missing_state",
                "arguments": {"field": "entity_state", "as": "missing_state_count"},
            },
            {
                "name": "matches",
                "operation": "filter",
                "input": "with_entity",
                "output": "matches",
                "arguments": {
                    "field": "entity_state",
                    "operator": "eq",
                    "value": "Maharashtra",
                    "missing_policy": "report_unassessable",
                },
            },
            {
                "name": "deal_count",
                "operation": "count",
                "input": "matches",
                "output": "deal_count",
                "arguments": {"as": "deal_count"},
            },
        ],
        "result_names": ["deal_count", "missing_state"],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "How many commercial deals are in Maharashtra?",
            "resources": ["deals", "entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "missing_data_requirements": [
                {
                    "resource": "entities",
                    "field": "state",
                    "usage": "predicate",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "completeness",
                    "description": "Count deals whose related state is unavailable.",
                    "resource": "entities",
                    "field": "state",
                    "provenance": "ontology",
                    "passage_ids": ["relationships.entity_dimensions"],
                }
            ],
        }
    )
    plan = await stages.plan(question, SemanticRetrievalResult(query="state", matches=[]))
    assert plan.result_names == ["deal_count", "missing_state"]
    assert plan.operations[0].arguments["how"] == "left"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "must read entities.state unfiltered" in repair["validation_errors"][0]["msg"]


async def test_planning_rejects_only_the_missing_policy_pushdown():
    invalid = {
        "reads": [
            {"name": "deals", "resource": "deals"},
            {
                "name": "entities",
                "resource": "entities",
                "filters": {"state": "Maharashtra"},
            },
        ],
        "operations": [
            {
                "name": "with_entity",
                "operation": "join",
                "input": "deals",
                "output": "with_entity",
                "arguments": {
                    "right": "entities",
                    "left_key": "entity_id",
                    "right_key": "id",
                    "how": "left",
                    "right_prefix": "entity_",
                },
            },
            {
                "name": "matches",
                "operation": "filter",
                "input": "with_entity",
                "output": "matches",
                "arguments": {
                    "field": "entity_state",
                    "operator": "eq",
                    "value": "Maharashtra",
                    "missing_policy": "report_unassessable",
                },
            },
        ],
        "result_names": ["matches"],
    }
    repaired = {
        **invalid,
        "reads": [
            {"name": "deals", "resource": "deals"},
            {"name": "entities", "resource": "entities"},
        ],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "How many commercial deals are in Maharashtra?",
            "resources": ["deals", "entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "missing_data_requirements": [
                {
                    "resource": "entities",
                    "field": "state",
                    "usage": "predicate",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "completeness",
                    "description": "Count deals whose related state is unavailable.",
                    "resource": "entities",
                    "field": "state",
                    "provenance": "ontology",
                    "passage_ids": ["relationships.entity_dimensions"],
                }
            ],
        }
    )
    await stages.plan(question, SemanticRetrievalResult(query="state", matches=[]))
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "must read entities.state unfiltered" in repair["validation_errors"][0]["msg"]


async def test_planning_repairs_mismatched_missing_count_result_name():
    operations = [
        {
            "name": "with_entity",
            "operation": "join",
            "input": "deals",
            "output": "with_entity",
            "arguments": {
                "right": "entities",
                "left_key": "entity_id",
                "right_key": "id",
                "how": "left",
                "right_prefix": "entity_",
            },
        },
        {
            "name": "missing_state",
            "operation": "missing_count",
            "input": "with_entity",
            "output": "missing_state",
            "arguments": {"field": "entity_state", "as": "missing_state_count"},
        },
        {
            "name": "matches",
            "operation": "filter",
            "input": "with_entity",
            "output": "matches",
            "arguments": {
                "field": "entity_state",
                "operator": "eq",
                "value": "Maharashtra",
                "missing_policy": "report_unassessable",
            },
        },
    ]
    invalid = {
        "reads": [
            {"name": "deals", "resource": "deals"},
            {"name": "entities", "resource": "entities"},
        ],
        "operations": operations,
        "result_names": ["matches"],
    }
    completions = FakeCompletions([invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "How many commercial deals are in Maharashtra?",
            "resources": ["deals", "entities"],
            "answer_shape": "scalar",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "missing_data_requirements": [
                {
                    "resource": "entities",
                    "field": "state",
                    "usage": "predicate",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "completeness",
                    "description": "Count deals whose related state is unavailable.",
                    "resource": "entities",
                    "field": "state",
                    "provenance": "ontology",
                    "passage_ids": ["relationships.entity_dimensions"],
                }
            ],
        }
    )
    plan = await stages.plan(question, SemanticRetrievalResult(query="state", matches=[]))
    assert plan.result_names == ["matches"]
    assert len(completions.calls) == 1


async def test_planning_keeps_null_measure_rows_until_group_aggregation():
    group = {
        "name": "by_state",
        "operation": "group",
        "input": "assets",
        "output": "by_state",
        "arguments": {
            "by": ["state"],
            "aggregations": [
                {
                    "operation": "sum",
                    "field": "indicative_value_cr",
                    "as": "total_value_cr",
                }
            ],
        },
    }
    invalid = {
        "reads": [{"name": "assets", "resource": "asset_monetisation"}],
        "operations": [
            {
                "name": "known_values",
                "operation": "filter",
                "input": "assets",
                "output": "known_values",
                "arguments": {"field": "indicative_value_cr", "operator": "not_null"},
            },
            {**group, "input": "known_values"},
        ],
        "result_names": ["by_state"],
    }
    repaired = {
        "reads": [{"name": "assets", "resource": "asset_monetisation"}],
        "operations": [group],
        "result_names": ["by_state"],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "Rank states by indicative asset value and report missing values.",
            "resources": ["asset_monetisation"],
            "answer_shape": "ranked",
            "metrics": [
                {
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "operation": "sum",
                }
            ],
            "grouping": ["state"],
            "missing_data_requirements": [
                {
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "usage": "measure",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "completeness",
                    "description": "Report missing indicative values for each ranked state.",
                    "resource": "asset_monetisation",
                    "field": "indicative_value_cr",
                    "provenance": "ontology",
                    "passage_ids": ["metrics.asset_monetisation"],
                }
            ],
        }
    )

    plan = await stages.plan(question, SemanticRetrievalResult(query="asset value by state", matches=[]))

    assert [step.operation for step in plan.operations] == ["group"]
    assert plan.operations[0].input == "assets"
    assert len(completions.calls) == 2
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "retain null-bearing rows through aggregation" in repair["validation_errors"][0]["msg"]


async def test_planning_canonicalizes_list_valued_verified_filter_before_coverage():
    invalid = {
        "reads": [{"name": "asks", "resource": "syndication"}],
        "operations": [],
        "result_names": ["asks"],
    }
    excluded = ["Dropped", "Withdrawn", "Rejected", "Sanctioned", "Disbursed"]
    repaired = {
        **invalid,
        "operations": [
            {
                "name": "live_asks",
                "operation": "filter",
                "input": "asks",
                "output": "live_asks",
                "arguments": {
                    "field": "status",
                    "operator": "not_in",
                    "values": excluded,
                },
            }
        ],
        "result_names": ["live_asks"],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List live syndication asks.",
            "resources": ["syndication"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "syndication",
                    "field": "status",
                    "operator": "not_in",
                    "value": excluded,
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List live asks",
                    "provenance": "user",
                    "source_text": "List live syndication asks",
                }
            ],
        }
    )
    assert question.filters[0]["values"] == excluded
    assert "value" not in question.filters[0]
    plan = await stages.plan(question, SemanticRetrievalResult(query="live asks", matches=[]))
    assert plan.operations[0].arguments["values"] == excluded
    assert len(completions.calls) == 2


def test_verified_question_keeps_scalar_filter_value_scalar():
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List EV Mobility companies.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "equals",
                    "value": "EV Mobility",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List companies",
                    "provenance": "user",
                    "source_text": "List EV Mobility companies",
                }
            ],
        }
    )
    assert question.filters[0]["value"] == "EV Mobility"
    assert "values" not in question.filters[0]


def _qualitative_question() -> VerifiedQuestion:
    return VerifiedQuestion.model_validate(
        {
            "standalone_question": "Summarize recorded reasons for rejected lending cases.",
            "resources": ["lending"],
            "answer_shape": "qualitative",
            "qualitative_analysis": True,
            "filters": [
                {
                    "resource": "lending",
                    "field": "stage",
                    "operator": "equals",
                    "value": "Rejected",
                }
            ],
            "missing_data_requirements": [
                {
                    "resource": "lending",
                    "field": "remarks",
                    "usage": "qualitative_source",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "qualitative",
                    "description": "Analyze recorded remarks.",
                    "resource": "lending",
                    "field": "remarks",
                    "provenance": "register",
                },
                {
                    "kind": "completeness",
                    "description": "Report missing remark coverage.",
                    "resource": "lending",
                    "field": "remarks",
                    "provenance": "register",
                },
            ],
        }
    )


def _qualitative_row_plan() -> dict:
    return {
        "reads": [
            {
                "name": "rejected",
                "resource": "lending",
                "filters": {"stage": "Rejected"},
            }
        ],
        "operations": [],
        "result_names": ["rejected"],
    }


async def test_qualitative_source_row_plan_validates_without_repair():
    completions = FakeCompletions([_qualitative_row_plan()])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    plan = await stages.plan(
        _qualitative_question(),
        SemanticRetrievalResult(query="rejected reasons", matches=[]),
    )

    assert plan.result_names == ["rejected"]
    assert len(completions.calls) == 1


async def test_joined_qualitative_source_retains_row_grain_without_literal_text_filter():
    plan_payload = {
        "reads": [
            {"name": "rejected", "resource": "lending", "filters": {"stage": "Rejected"}},
            {"name": "entities", "resource": "entities"},
        ],
        "operations": [
            {
                "name": "with_company",
                "operation": "join",
                "input": "rejected",
                "output": "with_company",
                "arguments": {
                    "right": "entities",
                    "left_key": "entity_id",
                    "right_key": "id",
                    "how": "left",
                    "right_prefix": "entity_",
                },
            },
            {
                "name": "qualitative_rows",
                "operation": "project",
                "input": "with_company",
                "output": "qualitative_rows",
                "arguments": {"fields": ["id", "remarks", "entity_legal_name"]},
            },
        ],
        "result_names": ["qualitative_rows"],
        "result_shapes": {"qualitative_rows": "rows"},
    }
    completions = FakeCompletions([plan_payload])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    plan = await stages.plan(
        _qualitative_question().model_copy(update={"resources": ["lending", "entities"]}),
        SemanticRetrievalResult(query="rejected company reasons", matches=[]),
    )

    assert [step.operation for step in plan.operations] == ["join", "project"]
    assert len(completions.calls) == 1


@pytest.mark.parametrize(
    "invalid_plan",
    [
        {
            "reads": [
                {
                    "name": "rejected",
                    "resource": "lending",
                    "filters": {"stage": "Rejected"},
                    "q": "loss-making",
                }
            ],
            "operations": [],
            "result_names": ["rejected"],
        },
        {
            "reads": [{"name": "rejected", "resource": "lending", "filters": {"stage": "Rejected"}}],
            "operations": [
                {
                    "name": "text_match",
                    "operation": "filter",
                    "input": "rejected",
                    "output": "text_match",
                    "arguments": {"field": "remarks", "operator": "icontains", "value": "loss"},
                }
            ],
            "result_names": ["text_match"],
        },
        {
            "reads": [{"name": "rejected", "resource": "lending", "filters": {"stage": "Rejected"}}],
            "operations": [
                {
                    "name": "drop_missing",
                    "operation": "filter",
                    "input": "rejected",
                    "output": "drop_missing",
                    "arguments": {"field": "remarks", "operator": "not_null"},
                }
            ],
            "result_names": ["drop_missing"],
        },
        {
            "reads": [{"name": "rejected", "resource": "lending", "filters": {"stage": "Rejected"}}],
            "operations": [
                {
                    "name": "premature_count",
                    "operation": "count",
                    "input": "rejected",
                    "output": "premature_count",
                    "arguments": {"as": "case_count"},
                }
            ],
            "result_names": ["premature_count"],
        },
    ],
)
async def test_qualitative_source_normalizes_text_selection_null_dropping_and_aggregation(
    invalid_plan,
):
    completions = FakeCompletions([invalid_plan, _qualitative_row_plan()])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    plan = await stages.plan(
        _qualitative_question(),
        SemanticRetrievalResult(query="rejected reasons", matches=[]),
    )

    assert len(completions.calls) == 1
    assert plan.reads[0].q is None
    assert plan.result_names == ["rejected"]
    assert not any(
        step.operation == "filter" and step.arguments.get("field") == "remarks" for step in plan.operations
    )
    assert not any(
        step.operation
        in {"count", "sum", "distinct_count", "average", "min", "max", "missing_count", "group"}
        and step.output in plan.result_names
        for step in plan.operations
    )


async def test_valid_non_pushdown_field_uses_post_read_filter_without_repair():
    plan = {
        "reads": [{"name": "lending", "resource": "lending"}],
        "operations": [
            {
                "name": "assigned",
                "operation": "filter",
                "input": "lending",
                "output": "assigned",
                "arguments": {"field": "analyst", "operator": "eq", "value": "Anita"},
            },
            {
                "name": "active",
                "operation": "filter",
                "input": "assigned",
                "output": "active",
                "arguments": {
                    "field": "stage",
                    "operator": "not_in",
                    "values": ["Disbursed", "Rejected"],
                },
            },
            {
                "name": "case_count",
                "operation": "count",
                "input": "active",
                "output": "case_count",
                "arguments": {"as": "case_count"},
            },
        ],
        "result_names": ["case_count"],
    }
    completions = FakeCompletions([plan])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "Count Anita's non-terminal own-book cases.",
            "resources": ["lending"],
            "answer_shape": "scalar",
            "filters": [
                {"resource": "lending", "field": "analyst", "operator": "equals", "value": "Anita"},
                {
                    "resource": "lending",
                    "field": "stage",
                    "operator": "not_in",
                    "values": ["Disbursed", "Rejected"],
                },
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Return a count.",
                    "provenance": "user",
                    "source_text": "Count",
                }
            ],
        }
    )

    result = await stages.plan(question, SemanticRetrievalResult(query="cases", matches=[]))

    assert result.reads[0].filters == {}
    assert [step.operation for step in result.operations] == ["filter", "filter", "count"]
    assert len(completions.calls) == 1


async def test_new_analyst_filter_stays_on_register_without_repair():
    invalid = {
        "reads": [{"name": "lending", "resource": "lending", "filters": {"analyst": "Anita"}}],
        "operations": [],
        "result_names": ["lending"],
    }
    repaired = {
        "reads": [{"name": "lending", "resource": "lending"}],
        "operations": [
            {
                "name": "assigned",
                "operation": "filter",
                "input": "lending",
                "output": "assigned",
                "arguments": {"field": "analyst", "operator": "eq", "value": "Anita"},
            }
        ],
        "result_names": ["assigned"],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List Anita's lending cases.",
            "resources": ["lending"],
            "answer_shape": "list",
            "filters": [{"resource": "lending", "field": "analyst", "operator": "equals", "value": "Anita"}],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List cases.",
                    "provenance": "user",
                    "source_text": "List",
                }
            ],
        }
    )

    plan = await stages.plan(question, SemanticRetrievalResult(query="Anita lending", matches=[]))

    assert len(completions.calls) == 1
    assert plan.reads[0].filters == {"analyst": "Anita"}
    assert plan.result_names == ["lending"]
    assert plan.operations == []


async def test_trusted_governed_composite_read_filter_is_lowered_without_repair():
    stored_value = "Sent - Mandate Signed | [Syndication: Yes, Partnership: No]"
    invalid = {
        "reads": [
            {
                "name": "syndication",
                "resource": "syndication",
                "filters": {"mandate_status": stored_value},
            }
        ],
        "operations": [],
        "result_names": ["syndication"],
    }
    completions = FakeCompletions([invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List mandates sent and signed for syndication.",
            "resources": ["syndication"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "syndication",
                    "field": "mandate_status",
                    "operator": "equals",
                    "value": stored_value,
                }
            ],
            "grounded_values": [
                {
                    "field": "mandate_status",
                    "user_term": "sent and signed for syndication",
                    "canonical_value": stored_value,
                    "candidate_id": "composite-visible",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching mandates.",
                    "provenance": "user",
                    "source_text": "List mandates sent and signed for syndication.",
                }
            ],
        }
    )

    plan = await stages.plan(
        question,
        SemanticRetrievalResult(query="mandates sent signed", matches=[]),
    )

    assert len(completions.calls) == 1
    assert plan.reads[0].filters == {}
    assert plan.operations[0].arguments == {
        "field": "mandate_status",
        "operator": "eq",
        "value": stored_value,
        "verified_filter_ids": ["f1"],
    }
    assert plan.result_names == ["host_syndication_mandate_status"]


async def test_non_pushdown_relocation_refuses_read_dependency_semantic_change():
    invalid = {
        "reads": [
            {
                "name": "lending",
                "resource": "lending",
                "filters": {"tracker_no": "Anita"},
            },
            {
                "name": "entities",
                "resource": "entities",
                "filters": {"id": "$lending.entity_id"},
            },
        ],
        "operations": [],
        "result_names": ["entities"],
    }
    completions = FakeCompletions([invalid, invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "Find the company for Anita's lending case.",
            "resources": ["lending", "entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "lending",
                    "field": "tracker_no",
                    "operator": "equals",
                    "value": "Anita",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Return the related company.",
                    "provenance": "user",
                    "source_text": "Find the company for Anita's lending case.",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="supplies a dependent Register read"):
        await stages.plan(
            question,
            SemanticRetrievalResult(query="Anita lending company", matches=[]),
        )


async def test_joined_qualitative_filter_is_removed_using_field_lineage():
    plan_payload = {
        "reads": [
            {"name": "entities", "resource": "entities"},
            {"name": "lending", "resource": "lending", "filters": {"stage": "Rejected"}},
        ],
        "operations": [
            {
                "name": "with_lending",
                "operation": "join",
                "input": "entities",
                "output": "with_lending",
                "arguments": {
                    "right": "lending",
                    "left_key": "id",
                    "right_key": "entity_id",
                    "how": "left",
                    "right_prefix": "case_",
                },
            },
            {
                "name": "text_selected",
                "operation": "filter",
                "input": "with_lending",
                "output": "text_selected",
                "arguments": {
                    "field": "case_remarks",
                    "operator": "icontains",
                    "value": "loss",
                },
            },
        ],
        "result_names": ["text_selected"],
    }
    completions = FakeCompletions([plan_payload])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = _qualitative_question().model_copy(update={"resources": ["entities", "lending"]})

    plan = await stages.plan(
        question,
        SemanticRetrievalResult(query="rejected company reasons", matches=[]),
    )

    assert len(completions.calls) == 1
    assert [step.operation for step in plan.operations] == ["join"]
    assert plan.result_names == ["with_lending"]


async def test_resource_scope_needs_no_duplicate_predicate_and_ranked_plan_is_valid():
    active = ["Data Awaited", "Diligence", "Note Circulated", "Sanctioned"]
    plan = {
        "reads": [{"name": "lending", "resource": "lending"}],
        "operations": [
            {
                "name": "active",
                "operation": "filter",
                "input": "lending",
                "output": "active",
                "arguments": {"field": "stage", "operator": "in", "values": active},
            },
            {
                "name": "by_analyst",
                "operation": "group",
                "input": "active",
                "output": "by_analyst",
                "arguments": {
                    "by": ["analyst"],
                    "aggregations": [{"operation": "count", "as": "case_count"}],
                },
            },
            {
                "name": "largest",
                "operation": "rank",
                "input": "by_analyst",
                "output": "largest",
                "arguments": {"field": "case_count", "direction": "top", "count": 1},
            },
        ],
        "result_names": ["largest"],
    }
    completions = FakeCompletions([plan])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "Which analyst has the most active own-book cases?",
            "resources": ["lending"],
            "answer_shape": "ranked",
            "filters": [{"resource": "lending", "field": "stage", "operator": "in", "values": active}],
            "grouping": ["analyst"],
            "ranking": {"field": "case_count", "direction": "top", "ties": "preserve"},
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Return the top analyst and preserve ties.",
                    "provenance": "user",
                    "source_text": "Which analyst has the most",
                }
            ],
        }
    )

    result = await stages.plan(question, SemanticRetrievalResult(query="active analyst", matches=[]))

    assert result.reads[0].filters == {}
    assert [step.operation for step in result.operations] == ["filter", "group", "rank"]
    assert len(completions.calls) == 1


async def test_planning_repair_receives_prefixed_join_fields():
    reads = [
        {"name": "syndication", "resource": "syndication"},
        {
            "name": "entities",
            "resource": "entities",
            "filters": {"sector": "EV Mobility"},
        },
    ]
    join = {
        "name": "with_entities",
        "operation": "join",
        "input": "syndication",
        "output": "with_entities",
        "arguments": {
            "right": "entities",
            "left_key": "entity_id",
            "right_key": "id",
            "how": "inner",
            "relationship_required": True,
            "right_prefix": "entity_",
        },
    }
    invalid = {
        "reads": reads,
        "operations": [
            join,
            {
                "name": "display",
                "operation": "project",
                "input": "with_entities",
                "output": "display",
                "arguments": {"fields": ["legal_name", "amount_cr"]},
            },
        ],
        "result_names": ["display"],
    }
    repaired = {
        **invalid,
        "operations": [
            join,
            {
                **invalid["operations"][1],
                "arguments": {"fields": ["entity_legal_name", "amount_cr"]},
            },
        ],
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    question = VerifiedQuestion.model_validate(
        {
            "standalone_question": "List EV Mobility syndication asks and company names.",
            "resources": ["syndication", "entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "equals",
                    "value": "EV Mobility",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List qualifying companies and asks.",
                    "provenance": "user",
                    "source_text": "List",
                }
            ],
        }
    )
    plan = await stages.plan(question, SemanticRetrievalResult(query="EV asks", matches=[]))
    assert plan.operations[1].arguments["fields"] == ["legal_name", "amount_cr"]
    assert len(completions.calls) == 1


async def test_grounding_rejects_ontology_provenance_that_was_not_retrieved():
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["deals"],
            "semantic_bindings": [
                {
                    "user_term": "active deals",
                    "binding_kind": "lifecycle",
                    "resource": "deals",
                    "field": "stage",
                    "canonical_values": ["In Pipeline"],
                    "passage_ids": ["invented.passage"],
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = {
        "standalone_question": "Show active deals",
        "terms": [{"text": "active deals", "role": "subject", "source_text": "active deals"}],
        "requested_shape": "list",
    }
    retrieval = SemanticRetrievalResult(
        query="active deals",
        matches=[
            RetrievalMatch(
                passage_id="deals.lifecycle",
                source="deals",
                version="1",
                content="Commercial deal lifecycle.",
                fusion_score=1.0,
            )
        ],
    )
    with pytest.raises(ValueError, match="were not retrieved: invented.passage"):
        await stages.ground(QuestionInterpretation.model_validate(interpretation), retrieval, {})


async def test_grounding_repairs_noncanonical_ontology_resource_labels():
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": [
                "LendingTracker",
                "SyndicationTracker",
                "AssetMonetisation",
                "Entity",
            ],
            "semantic_bindings": [
                {
                    "user_term": "own-book lending",
                    "binding_kind": "book_scope",
                    "resource": "LendingTracker",
                    "field": "stage",
                    "passage_ids": ["books"],
                },
                {
                    "user_term": "syndication",
                    "binding_kind": "resource",
                    "resource": "SyndicationTracker",
                    "field": "status",
                    "passage_ids": ["books"],
                },
                {
                    "user_term": "asset monetisation",
                    "binding_kind": "resource",
                    "resource": "AssetMonetisation",
                    "field": "status",
                    "passage_ids": ["books"],
                },
                {
                    "user_term": "company",
                    "binding_kind": "resource",
                    "resource": "Entity",
                    "field": "id",
                    "passage_ids": ["books"],
                },
            ],
        },
    }
    repaired = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending", "syndication", "asset_monetisation", "entities"],
            "semantic_bindings": [
                {
                    **binding,
                    "binding_kind": "resource",
                    "resource": resource,
                }
                for binding, resource in zip(
                    invalid["established_meaning"]["semantic_bindings"],
                    ["lending", "syndication", "asset_monetisation", "entities"],
                    strict=True,
                )
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Check the three books for this company.",
            "terms": [{"text": "company", "source_text": "company"}],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="three books company",
        matches=[
            RetrievalMatch(
                passage_id="books",
                source="books",
                version="1",
                content="Canonical business books and their Entity relationship.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(interpretation, retrieval, {})
    assert result.established_meaning.resources == [
        "lending",
        "syndication",
        "asset_monetisation",
        "entities",
    ]
    assert [binding.resource for binding in result.established_meaning.semantic_bindings] == [
        "lending",
        "syndication",
        "asset_monetisation",
        "entities",
    ]
    assert result.established_meaning.semantic_bindings[0].binding_kind == "resource"
    assert len(completions.calls) == 2
    request_payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert request_payload["input"]["canonical_resource_fields"]["lending"]


async def test_grounding_preserves_identity_scoped_book_existence_when_metric_is_missing():
    entity_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    meaning = {
        "resources": ["lending", "syndication", "asset_monetisation", "entities"],
        "semantic_bindings": [
            {
                "user_term": "exposure",
                "binding_kind": "metric",
                "passage_ids": ["books"],
            },
            {
                "user_term": "Example Energy only",
                "binding_kind": "relationship",
                "resource": "entities",
                "field": "id",
                "passage_ids": ["books"],
            },
        ],
        "grounded_values": [
            {
                "resource": "entities",
                "field": "id",
                "user_term": "Example Energy",
                "canonical_value": entity_id,
                "candidate_id": entity_id,
                "candidate_label": "Example Energy",
                "exact_match": True,
                "normalized_match": True,
            }
        ],
        "unavailable_obligations": [
            {
                "description": "One combined monetary exposure across the named books",
                "reason": "The books have no common authoritative monetary definition.",
                "prevents_main_answer": False,
            }
        ],
    }
    completions = FakeCompletions(
        [{"status": "RESOLVED", "established_meaning": meaning}]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": (
                "Check lending, syndication and asset-monetisation exposure for Example Energy only."
            ),
            "terms": [
                {
                    "text": "lending, syndication and asset-monetisation exposure",
                    "role": "measure",
                    "source_text": "lending, syndication and asset-monetisation exposure",
                },
                {"text": "exposure", "role": "measure", "source_text": "exposure"},
                {
                    "text": "Example Energy",
                    "role": "actor",
                    "source_text": "Example Energy",
                },
            ],
            "requested_shape": "scalar",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="book exposure",
        matches=[
            RetrievalMatch(
                passage_id="books",
                source="books",
                version="1",
                content="Named books retain answerable record-existence cohorts.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(
        interpretation,
        retrieval,
        {"entities": [{"id": entity_id, "legal_name": "Example Energy"}]},
    )
    assert result.status == "RESOLVED"
    assert result.issue is None
    assert result.established_meaning.unavailable_obligations[0].prevents_main_answer is False
    assert len(completions.calls) == 1


async def test_grounding_repairs_preexecution_absence_claim_for_grounded_field():
    meaning = {
        "resources": ["syndication", "entities"],
        "semantic_bindings": [
            {
                "user_term": "EV Mobility company",
                "binding_kind": "relationship",
                "resource": "entities",
                "field": "sector",
                "canonical_values": ["EV Mobility"],
                "passage_ids": ["relationships.entity_dimensions"],
            }
        ],
        "grounded_values": [
            {
                "field": "sector",
                "user_term": "EV Mobility",
                "canonical_value": "EV Mobility",
                "exact_match": True,
                "normalized_match": True,
            }
        ],
    }
    invalid = {
        "status": "NEEDS_CLARIFICATION",
        "established_meaning": meaning,
        "issue": {
            "term": "EV Mobility company",
            "kind": "NOT_FOUND",
            "reason": "No authoritative Entity-sector assignment is available.",
            "missing_definition": "Entity assignment to the EV Mobility sector",
            "missing_kind": "AUTHORITATIVE_DATA",
        },
    }
    repaired = {
        "status": "RESOLVED",
        "established_meaning": meaning,
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "List every EV Mobility company with a live syndication ask.",
            "terms": [
                {
                    "text": "EV Mobility company",
                    "role": "dimension",
                    "source_text": "EV Mobility company",
                }
            ],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="EV Mobility company",
        matches=[
            RetrievalMatch(
                passage_id="relationships.entity_dimensions",
                source="relationships",
                version="1",
                content="Sector is a governed Entity field.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(
        interpretation,
        retrieval,
        {"reference_values": {"Sector": [{"value": "EV Mobility"}]}},
    )
    assert result.status == "RESOLVED"
    assert len(completions.calls) == 2
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "defer matching-row existence to plan execution" in (repair["validation_errors"][0]["msg"])


async def test_grounding_repairs_missing_claim_for_live_controlled_value():
    invalid = {
        "status": "NEEDS_CLARIFICATION",
        "established_meaning": {"resources": ["syndication", "entities"]},
        "issue": {
            "term": "EV Mobility company",
            "kind": "NOT_FOUND",
            "reason": "No authoritative sector classification is available.",
            "missing_definition": "Entity classification for EV Mobility sector",
            "missing_kind": "AUTHORITATIVE_DATA",
        },
    }
    repaired = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["syndication", "entities"],
            "semantic_bindings": [
                {
                    "user_term": "EV Mobility company",
                    "binding_kind": "relationship",
                    "resource": "entities",
                    "field": "sector",
                    "canonical_values": ["EV Mobility"],
                    "passage_ids": ["company.dimension"],
                }
            ],
            "grounded_values": [
                {
                    "field": "sector",
                    "user_term": "EV Mobility",
                    "canonical_value": "EV Mobility",
                    "exact_match": True,
                    "normalized_match": True,
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "List every EV Mobility company with a live syndication ask.",
            "terms": [
                {
                    "text": "EV Mobility company",
                    "role": "dimension",
                    "source_text": "EV Mobility company",
                }
            ],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="EV Mobility company",
        matches=[
            RetrievalMatch(
                passage_id="company.dimension",
                source="relationships",
                version="1",
                content="Company dimensions apply through Entity fields.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(
        interpretation,
        retrieval,
        {"reference_values": {"Sector": [{"value": "EV Mobility", "label": "EV Mobility"}]}},
    )
    assert result.status == "RESOLVED"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "caller-visible controlled value 'EV Mobility'" in (repair["validation_errors"][0]["msg"])


def test_only_answerable_outcome_can_contain_verified_question():
    with pytest.raises(ValueError, match="Only ANSWERABLE"):
        AnswerabilityResult(
            outcome="OUT_OF_SCOPE",
            reason="Mutation request",
            verified_question={
                "standalone_question": "Create a lead",
                "resources": ["leads"],
                "answer_shape": "scalar",
                "evidence_obligations": [
                    {
                        "kind": "answer_shape",
                        "description": "Create a lead",
                        "provenance": "user",
                        "source_text": "Create a lead",
                    }
                ],
            },
        )
    with pytest.raises(ValueError, match="requires verified_question"):
        AnswerabilityResult(outcome="ANSWERABLE", reason="Missing verified question")
    with pytest.raises(ValueError, match="requires clarification_question"):
        AnswerabilityResult(
            outcome="CLARIFICATION_REQUIRED",
            reason="A material term is ambiguous.",
        )


def test_question_level_grounding_contract_requires_complete_alternatives():
    with pytest.raises(ValueError, match="at least two alternatives"):
        ValueGroundingResult.model_validate(
            {
                "status": "NEEDS_CLARIFICATION",
                "issue": {
                    "term": "active deals",
                    "kind": "AMBIGUOUS",
                    "reason": "More than one business book remains.",
                    "alternatives": [
                        {
                            "label": "commercial deal funnel",
                            "meaning": {"resources": ["deals"]},
                        }
                    ],
                },
            }
        )
    with pytest.raises(ValueError, match="RESOLVED grounding cannot contain an issue"):
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "issue": {
                    "term": "stale",
                    "kind": "NOT_FOUND",
                    "reason": "No threshold is defined.",
                    "missing_definition": "Ageing threshold",
                    "missing_kind": "AUTHORITATIVE_DATA",
                },
            }
        )
    with pytest.raises(ValueError, match="requires an issue"):
        ValueGroundingResult(status="NEEDS_CLARIFICATION")


async def test_answerability_rejects_answerable_material_ambiguity():
    invalid_answer = {
        "outcome": "ANSWERABLE",
        "reason": "A book was selected.",
        "verified_question": {
            "standalone_question": "Show active deals",
            "resources": ["deals"],
            "answer_shape": "list",
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List active deals",
                    "provenance": "user",
                    "source_text": "Show active deals",
                }
            ],
        },
    }
    completions = FakeCompletions([invalid_answer, invalid_answer])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show active deals",
            "terms": [{"text": "active deals", "role": "subject", "source_text": "active deals"}],
            "requested_shape": "list",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "NEEDS_CLARIFICATION",
            "issue": {
                "term": "active deals",
                "kind": "AMBIGUOUS",
                "reason": "More than one complete book interpretation remains.",
                "alternatives": [
                    {
                        "label": "commercial deal funnel",
                        "meaning": {"resources": ["deals"]},
                    },
                    {
                        "label": "own-book lending",
                        "meaning": {"resources": ["lending"]},
                    },
                ],
            },
        }
    )
    retrieval = SemanticRetrievalResult(
        query="active deals",
        matches=[
            RetrievalMatch(
                passage_id="books.ambiguity",
                source="books",
                version="test-ontology-v3",
                content="Unqualified deal language is ambiguous.",
                fusion_score=1.0,
            ),
            RetrievalMatch(
                passage_id="books.boundaries",
                source="books",
                version="test-ontology-v3",
                content="Ledger books have distinct boundaries.",
                fusion_score=0.9,
            ),
        ],
    )
    with pytest.raises(ValueError, match="unresolved Grounding"):
        await stages.answerability(interpretation, grounding, retrieval)


async def test_grounding_rejects_unretrieved_alternative_provenance():
    invalid = {
        "status": "NEEDS_CLARIFICATION",
        "issue": {
            "term": "active deals",
            "kind": "AMBIGUOUS",
            "reason": "More than one complete book interpretation remains.",
            "alternatives": [
                {
                    "label": "commercial deal funnel",
                    "meaning": {
                        "resources": ["deals"],
                        "semantic_bindings": [
                            {
                                "user_term": "active deals",
                                "binding_kind": "resource",
                                "resource": "deals",
                                "passage_ids": ["resources.deals"],
                            }
                        ],
                    },
                },
                {
                    "label": "own-book lending",
                    "meaning": {
                        "resources": ["lending"],
                        "semantic_bindings": [
                            {
                                "user_term": "active deals",
                                "binding_kind": "resource",
                                "resource": "lending",
                                "passage_ids": ["invented.book"],
                            }
                        ],
                    },
                },
            ],
        },
    }
    completions = FakeCompletions([invalid, invalid])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show active deals",
            "terms": [{"text": "active deals", "role": "subject", "source_text": "active deals"}],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="active deals",
        matches=[
            RetrievalMatch(
                passage_id="books.ambiguity",
                source="books",
                version="test-ontology-v3",
                content="Unqualified deal language is ambiguous.",
                fusion_score=1.0,
            ),
            RetrievalMatch(
                passage_id="resources.deals",
                source="deals",
                version="test-ontology-v3",
                content="Deal is the commercial funnel.",
                fusion_score=0.9,
            ),
        ],
    )
    with pytest.raises(ValueError, match="were not retrieved: invented.book"):
        await stages.ground(interpretation, retrieval, {})


async def test_grounding_preserves_complete_retrieved_ambiguity_without_repair():
    ambiguous = {
        "status": "NEEDS_CLARIFICATION",
        "issue": {
            "term": "active work",
            "kind": "AMBIGUOUS",
            "reason": "Two governed books remain possible.",
            "alternatives": [
                {
                    "label": "commercial funnel",
                    "meaning": {
                        "resources": ["deals"],
                        "semantic_bindings": [
                            {
                                "user_term": "active work",
                                "binding_kind": "resource",
                                "resource": "deals",
                                "passage_ids": ["books.ambiguity"],
                            }
                        ],
                    },
                },
                {
                    "label": "own-book facilities",
                    "meaning": {
                        "resources": ["lending"],
                        "semantic_bindings": [
                            {
                                "user_term": "active work",
                                "binding_kind": "resource",
                                "resource": "lending",
                                "passage_ids": ["books.ambiguity"],
                            }
                        ],
                    },
                },
            ],
        },
    }
    completions = FakeCompletions([ambiguous])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show active work",
            "terms": [{"text": "active work", "role": "subject", "source_text": "active work"}],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="active work",
        matches=[
            RetrievalMatch(
                passage_id="books.ambiguity",
                source="books",
                version="test-ontology-v2",
                content="Unqualified work language does not select a governed business book.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(interpretation, retrieval, {})
    assert result.status == "NEEDS_CLARIFICATION"
    assert result.issue is not None
    assert [alternative.meaning.resources for alternative in result.issue.alternatives] == [
        ["deals"],
        ["lending"],
    ]
    assert len(completions.calls) == 1


async def test_grounding_repairs_noncanonical_resource_in_alternatives():
    invalid = {
        "status": "NEEDS_CLARIFICATION",
        "issue": {
            "term": "workload",
            "kind": "AMBIGUOUS",
            "reason": "More than one complete workload meaning remains.",
            "alternatives": [
                {
                    "label": "commercial",
                    "meaning": {"resources": ["Deal"]},
                },
                {
                    "label": "own-book",
                    "meaning": {"resources": ["lending"]},
                },
            ],
        },
    }
    repaired = {
        **invalid,
        "issue": {
            **invalid["issue"],
            "alternatives": [
                {
                    **invalid["issue"]["alternatives"][0],
                    "meaning": {"resources": ["deals"]},
                },
                invalid["issue"]["alternatives"][1],
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Who has the heaviest workload?",
            "terms": [{"text": "workload", "role": "measure", "source_text": "workload"}],
            "requested_shape": "ranked",
        }
    )
    result = await stages.ground(
        interpretation,
        SemanticRetrievalResult(query="workload", matches=[]),
        {},
    )
    assert result.issue is not None
    assert result.issue.alternatives[0].meaning.resources == ["deals"]
    assert len(completions.calls) == 2


async def test_grounding_repair_uses_canonical_resource_ids_and_field_ownership():
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending", "entities"],
            "semantic_bindings": [
                {
                    "user_term": "company relationship",
                    "binding_kind": "relationship",
                    "resource": "entities",
                    "field": "entity_id",
                    "passage_ids": ["relationships.entity_lines"],
                }
            ],
        },
    }
    repaired = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending", "entities"],
            "semantic_bindings": [
                {
                    "user_term": "company relationship",
                    "binding_kind": "relationship",
                    "resource": "lending",
                    "field": "entity_id",
                    "definition": "Join lending.entity_id to entities.id.",
                    "passage_ids": ["relationships.entity_lines"],
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show each facility's company",
            "terms": [
                {
                    "text": "facility's company",
                    "role": "relationship",
                    "source_text": "facility's company",
                }
            ],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="facility company",
        matches=[
            RetrievalMatch(
                passage_id="relationships.entity_lines",
                source="relationships",
                version="test-ontology-v1",
                content="LendingTracker joins Entity by entity_id=id.",
                fusion_score=1.0,
            )
        ],
    )
    result = await stages.ground(interpretation, retrieval, {})
    meaning = result.established_meaning
    assert meaning.resources == ["lending", "entities"]
    assert meaning.semantic_bindings[0].resource == "lending"
    assert meaning.semantic_bindings[0].field == "entity_id"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    error = repair["validation_errors"][0]["msg"]
    repair_matches = repair["repair_context"]["semantic_material"]["matches"]
    assert any(item["passage_id"] == "relationships.entity_lines" for item in repair_matches)
    assert "relationships.entity_lines" in repair["repair_context"]["allowed_passage_ids"]
    assert "lending" in repair["repair_context"]["canonical_resource_fields"]
    assert "unknown field 'entity_id' on resource 'entities'" in error
    assert "actual field owners" in error
    assert "lending" in error
    assert "entities" not in error.split("actual field owners:", 1)[1]
    repair_system_prompt = completions.calls[1]["messages"][0]["content"]
    assert "Do not introduce or remove semantic ambiguity" in repair_system_prompt
    assert "repair-context object keys are not passage ids" in repair_system_prompt


async def test_grounding_repairs_surface_label_used_for_candidate_backed_id():
    entity_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending"],
            "semantic_bindings": [
                {
                    "user_term": "Northwind Storage",
                    "binding_kind": "relationship",
                    "resource": "lending",
                    "field": "entity_id",
                    "passage_ids": ["relationships.entity_lines"],
                }
            ],
            "grounded_values": [
                {
                    "field": "entity_id",
                    "user_term": "Northwind Storage",
                    "canonical_value": "Northwind Storage Private Limited",
                    "candidate_id": entity_id,
                    "candidate_label": "Northwind Storage Private Limited",
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [
                {
                    **invalid["established_meaning"]["grounded_values"][0],
                    "canonical_value": entity_id,
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show own-book work for Northwind Storage",
            "terms": [
                {"text": "own-book work", "role": "subject", "source_text": "own-book work"},
                {
                    "text": "Northwind Storage",
                    "role": "actor",
                    "source_text": "Northwind Storage",
                },
            ],
            "requested_shape": "list",
        }
    )
    retrieval = SemanticRetrievalResult(
        query="own-book work company",
        matches=[
            RetrievalMatch(
                passage_id="relationships.entity_lines",
                source="relationships",
                version="test-ontology-v2",
                content="Work rows join their company through entity_id.",
                fusion_score=1.0,
            )
        ],
    )
    candidates = {
        "entities": [
            {
                "id": entity_id,
                "legal_name": "Northwind Storage Private Limited",
            }
        ],
        "counterparties": [],
    }
    result = await stages.ground(interpretation, retrieval, candidates)
    grounded = result.established_meaning.grounded_values[0]
    assert grounded.canonical_value == entity_id
    assert len(completions.calls) == 2
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "entities.id as canonical_value" in repair["validation_errors"][0]["msg"]
    assert repair["repair_context"]["caller_visible_candidates"]["entities"][0]["id"] == entity_id


async def test_grounding_repairs_person_uuid_to_stored_assignment_handle():
    person_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending"],
            "grounded_values": [
                {
                    "field": "rm",
                    "user_term": "Prateek",
                    "canonical_value": person_id,
                    "candidate_id": person_id,
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [
                {
                    **invalid["established_meaning"]["grounded_values"][0],
                    "canonical_value": "PS",
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Show lending work assigned to Prateek",
            "terms": [{"text": "Prateek", "role": "actor", "source_text": "Prateek"}],
            "requested_shape": "list",
        }
    )
    result = await stages.ground(
        interpretation,
        SemanticRetrievalResult(query="lending Prateek", matches=[]),
        {"people": [{"id": person_id, "name": "PS", "full_name": "Prateek Singh"}]},
    )
    grounded = result.established_meaning.grounded_values[0]
    assert grounded.canonical_value == "PS"
    assert grounded.candidate_id == person_id
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "people.name as canonical_value" in repair["validation_errors"][0]["msg"]
    assert repair["repair_context"]["caller_visible_candidates"]["people"][0]["name"] == "PS"


async def test_grounding_repair_receives_unique_candidate_id_for_known_stored_value():
    correct_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending"],
            "grounded_values": [
                {
                    "field": "analyst",
                    "user_term": "Prateek",
                    "canonical_value": "PS",
                    "candidate_id": "invented-id",
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [
                {
                    **invalid["established_meaning"]["grounded_values"][0],
                    "candidate_id": correct_id,
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "How many clients is Prateek handling?",
            "terms": [{"text": "Prateek", "role": "actor", "source_text": "Prateek"}],
            "requested_shape": "scalar",
        }
    )
    result = await stages.ground(
        interpretation,
        SemanticRetrievalResult(query="Prateek clients", matches=[]),
        {"people": [{"id": correct_id, "name": "PS", "full_name": "Prateek Seth"}]},
    )
    assert result.established_meaning.grounded_values[0].candidate_id == correct_id
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert correct_id in repair["validation_errors"][0]["msg"]


async def test_grounding_does_not_bind_a_class_noun_to_one_candidate():
    lender_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["counterparties", "syndication_lenders"],
            "grounded_values": [
                {
                    "field": "lender_name",
                    "user_term": "active lenders",
                    "canonical_value": "Kahn Capital",
                    "candidate_id": lender_id,
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Which active lenders have never been approached?",
            "terms": [
                {
                    "text": "active lenders",
                    "role": "subject",
                    "source_text": "active lenders",
                }
            ],
            "requested_shape": "list",
        }
    )
    result = await stages.ground(
        interpretation,
        SemanticRetrievalResult(query="active lenders approached", matches=[]),
        {"counterparties": [{"id": lender_id, "name": "Kahn Capital"}]},
    )
    assert result.established_meaning.grounded_values == []
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "does not name its selected" in repair["validation_errors"][0]["msg"]


async def test_grounding_cannot_collapse_separately_described_entity_cohorts():
    entity_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    first = {
        "field": "entity_id",
        "user_term": "Zeon Charging",
        "canonical_value": entity_id,
        "candidate_id": entity_id,
    }
    second = {
        "field": "entity_id",
        "user_term": "Zeon entity lead",
        "canonical_value": entity_id,
        "candidate_id": entity_id,
    }
    invalid = {
        "status": "RESOLVED",
        "established_meaning": {
            "resources": ["lending", "syndication", "leads"],
            "grounded_values": [first, second],
        },
    }
    repaired = {
        **invalid,
        "established_meaning": {
            **invalid["established_meaning"],
            "grounded_values": [first],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": (
                "Show Zeon Charging lending and syndication, and keep any unconverted "
                "Zeon entity lead separate."
            ),
            "terms": [
                {"text": "Zeon Charging", "role": "actor", "source_text": "Zeon Charging"},
                {"text": "Zeon entity lead", "role": "actor", "source_text": "Zeon entity lead"},
            ],
            "requested_shape": "list",
        }
    )
    result = await stages.ground(
        interpretation,
        SemanticRetrievalResult(query="Zeon separate", matches=[]),
        {"entities": [{"id": entity_id, "legal_name": "Zeon Charging Private Limited"}]},
    )
    assert [value.user_term for value in result.established_meaning.grounded_values] == ["Zeon Charging"]
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "separately described cohorts" in repair["validation_errors"][0]["msg"]


async def test_answerability_can_mark_missing_authoritative_fact_out_of_scope():
    completions = FakeCompletions(
        [
            {
                "outcome": "OUT_OF_SCOPE",
                "reason": "The Ledger does not hold an authoritative promoter-person field.",
            }
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Who is the promoter?",
            "terms": [{"text": "promoter", "role": "relationship", "source_text": "promoter"}],
            "requested_shape": "scalar",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "NEEDS_CLARIFICATION",
            "established_meaning": {"resources": ["entities"]},
            "issue": {
                "term": "promoter",
                "kind": "NOT_FOUND",
                "reason": "No authoritative promoter-person field exists.",
                "missing_definition": "Authoritative promoter-person identity",
                "missing_kind": "AUTHORITATIVE_DATA",
            },
        }
    )
    result = await stages.answerability(
        interpretation,
        grounding,
        SemanticRetrievalResult(query="promoter", matches=[]),
    )
    assert result.outcome == "OUT_OF_SCOPE"


async def test_answerability_labels_natural_authoritative_choice_as_clarification():
    completions = FakeCompletions(
        [
            {
                "outcome": "OUT_OF_SCOPE",
                "reason": "The intended exposure meaning is not established.",
            },
        ]
    )
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "What is our exposure to Example Energy?",
            "terms": [{"text": "exposure", "role": "measure", "source_text": "exposure"}],
            "requested_shape": "scalar",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "NEEDS_CLARIFICATION",
            "established_meaning": {"resources": ["entities"]},
            "issue": {
                "term": "exposure",
                "kind": "NOT_FOUND",
                "reason": "The question does not define which exposure meaning is intended.",
                "missing_definition": "Whether own-book or off-book exposure is intended.",
                "missing_kind": "AUTHORITATIVE_DATA",
            },
        }
    )
    result = await stages.answerability(
        interpretation, grounding, SemanticRetrievalResult(query="exposure", matches=[])
    )
    assert result.outcome == "OUT_OF_SCOPE"
    assert len(completions.calls) == 1
    assert result.clarification_question is None


async def test_answerability_repairs_unavailable_metric_to_named_book_existence():
    invalid = {
        "outcome": "OUT_OF_SCOPE",
        "reason": "No common monetary exposure metric is held.",
    }
    repaired = {
        "outcome": "ANSWERABLE",
        "reason": "Named-book record existence remains answerable.",
        "verified_question": {
            "standalone_question": "Check three books for Example Energy only.",
            "resources": ["lending", "syndication", "asset_monetisation", "entities"],
            "answer_shape": "list",
            "metrics": [
                {"resource": resource, "aggregation": "count"}
                for resource in ("lending", "syndication", "asset_monetisation")
            ],
            "filters": [
                {
                    "field": "entity_id",
                    "operator": "exclude_from_combination",
                    "value": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "source_text": "keep Example Green separate",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "Report each named book separately.",
                    "provenance": "user",
                    "source_text": "three books",
                },
                {
                    "kind": "display",
                    "description": "Keep Example Green separate.",
                    "provenance": "user",
                    "source_text": "keep Example Green separate",
                },
                {
                    "kind": "unavailable",
                    "description": "One combined monetary exposure across the named books",
                    "provenance": "ontology",
                    "passage_ids": ["books"],
                    "availability": "UNAVAILABLE",
                    "unavailable_reason": (
                        "The books have no common authoritative monetary definition."
                    ),
                    "prevents_main_answer": False,
                },
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Check three books for Example Energy only.",
            "terms": [{"text": "exposure", "role": "measure", "source_text": "exposure"}],
            "requested_shape": "list",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["lending", "syndication", "asset_monetisation", "entities"],
                "semantic_bindings": [
                    {
                        "user_term": "exposure",
                        "binding_kind": "metric",
                        "passage_ids": ["books"],
                    }
                ],
                "unavailable_obligations": [
                    {
                        "description": "One combined monetary exposure across the named books",
                        "reason": "The books have no common authoritative monetary definition.",
                        "prevents_main_answer": False,
                    }
                ],
                "grounded_values": [
                    {
                        "field": "entity_id",
                        "user_term": "Example Energy",
                        "canonical_value": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "candidate_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    }
                ],
            },
        }
    )
    result = await stages.answerability(
        interpretation, grounding, SemanticRetrievalResult(query="books", matches=[])
    )
    assert result.outcome == "ANSWERABLE"
    assert result.verified_question is not None
    assert len(completions.calls) == 2


async def test_answerability_repairs_internal_clarification_for_missing_authority():
    invalid = {
        "outcome": "CLARIFICATION_REQUIRED",
        "reason": "The group membership is unavailable.",
        "clarification_question": "What internal group code should I use?",
    }
    repaired = {
        "outcome": "OUT_OF_SCOPE",
        "reason": "The governed data does not hold authoritative group membership.",
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Combine companies in the same promoter group",
            "terms": [
                {
                    "text": "same promoter group",
                    "role": "relationship",
                    "source_text": "same promoter group",
                }
            ],
            "requested_shape": "scalar",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "NEEDS_CLARIFICATION",
            "established_meaning": {"resources": ["entities"]},
            "issue": {
                "term": "same promoter group",
                "kind": "NOT_FOUND",
                "reason": "Caller-visible authoritative membership is unavailable.",
                "missing_definition": "Authoritative group membership",
                "missing_kind": "AUTHORITATIVE_DATA",
            },
        }
    )
    result = await stages.answerability(
        interpretation, grounding, SemanticRetrievalResult(query="group", matches=[])
    )
    assert result.outcome == "OUT_OF_SCOPE"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "must return OUT_OF_SCOPE" in repair["validation_errors"][0]["msg"]


async def test_answerability_repairs_noncanonical_resource_and_field_spelling():
    invalid = {
        "outcome": "ANSWERABLE",
        "reason": "The governed state is filterable.",
        "verified_question": {
            "standalone_question": "List companies in Maharashtra.",
            "resources": ["Entity"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "Entity",
                    "field": "STATE",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List companies in Maharashtra.",
                }
            ],
        },
    }
    repaired = {
        **invalid,
        "verified_question": {
            **invalid["verified_question"],
            "resources": ["entities"],
            "filters": [
                {
                    **invalid["verified_question"]["filters"][0],
                    "resource": "entities",
                    "field": "state",
                }
            ],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List companies in Maharashtra.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["entities"],
                    "semantic_bindings": [
                        {
                            "user_term": "Maharashtra",
                            "binding_kind": "field",
                            "resource": "entities",
                            "field": "state",
                            "canonical_values": ["Maharashtra"],
                            "passage_ids": ["entities.state"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "state",
                            "user_term": "Maharashtra",
                            "canonical_value": "Maharashtra",
                            "exact_match": True,
                        }
                    ],
                },
            }
        ),
        SemanticRetrievalResult(query="companies Maharashtra", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.resources == ["entities"]
    assert result.verified_question.filters[0]["resource"] == "entities"
    assert result.verified_question.filters[0]["field"] == "state"
    assert len(completions.calls) == 2


async def test_answerability_accepts_controlled_state_alias_provenance():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The governed state is filterable.",
        "verified_question": {
            "standalone_question": "List companies in Maharashtra.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "equals",
                    "value": "Maharashtra",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List companies in Maharashtra.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List companies in Maharashtra.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["entities"],
                    "semantic_bindings": [
                        {
                            "user_term": "Maharashtra",
                            "binding_kind": "field",
                            "resource": "entities",
                            "field": "state",
                            "canonical_values": ["MH"],
                            "passage_ids": ["entities.state"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "state",
                            "user_term": "Maharashtra",
                            "canonical_value": "MH",
                            "exact_match": True,
                        }
                    ],
                },
            }
        ),
        SemanticRetrievalResult(query="companies Maharashtra", matches=[]),
    )

    assert result.outcome == "ANSWERABLE"
    assert result.verified_question is not None
    assert result.verified_question.filters[0]["value"] == "MH"
    assert result.verified_question.grounded_values[0].canonical_value == "MH"
    assert len(completions.calls) == 1


async def test_answerability_substitutes_unique_grounded_sector_value():
    answerable = {
        "outcome": "ANSWERABLE",
        "reason": "The governed sector is filterable.",
        "verified_question": {
            "standalone_question": "List Solar EPC companies.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "in",
                    "values": ["Solar"],
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List Solar EPC companies.",
                }
            ],
        },
    }
    completions = FakeCompletions([answerable])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List Solar EPC companies.",
                "requested_shape": "list",
            }
        ),
        ValueGroundingResult.model_validate(
            {
                "status": "RESOLVED",
                "established_meaning": {
                    "resources": ["entities"],
                    "semantic_bindings": [
                        {
                            "user_term": "Solar EPC",
                            "binding_kind": "field",
                            "resource": "entities",
                            "field": "sector",
                            "canonical_values": ["Solar - EPC"],
                            "passage_ids": ["entities.sector"],
                        }
                    ],
                    "grounded_values": [
                        {
                            "field": "sector",
                            "user_term": "Solar EPC",
                            "canonical_value": "Solar - EPC",
                        }
                    ],
                },
            }
        ),
        SemanticRetrievalResult(query="Solar EPC companies", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.filters[0]["values"] == ["Solar - EPC"]
    assert len(completions.calls) == 1


async def test_answerability_repairs_ambiguous_controlled_filter_with_candidates():
    broad = {
        "outcome": "ANSWERABLE",
        "reason": "The governed sectors are filterable.",
        "verified_question": {
            "standalone_question": "List the selected solar companies.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "equals",
                    "value": "Solar",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List matching companies.",
                    "provenance": "user",
                    "source_text": "List the selected solar companies.",
                }
            ],
        },
    }
    repaired = {
        **broad,
        "verified_question": {
            **broad["verified_question"],
            "filters": [
                {
                    "resource": "entities",
                    "field": "sector",
                    "operator": "in",
                    "values": ["Solar - EPC", "Solar - Rooftop"],
                }
            ],
        },
    }
    completions = FakeCompletions([broad, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["entities"],
                "semantic_bindings": [
                    {
                        "user_term": "selected solar sectors",
                        "binding_kind": "field",
                        "resource": "entities",
                        "field": "sector",
                        "canonical_values": ["Solar - EPC", "Solar - Rooftop"],
                        "passage_ids": ["entities.sector"],
                    }
                ],
                "grounded_values": [
                    {
                        "field": "sector",
                        "user_term": "Solar EPC",
                        "canonical_value": "Solar - EPC",
                    },
                    {
                        "field": "sector",
                        "user_term": "Solar Rooftop",
                        "canonical_value": "Solar - Rooftop",
                    },
                ],
            },
        }
    )

    result = await stages.answerability(
        QuestionInterpretation.model_validate(
            {
                "standalone_question": "List the selected solar companies.",
                "requested_shape": "list",
            }
        ),
        grounding,
        SemanticRetrievalResult(query="selected solar companies", matches=[]),
    )

    assert result.verified_question is not None
    assert result.verified_question.filters[0]["values"] == [
        "Solar - EPC",
        "Solar - Rooftop",
    ]
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    message = repair["validation_errors"][0]["msg"]
    assert "entities.sector" in message
    assert "model value 'Solar'" in message
    assert "[Solar - EPC, Solar - Rooftop]" in message


async def test_answerability_repairs_field_to_its_canonical_resource_owner():
    obligation = {
        "kind": "row_field",
        "description": "Return lender names",
        "resource": "counterparties",
        "field": "lender_name",
        "provenance": "ontology",
        "passage_ids": ["metrics.lender_approach"],
    }
    invalid = {
        "outcome": "ANSWERABLE",
        "reason": "The comparison is defined.",
        "verified_question": {
            "standalone_question": "Which lenders have never been approached?",
            "resources": ["counterparties", "syndication_lenders"],
            "answer_shape": "list",
            "evidence_obligations": [obligation],
        },
    }
    repaired = {
        **invalid,
        "verified_question": {
            **invalid["verified_question"],
            "evidence_obligations": [{**obligation, "field": "name"}],
        },
    }
    completions = FakeCompletions([invalid, repaired])
    stages = ModelStages(SimpleNamespace(chat=SimpleNamespace(completions=completions)), _settings())
    interpretation = QuestionInterpretation.model_validate(
        {
            "standalone_question": "Which lenders have never been approached?",
            "terms": [{"text": "lenders", "role": "subject", "source_text": "lenders"}],
            "requested_shape": "list",
        }
    )
    grounding = ValueGroundingResult.model_validate(
        {
            "status": "RESOLVED",
            "established_meaning": {
                "resources": ["counterparties", "syndication_lenders"],
                "semantic_bindings": [
                    {
                        "user_term": "never approached",
                        "binding_kind": "relationship",
                        "resource": "syndication_lenders",
                        "field": "lender_name",
                        "passage_ids": ["metrics.lender_approach"],
                    }
                ],
            },
        }
    )
    result = await stages.answerability(
        interpretation,
        grounding,
        SemanticRetrievalResult(query="lenders approached", matches=[]),
    )
    assert result.verified_question is not None
    assert result.verified_question.evidence_obligations[0].field == "name"
    repair = json.loads(completions.calls[1]["messages"][1]["content"])
    assert "actual field owners" in repair["validation_errors"][0]["msg"]
    assert "counterparties" in repair["repair_context"]["canonical_resource_fields"]
