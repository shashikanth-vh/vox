"""The STT front door: OpenAI-compatible multipart in, transcript JSON out — plus the
auth, size-cap and health behaviour VocX relies on."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import create_app

pytestmark = pytest.mark.asyncio


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_transcribe_multipart_roundtrip():
    app = create_app()
    async with _client(app) as c:
        r = await c.post("/v1/audio/transcriptions",
                         data={"model": "whisper-1", "language": "en"},
                         files={"file": ("clip.wav", b"RIFF....fake-audio")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"].startswith("Met the EcoSoch")
    assert body["backend"] == "stub"
    # The response shape the VocX APITranscriber consumes.
    assert set(body) >= {"text", "language", "duration", "segments"}


async def test_bearer_key_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("STT_API_KEYS", "k1, k2")
    get_settings.cache_clear()
    app = create_app()
    async with _client(app) as c:
        anon = await c.post("/v1/audio/transcriptions",
                            files={"file": ("a.wav", b"x")})
        wrong = await c.post("/v1/audio/transcriptions",
                             headers={"Authorization": "Bearer nope"},
                             files={"file": ("a.wav", b"x")})
        ok_bearer = await c.post("/v1/audio/transcriptions",
                                 headers={"Authorization": "Bearer k2"},
                                 files={"file": ("a.wav", b"x")})
        ok_header = await c.post("/v1/audio/transcriptions",
                                 headers={"X-API-Key": "k1"},
                                 files={"file": ("a.wav", b"x")})
    assert anon.status_code == 401
    assert wrong.status_code == 401
    assert ok_bearer.status_code == 200
    assert ok_header.status_code == 200


async def test_task_validated_and_accepted():
    app = create_app()
    async with _client(app) as c:
        ok = await c.post("/v1/audio/transcriptions",
                          data={"task": "translate"},
                          files={"file": ("a.wav", b"x")})
        bad = await c.post("/v1/audio/transcriptions",
                           data={"task": "summarize"},
                           files={"file": ("a.wav", b"x")})
    assert ok.status_code == 200
    assert bad.status_code == 400


async def test_prompt_field_accepted_and_bounded():
    """The vocabulary-priming prompt rides as a form field (stub ignores it, the real
    engine passes it to Whisper as initial_prompt)."""
    app = create_app()
    async with _client(app) as c:
        r = await c.post("/v1/audio/transcriptions",
                         data={"task": "translate", "prompt": "crore, tenor, EcoSoch Solar"},
                         files={"file": ("a.wav", b"x")})
    assert r.status_code == 200, r.text


async def test_caps_and_empty_body(monkeypatch):
    monkeypatch.setenv("STT_MAX_AUDIO_BYTES", "8")
    get_settings.cache_clear()
    app = create_app()
    async with _client(app) as c:
        big = await c.post("/v1/audio/transcriptions",
                           files={"file": ("a.wav", b"123456789")})
        empty = await c.post("/v1/audio/transcriptions",
                             files={"file": ("a.wav", b"")})
    assert big.status_code == 413
    assert empty.status_code == 400


async def test_health_and_ready():
    app = create_app()
    async with _client(app) as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        ready = (await c.get("/readyz")).json()
    assert ready["status"] == "ok"
