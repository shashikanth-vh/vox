from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import _status_event, _stream_pipeline_completion
from app.config import Settings
from app.executor import PlanExecutor, _operation_completeness
from app.pipeline import RESULT_TABLE_DISPLAY_LIMIT, PipelineResponse, _render_result_table
from app.register_access import REPORT_UNASSESSABLE_FILTER_FIELDS, RegisterPlanError
from app.stage_models import QueryPlan


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_native_client_started_status_keeps_progress_indicator_active():
    assert _status_event(
        {
            "stage": "semantic_retrieval",
            "status": "started",
            "description": "Finding the business definitions needed to interpret your request…",
            "elapsed_ms": 0.0,
            "model": None,
        }
    ) == {
        "type": "status",
        "data": {
            "stage": "semantic_retrieval",
            "status": "started",
            "description": "Finding the business definitions needed to interpret your request…",
            "elapsed_ms": 0.0,
            "model": None,
            "done": False,
            "action": "semantic_retrieval",
        },
    }


@pytest.mark.parametrize("field", ["scope", "own_book_scope"])
def test_semantic_pseudo_field_in_read_filters_is_rejected_before_access(field: str):
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(
            {
                "reads": [
                    {
                        "name": "lending",
                        "resource": "lending",
                        "filters": {field: "own-book lending"},
                    }
                ],
                "operations": [],
                "result_names": ["lending"],
            }
        )


@pytest.mark.parametrize(
    ("resource", "field", "operator"),
    [
        ("entities", "sector", "contains"),
        ("entities", "sector", "icontains"),
        ("syndication", "mandate_status", "contains"),
        ("syndication", "mandate_status", "icontains"),
    ],
)
def test_substring_predicates_on_governed_or_composite_fields_are_rejected(
    resource: str,
    field: str,
    operator: str,
):
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "source", "resource": resource}],
            "operations": [
                {
                    "name": "unsafe",
                    "operation": "filter",
                    "input": "source",
                    "output": "unsafe",
                    "arguments": {
                        "field": field,
                        "operator": operator,
                        "value": "sent",
                    },
                }
            ],
            "result_names": ["unsafe"],
        }
    )

    with pytest.raises(RegisterPlanError, match="Substring predicates are unsafe"):
        PlanExecutor(_settings()).validate_plan(plan)


def test_exact_predicate_on_governed_composite_field_remains_valid():
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "source", "resource": "syndication"}],
            "operations": [
                {
                    "name": "exact",
                    "operation": "filter",
                    "input": "source",
                    "output": "exact",
                    "arguments": {
                        "field": "mandate_status",
                        "operator": "eq",
                        "value": "Sent - pending signature",
                    },
                }
            ],
            "result_names": ["exact"],
        }
    )

    PlanExecutor(_settings()).validate_plan(plan)


def test_filter_missing_policy_counts_input_rows_and_is_inert():
    rows = [{"state": None}, {"state": "Maharashtra"}, {"state": "Gujarat"}]
    args = {
        "field": "state",
        "operator": "eq",
        "value": "Maharashtra",
        "missing_policy": "report_unassessable",
    }
    assert _operation_completeness("filter", rows, args, {}) == {
        "state": {"assessed_count": 2, "missing_count": 1}
    }


def test_host_missing_policy_registry_covers_dimension_and_measure_filters():
    assert {
        ("entities", "state"),
        ("entities", "sector"),
        ("leads", "sector"),
        ("asset_monetisation", "state"),
        ("lending", "amount_cr"),
        ("syndication", "amount_cr"),
        ("asset_monetisation", "indicative_value_cr"),
    } <= REPORT_UNASSESSABLE_FILTER_FIELDS


