"""The CAM workbench: generate → refine → finalise over a stubbed register, stub engine.

The engine seam is the point under test as much as the flow: the workbench must run the
whole lifecycle with NO vendor account (StubEngine), record every turn durably on the
register, refuse to draft without a readable prompt doc, and never silently drop a
document it could not read.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings

pytestmark = pytest.mark.asyncio

LENDING = "11111111-2222-3333-4444-555555555555"


def _app(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_API_KEYS", "k")
    monkeypatch.setenv("WORKFLOWS_ANTHROPIC_API_KEY", "")   # → StubEngine
    get_settings.cache_clear()
    from app.api import create_app

    app = create_app()
    # Stand in for what lifespan would set — no Temporal, no OIDC verifier.
    app.state.oidc = None
    app.state.temporal = None
    app.state.http = None
    return app


class _RegisterStub:
    """The register as the workbench sees it: documents with content, cam-report rows."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[str, str]] = {   # id -> (content_type, body)
            "prompt-1": ("text/markdown", "# CAM prompts\nAssess the borrower."),
            "fin-1": ("text/csv", "year,revenue\n2025,120"),
            "pdf-1": ("application/pdf", "%PDF-1.4 binary"),
        }
        self.reports: dict[str, dict] = {}
        self.turns: list[dict] = []
        self._n = 0

    async def get(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        if "/v1/documents/" in u and u.endswith("/content"):
            doc_id = u.split("/v1/documents/")[1].split("/")[0]
            if doc_id not in self.docs:
                return httpx.Response(404, request=httpx.Request("GET", u))
            ctype, body = self.docs[doc_id]
            return httpx.Response(200, content=body.encode(),
                                  headers={"content-type": ctype},
                                  request=httpx.Request("GET", u))
        if u.endswith("/v1/internal/cam-reports"):
            rows = [r for r in self.reports.values()
                    if r["lending_id"] == (kw.get("params") or {}).get("lending_id")]
            return httpx.Response(200, json=rows, request=httpx.Request("GET", u))
        if "/v1/internal/cam-reports/" in u:
            rid = u.rsplit("/", 1)[1]
            row = dict(self.reports.get(rid) or {})
            row["turns"] = [t for t in self.turns if t["report_id"] == rid]
            return httpx.Response(200 if row else 404, json=row,
                                  request=httpx.Request("GET", u))
        return httpx.Response(404, request=httpx.Request("GET", u))

    async def post(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        body = kw.get("json") or {}
        if u.endswith("/v1/internal/cam-reports"):
            self._n += 1
            rid = f"cam-{self._n}"
            self.reports[rid] = {"id": rid, "lending_id": body["lending_id"],
                                 "report_version": self._n, "status": "Draft",
                                 "engine": body.get("engine"), "draft_md": "",
                                 "source_doc_ids": body.get("source_doc_ids") or []}
            return httpx.Response(201, json=self.reports[rid],
                                  request=httpx.Request("POST", u))
        if "/turns" in u:
            rid = u.split("/cam-reports/")[1].split("/")[0]
            self.turns.append({"report_id": rid, **body})
            if body.get("draft_md") is not None:
                self.reports[rid]["draft_md"] = body["draft_md"]
            if body.get("document_id") is not None:
                self.reports[rid]["document_id"] = body["document_id"]
            return httpx.Response(201, json={"ok": True},
                                  request=httpx.Request("POST", u))
        if "/submit" in u:
            rid = u.split("/cam-reports/")[1].split("/")[0]
            self.reports[rid]["status"] = "Submitted"
            self.reports[rid]["document_id"] = body.get("document_id")
            return httpx.Response(200, json=self.reports[rid],
                                  request=httpx.Request("POST", u))
        if "/documents/upload" in u:
            return httpx.Response(201, json={"id": "doc-cam-1", "checksum": "c" * 64},
                                  request=httpx.Request("POST", u))
        if "/decide" in u:
            rid = u.split("/cam-reports/")[1].split("/")[0]
            if rid not in self.reports:
                return httpx.Response(404, request=httpx.Request("POST", u))
            self.reports[rid]["status"] = body["decision"]
            self.reports[rid]["decision_note"] = body.get("note")
            return httpx.Response(200, json=self.reports[rid],
                                  request=httpx.Request("POST", u))
        return httpx.Response(404, request=httpx.Request("POST", u))


async def _call(app, method: str, path: str, **kw):  # noqa: ANN001, ANN003
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        return await c.request(method, path,
                               headers={"X-API-Key": "k", "X-Tenant": "EVAM",
                                        "X-User-Email": "bhavana@evamfinance.com"}, **kw)


async def test_generate_refine_finalise_with_the_stub_engine(monkeypatch):
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub

    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1", "pdf-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    body = gen.json()
    # The stub engine drafted; the version is open on the register with its transcript.
    assert "CAM" in body["draft_md"] and body["engine"] == "stub:offline"
    # No silent omission: the fake PDF that could not be read as text is NAMED, with why
    # (a real PDF now extracts through pypdf; this stub body is not a parseable PDF).
    assert [s["doc_id"] for s in body["skipped"]] == ["pdf-1"]
    assert "pdf" in body["skipped"][0]["reason"].lower()
    assert [i["doc_id"] for i in body["included"]] == ["fin-1"]
    assert [t["role"] for t in stub.turns] == ["user", "assistant"]

    ref = await _call(app, "POST", f"/v1/cam/{LENDING}/refine",
                      json={"instruction": "Tighten the collateral section."})
    assert ref.status_code == 200, ref.text
    assert [t["role"] for t in stub.turns] == ["user", "assistant", "user", "assistant"]

    fin = await _call(app, "POST", f"/v1/cam/{LENDING}/finalise", json={})
    assert fin.status_code == 200, fin.text
    out = fin.json()
    assert out["status"] == "Submitted" and out["document_id"] == "doc-cam-1"
    assert stub.reports[out["report_id"]]["status"] == "Submitted"


async def test_an_uploaded_cam_document_files_without_submitting(monkeypatch):
    """The Word lane: the analyst downloads the template, fills it OUTSIDE, uploads the
    file — finalise with its document_id opens a version row (none existed) and ATTACHES
    the document, but the version stays Draft. Filing is workbench work; the committee
    request is the separate "Send to credit committee" step, so nothing lands on any
    approver's queue from here."""
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub

    fin = await _call(app, "POST", f"/v1/cam/{LENDING}/finalise",
                      json={"document_id": "doc-word-1"})
    assert fin.status_code == 200, fin.text
    out = fin.json()
    assert out["status"] == "Draft" and out["document_id"] == "doc-word-1"
    row = stub.reports[out["report_id"]]
    assert row["status"] == "Draft" and row["document_id"] == "doc-word-1"
    assert row["engine"] == "analyst:document"       # no engine drafted this one
    assert any("[uploaded CAM]" in t["content"] for t in stub.turns)

    # With an OPEN draft the uploaded file joins that version — no second row, and the
    # version still is not submitted.
    stub2 = _RegisterStub()
    app.state.http = stub2
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1"], "prompt_doc_id": "prompt-1"})
    rid = gen.json()["report_id"]
    fin2 = await _call(app, "POST", f"/v1/cam/{LENDING}/finalise",
                       json={"document_id": "doc-word-2"})
    assert fin2.status_code == 200, fin2.text
    assert fin2.json()["report_id"] == rid
    assert stub2.reports[rid]["document_id"] == "doc-word-2"
    assert stub2.reports[rid]["status"] == "Draft"
    assert len(stub2.reports) == 1


async def test_no_readable_prompt_doc_means_no_draft(monkeypatch):
    """The brief comes from the credit team — a document or typed text; the workbench
    refuses to invent one."""
    app = _app(monkeypatch)
    app.state.http = _RegisterStub()
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1"], "prompt_doc_id": "missing"})
    assert r.status_code == 422, r.text
    assert "prompt document" in r.json()["error"]["detail"]

    # Neither a document nor typed text: refused with what to do.
    none = await _call(app, "POST", f"/v1/cam/{LENDING}/generate",
                       json={"source_doc_ids": ["fin-1"]})
    assert none.status_code == 422, none.text
    assert "type the drafting brief" in none.json()["error"]["detail"]


async def test_a_typed_brief_drafts_without_any_prompt_document(monkeypatch):
    """The analyst may TYPE the brief in the workbench — document + typed prompt go to
    the engine exactly like document + prompt-doc."""
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1"],
        "prompt_text": "Draft a CAM covering financials and risks."})
    assert r.status_code == 201, r.text
    assert "CAM" in r.json()["draft_md"]
    # The transcript records a typed brief (no invented document id).
    assert any("typed brief" in t["content"] for t in stub.turns if t["role"] == "user")

    # And a selection where NOTHING is readable refuses too, naming each reason.
    r2 = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["pdf-1"], "prompt_doc_id": "prompt-1"})
    assert r2.status_code == 422, r2.text
    assert "pdf-1" in r2.json()["error"]["detail"]


