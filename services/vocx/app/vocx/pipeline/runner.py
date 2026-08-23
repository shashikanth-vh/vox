"""The pipeline runner — one conversation, driven from wherever it stands to
``ready``, with failure as a state and every step resumable.

The runner holds NO state of its own: the register row is the truth. Each run
reads the row, decides which stages still need doing (a stored transcript is
never re-transcribed), executes them under per-stage timeouts, and writes
progress back through the register's guarded pipeline endpoint. A crash between
stages costs nothing but the crashed stage.

Failure policy (Build Specification 8.2): any stage failure sets
``processing_failed`` with the stage named in the error; ``MAX_RETRIES``
attempts are allowed before ``failed_permanently`` plus an admin alert. The
retry reuses the uploaded audio and whatever stages already succeeded.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .structure import StructuringError, structure_transcript
from .suspect import mark_suspect_segments, transcript_for_structuring

log = logging.getLogger("vox.pipeline")

MAX_RETRIES = 5

# Per-stage wall-clock budgets (seconds). Generous — a 90-minute meeting takes
# real time to transcribe — but finite: nothing in this pipeline may hang a row
# in `processing` forever.
STAGE_TIMEOUTS = {"transcribe": 1800, "structure": 300}


class StageTimeout(RuntimeError):
    def __init__(self, stage: str, seconds: float):
        self.stage = stage
        super().__init__(f"{stage} exceeded its {seconds:.0f}s budget")


class RegisterGone(RuntimeError):
    """The register refused or vanished mid-run. The row keeps its last honest
    state; the next run resumes."""


def _run_with_timeout(fn: Callable[[], Any], stage: str, seconds: float) -> Any:
    """Run ``fn`` under a wall-clock budget. Thread-based so it works for the
    CPU/IO-mixed stages (ASR, HTTP) without cooperation from the callee; the
    stage result or its exception crosses back, a timeout raises StageTimeout.
    The abandoned worker cannot corrupt anything — every stage writes only
    through the guarded register endpoint, and only on success."""
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — crosses the thread boundary
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True, name=f"vox-{stage}")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise StageTimeout(stage, seconds)
    if "error" in box:
        raise box["error"]
    return box.get("result")


class PipelineRunner:
    """Drives one conversation through the pipeline against an injected register
    client and injected heavy dependencies — fully testable, fully swappable.

    register: an object with
        get(conversation_id) -> row dict
        patch(conversation_id, **fields) -> row dict     (the guarded endpoint)
    transcribe(audio_ref) -> {"text", "segments": [...], "language"}
    ask_model(model, system, user) -> str
    alert(message) -> None                                (admin alert sink)
    """

    def __init__(self, register, transcribe: Callable[[str], dict],
                 ask_model: Callable[[str, str, str], str],
                 alert: Callable[[str], None] | None = None,
                 timeouts: dict[str, float] | None = None):
        self.register = register
        self.transcribe = transcribe
        self.ask_model = ask_model
        self.alert = alert or (lambda msg: log.error("ADMIN ALERT: %s", msg))
        self.timeouts = {**STAGE_TIMEOUTS, **(timeouts or {})}

    # -------------------------------------------------------------- the one entry

    def process(self, conversation_id: str) -> dict:
        """Advance the conversation as far as it can go this run. Returns the
        final row. Safe to call repeatedly — a ready/submitted row is untouched,
        a failed row consumes one retry, a half-done row resumes."""
        row = self.register.get(conversation_id)
        status = row.get("status")

        if status in ("ready", "submitted"):
            return row                                   # nothing to do — idempotent
        if status == "failed_permanently":
            return row                                   # a human decision reopens it
        if status == "processing_failed":
            if (row.get("retry_count") or 0) >= MAX_RETRIES:
                row = self.register.patch(conversation_id, status="failed_permanently")
                self.alert(f"VOX conversation {conversation_id} failed permanently "
                           f"after {MAX_RETRIES} retries: {row.get('processing_error')}")
                return row
            row = self.register.patch(conversation_id, status="processing",
                                      retry_increment=True)
        elif status in ("queued", "uploading"):
            row = self.register.patch(conversation_id, status="processing")
        # status == "processing": a previous runner died mid-flight — resume.

        try:
            row = self._transcribe_if_needed(conversation_id, row)
            row = self._structure_if_needed(conversation_id, row)
            row = self.register.patch(conversation_id, status="ready",
                                      processing_stage="ready")
        except Exception as exc:  # noqa: BLE001 — failure is a state, not a crash
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("conversation %s failed: %s", conversation_id, detail)
            row = self.register.patch(conversation_id, status="processing_failed",
                                      processing_error=detail[:2000])
            if (row.get("retry_count") or 0) >= MAX_RETRIES:
                row = self.register.patch(conversation_id, status="failed_permanently")
                self.alert(f"VOX conversation {conversation_id} failed permanently "
                           f"after {MAX_RETRIES} retries: {detail}")
        return row

    # ------------------------------------------------------------------- stages

    def _transcribe_if_needed(self, cid: str, row: dict) -> dict:
        if row.get("raw_transcript"):
            return row                                   # resume: never re-transcribe
        audio_ref = row.get("audio_ref")
        if not audio_ref:
            raise RuntimeError("transcribe: no audio_ref on the conversation")
        t0 = time.monotonic()
        result = _run_with_timeout(lambda: self.transcribe(audio_ref),
                                   "transcribe", self.timeouts["transcribe"])
        segments = mark_suspect_segments(list(result.get("segments") or []))
        suspects = sum(1 for s in segments if s.get("suspect"))
        log.info("conversation %s transcribed in %.1fs (%d segments, %d suspect)",
                 cid, time.monotonic() - t0, len(segments), suspects)
        return self.register.patch(
            cid,
            raw_transcript=result.get("text") or transcript_for_structuring(segments),
            transcript_segments=segments,
            language_detected=result.get("language"),
            processing_stage="transcribed",
        )

    def _structure_if_needed(self, cid: str, row: dict) -> dict:
        if row.get("structured_report"):
            return row                                   # resume: keep the valid report
        segments = row.get("transcript_segments") or []
        transcript = (transcript_for_structuring(segments) if segments
                      else (row.get("raw_transcript") or ""))
        if not transcript.strip():
            raise StructuringError("structure: the transcript is empty")
        t0 = time.monotonic()
        out = _run_with_timeout(
            lambda: structure_transcript(
                transcript,
                mode=row.get("recording_mode") or "post_meeting",
                ask_model=self.ask_model,
                capture_ts=row.get("created_at"),
            ),
            "structure", self.timeouts["structure"])
        log.info("conversation %s structured by %s in %.1fs",
                 cid, out["model"], time.monotonic() - t0)
        report = out["report"]
        return self.register.patch(
            cid,
            structured_report=report,
            entity_candidates=report.get("entity_candidates") or [],
            prompt_version=out["prompt_version"],
            registry_version=out["registry_version"],
            processing_stage="structured",
        )
