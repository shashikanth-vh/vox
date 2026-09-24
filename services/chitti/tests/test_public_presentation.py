"""The native client's response contains business prose and evidence, never diagnostics."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import _stream_pipeline_completion
from app.pipeline import PipelineResponse
from app.presentation import business_text, public_result, public_status


def result():
    return PipelineResponse(
        content="There are 12 active leads.\n\n| entity_id | rm_id |\n| private-db-id | user-1 |",
        public_content="There are 12 active leads.",
        metadata={"request_id": "req-1", "outcome": "ANSWERED", "completeness": "COMPLETE",
                  "result_table": [{"entity_id": "private-db-id"}],
                  "retrieval_trace": {"sql": "SELECT * FROM leads"},
                  "stages": [{"model": "internal-model", "input": "private"}],
                  "public_evidence": [{"reference": "E1", "label": "Based on 12 accessible records"}]},
    )


def test_business_projection_omits_internal_tables_and_metadata():
    public = public_result(result())
    assert public.content == "There are 12 active leads."
    assert set(public.metadata) == {"request_id", "outcome", "completeness", "evidence"}
    assert public.metadata["evidence"][0]["reference"] == "E1"
    assert "entity_id" not in json.dumps(public.metadata)


@pytest.mark.parametrize("text", ["entity_id is private", "| Name | id |", "```sql\nSELECT * FROM leads",
                                  "Record 12345678-1234-1234-1234-123456789abc",
                                  "The result is complete based on caller-visible Register data.",
                                  "The answer is an aggregate scalar.",
                                  "One obligation to confirm the total is unconfirmable."])
def test_schema_tables_code_and_opaque_ids_are_withheld(text):
    assert business_text(text) != text
    assert "try rephrasing" in business_text(text)


@pytest.mark.asyncio
async def test_streamed_public_contract_preserves_progress_and_projects_final_answer():
    class Pipeline:
        async def run(self, *args, on_stage, **kwargs):
            on_stage({"stage": "register_read", "status": "completed", "model": "internal-model",
                      "description": "SELECT * FROM leads"})
            return result()

    stream = _stream_pipeline_completion(
        pipeline=Pipeline(), messages=[], identity=SimpleNamespace(display_scope="DELEGATED"),
        request_id="req-1", completion_id="chatcmpl-test", created=0, model="prism-chitti",
        timeout_seconds=1, business=True,
    )
    output = "".join([chunk async for chunk in stream])
    assert "12 active leads" in output
    assert output.endswith("data: [DONE]\n\n")
    assert "SELECT * FROM leads" in output
    for internal in ("entity_id", "rm_id", "private-db-id", "internal-model", "retrieval_trace"):
        assert internal not in output


@pytest.mark.parametrize("status", ["started", "completed", "failed"])
def test_progress_description_passes_through_unchanged(status):
    description = "Matched rm_id → Shubh Dave. SELECT * FROM leads\n" + "Details. " * 40
    assert public_status({"stage": "value_grounding", "status": status,
                          "description": description, "model": "private-model"}) == {
        "type": "status", "data": {"description": description}}