def test_inner_join_supplying_missing_policy_field_is_rejected():
    plan = {
        "reads": [
            {"name": "deals", "resource": "deals"},
            {"name": "entities", "resource": "entities"},
        ],
        "operations": [
            {
                "name": "joined",
                "operation": "join",
                "input": "deals",
                "output": "joined",
                "arguments": {
                    "right": "entities",
                    "left_key": "entity_id",
                    "right_key": "id",
                    "how": "inner",
                    "relationship_required": True,
                    "right_prefix": "entity_",
                },
            },
            {
                "name": "filtered",
                "operation": "filter",
                "input": "joined",
                "output": "filtered",
                "arguments": {
                    "field": "entity_state",
                    "operator": "eq",
                    "value": "Maharashtra",
                    "missing_policy": "report_unassessable",
                },
            },
        ],
        "result_names": ["filtered"],
    }
    with pytest.raises(RegisterPlanError, match="must be left"):
        PlanExecutor(_settings()).validate_plan(QueryPlan.model_validate(plan))


def test_inner_join_after_nullable_related_filter_is_rejected():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "deals", "resource": "deals"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "filtered_entities",
                    "operation": "filter",
                    "input": "entities",
                    "output": "filtered_entities",
                    "arguments": {
                        "field": "state",
                        "operator": "eq",
                        "value": "Maharashtra",
                        "missing_policy": "report_unassessable",
                    },
                },
                {
                    "name": "joined",
                    "operation": "join",
                    "input": "deals",
                    "output": "joined",
                    "arguments": {
                        "right": "filtered_entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "how": "inner",
                        "relationship_required": True,
                    },
                },
            ],
            "result_names": ["joined"],
        }
    )
    with pytest.raises(RegisterPlanError, match="nullable resource"):
        PlanExecutor(_settings()).validate_plan(plan)


def test_result_table_hides_opaque_ids_but_keeps_business_references():
    rendered = _render_result_table(
        [{"result": "deals", "id": "internal-1", "tracker_no": "A006", "stage": "Live"}],
        {
            "deals": {
                "id": [{"resource": "deals", "field": "id"}],
                "tracker_no": [{"resource": "lending", "field": "tracker_no"}],
                "stage": [{"resource": "deals", "field": "stage"}],
            }
        },
    )
    assert "internal-1" not in rendered
    assert "tracker_no" in rendered
    assert "A006" in rendered
    assert "Live" in rendered


def test_result_table_unions_columns_and_suppresses_ids_per_all_result_origins():
    rendered = _render_result_table(
        [
            {"result": "deals", "deal_no": "D-1", "stage": "Live"},
            {"result": "entities", "id": "entity-1", "legal_name": "Acme"},
        ],
        {
            "deals": {
                "deal_no": [{"resource": "deals", "field": "deal_no"}],
                "stage": [{"resource": "deals", "field": "stage"}],
            },
            "entities": {
                "id": [{"resource": "entities", "field": "id"}],
                "legal_name": [{"resource": "entities", "field": "legal_name"}],
            },
        },
    )
    assert "deal_no" in rendered and "legal_name" in rendered
    assert "entity-1" not in rendered


def test_scalar_result_has_no_table_and_limited_result_reports_true_total_in_order():
    assert _render_result_table([], {}) == ""
    below_rows = [{"result": "deals", "deal_no": "D-00"}]
    below = _render_result_table(
        below_rows,
        {"deals": {"deal_no": [{"resource": "deals", "field": "deal_no"}]}},
        display_limit=50,
    )
    assert "D-00" in below
    assert "Showing" not in below
    exact_rows = [{"result": "deals", "deal_no": f"D-{index:02d}"} for index in range(50)]
    exact = _render_result_table(
        exact_rows,
        {"deals": {"deal_no": [{"resource": "deals", "field": "deal_no"}]}},
        display_limit=50,
    )
    assert "Showing" not in exact
    rows = [{"result": "deals", "deal_no": f"D-{index:02d}"} for index in range(51)]
    rendered = _render_result_table(
        rows,
        {"deals": {"deal_no": [{"resource": "deals", "field": "deal_no"}]}},
        display_limit=50,
    )
    assert "Showing 50 of 51 rows." in rendered
    assert rendered.index("D-00") < rendered.index("D-49")
    assert "D-50" not in rendered


