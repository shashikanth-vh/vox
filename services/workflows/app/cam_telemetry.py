"""What a CAM draft cost: every model call and every document read, counted.

Engines report each call (tokens, time, outcome) to ``record_llm_call``; document reads
report theirs to ``record_document``. Both log one line per event — the numbers are in
the message for a terminal and in structured fields for a log pipeline — and, when a
``UsageMeter`` is active for the current task, add to it. A staged draft activates one
meter for the whole job; the asyncio tasks it fans out to inherit it, so parallel
sections all count against the same job.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from evam_backend_core.logging import get_logger

log = get_logger("cam.telemetry")

_meter: ContextVar[UsageMeter | None] = ContextVar("cam_usage_meter", default=None)
_stage: ContextVar[str] = ContextVar("cam_usage_stage", default="")


@dataclass
class _Tally:
    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def add(self, *, ok: bool, input_tokens: int, output_tokens: int, seconds: float) -> None:
        self.calls += 1
        self.failed += 0 if ok else 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.seconds += seconds

    def as_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "failed": self.failed,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "seconds": round(self.seconds, 1)}


@dataclass
class UsageMeter:
    """Everything one draft (or one request) spent."""

    llm: _Tally = field(default_factory=_Tally)
    by_stage: dict[str, _Tally] = field(default_factory=dict)
    engines: set[str] = field(default_factory=set)
    unreported_calls: int = 0
    documents: dict[str, int] = field(default_factory=lambda: {
        "read": 0, "skipped": 0, "via_docrag": 0, "via_basic": 0, "docrag_cache_hits": 0,
        "pages": 0, "ocr_pages": 0, "sarvam_jobs": 0, "sarvam_failed_jobs": 0,
        "sarvam_pages_submitted": 0, "sarvam_pages_succeeded": 0,
        "sarvam_pages_failed": 0})
    doc_formats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"llm": {**self.llm.as_dict(), "engines": sorted(self.engines),
                        "calls_without_usage": self.unreported_calls,
                        "by_stage": {k: v.as_dict() for k, v in self.by_stage.items()}},
                "documents": {**self.documents, "formats": dict(self.doc_formats)}}


def current_meter() -> UsageMeter | None:
    return _meter.get()


@contextmanager
def metered(meter: UsageMeter | None = None) -> Iterator[UsageMeter]:
    """Count everything spent inside the block (and the tasks it starts)."""
    meter = meter or UsageMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Label the model calls made inside the block (digest, dataset, section, ...)."""
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def record_llm_call(*, engine: str, ok: bool, started: float,
                    usage: dict[str, Any] | None, finish: str = "",
                    error: str = "") -> None:
    """One request to a model. ``usage`` is the provider's token report, when it sent
    one; a call without it still counts (and is flagged) rather than reading as free."""
    seconds = max(time.monotonic() - started, 0.0)
    usage = usage or {}
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    name = _stage.get() or "request"
    log.info(
        "cam_llm_call engine=%s stage=%s ok=%s input_tokens=%d output_tokens=%d "
        "seconds=%.1f finish=%s%s", engine, name, ok, inp, out, seconds, finish or "-",
        f" error={error[:200]}" if error else "",
        extra={"event": "cam_llm_call", "engine": engine, "stage": name, "ok": ok,
               "input_tokens": inp, "output_tokens": out, "seconds": round(seconds, 2),
               "finish": finish, "usage_reported": bool(usage), "error": error[:500]})
    meter = _meter.get()
    if meter is None:
        return
    meter.engines.add(engine)
    meter.llm.add(ok=ok, input_tokens=inp, output_tokens=out, seconds=seconds)
    meter.by_stage.setdefault(name, _Tally()).add(
        ok=ok, input_tokens=inp, output_tokens=out, seconds=seconds)
    if ok and not usage:
        meter.unreported_calls += 1


def record_document(*, doc_id: str, fmt: str, via: str, ok: bool,
                    telemetry: dict[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    """One document read for a draft. ``telemetry`` is DocRAG's report for the read
    (cache hit, pages, OCR pages, Sarvam jobs). Returns the per-document figures."""
    t = telemetry or {}
    sarvam = t.get("sarvam") or {}
    row: dict[str, Any] = {"format": fmt, "via": via, "cached": bool(t.get("cached")),
           "pages": int(t.get("pages") or 0), "ocr_pages": int(t.get("ocr_pages") or 0),
           "sarvam_jobs": int(sarvam.get("jobs") or 0),
           "sarvam_pages": int(sarvam.get("pages_submitted") or 0)}
    log.info(
        "cam_document doc=%s format=%s via=%s ok=%s cached=%s pages=%d ocr_pages=%d "
        "sarvam_jobs=%d sarvam_pages=%d%s", doc_id, fmt, via, ok, row["cached"],
        row["pages"], row["ocr_pages"], row["sarvam_jobs"], row["sarvam_pages"],
        f" reason={reason[:200]}" if reason else "",
        extra={"event": "cam_document", "doc_id": doc_id, "ok": ok, "reason": reason[:500],
               **row, "sarvam": sarvam})
    meter = _meter.get()
    if meter is not None:
        d = meter.documents
        d["read" if ok else "skipped"] += 1
        d["via_docrag" if via == "docrag" else "via_basic"] += 1
        d["docrag_cache_hits"] += 1 if row["cached"] else 0
        d["pages"] += row["pages"]
        d["ocr_pages"] += row["ocr_pages"]
        d["sarvam_jobs"] += row["sarvam_jobs"]
        d["sarvam_failed_jobs"] += int(sarvam.get("failed_jobs") or 0)
        d["sarvam_pages_submitted"] += row["sarvam_pages"]
        d["sarvam_pages_succeeded"] += int(sarvam.get("pages_succeeded") or 0)
        d["sarvam_pages_failed"] += int(sarvam.get("pages_failed") or 0)
        meter.doc_formats[fmt] = meter.doc_formats.get(fmt, 0) + 1
    return row
