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
                   task: str = "transcribe", prompt: str | None = None,
                   model_size: str | None = None) -> dict[str, Any]: ...

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
                   task: str = "transcribe", prompt: str | None = None,
                   model_size: str | None = None) -> dict[str, Any]:
        return _result(self.text, language or "en", None, [], "stub")


class ModelUnavailable(RuntimeError):
    """The model itself could not be loaded — a DEPLOYMENT fault, not a bad request.

    Kept distinct from a decode failure so the API can answer 503 with something a person
    can act on (the size, the directory, the offline flag) instead of a bare 500 that
    reaches the caller's log as nothing more than the number 500.
    """


class AudioUndecodable(ValueError):
    """The bytes were received but are not audio this build can decode."""


def sniff_container(blob: bytes) -> str:
    """Name the container from its magic bytes. A decode failure that says only
    "could not be transcribed" leaves an operator guessing whether the phone sent a
    format this build lacks, a truncated upload, or silence; naming what actually
    arrived — and how many bytes of it — is the difference between a shrug and a fix."""
    head = blob[:16]
    if head[4:8] == b"ftyp":
        return f"MP4/M4A ({head[8:12].decode('ascii', 'replace')})"   # iOS Safari records this
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "WebM/Matroska"                                        # Chrome, Android
    if head[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return "WAV"
    if head[:4] == b"OggS":
        return "Ogg"
    if head[:4] == b"fLaC":
        return "FLAC"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "MP3/ADTS"
    return "unrecognised (no known audio container signature)"


class FasterWhisperEngine:
    def __init__(self, settings: Settings):
        self.s = settings
        # size -> model. The default size is loaded at warm(); extra baked sizes
        # (settings.extra_model_sizes) load lazily on first request that names them.
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.load_error: str = ""

    @property
    def loaded(self) -> bool:
        return self.s.model_size in self._models

    def known_sizes(self) -> set[str]:
        extras = {x.strip() for x in
                  (self.s.extra_model_sizes or "").replace(",", " ").split() if x.strip()}
        return {self.s.model_size, *extras}

    def warm(self) -> None:
        with self._lock:
            self._load()

    def _load(self, size: str | None = None) -> Any:
        size = size if size in self.known_sizes() else self.s.model_size
        if size not in self._models:
            from faster_whisper import WhisperModel  # heavy; lazy
            try:
                self._models[size] = WhisperModel(size, device=self.s.device,
                                                  compute_type=self.s.compute_type,
                                                  cpu_threads=self.s.cpu_threads,
                                                  download_root=self.s.model_dir)
            except Exception as exc:              # noqa: BLE001 - re-raised, typed
                self.load_error = (
                    f"model {size!r} could not be loaded from "
                    f"{self.s.model_dir!r} on device {self.s.device!r} "
                    f"(compute_type {self.s.compute_type!r}): {exc}. The image bakes its "
                    f"models at build time and runs with HF_HUB_OFFLINE=1, so "
                    f"STT_MODEL_SIZE / STT_EXTRA_MODELS must match what was baked.")
                raise ModelUnavailable(self.load_error) from exc
            self.load_error = ""
        return self._models[size]

    def transcribe(self, audio: bytes, language: str | None = None,
                   task: str = "transcribe", prompt: str | None = None,
                   model_size: str | None = None) -> dict[str, Any]:
        # task="translate" is Whisper's built-in any-language → ENGLISH mode: the text
        # comes out in English while info.language still reports what was SPOKEN.
        # prompt (initial_prompt) primes recognition toward expected vocabulary —
        # client names and finance terms — Whisper only reads its last ~224 tokens,
        # so the caller puts the most valuable words (names) LAST.
        # model_size selects among the BAKED sizes (an unknown name falls back to the
        # default — an OpenAI-compat caller sending "whisper-1" gets the default).
        # ONE lock across all sizes: concurrent CPU decodes thrash; they queue.
        with self._lock:
            model = self._load(model_size)
            try:
                segments, info = model.transcribe(
                    io.BytesIO(audio), language=language,
                    task=("translate" if task == "translate" else "transcribe"),
                    initial_prompt=(prompt or None),
                    beam_size=self.s.beam_size, vad_filter=self.s.vad_filter)
                segs: list[dict[str, Any]] = []
                parts: list[str] = []
                # faster-whisper decodes and infers LAZILY — the container is demuxed and
                # the segments produced as this generator is consumed, so a clip in a codec
                # this build cannot decode raises HERE, not at the call above. Both are the
                # caller's payload, not a service fault; both must say which.
                for seg in segments:                  # generator — consume once
                    parts.append(seg.text)
                    segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                                 "text": seg.text.strip()})
            except Exception as exc:                  # noqa: BLE001 - re-raised, typed
                raise AudioUndecodable(
                    f"the {len(audio)} byte clip ({sniff_container(audio)}) could not be "
                    f"transcribed: {type(exc).__name__}: {exc}") from exc
            return _result("".join(parts), getattr(info, "language", None),
                           getattr(info, "duration", None), segs, "faster_whisper")


def build_engine(settings: Settings) -> Engine:
    if settings.stub_text:
        return StubEngine(settings.stub_text)
    return FasterWhisperEngine(settings)