async def test_an_ask_answers_without_touching_the_working_draft(monkeypatch):
    """update_draft=false: the reply joins the transcript but the analyst's draft stays
    theirs — the ASK lane for mining answers while hand-filling the working CAM."""
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub
    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1"], "prompt_doc_id": "prompt-1"})
    rid = gen.json()["report_id"]
    before = stub.reports[rid]["draft_md"]

    r = await _call(app, "POST", f"/v1/cam/{LENDING}/refine",
                    json={"instruction": "What was FY25 revenue?", "update_draft": False})
    assert r.status_code == 200, r.text
    assert r.json()["updated_draft"] is False
    assert stub.reports[rid]["draft_md"] == before          # draft untouched
    # …but the exchange IS on the durable transcript.
    assert [t["role"] for t in stub.turns][-2:] == ["user", "assistant"]


async def test_the_first_ask_opens_the_version_itself(monkeypatch):
    """The workbench is a conversation from the first question: an ask on a line with no
    open CAM opens the version row and answers — no generate-then-talk two-step."""
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/refine",
                    json={"instruction": "Summarise the file.", "update_draft": False})
    assert r.status_code == 200, r.text
    assert len(stub.reports) == 1
    row = next(iter(stub.reports.values()))
    assert row["status"] == "Draft" and row["lending_id"] == LENDING


