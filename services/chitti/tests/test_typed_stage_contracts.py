from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.model_stages import (
    ModelResponseTerminationError,
    ModelStages,
    _split_grounding_candidates,
    _usage_sink,
    _validate_filter_correspondence,
    strict_output_schema,
)
from app.operations import OPERATIONS
from app.register_access import RESOURCE_FIELDS, RESOURCE_SPECS
from app.stage_models import (
    AnswerabilityResultDraft,
    AnswerGenerationResult,
    ConversationResolutionDraft,
    PlannedOperation,
    QualitativeAnalysisDraft,
    QueryPlan,
    QueryPlanDraft,
    QuestionInterpretation,
    SemanticBinding,
    ValueGroundingResult,
    VerifiedFilter,
    VerifiedQuestion,
)

OUTPUT_MODELS = (
    ConversationResolutionDraft,
    QuestionInterpretation,
    ValueGroundingResult,
    AnswerabilityResultDraft,
    QueryPlanDraft,
    QualitativeAnalysisDraft,
    AnswerGenerationResult,
)


def _settings() -> Settings:
    return Settings(_env_file=None, conversation_model="conversation-model")


def _response(*, content: str | None, finish_reason: str = "stop", refusal: str | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=3,
            total_tokens=14,
            prompt_tokens_details=SimpleNamespace(cached_tokens=2),
        ),
        model_extra={"provider": "test-provider"},
    )


