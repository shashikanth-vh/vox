"""Pagination.

Keyset (cursor) pagination is the default because it stays O(1) at any depth — the
right choice when a table holds several lakh rows and ``OFFSET 200000`` would force
PostgreSQL to walk every skipped row. The cursor encodes the ordering key of the last
row seen ``(created_at, id)``. An optional exact ``total`` is opt-in, since ``COUNT(*)``
over a huge table is itself expensive.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from evam_backend_core.errors import ValidationAppError

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    count: int = Field(description="Number of items in this page")
    next_cursor: str | None = Field(default=None, description="Opaque cursor for the next page")
    total: int | None = Field(default=None, description="Exact total (only when requested)")


def encode_cursor(created_at: datetime, row_id: Any) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "id": str(row_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        data = json.loads(raw)
        return datetime.fromisoformat(data["c"]), data["id"]
    except (binascii.Error, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValidationAppError("Malformed pagination cursor.") from exc
