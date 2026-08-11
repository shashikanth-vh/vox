"""The transcription BUDGET — what a capture is allowed to spend before it must answer.

Three attempts at a 300s per-attempt timeout let one capture occupy VocX for a quarter
of an hour, long after the browser had abandoned it at its own 300s and told the person
holding the phone "VocX did not answer in time" — a sentence that names nothing, on a
clip that was in fact still decoding. These tests pin the two halves of the fix: the
retries live inside ONE total allowance, and when that allowance runs out VocX ANSWERS
(504, in words) instead of leaving the caller to time out on it.
"""

from __future__ import annotations

import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.vocx.speech import stt as vocx_stt

from tests.test_vocx_pipeline import stub_register  # noqa: F401 - fixture


def _api(monkeypatch, **kw) -> vocx_stt.APITranscriber:
    monkeypatch.setenv("VOCX_STT_API_URL", "http://stt:8000/v1/audio/transcriptions")
    return vocx_stt.APITranscriber(**kw)


def test_retries_stay_inside_the_total_budget(monkeypatch):
    """A budget is a wall-clock ceiling on the WHOLE call, not on one attempt. Three
    attempts that each get the full timeout is how the old code outlived its caller."""
    calls: list[float] = []

    def boom(*a, **kw):
        calls.append(kw.get("timeout", 0.0))
        raise httpx.ConnectError("stt is down")

    monkeypatch.setattr(httpx, "post", boom)
    tr = _api(monkeypatch, timeout=60, budget_s=0.4)
    started = time.monotonic()
    with pytest.raises(vocx_stt.SttTimeoutError):
        tr.transcribe(b"RIFFfake", content_type="audio/webm")
    spent = time.monotonic() - started
    # Nowhere near 3 × 60s, and not even a full second: the budget, not the attempt
    # count, is what ends it.
    assert spent < 5, spent
    assert calls, "the first attempt must still be made"


def test_each_attempt_is_capped_by_what_is_left(monkeypatch):
    """An attempt may never be handed a timeout longer than the budget still holds —
    otherwise one slow attempt blows the ceiling on its own."""
    seen: list[float] = []

    def boom(*a, **kw):
        seen.append(float(kw["timeout"]))
        raise httpx.ConnectError("stt is down")

    monkeypatch.setattr(httpx, "post", boom)
    tr = _api(monkeypatch, timeout=600, budget_s=30)
    with pytest.raises((vocx_stt.SttTimeoutError, RuntimeError)):
        tr.transcribe(b"RIFFfake", content_type="audio/webm")
    assert seen and all(t <= 30.0 for t in seen), seen


def test_a_timeout_says_so_and_a_failure_does_not(monkeypatch):
    """The two outcomes are not the same fact and must not read the same. Ran out of
    time ⇒ SttTimeoutError, whose message names the budget. Refused/broke inside the
    budget ⇒ the ordinary failure, naming the upstream."""
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    tr = _api(monkeypatch, timeout=1, budget_s=1)
    with pytest.raises(vocx_stt.SttTimeoutError) as slow:
        tr.transcribe(b"RIFFfake", content_type="audio/webm")
    assert "did not finish within" in str(slow.value)
    # The clip is NOT lost, and the message has to say so — the audio was archived
    # before transcription ever started.
    assert "recording is stored" in str(slow.value)


def test_the_budget_is_read_from_config(monkeypatch):
    """Sizing this correctly is a DEPLOYMENT decision (it must expire before the
    browser's own capture timeout), so it has to be reachable from config."""
    tr = vocx_stt.build_transcriber({"stt": {"backend": "api", "api": {
        "timeout_s": 90, "budget_s": 120, "attempts": 2}}})
    assert isinstance(tr, vocx_stt.APITranscriber)
    assert (tr.timeout, tr.budget_s, tr.attempts) == (90, 120.0, 2)


def test_the_budget_defaults_to_one_attempt_timeout():
    """Unset, retries must not silently multiply the wait."""
    tr = vocx_stt.build_transcriber({"stt": {"backend": "api", "api": {"timeout_s": 200}}})
    assert tr.budget_s == 200.0


@pytest.mark.asyncio
async def test_capture_answers_504_instead_of_hanging_up(stub_register, monkeypatch):  # noqa: F811
    """The whole point: VocX must be the one that speaks. A 504 carrying the reason
    reaches the recorder as a sentence; silence reaches it as a blind client abort."""
    from app.vocx.core import pipeline as vocx_pipeline

    def too_slow(*a, **kw):
        raise vocx_stt.SttTimeoutError(
            "transcription did not finish within the 240s this capture was given. "
            "The recording is stored — a shorter clip, or a faster STT deployment, "
            "will get through.")

    monkeypatch.setattr(vocx_pipeline, "process_audio_capture", too_slow)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           headers={"X-User-Email": "priya@evamfinance.com"}) as c:
        r = await c.post("/v1/capture_audio?rm=Priya", content=b"RIFFfake",
                         headers={"Content-Type": "audio/webm"})
    assert r.status_code == 504, r.text
    body = r.json()
    assert body["ok"] is False
    assert "did not finish within" in body["error"]
