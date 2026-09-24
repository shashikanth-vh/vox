"""OpenAI-compatible request and response models used by Chitti's public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatMessage(BaseModel):
    """A message shape broad enough for the metadata the native chat client sends."""

    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """The OpenAI subset exercised by the native chat client.

    Unknown compatibility metadata is retained by Pydantic. Request-level generation
    controls are accepted for client compatibility but are not forwarded; Chitti builds
    its model-stage requests internally.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    n: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    user: str | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    seed: int | None = None
    stream_options: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_user_message(self) -> ChatCompletionRequest:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("messages must contain at least one user message")
        return self


class ChittiMetadata(BaseModel):
    request_id: str
    outcome: Literal["DIAGNOSTIC"] = "DIAGNOSTIC"
    last_completed_stage: Literal["api_contract"] = "api_contract"
    failed_stage: None = None
    completeness: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    scope: Literal["NO_LEDGER_ACCESS"] = "NO_LEDGER_ACCESS"
    pipeline_status: Literal["NOT_CONNECTED_TO_REGISTER"] = "NOT_CONNECTED_TO_REGISTER"