async def test_scanned_pdfs_reach_an_engine_that_reads_them(monkeypatch):
    """A PDF with no text layer is not a dead end: an engine with visual PDF support
    gets the FILE as a document block — on the first draft AND re-attached on every
    rework (the durable transcript stores text, not bytes). The offline stub cannot
    read scans, and the skip reason says exactly that."""
    from app import cam as cam_mod

    real_extract = cam_mod.extract_text

    def fake_extract(ctype, blob):  # noqa: ANN001 — the stub's scan marker
        if b"scanned" in blob:
            return "", cam_mod._SCANNED_PDF
        return real_extract(ctype, blob)

    monkeypatch.setattr(cam_mod, "extract_text", fake_extract)

    class _SeeingEngine(cam_mod.CamEngine):
        name = "test:seeing"
        supports_documents = True

        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        async def generate(self, http, system, turns):  # noqa: ANN001
            self.seen.append(turns)
            return "# CAM from the scans"

    engine = _SeeingEngine()
    monkeypatch.setattr(cam_mod, "build_engine", lambda _s: engine)

    app = _app(monkeypatch)
    stub = _RegisterStub()
    stub.docs["scan-1"] = ("application/pdf", "%PDF-1.7 scanned pages")
    app.state.http = stub

    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["scan-1", "fin-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    body = gen.json()
    assert {d["doc_id"] for d in body["included"]} == {"scan-1", "fin-1"}
    scan_note = next(d for d in body["included"] if d["doc_id"] == "scan-1")["note"]
    assert "attached" in scan_note
    first = engine.seen[0][0]["content"]
    assert isinstance(first, list) and first[0]["type"] == "document"
    assert first[0]["source"]["media_type"] == "application/pdf"
    assert first[-1]["type"] == "text" and "prompt" not in first[-1]["text"][:0]

    # Rework: the scan rides along again, so the engine keeps seeing the same pages.
    ref = await _call(app, "POST", f"/v1/cam/{LENDING}/refine",
                      json={"instruction": "Deepen the collateral section."})
    assert ref.status_code == 200, ref.text
    last_turn = engine.seen[1][-1]["content"]
    assert isinstance(last_turn, list) and last_turn[0]["type"] == "document"


async def test_the_stub_engine_names_why_a_scan_is_skipped(monkeypatch):
    from app import cam as cam_mod

    real_extract = cam_mod.extract_text
    monkeypatch.setattr(cam_mod, "extract_text",
                        lambda c, b: ("", cam_mod._SCANNED_PDF) if b"scanned" in b
                        else real_extract(c, b))
    app = _app(monkeypatch)   # no key → StubEngine, supports_documents=False
    stub = _RegisterStub()
    stub.docs["scan-1"] = ("application/pdf", "%PDF-1.7 scanned pages")
    app.state.http = stub

    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["scan-1", "fin-1"], "prompt_doc_id": "prompt-1"})
    assert gen.status_code == 201, gen.text
    body = gen.json()
    assert [d["doc_id"] for d in body["included"]] == ["fin-1"]
    reason = next(s for s in body["skipped"] if s["doc_id"] == "scan-1")["reason"]
    assert "stub engine cannot read scans" in reason


