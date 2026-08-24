"""
speech.segment_store — the server-side copy of a recording WHILE it is being made.

The browser's IndexedDB protects a take against a refresh on the same machine;
this protects it against everything else. While the mic is open the panel
streams chunk batches here, so at any moment the server holds the take up to a
few seconds ago — survivable across a dead phone, a drained battery, or the
user walking to a different device. On return, the recording screen lists the
caller's unfinished takes, plays back what is stored, and continues onto the
next segment of the SAME capture id.

Layout (always local disk — segments are working state until finalize):

    {base}/{capture_id}/manifest.json
    {base}/{capture_id}/seg000.webm
    {base}/{capture_id}/seg001.webm       # a new browser session = a new segment

Within one browser session MediaRecorder emits ONE continuous stream, so
appending its chunk batches in order reconstructs a valid, playable file. A
refresh or another device starts a fresh recorder — a fresh stream — hence a
fresh segment. Segments are therefore transcribed one by one, in order, and
the transcripts merge; no audio-container surgery is ever attempted.

Every write is defensive: per-capture locks serialize concurrent appends, the
manifest is written atomically (tmp + rename), ids are charset-checked, and
per-capture size is capped. A storage failure returns an error to the caller —
it never corrupts what is already on disk.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from typing import Any

_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,99}$")
MAX_SEGMENTS = 200
MAX_CAPTURE_BYTES = 64 * 1024 * 1024        # matches the capture door's wall
STALE_AFTER_S = 7 * 24 * 3600               # abandoned takes are prunable after a week

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(capture_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(capture_id, threading.Lock())


class SegmentStoreError(RuntimeError):
    """A named refusal (bad id, cap exceeded, unknown capture) — the endpoint
    turns it into a 4xx with this message; disk state is untouched."""


class SegmentStore:
    def __init__(self, base_dir: str) -> None:
        self.base = base_dir

    # ------------------------------------------------------------------ paths
    def _dir(self, capture_id: str) -> str:
        if not _ID_RX.match(capture_id or ""):
            raise SegmentStoreError("invalid capture_id")
        return os.path.join(self.base, capture_id)

    def _manifest_path(self, capture_id: str) -> str:
        return os.path.join(self._dir(capture_id), "manifest.json")

    def _seg_path(self, capture_id: str, idx: int) -> str:
        if not (0 <= idx < MAX_SEGMENTS):
            raise SegmentStoreError(f"segment index out of range (0..{MAX_SEGMENTS - 1})")
        return os.path.join(self._dir(capture_id), f"seg{idx:03d}.webm")

    # -------------------------------------------------------------- manifest
    def manifest(self, capture_id: str) -> dict[str, Any] | None:
        path = self._manifest_path(capture_id)
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_manifest(self, capture_id: str, data: dict[str, Any]) -> None:
        d = self._dir(capture_id)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".manifest.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self._manifest_path(capture_id))

    # ---------------------------------------------------------------- append
    def append(self, capture_id: str, seg: int, chunk: bytes, *,
               rm: str = "", mime: str = "", mode: str = "",
               consent_id: str = "", elapsed: int = 0) -> dict[str, Any]:
        """Append a chunk batch to a segment; returns the updated totals.
        Idempotence is byte-level: the panel sends each batch once, and a
        retried batch after a failed response merely duplicates a moment of
        audio — unpleasant, never corrupting. Empty chunks are a no-op."""
        if not chunk:
            m = self.manifest(capture_id) or {}
            return {"bytes_total": m.get("bytes_total", 0)}
        with _lock_for(capture_id):
            m = self.manifest(capture_id) or {
                "capture_id": capture_id, "rm": rm, "mime": mime, "mode": mode,
                "consent_id": consent_id, "created_at": time.time(),
                "segments": {}, "bytes_total": 0, "finalized": False,
            }
            if m.get("finalized"):
                raise SegmentStoreError("this take is already finished")
            if m["bytes_total"] + len(chunk) > MAX_CAPTURE_BYTES:
                raise SegmentStoreError("recording exceeds the size cap")
            path = self._seg_path(capture_id, seg)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(chunk)
            key = str(seg)
            seg_info = m["segments"].get(key) or {"bytes": 0}
            seg_info["bytes"] += len(chunk)
            m["segments"][key] = seg_info
            m["bytes_total"] += len(chunk)
            m["updated_at"] = time.time()
            if elapsed:
                m["elapsed"] = max(int(m.get("elapsed") or 0), int(elapsed))
            if rm and not m.get("rm"):
                m["rm"] = rm
            if mime and not m.get("mime"):
                m["mime"] = mime
            if mode:
                m["mode"] = mode
            if consent_id:
                m["consent_id"] = consent_id
            self._write_manifest(capture_id, m)
            return {"bytes_total": m["bytes_total"], "segments": len(m["segments"]),
                    "elapsed": m.get("elapsed", 0)}

    # ----------------------------------------------------------------- reads
    def segment_paths(self, capture_id: str) -> list[str]:
        """Ordered on-disk segment files for transcription/playback."""
        m = self.manifest(capture_id)
        if not m:
            return []
        out = []
        for key in sorted(m.get("segments", {}), key=int):
            p = self._seg_path(capture_id, int(key))
            if os.path.exists(p):
                out.append(p)
        return out

    def unfinished_for(self, rm: str) -> list[dict[str, Any]]:
        """The caller's takes that streamed but never finished — newest first.
        Stale abandoned takes (a week untouched) are pruned on the way."""
        out: list[dict[str, Any]] = []
        try:
            entries = os.listdir(self.base)
        except OSError:
            return out
        now = time.time()
        for cid in entries:
            m = self.manifest(cid) if _ID_RX.match(cid) else None
            if not m or m.get("finalized"):
                continue
            if now - (m.get("updated_at") or m.get("created_at") or now) > STALE_AFTER_S:
                self.discard(cid)
                continue
            if (m.get("rm") or "").strip().lower() != (rm or "").strip().lower():
                continue
            out.append({"capture_id": cid, "elapsed": m.get("elapsed", 0),
                        "bytes_total": m.get("bytes_total", 0),
                        "segments": len(m.get("segments", {})),
                        "mode": m.get("mode") or "post_meeting",
                        "consent_id": m.get("consent_id") or "",
                        "updated_at": m.get("updated_at")})
        out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
        return out

    # ------------------------------------------------------------- lifecycle
    def finalize(self, capture_id: str) -> list[str]:
        """Mark the take finished and return its ordered segment paths.
        Idempotent — finishing twice returns the same paths."""
        with _lock_for(capture_id):
            m = self.manifest(capture_id)
            if not m:
                raise SegmentStoreError("no streamed take under this capture id")
            if not m.get("finalized"):
                m["finalized"] = True
                m["updated_at"] = time.time()
                self._write_manifest(capture_id, m)
        paths = self.segment_paths(capture_id)
        if not paths:
            raise SegmentStoreError("the streamed take holds no audio")
        return paths

    def discard(self, capture_id: str) -> None:
        d = self._dir(capture_id)
        with _lock_for(capture_id):
            shutil.rmtree(d, ignore_errors=True)


def transcribe_segments(paths: list[str], transcriber: Any,
                        prompt: str | None = None) -> dict[str, Any]:
    """Transcribe each segment IN ORDER and merge — text joined, segment lists
    concatenated, the first detected language wins. One bad segment fails the
    whole take loudly (the pipeline's retry machinery owns what happens next);
    a silent hole in the middle of a meeting would be worse than a retry."""
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    language = None
    for p in paths:
        result = transcriber.transcribe(p, prompt=prompt)
        if isinstance(result, str):
            result = {"text": result, "segments": [{"text": result}], "language": None}
        texts.append((result.get("text") or "").strip())
        segments.extend(list(result.get("segments") or []))
        language = language or result.get("language")
    return {"text": "\n".join(t for t in texts if t), "segments": segments,
            "language": language}
