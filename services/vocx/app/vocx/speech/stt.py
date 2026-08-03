"""
speech.stt — speech-to-text: turn a captured audio clip into a transcript.

The rest of VOX (extract -> resolve -> gate -> write) is unchanged; step 4 only
puts a transcriber in front of process_capture, swapping the typed transcript for
real audio.

Backends (config: stt.backend):
  faster_whisper  self-hosted faster-whisper (medium / large-v3 to start)
  api             a remote Whisper-compatible HTTP endpoint (swap-in later)
  stub            deterministic, no model — for tests and dry runs

All backends implement transcribe(audio) -> {text, language, duration, segments}.
Heavy deps (faster_whisper, requests) are imported lazily, so importing this
module and using the stub needs nothing installed.
"""

from __future__ import annotations

import os
from typing import Any, Union

AudioInput = Union[str, bytes]


class Transcriber:
    """Interface: transcribe(audio) -> transcript dict. `prompt` primes recognition
    toward expected vocabulary (client names, finance terms) where supported."""

    def transcribe(self, audio: AudioInput, language: str | None = None,
                   prompt: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


def _result(text: str, language: str | None = None, duration: float | None = None,
            segments: list[dict[str, Any]] | None = None, backend: str = "") -> dict[str, Any]:
    return {
        "text": (text or "").strip(),
        "language": language,
        "duration": duration,
        "segments": segments or [],
        "backend": backend,
    }


# --- stub ---------------------------------------------------------------------
class StubTranscriber(Transcriber):
    """No model. Returns fixed text, or reads a .txt sidecar so audio fixtures can
    ship their expected transcript next to them (foo.wav -> foo.wav.txt or foo.txt)."""

    def __init__(self, text: str | None = None):
        self.text = text

    def transcribe(self, audio: AudioInput, language: str | None = None,
                   prompt: str | None = None) -> dict[str, Any]:
        if self.text is not None:
            return _result(self.text, language or "en", backend="stub")
        if isinstance(audio, str):
            for cand in (audio + ".txt", os.path.splitext(audio)[0] + ".txt"):
                if os.path.exists(cand):
                    with open(cand, encoding="utf-8") as fh:
                        return _result(fh.read(), language or "en", backend="stub")
            if audio.endswith(".txt") and os.path.exists(audio):
                with open(audio, encoding="utf-8") as fh:
                    return _result(fh.read(), language or "en", backend="stub")
        return _result("", language, backend="stub")


# --- faster-whisper (self-hosted) --------------------------------------------
class FasterWhisperTranscriber(Transcriber):
    """Self-hosted faster-whisper. The model is loaded lazily on first use and
    cached on the instance, so constructing this never downloads anything."""

    def __init__(self, model_size: str = "medium", device: str = "auto",
                 compute_type: str = "int8", beam_size: int = 5,
                 vad_filter: bool = True, default_language: str | None = None,
                 model_cache_dir: str | None = None, task: str = "translate"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.default_language = default_language
        self.model_cache_dir = model_cache_dir or None
        self.task = task            # translate = any spoken language → English text
        self._model = None
        # ctranslate2 inference is not guaranteed thread-safe on one model instance,
        # and concurrent CPU decodes would thrash anyway — serialize per process.
        import threading
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy; lazy
            kw = {"download_root": self.model_cache_dir} if self.model_cache_dir else {}
            try:
                self._model = WhisperModel(self.model_size, device=self.device,
                                           compute_type=self.compute_type, **kw)
            except Exception:  # noqa: BLE001
                # A GPU was auto-detected but its CUDA runtime (e.g. libcublas)
                # isn't installed — fall back to CPU rather than failing the capture.
                if self.device != "cpu":
                    self._model = WhisperModel(self.model_size, device="cpu",
                                               compute_type="int8", **kw)
                else:
                    raise
        return self._model

    def transcribe(self, audio: AudioInput, language: str | None = None,
                   prompt: str | None = None) -> dict[str, Any]:
        with self._lock:
            return self._transcribe_locked(audio, language, prompt)

    def _transcribe_locked(self, audio: AudioInput, language: str | None = None,
                           prompt: str | None = None) -> dict[str, Any]:
        import io
        model = self._load()
        src: Any = io.BytesIO(audio) if isinstance(audio, bytes) else audio
        segments, info = model.transcribe(
            src, language=language or self.default_language,
            task=("translate" if self.task == "translate" else "transcribe"),
            initial_prompt=(prompt or None),
            beam_size=self.beam_size, vad_filter=self.vad_filter)
        segs, parts = [], []
        for s in segments:                       # generator — consume once
            parts.append(s.text)
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                         "text": s.text.strip()})
        return _result("".join(parts), getattr(info, "language", None),
                       getattr(info, "duration", None), segs, backend="faster_whisper")