def test_docx_and_unreadable_formats_extract_or_say_why():
    """A .docx (a zip holding word/document.xml) extracts with the stdlib — even when
    the content-type label is wrong — and formats with no extractor name themselves."""
    import io
    import zipfile

    from app.cam import extract_text

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   "<w:document><w:body><w:p><w:r><w:t>Assess the borrower "
                   "&amp; the collateral.</w:t></w:r></w:p></w:body></w:document>")
    docx = buf.getvalue()

    text, reason = extract_text(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx)
    assert reason is None and "Assess the borrower & the collateral." in text

    # Mislabelled but sniffed: the bytes say what they are.
    text2, reason2 = extract_text("application/octet-stream", docx)
    assert reason2 is None and "Assess the borrower" in text2

    _, why = extract_text("image/png", b"\x89PNG....")
    assert why and "no extractor" in why


async def test_the_sanction_letter_yields_cp_cs_and_covenants_as_data(monkeypatch):
    """extract-terms: the engine reads the letter and hands back the three lists —
    validated, frequency coerced to the register's vocabulary, nothing invented by the
    endpoint itself. The offline stub (which answers prose, not JSON) refuses with
    'fill by hand' instead of seeding garbage."""
    from app import cam as cam_mod

    class _JsonEngine(cam_mod.CamEngine):
        name = "test:json"
        supports_documents = True

        async def generate(self, http, system, turns):  # noqa: ANN001
            assert "JSON only" in system
            # The credit note travels with the letter when the caller supplies it.
            content = turns[0]["content"]
            assert "CREDIT NOTE" in str(content)
            return ('{"cp_items": ["Security created", "Escrow account opened"], '
                    '"cs_items": [{"label": "End-use certificate", '
                    '"timeline": "30 days from first disbursement"}], '
                    '"covenants": [{"name": "Monthly stock statement", '
                    '"frequency": "Monthly"}, {"name": "Debtor ageing", '
                    '"frequency": "every quarter"}], '
                    '"terms": {"amount_cr": 1.0, "rate_kind": "Fixed", '
                    '"rate_pct": 15.0, "tenor_months": 54, "emi_amount": 447608, '
                    '"day_count": "365", "schedule_kind": "EMI", '
                    '"spread_pct": null, "repayment_start": "2026-05-07", '
                    '"penal_rate_pct": 240.0, "moratorium_months": null}}')

    monkeypatch.setattr(cam_mod, "build_engine", lambda _s: _JsonEngine())
    app = _app(monkeypatch)
    stub = _RegisterStub()
    stub.docs["letter-1"] = ("text/markdown", "# Sanction letter\nCP: security. CS: …")
    app.state.http = stub

    r = await _call(app, "POST", "/v1/cam/extract-terms",
                    json={"doc_id": "letter-1",
                          "credit_note": "Approved at 15% fixed, 54 months."})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["cp_items"] == ["Security created", "Escrow account opened"]
    assert out["cs_items"][0]["timeline"].startswith("30 days")
    # An off-vocabulary frequency lands on the register's default, not on garbage.
    assert [c["frequency"] for c in out["covenants"]] == ["Monthly", "Monthly"]
    # The NUMERIC terms come back validated: nulls dropped, an impossible figure
    # (a 240% penal rate) rejected rather than passed through to the form.
    t = out["terms"]
    assert t["amount_cr"] == 1.0 and t["rate_pct"] == 15.0
    assert t["tenor_months"] == 54 and t["repayment_start"] == "2026-05-07"
    assert t["day_count"] == "365" and t["schedule_kind"] == "EMI"
    assert "spread_pct" not in t and "moratorium_months" not in t
    assert "penal_rate_pct" not in t

    # The stub engine's prose reply parses to nothing → an honest refusal.
    monkeypatch.undo()                       # drop the fake engine (and re-set below)
    app2 = _app(monkeypatch)
    stub2 = _RegisterStub()
    stub2.docs["letter-1"] = ("text/markdown", "# Sanction letter\nCP: security.")
    app2.state.http = stub2
    r2 = await _call(app2, "POST", "/v1/cam/extract-terms", json={"doc_id": "letter-1"})
    assert r2.status_code == 422, r2.text
    assert "by hand" in r2.json()["error"]["detail"]


