"""What a CAM draft cost: model tokens per stage, and every document read (DocRAG cache,
OCR pages, Sarvam jobs) — counted per job, returned with it, and logged per event."""

from __future__ import annotations

import httpx
import pytest
from test_cam_glm_docrag import _docrag_app, _fake_http, _WithDocrag
from test_cam_staged import MASTER, _Fake, _poll
from test_cam_workbench import LENDING, _call

from app import cam as cam_mod
from app.cam_telemetry import metered, record_llm_call, stage

pytestmark = pytest.mark.asyncio

USAGE_EVENT = 'data: {"choices":[],"usage":{"prompt_tokens":1200,"completion_tokens":340}}'


async def test_glm_calls_are_counted_with_the_tokens_bedrock_reports():
    eng = cam_mod.OpenAICompatEngine("zai.glm-5", "https://b/v1", "bk")
    seen: dict = {}
    with metered() as meter, stage("digest"):
        out = await eng.generate(_fake_http([
            'data: {"choices":[{"delta":{"content":"Fact sheet."},"finish_reason":"stop"}]}',
            USAGE_EVENT, 'data: [DONE]'], seen=seen), "s", [{"role": "user", "content": "x"}])
    assert out == "Fact sheet."
    assert seen["json"]["stream_options"] == {"include_usage": True}
    llm = meter.summary()["llm"]
    assert (llm["calls"], llm["input_tokens"], llm["output_tokens"]) == (1, 1200, 340)
    assert llm["by_stage"]["digest"]["total_tokens"] == 1540
    assert llm["engines"] == ["bedrock:zai.glm-5"] and llm["calls_without_usage"] == 0


async def test_a_plain_reply_a_continuation_and_a_refusal_all_count():
    eng = cam_mod.OpenAICompatEngine("zai.glm-5", "https://b/v1", "bk")
    with metered() as meter:
        await eng.generate(_fake_http(ctype="application/json", body=(
            b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":10,"completion_tokens":2}}')), "s",
            [{"role": "user", "content": "x"}])
        with pytest.raises(RuntimeError):
            await eng.generate(_fake_http(status=500, ctype="application/json",
                                          body=b'{"error":{"message":"busy"}}'),
                               "s", [{"role": "user", "content": "x"}])
    llm = meter.summary()["llm"]
    assert (llm["calls"], llm["failed"], llm["input_tokens"]) == (2, 1, 10)


async def test_anthropic_usage_comes_from_the_stream_events():
    eng = cam_mod.AnthropicEngine("claude-haiku-4-5", "ak")
    with metered() as meter:
        out = await eng.generate(_fake_http([
            'data: {"type":"message_start","message":{"usage":{"input_tokens":900}}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":45}}']), "s", [{"role": "user", "content": "x"}])
    assert out == "Hi"
    llm = meter.summary()["llm"]
    assert (llm["input_tokens"], llm["output_tokens"]) == (900, 45)


async def test_a_call_without_a_usage_report_is_flagged_not_free():
    with metered() as meter:
        record_llm_call(engine="bedrock:x", ok=True, started=0.0, usage=None)
    assert meter.summary()["llm"]["calls_without_usage"] == 1


class _MeteredEngine(cam_mod.CamEngine):
    """The staged fake, reporting a fixed token cost per call like a real engine."""

    name = "fake:glm"

    def __init__(self) -> None:
        self.fake = _Fake()

    async def generate(self, http, system, turns):  # noqa: ANN001
        record_llm_call(engine=self.name, ok=True, started=0.0,
                        usage={"prompt_tokens": 100, "completion_tokens": 10})
        return await self.fake(system, turns)


class _DocragWithImages(_WithDocrag):
    """DocRAG as it now answers: telemetry on every extract, images read by OCR."""

    def __init__(self) -> None:
        super().__init__()
        self.docs["master"] = ("text/markdown", MASTER)
        self.docs["photo-1"] = ("image/jpeg", "jpeg bytes")

    async def post(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        if u.endswith("/v1/extract"):
            self.extract_calls.append(kw)
            name, _data = kw["files"]["file"]
            ocr = name.endswith(".jpg")
            return httpx.Response(200, json={
                "markdown": "[page 1]\n\nSite photo: Rs 42 Cr plant" if ocr else
                            "[page 1]\n\n# Term Sheet\n\nTenor 36 months",
                "warnings": [], "page_count": 1,
                "extraction_engines": ["sarvam_docai"] if ocr else ["opendataloader"],
                "telemetry": {"cached": not ocr, "pages": 1, "ocr_pages": 1 if ocr else 0,
                              "sarvam": {"jobs": 1 if ocr else 0,
                                         "pages_submitted": 1 if ocr else 0,
                                         "pages_succeeded": 1 if ocr else 0}}},
                request=httpx.Request("POST", u))
        return await super().post(url, **kw)


async def test_a_staged_job_reports_what_it_spent(monkeypatch):
    eng = _MeteredEngine()
    app = _docrag_app(monkeypatch, eng)
    stub = _DocragWithImages()
    app.state.http = stub

    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["pdf-1", "photo-1", "fin-1"]})
    assert start.status_code == 202, start.text
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job

    # The image went to DocRAG as an image, and its OCR is on the document's row.
    assert {c["files"]["file"][0] for c in stub.extract_calls} == {"pdf-1.pdf", "photo-1.jpg"}
    rows = {d["doc_id"]: d for d in job["documents"]}
    assert rows["photo-1"]["via"] == "docrag" and rows["photo-1"]["format"] == "jpg"
    assert rows["photo-1"]["ocr_pages"] == 1 and rows["photo-1"]["sarvam_jobs"] == 1
    assert rows["pdf-1"]["cached"] is True and rows["fin-1"]["via"] == "basic"

    usage = job["usage"]
    llm, docs = usage["llm"], usage["documents"]
    assert llm["calls"] == job["calls"]
    assert llm["input_tokens"] == 100 * job["calls"] and llm["output_tokens"] == 10 * job["calls"]
    assert {"digest", "dataset", "public_checks", "section", "final_section"} <= set(
        llm["by_stage"])
    assert (docs["read"], docs["skipped"], docs["via_docrag"], docs["docrag_cache_hits"]) == (
        3, 0, 2, 1)
    assert (docs["sarvam_jobs"], docs["sarvam_pages_submitted"], docs["ocr_pages"]) == (1, 1, 1)
    assert docs["formats"] == {"pdf": 1, "jpg": 1, "text": 1}


async def test_an_image_without_ocr_names_the_fix(monkeypatch):
    app = _docrag_app(monkeypatch, _MeteredEngine())
    stub = _WithDocrag()
    stub.docs["photo-2"] = ("image/png", "scanned png bytes")    # DocRAG finds no text
    app.state.http = stub
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["pdf-1", "photo-2"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    [skipped] = [d for d in gen.json()["skipped"] if d["doc_id"] == "photo-2"]
    assert skipped["reason"].startswith("image — OCR produced no text")
    assert gen.json()["usage"]["calls"] == 1
