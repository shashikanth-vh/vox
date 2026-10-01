"""The STT front door: OpenAI-compatible multipart in, transcript JSON out — plus the
auth, size-cap and health behaviour VocX relies on."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.engine import AudioUndecodable, ModelUnavailable
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


class _Broken:
    """An engine whose model will not load — the deployment fault."""
    loaded = False
    load_error = "model 'medium' could not be loaded from '/opt/models'"

    def transcribe(self, *a, **k):
        raise ModelUnavailable(self.load_error)


class _Garbled:
    """An engine that loads fine but cannot decode this particular clip."""
    loaded = True
    load_error = ""

    def transcribe(self, *a, **k):
        raise AudioUndecodable("the 9 byte clip could not be transcribed: ValueError: bad")


async def test_model_failure_is_503_and_says_why():
    """A model that will not load must not reach the caller as a bare 500.

    VocX retries a 5xx three times and then logs the STATUS. When that status carries no
    explanation the capture log reads 'STT service unreachable after retries: 500' and
    there is nothing in it to act on — which is exactly the report this covers.
    """
    app = create_app()
    app.state.engine = _Broken()
    async with _client(app) as c:
        r = await c.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"12345")})
        ready = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "model_unavailable"
    assert "/opt/models" in r.json()["error"]["detail"]
    # And readiness stops claiming the container can serve.
    assert ready.status_code == 503


async def test_undecodable_audio_is_400_not_500():
    """Bad bytes are the caller's fault: a 400 tells VocX to stop retrying at once."""
    app = create_app()
    app.state.engine = _Garbled()
    async with _client(app) as c:
        r = await c.post("/v1/audio/transcriptions", files={"file": ("a.webm", b"12345")})
    assert r.status_code == 400
    assert "could not be transcribed" in r.json()["error"]["detail"]


def test_engine_knows_its_baked_sizes_and_falls_back():
    """extra_model_sizes widens the per-request menu; anything not baked routes
    to the default size rather than failing an OpenAI-compat caller."""
    from app.config import Settings
    from app.engine import FasterWhisperEngine
    s = Settings(model_size="medium", extra_model_sizes="small, large-v3")
    e = FasterWhisperEngine(s)
    assert e.known_sizes() == {"medium", "small", "large-v3"}
    s2 = Settings(model_size="small")
    assert FasterWhisperEngine(s2).known_sizes() == {"small"}


# ------------------------------------------------- decoder fault vs bad clip

class _Model:
    """Stands in for a loaded WhisperModel: `transcribe` raises what we tell it."""
    def __init__(self, exc):
        self.exc = exc

    def transcribe(self, *a, **k):
        raise self.exc


def _real_engine(exc):
    from app.config import Settings
    from app.engine import FasterWhisperEngine
    eng = FasterWhisperEngine(Settings())
    eng._models[eng.s.model_size] = _Model(exc)   # skip the (offline) model load
    return eng


def test_a_library_fault_in_the_decoder_is_a_503_not_a_bad_clip():
    """1 Oct 2026: an unpinned PyAV 18 made av.open() refuse faster-whisper's
    metadata_errors keyword, and every recording came back 400 'could not be
    transcribed' — read by everyone as a bad clip. A TypeError (or any Python-level
    fault) inside the decoder is this BUILD's fault and must say so, as a 503."""
    eng = _real_engine(TypeError("open() got an unexpected keyword argument 'metadata_errors'"))
    with pytest.raises(ModelUnavailable) as err:
        eng.transcribe(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
    assert "decoder in this image is broken" in str(err.value)
    assert "metadata_errors" in str(err.value)
    assert "pyproject" in str(err.value)


def test_a_clip_the_decoder_cannot_read_is_still_a_400():
    eng = _real_engine(ValueError("Invalid data found when processing input"))
    with pytest.raises(AudioUndecodable) as err:
        eng.transcribe(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
    assert "WebM/Matroska" in str(err.value)
    assert "Invalid data" in str(err.value)