async def test_committee_triad_maps_verbs_to_register_decisions(monkeypatch):
    """/approve → Approved, /return → Returned (note through), and no committee
    authority means 403 before the register is ever asked."""
    app = _app(monkeypatch)
    stub = _RegisterStub()
    app.state.http = stub

    gen = await _call(app, "POST", f"/v1/cam/{LENDING}/generate", json={
        "source_doc_ids": ["fin-1"], "prompt_doc_id": "prompt-1"})
    rid = gen.json()["report_id"]
    await _call(app, "POST", f"/v1/cam/{LENDING}/finalise", json={})

    async def call_as(path, json, roles):  # noqa: ANN001
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://orch") as c:
            return await c.post(path, json=json, headers={
                "X-API-Key": "k", "X-Tenant": "EVAM",
                "X-User-Email": "divya@evamfinance.com", "X-User-Roles": roles})

    # An RM has no committee authority: refused up front, register untouched.
    denied = await call_as(f"/v1/workflows/cam-reports/{rid}/return",
                           {"returned_by": "x", "note": "tighten"}, "BDRM")
    assert denied.status_code == 403, denied.text
    assert stub.reports[rid]["status"] == "Submitted"

    ret = await call_as(f"/v1/workflows/cam-reports/{rid}/return",
                        {"returned_by": "x", "note": "tighten the security section"},
                        "Management")
    assert ret.status_code == 200, ret.text
    assert stub.reports[rid]["status"] == "Returned"
    assert stub.reports[rid]["decision_note"] == "tighten the security section"

    stub.reports[rid]["status"] = "Submitted"     # as if resubmitted
    ok = await call_as(f"/v1/workflows/cam-reports/{rid}/approve",
                       {"approved_by": "x", "note": "carried"}, "Credit Head")
    assert ok.status_code == 200, ok.text
    assert stub.reports[rid]["status"] == "Approved"


