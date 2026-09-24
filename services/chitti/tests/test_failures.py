from __future__ import annotations

from evam_register_client.errors import ForbiddenError, ServerError

from app.api import _pipeline_failure_response
from app.pipeline import PipelineStageError, _failure_name
from app.stage_models import StageName


def _error(failure_name: str) -> PipelineStageError:
    return PipelineStageError(
        StageName.EXECUTION,
        "private cause",
        failure_name=failure_name,
        last_completed_stage=StageName.PLANNING,
        stages=[],
    )


def test_register_access_denial_and_resource_failure_have_stable_names():
    assert _failure_name(StageName.EXECUTION, ForbiddenError("denied")) == (
        "REGISTER_ACCESS_DENIED"
    )
    assert _failure_name(StageName.EXECUTION, ServerError("down")) == (
        "REGISTER_RESOURCE_FAILED"
    )


def test_access_denial_and_register_failure_render_differently_from_zero():
    denied = _pipeline_failure_response(
        _error("REGISTER_ACCESS_DENIED"), request_id="denied", scope="TEAM"
    )
    failed = _pipeline_failure_response(
        _error("REGISTER_RESOURCE_FAILED"), request_id="failed", scope="FULL"
    )

    assert denied.metadata["outcome"] == "ACCESS_DENIED"
    assert "cannot access" in denied.content
    assert failed.metadata["outcome"] == "DEPENDENCY_FAILED"
    assert "not a zero-result answer" in failed.content
    assert denied.metadata["last_completed_stage"] == StageName.PLANNING


def test_failure_response_preserves_retrieval_trace_when_captured():
    error = PipelineStageError(
        StageName.ANSWERABILITY,
        "private cause",
        failure_name="ANSWERABILITY_FAILED",
        last_completed_stage=StageName.GROUNDING,
        stages=[],
        retrieval_trace={
            "question_interpretation": {"input": {"question": "q"}, "output": {"intent": "i"}},
            "semantic_retrieval": {"input": {"query": "q"}, "output": {"matches": []}},
        },
    )

    response = _pipeline_failure_response(error, request_id="failed", scope="FULL")

    assert response.metadata["retrieval_trace"] == error.retrieval_trace
