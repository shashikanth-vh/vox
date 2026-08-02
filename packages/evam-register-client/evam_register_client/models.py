"""Lightweight response containers.

Resource bodies are returned as plain ``dict`` (the Register's JSON), so the client stays
forward-compatible as the Register's schemas evolve — no per-field coupling. Only the
pagination envelope gets a typed wrapper for ergonomic iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Page:
    """One page of a keyset-paginated list response."""

    items: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    next_cursor: str | None = None
    total: int | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @classmethod
    def from_body(cls, body: dict[str, Any] | list[Any]) -> "Page":
        # A few Register endpoints (e.g. GET /v1/assignments) answer a BARE JSON array
        # rather than the {"items": [...]} envelope — normalise instead of crashing
        # with "'list' object has no attribute 'get'" inside a workflow activity.
        if isinstance(body, list):
            return cls(items=body, count=len(body), next_cursor=None, total=None)
        return cls(
            items=body.get("items", []),
            count=body.get("count", len(body.get("items", []))),
            next_cursor=body.get("next_cursor"),
            total=body.get("total"),
        )