async def test_export_docx_renders_the_box_content_as_word(monkeypatch):
    """The box content (Markdown) comes back as a real .docx: valid zip, valid XML,
    and the text round-trips through the workbench's own extractor — headings, list
    items and table cells all present."""
    app = _app(monkeypatch)
    md = ("# Credit Assessment Memo\n\n"
          "## Borrower\n"
          "**Advika Renewables** — solar EPC, *Karnataka*.\n\n"
          "- DSCR `1.31x`\n"
          "- Tenor 60 months\n\n"
          "| Metric | Value |\n|---|---|\n| Amount | ₹ 24 Cr |\n| Rate | 11.5% |\n")
    r = await _call(app, "POST", f"/v1/cam/{LENDING}/export-docx",
                    json={"markdown": md, "title": "CAM v1"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert 'filename="CAM v1.docx"' in r.headers["content-disposition"]
    blob = r.content
    assert blob[:2] == b"PK"

    import io
    import zipfile
    from xml.dom import minidom
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= names
        minidom.parseString(z.read("word/document.xml"))   # well-formed XML
        minidom.parseString(z.read("word/styles.xml"))

    from app.cam import extract_text
    text, reason = extract_text(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", blob)
    assert reason is None
    for expected in ("Credit Assessment Memo", "Borrower", "Advika Renewables",
                     "DSCR", "Tenor 60 months", "₹ 24 Cr", "11.5%", "CAM v1"):
        assert expected in text, expected


async def test_export_docx_requires_markdown_and_the_api_key(monkeypatch):
    app = _app(monkeypatch)
    empty = await _call(app, "POST", f"/v1/cam/{LENDING}/export-docx",
                        json={"markdown": ""})
    assert empty.status_code == 422
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        keyless = await c.post(f"/v1/cam/{LENDING}/export-docx",
                               json={"markdown": "# x"},
                               headers={"X-Tenant": "EVAM"})
    assert keyless.status_code in (401, 403), keyless.text


async def test_anthropic_engine_accumulates_streamed_deltas():
    """The vendor engine STREAMS (long CAM updates outlive non-streaming requests):
    text_delta events concatenate in order; stream errors surface as RuntimeError."""
    from app.cam import AnthropicEngine

    def fake_http(lines, status=200):
        class _Resp:
            status_code = status
            async def aread(self):
                return b'{"error":{"message":"bad key"}}'
            async def aiter_lines(self):
                for line in lines:
                    yield line
        class _Http:
            def stream(self, *a, **k):
                class _Ctx:
                    async def __aenter__(self):
                        return _Resp()
                    async def __aexit__(self, *exc):
                        return False
                return _Ctx()
        return _Http()

    eng = AnthropicEngine("claude-haiku-4-5", "k")
    out = await eng.generate(fake_http([
        'data: {"type":"message_start"}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"## Borrower\\n"}}',
        'event: ping',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Advika Renewables."}}',
        'data: {"type":"message_stop"}',
    ]), "sys", [{"role": "user", "content": "hi"}])
    assert out == "## Borrower\nAdvika Renewables."

    with pytest.raises(RuntimeError, match="refused"):
        await eng.generate(fake_http([], status=401), "sys", [{"role": "user", "content": "x"}])

    with pytest.raises(RuntimeError, match="mid-answer"):
        await eng.generate(fake_http([
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"part"}}',
            'data: {"type":"error","error":{"message":"overloaded"}}',
        ]), "sys", [{"role": "user", "content": "x"}])
