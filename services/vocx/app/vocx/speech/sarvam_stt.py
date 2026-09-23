"""Sarvam speech-to-text — the Indic-tuned engine, as a Transcriber.

Used by the STT benchmark (``python -m evals.stt_bench``) to compete against
the deployed Whisper service on the SAME saved audio, and available as a
backend the deployment can adopt if the benchmark says so. English-at-rest:
the translate endpoint (saaras) emits English for any spoken language, the
same contract the Whisper path honours with task=translate.

Env:
  SARVAM_API_KEY        the subscription key (same one the structuring path uses)
  SARVAM_STT_URL        override endpoint (default: the translate endpoint)
  SARVAM_STT_MODEL      override model (default saaras:v2.5)
"""

from __future__ import annotations

import os
from typing import Any

from .stt import AudioInput, Transcriber

_DEFAULT_URL = "https://api.sarvam.ai/speech-to-text-translate"
_DEFAULT_MODEL = "saaras:v2.5"


class SarvamTranscriber(Transcriber):
    def __init__(self, timeout: int = 240):
        self.timeout = timeout

    def transcribe(self, audio: AudioInput, language: str | None = None,
                   prompt: str | None = None, content_type: str | None = None,
                   model: str | None = None) -> dict[str, Any]:
        import httpx  # lazy

        key = (os.environ.get("SARVAM_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("SARVAM_API_KEY is not set — the Regional STT engine "
                               "needs the same key the structuring path uses.")
        url = os.environ.get("SARVAM_STT_URL") or _DEFAULT_URL
        mdl = model or os.environ.get("SARVAM_STT_MODEL") or _DEFAULT_MODEL
        if isinstance(audio, str):
            with open(audio, "rb") as fh:
                data = fh.read()
            fname = os.path.basename(audio)
        else:
            data, fname = audio, "audio.webm"
        files = {"file": (fname, data, content_type or "audio/webm")}
        form: dict[str, str] = {"model": mdl}
        if prompt:
            form["prompt"] = prompt
        last = ""
        # Same two auth spellings the chat API accepts, tried in order.
        for headers in ({"api-subscription-key": key},
                        {"Authorization": f"Bearer {key}"}):
            r = httpx.post(url, headers=headers, data=form, files=files,
                           timeout=self.timeout)
            if r.status_code in (401, 403):
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            if r.status_code >= 300:
                raise RuntimeError(f"Sarvam STT HTTP {r.status_code}: {r.text[:300]}")
            body = r.json()
            text = (body.get("transcript") or body.get("text") or "").strip()
            if not text:
                raise RuntimeError(f"Sarvam STT returned no transcript: {str(body)[:300]}")
            return {"text": text,
                    "segments": [],
                    "language": body.get("language_code") or body.get("language") or "unknown"}
        raise RuntimeError(f"Sarvam STT auth refused under both header styles ({last})")