def test_result_table_default_preview_is_200_rows():
    rows = [{"result": "deals", "deal_no": f"D-{index:03d}"} for index in range(201)]

    rendered = _render_result_table(
        rows,
        {"deals": {"deal_no": [{"resource": "deals", "field": "deal_no"}]}},
    )

    assert RESULT_TABLE_DISPLAY_LIMIT == 200
    assert "Showing 200 of 201 rows." in rendered
    assert "D-199" in rendered
    assert "D-200" not in rendered


def test_inner_join_upstream_of_primary_missing_filter_is_rejected():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "deals", "resource": "deals"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "joined",
                    "operation": "join",
                    "input": "deals",
                    "output": "joined",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "how": "inner",
                        "relationship_required": True,
                    },
                },
                {
                    "name": "filtered",
                    "operation": "filter",
                    "input": "joined",
                    "output": "filtered",
                    "arguments": {
                        "field": "id",
                        "operator": "eq",
                        "value": "deal-1",
                        "missing_policy": "report_unassessable",
                    },
                },
            ],
            "result_names": ["filtered"],
        }
    )
    with pytest.raises(RegisterPlanError, match="after an inner join"):
        PlanExecutor(_settings()).validate_plan(plan)


def test_set_union_inherits_inner_join_lineage_for_missing_filter_guard():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "joined_deals", "resource": "deals"},
                {"name": "entities", "resource": "entities"},
                {"name": "clean_deals", "resource": "deals"},
            ],
            "operations": [
                {
                    "name": "joined",
                    "operation": "join",
                    "input": "joined_deals",
                    "output": "joined",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "how": "inner",
                        "relationship_required": True,
                    },
                },
                {
                    "name": "all_deals",
                    "operation": "set_union",
                    "input": "joined",
                    "output": "all_deals",
                    "arguments": {"others": ["clean_deals"], "key_fields": ["id"]},
                },
                {
                    "name": "filtered",
                    "operation": "filter",
                    "input": "all_deals",
                    "output": "filtered",
                    "arguments": {
                        "field": "id",
                        "operator": "eq",
                        "value": "deal-1",
                        "missing_policy": "report_unassessable",
                    },
                },
            ],
            "result_names": ["filtered"],
        }
    )
    with pytest.raises(RegisterPlanError, match="after an inner join"):
        PlanExecutor(_settings()).validate_plan(plan)


@pytest.mark.asyncio
async def test_streamed_pipeline_emits_progress_before_whole_answer():
    class Pipeline:
        async def run(self, messages, *, identity, request_id, on_stage):
            on_stage(
                {
                    "stage": "grounding",
                    "status": "completed",
                    "description": "Resolved 2 business meanings and matched CM → Chetan Mehta.",
                    "elapsed_ms": 2,
                    "model": "m",
                }
            )
            return PipelineResponse(
                "answer",
                {
                    "usage_source": "unavailable",
                    "stages": [],
                    "pipeline_status": "CONNECTED_TO_REGISTER",
                },
            )

    events = [
        json.loads(item.removeprefix("data: ").strip())
        async for item in _stream_pipeline_completion(
            pipeline=Pipeline(),
            messages=[],
            identity=SimpleNamespace(display_scope="admin"),
            request_id="r",
            completion_id="c",
            created=0,
            model="m",
            timeout_seconds=1,
        )
        if item != "data: [DONE]\n\n"
    ]
    assert events[1]["event"] == {
        "type": "status",
        "data": {
            "description": "Resolved 2 business meanings and matched CM → Chetan Mehta.",
            "done": True,
            "action": "grounding",
            "stage": "grounding",
            "status": "completed",
            "elapsed_ms": 2,
            "model": "m",
        },
    }
    assert events[-2]["choices"][0]["delta"]["content"] == "answer"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["chitti"]["pipeline_status"] == "CONNECTED_TO_REGISTER"


