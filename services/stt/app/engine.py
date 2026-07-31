"""The transcription engine — ONE shared faster-whisper model per process.

Contrast with running Whisper inside VocX: here a single model instance serves every
request (VocX's gunicorn workers each loaded their own copy), inference is serialized
behind a lock (ctranslate2 is not thread-safe on one instance and concurrent CPU
decodes thrash anyway), and the model comes from the image — no network at runtime.
"""

from __future__ import annotations

import io
import threading
from typing import Any, Protocol

from app.config import Settings


class Engine(Protocol):
    def transcribe(self, audio: bytes, language: str | None = None,
                   task: str = "transcribe", prompt: str | None = None) -> dict[str, Any]: ...

    def warm(self) -> None: ...


def _result(text: str, language: str | None, duration: float | None,
            segments: list[dict[str, Any]], backend: str) -> dict[str, Any]:
    return {"text": (text or "").strip(), "language": language, "duration": duration,
            "segments": segments, "backend": backend}


class StubEngine:
    """Deterministic, model-free — tests and CI."""

    def __init__(self, text: str):
        self.text = text
        self.loaded = True

    def warm(self) -> None:  # pragma: no cover - trivial
        return None

    def transcribe(self, audio: bytes, language: str | None = None,
                   task: str = "transcribe", prompt: str | None = None) -> dict[str, Any]:
        return _result(self.text, language or "en", None, [], "stub")


class FasterWhisperEngine:
    def __init__(self, settings: Settings):
        self.s = settings
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy; lazy
            self._model = WhisperModel(self.s.model_size, device=self.s.device,
                                       compute_type=self.s.compute_type,
                                       download_root=self.s.model_dir)
        return self._model

    def transcribe(self, audio: bytes, language: str | None = None,
                   task: str = "transcribe", prompt: str | None = None) -> dict[str, Any]:
        # task="translate" is Whisper's built-in any-language → ENGLISH mode: the text
        # comes out in English while info.language still reports what was SPOKEN.
        # prompt (initial_prompt) primes recognition toward expected vocabulary —
        # client names and finance terms — Whisper only reads its last ~224 tokens,
        # so the caller puts the most valuable words (names) LAST.
        with self._lock:
            model = self._load()
            segments, info = model.transcribe(
                io.BytesIO(audio), language=language,
                task=("translate" if task == "translate" else "transcribe"),
                initial_prompt=(prompt or None),
                beam_size=self.s.beam_size, vad_filter=self.s.vad_filter)
            segs: list[dict[str, Any]] = []
            parts: list[str] = []
            for seg in segments:                      # generator — consume once
                parts.append(seg.text)
                segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                             "text": seg.text.strip()})
            return _result("".join(parts), getattr(info, "language", None),
                           getattr(info, "duration", None), segs, "faster_whisper")


def build_engine(settings: Settings) -> Engine:
    if settings.stub_text:
        return StubEngine(settings.stub_text)
    return FasterWhisperEngine(settings)
