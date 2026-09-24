from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import (
    _completion_body,
    _pipeline_internal_error_response,
    _pipeline_timeout_response,
    _reported_usage,
    _stream_completion,
)
from app.main import create_app

pytestmark = pytest.mark.asyncio
AUTH = {"Authorization": "Bearer test-client-key"}


async def test_reported_usage_omits_missing_cached_token_detail():
    metadata = {
        "usage_source": "measured",
        "stages": [
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "cached_prompt_tokens": None,
                }
            }
        ],
    }
    assert _reported_usage(metadata) == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }


async def test_unmeasured_usage_is_explicitly_null():
    metadata = {"usage_source": "unavailable", "stages": []}

    assert _reported_usage(metadata) is None
    chunks = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in [
            part
            async for part in _stream_completion(
                completion_id="c",
                created=0,
                model="prism-chitti",
                content="unavailable",
                metadata=metadata,
                usage=None,
            )
            if part != "data: [DONE]\n\n"
        ]
    ]
    assert "usage" in chunks[-1]
    assert chunks[-1]["usage"] is None


async def test_request_timeout_is_unavailable_without_inbound_estimate():
    result = _pipeline_timeout_response(request_id="timeout-1", scope="admin")

    assert result.metadata["failed_stage"] == "request_timeout"
    assert result.metadata["usage_source"] == "unavailable"
    assert "timeout-1" in result.content
    assert _reported_usage(result.metadata) is None


async def test_internal_error_response_contract_is_shared_by_streaming_and_non_streaming():
    result = _pipeline_internal_error_response(request_id="internal-1", scope="admin")

    assert result.metadata == {
        "request_id": "internal-1",
        "outcome": "FAILED",
        "last_completed_stage": None,
        "failed_stage": "internal_error",
        "failure_name": "INTERNAL_ERROR",
        "completeness": "FAILED",
        "scope": "admin",
        "pipeline_status": "CONNECTED_TO_REGISTER",
        "usage_source": "unavailable",
        "stages": [],
    }


async def test_non_streaming_completion_keeps_present_null_usage_key():
    body = _completion_body(
        completion_id="c",
        created=0,
        model="prism-chitti",
        content="unavailable",
        usage=None,
        metadata={"usage_source": "unavailable"},
    )

    assert "usage" in body
    assert body["usage"] is None


def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_health_and_readiness_have_no_external_dependency():
    async with _client() as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.json() == {"status": "ok", "service": "prism-chitti", "version": "0.3.0"}
    assert ready.json()["pipeline_status"] == "NOT_CONNECTED_TO_REGISTER"
    assert len(ready.json()["runtime"]["service_app_sha256"]) == 64
    assert ready.json()["runtime"]["dense_candidates"] == 40
    assert ready.json()["runtime"]["sparse_candidates"] == 40
    assert ready.json()["runtime"]["fused_candidates"] == 40
    assert ready.json()["runtime"]["rerank_limit"] == 10
    assert ready.json()["runtime"]["capture_retrieval_trace"] is False
    assert ready.json()["runtime"]["ontology_collection"].startswith("chitti_ontology_")


async def test_model_discovery_is_openai_compatible():
    async with _client() as client:
        response = await client.get("/v1/models", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": "prism-chitti",
                "object": "model",
                "created": 0,
                "owned_by": "prism",
            }
        ],
    }


async def test_non_streaming_diagnostic_contract():
    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-Request-ID": "diagnostic-request"},
            json={
                "model": "prism-chitti",
                "messages": [{"role": "user", "content": "status"}],
                "stream": False,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"id", "object", "created", "model", "choices", "usage", "chitti"}
    assert body["object"] == "chat.completion"
    assert body["model"] == "prism-chitti"
    assert body["choices"][0]["message"]["role"] == "assistant"
    content = body["choices"][0]["message"]["content"]
    assert "PRISM Chitti 0.3.0 diagnostic" in content
    assert "request_id: diagnostic-request" in content
    assert "pipeline_status: NOT_CONNECTED_TO_REGISTER" in content
    assert "not a Ledger answer" in content
    assert body["chitti"] == {
        "request_id": "diagnostic-request",
        "outcome": "DIAGNOSTIC",
        "last_completed_stage": "api_contract",
        "failed_stage": None,
        "completeness": "NOT_APPLICABLE",
        "scope": "NO_LEDGER_ACCESS",
        "pipeline_status": "NOT_CONNECTED_TO_REGISTER",
        "usage_source": "estimate",
    }
    assert response.headers["X-Request-ID"] == "diagnostic-request"


async def test_native_client_captured_request_and_sse_termination():
    fixture = Path(__file__).parent / "fixtures" / "native-chat-request.json"
    payload = json.loads(fixture.read_text())

    async with _client() as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-Request-ID": "native-chat-contract"},
            json=payload,
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line.removeprefix("data: ") for line in response.text.splitlines() if line]
    assert events[-1] == "[DONE]"

    chunks = [json.loads(event) for event in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert "NOT_CONNECTED_TO_REGISTER" in chunks[1]["choices"][0]["delta"]["content"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] > 0


async def test_malformed_request_and_unknown_model_are_clear():
    async with _client() as client:
        malformed = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "prism-chitti", "messages": []},
        )
        unknown = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "not-chitti", "messages": [{"role": "user", "content": "status"}]},
        )

    assert malformed.status_code == 422
    assert malformed.json()["error"]["type"] == "validation_error"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["type"] == "not_found"