@pytest.mark.asyncio
async def test_streamed_usage_matches_the_stage_usage_sum():
    class Pipeline:
        async def run(self, messages, *, identity, request_id, on_stage):
            return PipelineResponse(
                "answer",
                {
                    "usage_source": "measured",
                    "stages": [
                        {"usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}},
                        {"usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}},
                    ],
                    "pipeline_status": "CONNECTED_TO_REGISTER",
                },
            )

    events = [
        json.loads(item.removeprefix("data: ").strip())
        async for item in _stream_pipeline_completion(
            pipeline=Pipeline(),
            messages=[],
            identity=SimpleNamespace(display_scope="admin"),
            request_id="r",
            completion_id="c",
            created=0,
            model="m",
            timeout_seconds=1,
        )
        if item != "data: [DONE]\n\n"
    ]
    assert events[-1]["usage"] == {
        "prompt_tokens": 18,
        "completion_tokens": 8,
        "total_tokens": 26,
    }


@pytest.mark.asyncio
async def test_streamed_pipeline_emits_stage_progress_in_pipeline_order():
    class Pipeline:
        async def run(self, messages, *, identity, request_id, on_stage):
            for stage in ("conversation_resolution", "planning", "answer_generation"):
                on_stage({"stage": stage, "status": "completed", "elapsed_ms": 1, "model": "m"})
            return PipelineResponse(
                "answer",
                {
                    "usage_source": "unavailable",
                    "stages": [],
                    "pipeline_status": "CONNECTED_TO_REGISTER",
                },
            )

    events = [
        json.loads(item.removeprefix("data: ").strip())
        async for item in _stream_pipeline_completion(
            pipeline=Pipeline(),
            messages=[],
            identity=SimpleNamespace(display_scope="admin"),
            request_id="r",
            completion_id="c",
            created=0,
            model="m",
            timeout_seconds=1,
        )
        if item != "data: [DONE]\n\n"
    ]
    progress = [event["event"]["data"]["stage"] for event in events if "event" in event]
    assert progress == ["conversation_resolution", "planning", "answer_generation"]


@pytest.mark.asyncio
async def test_streamed_pipeline_timeout_has_terminal_error_and_done():
    class Pipeline:
        async def run(self, messages, *, identity, request_id, on_stage):
            await asyncio.sleep(0.05)
            return PipelineResponse("late", {})

    events = [
        json.loads(item.removeprefix("data: ").strip())
        async for item in _stream_pipeline_completion(
            pipeline=Pipeline(),
            messages=[],
            identity=SimpleNamespace(display_scope="admin"),
            request_id="r",
            completion_id="c",
            created=0,
            model="m",
            timeout_seconds=0.01,
        )
        if item != "data: [DONE]\n\n"
    ]
    assert events[-3]["event"]["type"] == "status"
    assert events[-3]["event"]["data"]["done"] is True
    assert events[-3]["event"]["data"]["status"] == "failed"
    assert "time limit" in events[-2]["choices"][0]["delta"]["content"]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_streamed_pipeline_converts_unexpected_failure_to_terminal_response():
    class Pipeline:
        async def run(self, messages, *, identity, request_id, on_stage):
            raise RuntimeError("unexpected")

    events = [
        json.loads(item.removeprefix("data: ").strip())
        async for item in _stream_pipeline_completion(
            pipeline=Pipeline(),
            messages=[],
            identity=SimpleNamespace(display_scope="admin"),
            request_id="r",
            completion_id="c",
            created=0,
            model="m",
            timeout_seconds=1,
        )
        if item != "data: [DONE]\n\n"
    ]
    assert "could not complete" in events[-2]["choices"][0]["delta"]["content"]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("start_work", [False, True])
async def test_closing_stream_after_initial_event_stops_pipeline(start_work):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class BlockingPipeline:
        async def run(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    stream = _stream_pipeline_completion(
        pipeline=BlockingPipeline(), messages=[], identity=SimpleNamespace(display_scope="DELEGATED"),
        request_id="cancel-test", completion_id="chatcmpl-test", created=0,
        model="prism-chitti", timeout_seconds=10,
    )
    initial = await anext(stream)
    assert '"role":"assistant"' in initial
    if start_work:
        await asyncio.wait_for(started.wait(), 1)
    await stream.aclose()
    assert stopped.is_set() if start_work else not started.is_set()
