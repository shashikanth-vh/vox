"""The CAM workbench on GLM (an OpenAI-compatible endpoint, AWS Bedrock by default) with
documents extracted by DocRAG (OpenDataLoader + Sarvam OCR), and the CAM template fill."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from test_cam_workbench import LENDING, _app, _call, _RegisterStub

from app import cam as cam_mod
from app.config import get_settings

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- engine
def _fake_http(lines=(), *, status=200, ctype="text/event-stream", body=b"", seen=None):
    class _Resp:
        status_code = status
        headers = {"content-type": ctype}

        async def aread(self):
            return body

        async def aiter_lines(self):
            for line in lines:
                yield line

    class _Http:
        def stream(self, method, url, **kw):
            if seen is not None:
                seen.update(method=method, url=url, **kw)

            class _Ctx:
                async def __aenter__(self):
                    return _Resp()

                async def __aexit__(self, *exc):
                    return False
            return _Ctx()
    return _Http()


async def test_glm_engine_streams_the_answer_and_drops_reasoning():
    eng = cam_mod.OpenAICompatEngine("zai.glm-5", "https://bedrock.example/openai/v1/", "bk",
                                     max_tokens=1234)
    seen: dict = {}
    out = await eng.generate(_fake_http([
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"thinking about DSCR..."}}]}',
        'data: {"choices":[{"delta":{"content":"## Borrower\\n"}}]}',
        ': keep-alive',
        'data: {"choices":[{"delta":{"content":"Advika Renewables."}}]}',
        'data: [DONE]',
    ], seen=seen), "SYS", [{"role": "user", "content": "draft"},
                          {"role": "assistant", "content": "ok"},
                          {"role": "user", "content": [{"type": "text", "text": "again"}]}])
    assert out == "## Borrower\nAdvika Renewables."
    assert eng.name == "bedrock:zai.glm-5"
    assert seen["url"] == "https://bedrock.example/openai/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer bk"
    req = seen["json"]
    assert req["model"] == "zai.glm-5" and req["max_completion_tokens"] == 1234 and req["stream"]
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert req["messages"][-1] == {"role": "user", "content": "again"}   # blocks → text


async def test_glm_engine_accepts_a_plain_json_reply_and_reports_refusals():
    eng = cam_mod.OpenAICompatEngine("zai.glm-5", "https://b/v1", "bk")
    out = await eng.generate(_fake_http(
        ctype="application/json",
        body=b'{"choices":[{"message":{"content":"# CAM\\nText"}}]}'),
        "s", [{"role": "user", "content": "x"}])
    assert out == "# CAM\nText"
    with pytest.raises(RuntimeError, match=r"refused \(HTTP 403\): not authorized"):
        await eng.generate(_fake_http(status=403, ctype="application/json",
                                      body=b'{"error":{"message":"not authorized"}}'),
                           "s", [{"role": "user", "content": "x"}])
    with pytest.raises(RuntimeError, match="no text"):
        await eng.generate(_fake_http(['data: [DONE]']), "s", [{"role": "user", "content": "x"}])


def test_engine_choice_follows_config(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_CAM_ENGINE", "bedrock:zai.glm-5")
    monkeypatch.setenv("WORKFLOWS_CAM_LLM_API_KEY", "bk")
    get_settings.cache_clear()
    eng = cam_mod.build_engine(get_settings())
    assert isinstance(eng, cam_mod.OpenAICompatEngine) and eng.name == "bedrock:zai.glm-5"
    assert "bedrock-runtime.ap-south-1.amazonaws.com/openai/v1" in eng.base_url
    monkeypatch.setenv("WORKFLOWS_CAM_LLM_API_KEY", "")
    get_settings.cache_clear()
    assert isinstance(cam_mod.build_engine(get_settings()), cam_mod.StubEngine)
    get_settings.cache_clear()


# --------------------------------------------------------------------- DocRAG path
MD = "[page 1]\n\n# Term Sheet\n\n| Field | Value |\n| --- | --- |\n| Tenor | 36 months |\n"


class _Capture(cam_mod.CamEngine):
    name = "capture:test"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def generate(self, http, system, turns):  # noqa: ANN001
        self.calls.append(turns)
        return "# CAM\n\n## Borrower\nDraft."


class _WithDocrag(_RegisterStub):
    """The register stub plus a DocRAG that answers /v1/extract."""

    def __init__(self, *, docrag: str = "ok") -> None:
        super().__init__()
        self.docs["pdf-1"] = ("application/pdf", "%PDF-1.7 term sheet bytes")
        self.docs["scan-1"] = ("application/pdf", "%PDF-1.7 scanned")
        self.mode = docrag
        self.extract_calls: list[dict] = []

    async def post(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        if u.endswith("/v1/extract"):
            self.extract_calls.append(kw)
            if self.mode == "down":
                raise httpx.ConnectError("connection refused")
            name, data = kw["files"]["file"]
            if b"scanned" in data:
                return httpx.Response(200, json={
                    "markdown": "", "extraction_engines": ["opendataloader"],
                    "warnings": ["page 1: only 0 chars extracted locally (below 40); "
                                 "DOCRAG_SARVAM_API_KEY is not set"]},
                    request=httpx.Request("POST", u))
            return httpx.Response(200, json={"markdown": MD, "warnings": [],
                                             "extraction_engines": ["opendataloader"]},
                                  request=httpx.Request("POST", u))
        return await super().post(url, **kw)


def _docrag_app(monkeypatch, engine=None, **env):
    monkeypatch.setenv("WORKFLOWS_DOCRAG_URL", "http://docrag:8000")
    monkeypatch.setenv("WORKFLOWS_DOCRAG_API_KEY", "dk")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if engine is not None:
        monkeypatch.setattr(cam_mod, "build_engine", lambda settings: engine)
    return _app(monkeypatch)


async def test_generate_reads_pdfs_through_docrag(monkeypatch):
    eng = _Capture()
    app = _docrag_app(monkeypatch, eng)
    stub = _WithDocrag()
    app.state.http = stub

    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["pdf-1", "fin-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    included = {d["doc_id"]: d for d in gen.json()["included"]}
    assert included["pdf-1"]["via"] == "docrag"
    assert included["fin-1"]["via"] == "basic"          # CSV never leaves the orchestrator
    prompt = eng.calls[0][0]["content"]
    assert "| Tenor | 36 months |" in prompt and "Assess the borrower." in prompt
    [call] = stub.extract_calls                          # one DocRAG call: the PDF only
    assert call["headers"] == {"X-API-Key": "dk", "X-Tenant": "EVAM"}
    assert call["files"]["file"][0] == "pdf-1.pdf"


async def test_the_workbench_preview_shows_docrag_text(monkeypatch):
    app = _docrag_app(monkeypatch)
    app.state.http = _WithDocrag()
    r = await _call(app, "GET", "/v1/cam/doc-text", params={"doc_id": "pdf-1"})
    assert r.status_code == 200 and r.json()["via"] == "docrag"
    assert "| Tenor | 36 months |" in r.json()["text"]


async def test_docrag_down_falls_back_to_basic_extraction_and_says_so(monkeypatch):
    eng = _Capture()
    app = _docrag_app(monkeypatch, eng)
    stub = _WithDocrag(docrag="down")
    stub.docs["txt-pdf"] = ("application/pdf", "%PDF-1.4 binary")
    app.state.http = stub
    monkeypatch.setattr(cam_mod, "extract_text", lambda c, b: ("basic text of the pdf", None))
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/refine", json={
        "instruction": "summarise", "update_draft": False, "source_doc_ids": ["txt-pdf"]})
    assert r.status_code == 200, r.text
    [note] = r.json()["documents"]
    assert note["via"] == "basic" and "DocRAG unavailable (ConnectError)" in note["note"]
    assert "basic text of the pdf" in eng.calls[0][-1]["content"]


async def test_a_scan_with_no_ocr_is_skipped_with_the_fix_named(monkeypatch):
    app = _docrag_app(monkeypatch, _Capture())
    app.state.http = _WithDocrag()
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["scan-1", "pdf-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    [skip] = gen.json()["skipped"]
    assert skip["doc_id"] == "scan-1"
    assert "OCR produced no text" in skip["reason"] and "SARVAM" in skip["reason"]


async def test_one_request_stays_inside_the_total_document_budget(monkeypatch):
    eng = _Capture()
    app = _docrag_app(monkeypatch, eng, WORKFLOWS_CAM_MAX_TOTAL_DOC_CHARS=str(len(MD) + 10))
    stub = _WithDocrag()
    stub.docs["pdf-2"] = ("application/pdf", "%PDF-1.7 another")
    app.state.http = stub
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["pdf-1", "pdf-2"], "prompt_doc_id": "prompt-1"})
    body = gen.json()
    by_id = {d["doc_id"]: d for d in body["included"]}
    assert "note" not in by_id["pdf-1"]
    assert "total document budget" in by_id["pdf-2"]["note"]
    assert len(eng.calls[0][0]["content"]) < len(MD) * 2 + 400


# ------------------------------------------------------------------ template fill
def _template_docx() -> bytes:
    from app.docx_out import markdown_to_docx

    with zipfile.ZipFile(io.BytesIO(markdown_to_docx("placeholder", None))) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    doc = parts["word/document.xml"].decode().replace(
        "<w:body>", '<w:body><w:p><w:r><w:t xml:space="preserve">EVAM CAM TEMPLATE BODY'
        "</w:t></w:r></w:p>", 1)
    parts["word/document.xml"] = doc.encode()
    parts["word/styles.xml"] = parts["word/styles.xml"].replace(
        b'<w:style w:type="paragraph" w:styleId="Heading1">',
        b'<w:style w:type="paragraph" w:styleId="Heading1"><w:rPr><w:color w:val="0E7564"/></w:rPr>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in parts.items():
            z.writestr(n, b)
    return buf.getvalue()


async def test_generate_cam_fills_the_evam_cam_template(monkeypatch):
    app = _app(monkeypatch)
    stub = _RegisterStub()

    tmpl = _template_docx()
    orig_get = stub.get

    async def get(url, **kw):  # noqa: ANN001, ANN003
        if str(url).endswith("/v1/documents/tmpl-1/content"):
            return httpx.Response(200, content=tmpl, headers={"content-type": cam_mod._DOCX_TYPE},
                                  request=httpx.Request("GET", str(url)))
        return await orig_get(url, **kw)
    stub.get = get
    app.state.http = stub

    r = await _call(app, "POST", f"/v1/cam/{LENDING}/export-docx", json={
        "markdown": "# Credit Assessment Memo\n\n## Borrower\nAdvika Renewables.",
        "title": "CAM v1 draft", "template_doc_id": "tmpl-1"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        out_doc = z.read("word/document.xml").decode()
        out_styles = z.read("word/styles.xml").decode()
    assert "0E7564" in out_styles                       # the template's own styling
    assert "EVAM CAM TEMPLATE BODY" not in out_doc      # its placeholder body replaced
    assert "Advika Renewables." in out_doc and "Verify every figure" in out_doc

    # No template → today's standalone Word export still works.
    plain = await _call(app, "POST", f"/v1/cam/{LENDING}/export-docx",
                        json={"markdown": "# CAM", "template_doc_id": "missing-doc"})
    assert plain.status_code == 200 and plain.content[:2] == b"PK"


async def test_glm_engine_continues_a_cam_cut_off_at_the_output_limit():
    eng = cam_mod.OpenAICompatEngine("zai.glm-5", "https://b/v1", "bk")
    replies = iter([
        ['data: {"choices":[{"delta":{"content":"## Section 1\\nPart one, "},'
         '"finish_reason":"length"}]}', 'data: [DONE]'],
        ['data: {"choices":[{"delta":{"content":"part two.\\n## Annexure III\\nDebt."},'
         '"finish_reason":"stop"}]}', 'data: [DONE]'],
    ])
    sent: list = []

    class _Http:
        def stream(self, method, url, **kw):
            sent.append(kw["json"]["messages"])
            lines = next(replies)
            return _fake_http(lines).stream(method, url)

    out = await eng.generate(_Http(), "SYS", [{"role": "user", "content": "draft the CAM"}])
    assert out == "## Section 1\nPart one, part two.\n## Annexure III\nDebt."
    assert len(sent) == 2
    assert sent[1][-2] == {"role": "assistant", "content": "## Section 1\nPart one, "}
    assert "Continue exactly where you stopped" in sent[1][-1]["content"]


def test_the_drafting_brief_tells_the_engine_it_has_no_tools():
    assert "NO file system, NO web" in cam_mod._SYSTEM
    assert "NOT PERFORMED" in cam_mod._SYSTEM
    assert "never claim to have run a public check" in cam_mod._ASK_SYSTEM


async def test_cam_export_follows_the_evam_table_and_page_spec(monkeypatch):
    app = _app(monkeypatch)
    app.state.http = _RegisterStub()
    md = ("# CAM\n\n## Section 1: Executive Summary\n| Metric | FY25 |\n| --- | --- |\n"
          "| Revenue | 120.0 |\n| EBITDA | 14.2 |\n\n## Section 2: Management Profile\nText.")
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/export-docx", json={"markdown": md})
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        doc = z.read("word/document.xml").decode()
    assert 'w:fill="1F3864"' in doc and 'w:fill="D6E4F0"' in doc
    assert doc.count('<w:br w:type="page"/>') == 1        # before Section 2 only


def test_docrag_ca_is_added_to_the_public_roots(monkeypatch, tmp_path):
    """DocRAG on the Chitti host sits behind a private certificate: its CA is trusted in
    addition to the public roots (the orchestrator also calls Bedrock), and only when set."""
    import ssl

    from app.config import Settings

    assert Settings(docrag_ca_file="").docrag_verify() is True
    loaded: list[str] = []

    class _Ctx:
        def load_verify_locations(self, cafile):  # noqa: ANN001
            loaded.append(cafile)

    monkeypatch.setattr(ssl, "create_default_context", lambda: _Ctx())
    ca = tmp_path / "cipher-ca.crt"
    ctx = Settings(docrag_ca_file=str(ca)).docrag_verify()
    assert isinstance(ctx, _Ctx) and loaded == [str(ca)]


async def test_extraction_uses_the_docrag_client_when_one_is_configured(monkeypatch):
    """With the CA-trusting DocRAG client in place, documents go through it — the shared
    client (register, OIDC, Bedrock) keeps the default trust store."""
    app = _docrag_app(monkeypatch, _Capture())
    register, docrag = _WithDocrag(), _WithDocrag()
    app.state.http, app.state.docrag_http = register, docrag
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["pdf-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    assert len(docrag.extract_calls) == 1 and not register.extract_calls
