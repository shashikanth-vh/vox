"""Stable records at the Register retrieval and evidence boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_LIMIT = "PARTIAL_LIMIT"
    PARTIAL_TIMEOUT = "PARTIAL_TIMEOUT"
    FAILED = "FAILED"


class RegisterRead(BaseModel):
    resource: str
    q: str | None = Field(default=None, min_length=1, max_length=300)
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class CanonicalRecord(BaseModel):
    resource: str
    record_id: str
    fields: dict[str, Any]


class RetrievalWindow(BaseModel):
    resource: str
    started_at: datetime
    completed_at: datetime
    pages_retrieved: int
    records_retrieved: int
    completeness: Completeness
    next_cursor_present: bool = False
    controlled_value_issues: dict[str, int] = Field(default_factory=dict)


class RegisterEvidence(BaseModel):
    read: RegisterRead
    records: list[CanonicalRecord]
    window: RetrievalWindow


def utc_now() -> datetime:
    return datetime.now(UTC)