class Responses:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def _walk_schema(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_schema(value)


def test_every_model_output_schema_is_in_the_measured_strict_subset():
    for output_type in OUTPUT_MODELS:
        schema = strict_output_schema(output_type)
        assert schema["type"] == "object"
        for node in _walk_schema(schema):
            assert "oneOf" not in node
            assert "uniqueItems" not in node
            assert "const" not in node
            if "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["properties"]) == set(node["required"])


def test_schema_descriptions_carry_generation_guidance():
    schema = strict_output_schema(QueryPlanDraft)
    filter_args = schema["$defs"]["FilterArguments"]["properties"]
    join_args = schema["$defs"]["JoinArguments"]["properties"]
    assert "derived and joined" in filter_args["field"]["description"]
    assert "verified-filter ids" in filter_args["verified_filter_ids"]["description"]
    assert "optional display enrichment" in join_args["how"]["description"]


def test_grounding_caller_visible_candidate_block_is_byte_stable():
    first = {
        "people": [{"name": "Anita", "id": "p2"}, {"id": "p1", "name": "Arun"}],
        "counterparties": [{"name": "Bank B", "id": "c2"}],
        "reference_values": {"state": [{"label": "Maharashtra", "value": "MH"}]},
        "entities": [{"id": "e1", "legal_name": "First question company"}],
        "completeness": {"entities": True, "people": True, "counterparties": True},
    }
    second = {
        "people": [{"name": "Arun", "id": "p1"}, {"id": "p2", "name": "Anita"}],
        "counterparties": [{"id": "c2", "name": "Bank B"}],
        "reference_values": {"state": [{"value": "MH", "label": "Maharashtra"}]},
        "entities": [{"id": "e9", "legal_name": "Different question company"}],
        "completeness": {"counterparties": True, "people": True, "entities": False},
    }
    first_stable, first_variable = _split_grounding_candidates(first)
    second_stable, second_variable = _split_grounding_candidates(second)

    def render(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    assert render(first_stable) == render(second_stable)
    assert render(first_variable) != render(second_variable)


def test_resource_enums_are_derived_from_the_register_maps():
    schema = strict_output_schema(QueryPlanDraft)
    resource_enum = schema["$defs"]["PlannedRead"]["properties"]["resource"]["enum"]
    assert set(resource_enum) == set(RESOURCE_SPECS) == set(RESOURCE_FIELDS)


def test_unknown_resource_wrong_owner_and_empty_dataset_are_rejected():
    with pytest.raises(ValidationError):
        SemanticBinding.model_validate(
            {
                "user_term": "x",
                "binding_kind": "field",
                "resource": "invented",
                "field": "id",
                "passage_ids": ["p"],
            }
        )
    with pytest.raises(ValidationError, match="actual field owners: .*lending"):
        SemanticBinding.model_validate(
            {
                "user_term": "company",
                "binding_kind": "relationship",
                "resource": "entities",
                "field": "entity_id",
                "passage_ids": ["p"],
            }
        )
    with pytest.raises(ValidationError):
        QueryPlanDraft.model_validate({"reads": [{"name": "", "resource": "entities"}], "operations": []})


def test_verified_filter_requires_a_field_and_operator_value_shape():
    with pytest.raises(ValidationError):
        VerifiedFilter.model_validate({"resource": "entities", "value": "MH"})

    normalized = VerifiedFilter.model_validate(
        {
            "resource": "entities",
            "field": "state",
            "operator": "eq",
            "value": None,
            "values": ["MH"],
        }
    )
    assert normalized.value == "MH"
    assert normalized.values is None

    redundant = VerifiedFilter.model_validate(
        {
            "resource": "entities",
            "field": "state",
            "operator": "eq",
            "value": "MH",
            "values": ["MH"],
        }
    )
    assert redundant.value == "MH"
    assert redundant.values is None

    with pytest.raises(ValidationError, match="accepts one value"):
        VerifiedFilter.model_validate(
            {
                "resource": "entities",
                "field": "state",
                "operator": "eq",
                "values": ["MH", "GJ"],
            }
        )

    with pytest.raises(ValidationError, match="accepts one value"):
        VerifiedFilter.model_validate(
            {
                "resource": "entities",
                "field": "state",
                "operator": "eq",
                "value": "MH",
                "values": ["GJ"],
            }
        )


def test_operation_variants_equal_the_executor_vocabulary_and_reject_cross_arguments():
    variants = get_args(PlannedOperation)
    tags = {get_args(variant.model_fields["operation"].annotation)[0] for variant in variants}
    assert tags == set(OPERATIONS)
    with pytest.raises(ValidationError):
        QueryPlanDraft.model_validate(
            {
                "reads": [{"name": "entities", "resource": "entities"}],
                "operations": [
                    {
                        "name": "bad",
                        "operation": "count",
                        "input": "entities",
                        "output": "bad",
                        "arguments": {"field": "state"},
                    }
                ],
            }
        )


def test_host_derived_fields_are_absent_from_draft_schemas():
    answerability = strict_output_schema(AnswerabilityResultDraft)
    planning = strict_output_schema(QueryPlanDraft)
    assert "missing_data_requirements" not in answerability["$defs"]["VerifiedQuestionDraft"]["properties"]
    assert "result_shapes" not in planning["properties"]
    draft = QueryPlanDraft.model_validate(
        {
            "reads": [{"name": "entities", "resource": "entities"}],
            "operations": [],
            "result_shapes": {"entities": "scalar"},
        }
    )
    final = QueryPlan.model_validate(draft.model_dump(mode="json"))
    assert final.result_shapes == {"entities": "rows"}


def _filtered_question() -> VerifiedQuestion:
    return VerifiedQuestion.model_validate(
        {
            "standalone_question": "List Maharashtra companies.",
            "resources": ["entities"],
            "answer_shape": "list",
            "filters": [
                {
                    "resource": "entities",
                    "field": "state",
                    "operator": "eq",
                    "value": "MH",
                }
            ],
            "evidence_obligations": [
                {
                    "kind": "answer_shape",
                    "description": "List companies.",
                    "provenance": "user",
                    "source_text": "List Maharashtra companies.",
                }
            ],
        }
    )


def test_filter_correspondence_has_pushdown_and_operation_parity():
    question = _filtered_question()
    pushdown = QueryPlan.model_validate(
        {
            "reads": [
                {
                    "name": "entities",
                    "resource": "entities",
                    "filters": [
                        {
                            "resource": "entities",
                            "field": "state",
                            "value": "MH",
                            "verified_filter_ids": ["f1"],
                        }
                    ],
                }
            ],
            "operations": [],
        }
    )
    operation = QueryPlan.model_validate(
        {
            "reads": [{"name": "entities", "resource": "entities"}],
            "operations": [
                {
                    "name": "state",
                    "operation": "filter",
                    "input": "entities",
                    "output": "state",
                    "arguments": {
                        "field": "state",
                        "operator": "eq",
                        "value": "MH",
                        "verified_filter_ids": ["f1"],
                    },
                }
            ],
        }
    )
    _validate_filter_correspondence(pushdown, question)
    _validate_filter_correspondence(operation, question)


def test_filter_correspondence_rejects_false_claim_and_coincidental_value():
    question = _filtered_question()
    false_claim = QueryPlan.model_validate(
        {
            "reads": [{"name": "entities", "resource": "entities"}],
            "operations": [
                {
                    "name": "wrong",
                    "operation": "filter",
                    "input": "entities",
                    "output": "mentions_MH",
                    "arguments": {
                        "field": "sector",
                        "operator": "eq",
                        "value": "MH",
                        "verified_filter_ids": ["f1"],
                    },
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="implements a different"):
        _validate_filter_correspondence(false_claim, question)

    coincidence = QueryPlan.model_validate(
        {
            "reads": [{"name": "MH", "resource": "entities"}],
            "operations": [],
        }
    )
    with pytest.raises(ValueError, match="omitted verified filter id"):
        _validate_filter_correspondence(coincidence, question)


@pytest.mark.parametrize("kind", ["refusal", "length", "content_filter"])
@pytest.mark.parametrize("repair", [False, True])
async def test_terminal_provider_outcomes_never_trigger_an_extra_repair(kind: str, repair: bool):
    terminal = {
        "refusal": _response(content=None, refusal="declined"),
        "length": _response(content="{", finish_reason="length"),
        "content_filter": _response(content=None, finish_reason="content_filter"),
    }[kind]
    responses = Responses([_response(content="{}"), terminal] if repair else [terminal])
    stages = ModelStages(
        SimpleNamespace(chat=SimpleNamespace(completions=responses)),
        _settings(),
    )
    sink = []
    token = _usage_sink.set(sink)
    try:
        with pytest.raises(ModelResponseTerminationError) as caught:
            await stages._json_call(
                stage="conversation_resolution",
                model="conversation-model",
                payload={"messages": []},
                output_type=ConversationResolutionDraft,
            )
    finally:
        _usage_sink.reset(token)
    assert caught.value.kind == kind
    assert len(responses.calls) == (2 if repair else 1)
    assert len(sink) == len(responses.calls)
    assert all(call.response_received for call in sink)
    assert all(call.provider == "test-provider" for call in sink)
    assert all(call.total_tokens == 14 for call in sink)
    assert all(call["response_format"]["type"] == "json_schema" for call in responses.calls)
    if repair:
        assert json.loads(responses.calls[1]["messages"][1]["content"])["invalid_output"] == "{}"
