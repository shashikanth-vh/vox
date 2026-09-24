"""OpenAI-compatible model discovery and chat-completion routes."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio
from evam_backend_core.errors import NotFoundError
from evam_backend_core.logging import get_logger, request_id_ctx
from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse, StreamingResponse
from starlette.responses import Response

from app import __version__
from app.auth import require_client_key
from app.config import Settings
from app.identity import resolve_caller
from app.models import ChatCompletionRequest, ChittiMetadata
from app.pipeline import PipelineResponse, PipelineStageError
from app.presentation import public_result, public_status

log = get_logger("chitti.api")


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/models", tags=["OpenAI compatibility"])
    async def list_models(request: Request) -> dict:
        require_client_key(request, settings)
        return {
            "object": "list",
            "data": [
                {
                    "id": settings.public_model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "prism",
                }
            ],
        }

    @router.post("/chat/completions", tags=["OpenAI compatibility"], response_model=None)
    async def chat_completions(
        payload: ChatCompletionRequest,
        request: Request,
    ) -> Response:
        require_client_key(request, settings)
        business = request.headers.get("X-Chitti-Presentation") == "business"
        if payload.model != settings.public_model_id:
            raise NotFoundError(f"Model '{payload.model}' is not available.")

        request_id = request_id_ctx.get() or uuid.uuid4().hex
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        if settings.pipeline_enabled:
            identity = resolve_caller(request, settings)
            pipeline = request.app.state.pipeline
            if pipeline is None:
                dependency = request.app.state.readiness_error or "pipeline dependencies unavailable"
                result = PipelineResponse(
                    content=f"Chitti is not ready: {dependency}",
                    metadata={
                        "request_id": request_id,
                        "outcome": "FAILED",
                        "last_completed_stage": None,
                        "failed_stage": "dependency_readiness",
                        "completeness": "FAILED",
                        "scope": identity.display_scope,
                        "pipeline_status": "DEPENDENCY_NOT_READY",
                        "usage_source": "estimate",
                    },
                )
            elif payload.stream:
                return StreamingResponse(
                    _stream_pipeline_completion(
                        pipeline=pipeline,
                        messages=[message.model_dump(mode="json") for message in payload.messages],
                        identity=identity,
                        request_id=request_id,
                        completion_id=completion_id,
                        created=created,
                        model=payload.model,
                        timeout_seconds=settings.request_timeout_seconds,
                        business=business,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                try:
                    async with asyncio.timeout(settings.request_timeout_seconds):
                        result = await pipeline.run(
                            [message.model_dump(mode="json") for message in payload.messages],
                            identity=identity,
                            request_id=request_id,
                        )
                except PipelineStageError as exc:
                    result = _pipeline_failure_response(
                        exc, request_id=request_id, scope=identity.display_scope
                    )
                except TimeoutError:
                    result = _pipeline_timeout_response(request_id=request_id, scope=identity.display_scope)
                except Exception:
                    log.exception("pipeline_request_failed", extra={"request_id": request_id})
                    result = _pipeline_internal_error_response(
                        request_id=request_id, scope=identity.display_scope
                    )
            if business:
                result = public_result(result)
            content = result.content
            metadata = result.metadata
        else:
            content = _diagnostic(settings, request_id)
            metadata = ChittiMetadata(request_id=request_id).model_dump()
            metadata["usage_source"] = "estimate"
            if business:
                public = public_result(PipelineResponse(content=content, metadata=metadata))
                content, metadata = public.content, public.metadata

        log.info(
            "chitti_response",
            extra={
                "model": payload.model,
                "stream": payload.stream,
                "message_count": len(payload.messages),
                "pipeline_status": metadata.get("pipeline_status", "PUBLIC_RESPONSE"),
            },
        )

        if payload.stream:
            stream_usage = _reported_usage(metadata)
            if metadata.get("usage_source", "estimate") == "estimate":
                prompt_estimate = _message_token_estimate(payload)
                completion_estimate = _token_estimate(content)
                stream_usage = {
                    "prompt_tokens": prompt_estimate,
                    "completion_tokens": completion_estimate,
                    "total_tokens": prompt_estimate + completion_estimate,
                }
            return StreamingResponse(
                _stream_completion(
                    completion_id=completion_id,
                    created=created,
                    model=payload.model,
                    content=content,
                    metadata=metadata,
                    usage=stream_usage,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        prompt_tokens = _message_token_estimate(payload)
        completion_tokens = _token_estimate(content)
        usage = (
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            if metadata.get("usage_source", "estimate") == "estimate"
            else _reported_usage(metadata)
        )
        return ORJSONResponse(
            content=_completion_body(
                completion_id=completion_id,
                created=created,
                model=payload.model,
                content=content,
                usage=usage,
                metadata=metadata,
            )
        )

    return router


def _pipeline_timeout_response(*, request_id: str, scope: str) -> PipelineResponse:
    return PipelineResponse(
        content=f"Chitti reached its request time limit. Request id: {request_id}.",
        metadata={
            "request_id": request_id,
            "outcome": "FAILED",
            "last_completed_stage": None,
            "failed_stage": "request_timeout",
            "completeness": "PARTIAL_TIMEOUT",
            "scope": scope,
            "pipeline_status": "CONNECTED_TO_REGISTER",
            "usage_source": "unavailable",
        },
    )


def _pipeline_internal_error_response(*, request_id: str, scope: str) -> PipelineResponse:
    return PipelineResponse(
        content=f"Chitti could not complete the request. Request id: {request_id}.",
        metadata={
            "request_id": request_id,
            "outcome": "FAILED",
            "last_completed_stage": None,
            "failed_stage": "internal_error",
            "failure_name": "INTERNAL_ERROR",
            "completeness": "FAILED",
            "scope": scope,
            "pipeline_status": "CONNECTED_TO_REGISTER",
            "usage_source": "unavailable",
            "stages": [],
        },
    )


def _completion_body(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str,
    usage: dict | None,
    metadata: dict,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "chitti": metadata,
    }


def _pipeline_failure_response(exc: PipelineStageError, *, request_id: str, scope: str) -> PipelineResponse:
    if exc.failure_name == "REGISTER_ACCESS_DENIED":
        content = (
            "Your verified identity cannot access the Register data required for this question. "
            f"Request id: {request_id}."
        )
        outcome = "ACCESS_DENIED"
    elif exc.failure_name == "REGISTER_RESOURCE_FAILED":
        content = (
            "Register could not supply the data required for this question; this is not a "
            f"zero-result answer. Request id: {request_id}."
        )
        outcome = "DEPENDENCY_FAILED"
    else:
        content = (
            f"Chitti could not complete {exc.stage}. No downstream stage ran. " f"Request id: {request_id}."
        )
        outcome = "FAILED"
    usage = [item["usage"] for item in exc.stages if item.get("usage")]
    attempted_calls = sum(item.get("attempted_calls", 0) for item in usage)
    source = (
        "estimate"
        if attempted_calls == 0
        else (
            "unavailable"
            if any(item.get("measurement") == "unavailable" for item in usage)
            else (
                "partial"
                if any(item.get("measurement") == "partial" for item in usage)
                else "measured"
                if usage and all(item.get("measurement") == "measured" for item in usage)
                else "unavailable"
            )
        )
    )
    metadata: dict[str, Any] = {
        "request_id": request_id,
        "outcome": outcome,
        "last_completed_stage": exc.last_completed_stage,
        "failed_stage": exc.stage,
        "failure_name": exc.failure_name,
        "completeness": "FAILED",
        "scope": scope,
        "pipeline_status": "CONNECTED_TO_REGISTER",
        "usage_source": source,
        "stages": exc.stages,
    }
    if exc.retrieval_trace is not None:
        metadata["retrieval_trace"] = exc.retrieval_trace
    return PipelineResponse(
        content=content,
        metadata=metadata,
    )


def _diagnostic(settings: Settings, request_id: str) -> str:
    return "\n".join(
        (
            f"PRISM Chitti {__version__} diagnostic",
            f"request_id: {request_id}",
            f"selected_model: {settings.public_model_id}",
            "pipeline_status: NOT_CONNECTED_TO_REGISTER",
            "This is a service diagnostic, not a Ledger answer.",
        )
    )


async def _stream_completion(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str,
    metadata: dict,
    usage: dict | None,
) -> AsyncIterator[str]:
    yield _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            "chitti": metadata,
        }
    )
    yield _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
    )
    yield _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage,
        }
    )
    yield "data: [DONE]\n\n"


async def _stream_pipeline_completion(*, pipeline, messages, identity, request_id, completion_id,
                                      created, model, timeout_seconds, business=False) -> AsyncIterator[str]:
    events: asyncio.Queue[dict] = asyncio.Queue()

    def on_stage(event: dict) -> None:
        events.put_nowait(event)

    async def work():
        try:
            return await pipeline.run(messages, identity=identity, request_id=request_id, on_stage=on_stage)
        except PipelineStageError as exc:
            return _pipeline_failure_response(exc, request_id=request_id, scope=identity.display_scope)
        except Exception:
            log.exception("pipeline_stream_failed", extra={"request_id": request_id})
            return _pipeline_internal_error_response(
                request_id=request_id,
                scope=identity.display_scope,
            )

    async def bounded_work():
        async with asyncio.timeout(timeout_seconds):
            return await work()

    task = asyncio.create_task(bounded_work())
    try:
        yield _sse({
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        })
        while not task.done():
            try:
                event = await asyncio.wait_for(events.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "event": (public_status(event) if business else _status_event(event))})
        result = await asyncio.wait_for(task, timeout=timeout_seconds)
        while not events.empty():
            event = events.get_nowait()
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "event": (public_status(event) if business else _status_event(event))})
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        result = _pipeline_timeout_response(request_id=request_id, scope=identity.display_scope)
        yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                    "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                    "event": (public_status if business else _status_event)({
                        "stage": "request_timeout", "status": "failed",
                        "description": "I couldn't finish the request within the available time.",
                        "elapsed_ms": timeout_seconds * 1000, "model": None,
                    })})
    finally:
        # Closing the generator after any yield (including the initial role event)
        # must stop provider/Register work. Shield cleanup from disconnect cancellation.
        task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(task, return_exceptions=True)
    if business:
        result = public_result(result)
    yield _sse({
        "id": completion_id, "object": "chat.completion.chunk", "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": result.content}, "finish_reason": None}],
    })
    yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": _reported_usage(result.metadata), "chitti": result.metadata})
    yield "data: [DONE]\n\n"


def _status_event(event: dict) -> dict:
    status = str(event["status"])
    stage = str(event["stage"])
    description = event.get("description") or f"Chitti: {stage.replace('_', ' ')} {status}"
    return {
        "type": "status",
        "data": {
            **event,
            "description": description,
            "done": status in {"completed", "failed"},
            "action": stage,
        },
    }


def _sse(value: dict) -> str:
    return f"data: {json.dumps(value, separators=(',', ':'))}\n\n"


def _token_estimate(text: str) -> int:
    # The compatibility response reports a deterministic estimate rather than adding a
    # tokenizer dependency for provider-specific token accounting.
    return max(1, (len(text) + 3) // 4)


def _message_token_estimate(payload: ChatCompletionRequest) -> int:
    serialized = " ".join(
        message.content if isinstance(message.content, str) else json.dumps(message.content)
        for message in payload.messages
        if message.content is not None
    )
    return _token_estimate(serialized)


def _reported_usage(metadata: dict) -> dict | None:
    # A connected pipeline with incomplete provider accounting must not expose a
    # character estimate as if it were measured usage. Estimates are reserved for
    # diagnostic/dependency-not-ready compatibility responses.
    stages = metadata.get("stages") or []
    usage = [item["usage"] for item in stages if isinstance(item, dict) and item.get("usage")]
    if metadata.get("usage_source") != "measured":
        return None
    reported = {
        "prompt_tokens": sum(item["prompt_tokens"] for item in usage),
        "completion_tokens": sum(item["completion_tokens"] for item in usage),
        "total_tokens": sum(item["total_tokens"] for item in usage),
    }
    cached = [item.get("cached_prompt_tokens") for item in usage]
    if cached and all(value is not None for value in cached):
        reported["prompt_tokens_details"] = {"cached_tokens": sum(cached)}
    return reported