def _detail(resp: Any) -> str:
    """The upstream's explanation, from the problem envelope if it sent one."""
    try:
        body = resp.json()
    except Exception:                             # noqa: BLE001 - not JSON; use the text
        return (getattr(resp, "text", "") or "")[:400] or "no response body"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("detail") or err.get("title") or err)[:400]
        if err:
            return str(err)[:400]
        if body.get("detail"):
            return str(body["detail"])[:400]
    return str(body)[:400]


# --- remote API (the PRISM STT service, or any Whisper-compatible endpoint) ---
class APITranscriber(Transcriber):
    """A remote Whisper-compatible endpoint (OpenAI-style multipart) — in PRISM this is
    the dedicated ``services/stt`` container. Endpoint and key come from env so no
    secrets live in config. Transient failures retry with backoff: a network blip must
    not fail a capture whose audio is already archived."""

    def __init__(self, endpoint_env: str = "VOCX_STT_API_URL", key_env: str = "VOCX_STT_API_KEY",
                 model: str = "whisper-1", timeout: int = 300, task: str = "translate"):
        self.endpoint_env = endpoint_env
        self.key_env = key_env
        self.model = model
        self.timeout = timeout
        # English-at-rest: "translate" makes Whisper emit ENGLISH text for ANY spoken
        # language (identity for English input); the detected original language still
        # comes back and lands in the interaction's `language` column.
        self.task = task

    def transcribe(self, audio: AudioInput, language: str | None = None,
                   prompt: str | None = None) -> dict[str, Any]:
        import time

        import httpx  # lazy
        url = os.environ.get(self.endpoint_env)
        key = os.environ.get(self.key_env)
        if not url:
            raise RuntimeError(f"STT API endpoint env {self.endpoint_env} is not set")
        if isinstance(audio, str):
            with open(audio, "rb") as fh:
                blob = fh.read()
            fname = os.path.basename(audio)
        else:
            blob, fname = audio, "audio.wav"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = {"model": self.model, "task": self.task}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt[:1500]
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = httpx.post(url, headers=headers, data=data,
                                  files={"file": (fname, blob)}, timeout=self.timeout)
                if resp.status_code >= 500:
                    # Carry the upstream's own words. "STT service unreachable after
                    # retries: 500" is unanswerable — it cannot distinguish a model that
                    # failed to load from audio that could not be decoded from a service
                    # that was killed mid-decode, and that is precisely what someone
                    # reading the capture log needs to know.
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} from {url}: {_detail(resp)}",
                        request=resp.request, response=resp)
                resp.raise_for_status()   # 4xx = our bug/config — no retry, surface it
                body = resp.json()
                return _result(body.get("text", ""), body.get("language", language),
                               body.get("duration"), body.get("segments"), backend="api")
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise
                last = e
                time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"STT service at {url} failed after 3 attempts: {last}")


# --- factory ------------------------------------------------------------------
def build_transcriber(config: dict[str, Any]) -> Transcriber:
    stt = (config or {}).get("stt", {})
    backend = stt.get("backend", "faster_whisper")
    if backend == "stub":
        return StubTranscriber(stt.get("stub_text"))
    if backend == "api":
        api = stt.get("api", {})
        return APITranscriber(api.get("endpoint_env", "VOCX_STT_API_URL"),
                              api.get("key_env", "VOCX_STT_API_KEY"),
                              api.get("model", "whisper-1"),
                              task=stt.get("task", "translate"))
    if backend == "faster_whisper":
        return FasterWhisperTranscriber(
            model_size=stt.get("model_size", "medium"),
            device=stt.get("device", "auto"),
            compute_type=stt.get("compute_type", "int8"),
            beam_size=stt.get("beam_size", 5),
            vad_filter=stt.get("vad_filter", True),
            default_language=stt.get("language"),
            model_cache_dir=stt.get("model_cache_dir"),
            task=stt.get("task", "translate"))
    raise ValueError(f"Unknown stt.backend: {backend!r}")


# --- audio archiving ----------------------------------------------------------
def archive_audio(audio: AudioInput, capture_ts: str, rm: str, config: dict[str, Any]) -> str | None:
    """Copy the capture into stt.archive_dir and return its path (the transcript_ref).
    Best-effort: returns None if archiving is off or the input isn't a real file."""
    stt = (config or {}).get("stt", {})
    archive_dir = stt.get("archive_dir")
    if not archive_dir:
        return None
    os.makedirs(archive_dir, exist_ok=True)
    ts = (capture_ts or "").replace(":", "").replace("T", "_")[:15] or "capture"
    safe_rm = "".join(c for c in (rm or "") if c.isalnum()) or "rm"
    if isinstance(audio, str) and os.path.exists(audio):
        ext = os.path.splitext(audio)[1] or ".wav"
        dst = os.path.join(archive_dir, f"{ts}_{safe_rm}{ext}")
        import shutil
        shutil.copy(audio, dst)
        return dst
    if isinstance(audio, bytes):
        dst = os.path.join(archive_dir, f"{ts}_{safe_rm}.wav")
        with open(dst, "wb") as fh:
            fh.write(audio)
        return dst
    return None
