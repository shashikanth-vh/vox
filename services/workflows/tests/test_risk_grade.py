"""Company 360 risk grade — PRISM's half: the company, its Data Register files and
its PULSE news are gathered AS the caller, the grade is computed by Chitti on the
AI host, cached per company and per visible-section set, and served as a report."""

from __future__ import annotations

import io
import zipfile

import httpx
from test_cam_workbench import _app, _call

from app import risk_grade as rg

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PANORAMA = {
    "anchor": {"entity_id": "e-1", "name": "Macwin Solar Energy Private Limited"},
    "restricted": [], "stats": {"documents": 5, "deals_in_flight": 1},
    "interactions": [{"notes": "x" * 5000, "type": "Call"} for _ in range(50)],
    "lending": [{"tracker_no": "L078"}],
    "contacts": [{"name": "R. Kumar"}],
}
DOCS = [  # the Data Register list, as the Register returns it
    # Stored as the UI's section codes (fin, bank, kyc) — and a display name, both accepted.
    {"id": "d1", "section": "fin", "status": "On File", "title": "Audited financials",
     "original_filename": "Macwin Audited Fins FY 2024-25 signed.pdf"},
    {"id": "d2", "section": "fin", "status": "On File", "title": "Projections / CMA data",
     "original_filename": "Macwin_CMA Axis - Company.xls"},
    {"id": "d3", "section": "fin", "status": "Pending", "title": "ITR acknowledgements"},
    {"id": "d4", "section": "kyc", "status": "Verified", "title": "PAN",
     "original_filename": "PAN.pdf"},
    {"id": "d5", "section": "Banking & Debt", "status": "Verified", "title": "Debtors",
     "original_filename": "Debtors Aging 30.6.25.csv"},
]
CONTENT = {"d1": ("application/pdf", b"%PDF-1.4 audited"),
           "d2": ("application/vnd.ms-excel", OLE2 + b"cma workbook"),
           "d4": ("application/pdf", b"%PDF-1.4 pan"),
           # A CSV that a Windows browser labelled as Excel: text, not a workbook.
           "d5": ("application/vnd.ms-excel", b"Debtor,Days\nNTPC,45\n")}
GRADE = {"rating": "AMBER", "provisional": True, "label": "AMBER – PROVISIONAL",
         "score": 52, "company": "Macwin Solar Energy Private Limited",
         "engine": "chitti:zai.glm-5", "confidence_pct": 64,
         "confidence_detail": {"data_coverage": 70, "agreement": 55, "spread": 9},
         "runs": [{"score": 50}, {"score": 52}, {"score": 59}],
         "pillars": [{"key": "A", "name": "Financial strength", "weight": 30, "score": 6,
                      "weighted": 18, "available": True, "evidence": "Revenue ₹61 Cr | FY25"}],
         "hard_triggers": [{"trigger": "NCLT", "status": "Clear", "evidence": ""}],
         "risks": ["thin banking"], "mitigants": ["audited"], "data_gaps": ["Bank statements"],
         "move_up": "bank statements", "move_down": "a decline", "action": "Hold",
         "adjustments": [], "inputs": {"lending_lines": 1}, "prompt": "ATLAS.md",
         "prompt_version": "abc", "generated_at": "2026-09-29T10:00:00+00:00"}


class _Register:
    def __init__(self, restricted=(), docs=DOCS):  # noqa: ANN001
        self.restricted, self.docs = list(restricted), docs
        self.reads: list[str] = []

    async def get(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        self.reads.append(u)
        req = httpx.Request("GET", u)
        if u.endswith("/v1/panorama"):
            return httpx.Response(200, json={**PANORAMA, "restricted": self.restricted},
                                  request=req)
        if "/v1/documents?entity_id=e-1" in u:
            return httpx.Response(200, json={"items": self.docs}, request=req)
        if u.endswith("/content"):
            ctype, body = CONTENT[u.split("/v1/documents/")[1].split("/")[0]]
            return httpx.Response(200, content=body, headers={"content-type": ctype},
                                  request=req)
        if "/v1/news/search" in u:
            q = kw["params"]["q"].strip('"')
            # PULSE's real shape: "title", and its verdicts as words. The second hit is
            # the kind of noise a name search returns — it does not name the firm.
            return httpx.Response(200, json={"articles": [
                {"title": f"{q} in the news", "source": "ET", "when": "20260920",
                 "severity": "GOOD"},
                {"title": "Solar stocks rally on budget", "source": "ET", "severity": "GOOD"}]},
                request=req)
        raise AssertionError(f"unexpected read {u}")


class _AiHost:
    """The AI host edge: DocRAG /v1/extract and Chitti /v1/risk-grade."""

    def __init__(self, status=200, body=None):  # noqa: ANN001
        self.status, self.body = status, body if body is not None else GRADE
        self.extracts: list[str] = []
        self.grades: list[tuple[str, dict]] = []

    async def post(self, url, **kw):  # noqa: ANN001, ANN003
        u = str(url)
        if u.endswith("/v1/extract"):
            name = kw["files"]["file"][0]
            self.extracts.append(name)
            return httpx.Response(200, json={"markdown": f"# text of {name}",
                                             "extraction_engines": ["opendataloader"],
                                             "cached": False},
                                  request=httpx.Request("POST", u))
        self.grades.append((u, kw))
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", u))


def _grading_app(monkeypatch, ai=None, register=None, *, pulse=True, docrag=True):  # noqa: ANN001
    monkeypatch.setenv("WORKFLOWS_CHITTI_URL", "https://ai-host:8443")
    monkeypatch.setenv("WORKFLOWS_CHITTI_API_KEY", "gw-key")
    monkeypatch.setenv("WORKFLOWS_PULSE_URL", "http://pulse:8000" if pulse else "")
    monkeypatch.setenv("WORKFLOWS_PULSE_API_KEY", "pulse-key")
    monkeypatch.setenv("WORKFLOWS_DOCRAG_URL", "https://ai-host:8443/docrag" if docrag else "")
    app = _app(monkeypatch)
    app.state.http = register or _Register()
    app.state.docrag_http = ai or _AiHost()
    return app


BODY = {"entity_id": "e-1", "company": "Macwin Solar Energy Private Limited",
        "news": [{"headline": "From the dialog", "source": "ET", "severity": "GREEN"}]}


async def test_the_grade_reads_the_data_register_the_cam_way(monkeypatch):
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["label"] == "AMBER – PROVISIONAL" and g["cached"] is False
    assert g["generated_by"] == "bhavana@evamfinance.com" and g["entity_id"] == "e-1"
    # Financials + Banking & Debt only; the Pending slot and the KYC file are not read.
    # The PDF and the real .xls go through DocRAG; the CSV-labelled-Excel is read as text.
    assert ai.extracts == ["d1.pdf", "d2.xls"]
    url, kw = ai.grades[0]
    assert url == "https://ai-host:8443/v1/risk-grade" and kw["headers"]["X-API-Key"] == "gw-key"
    sent = kw["json"]
    # Hard actuals first, the CMA projection last (it is what a tight budget cuts).
    assert [d["name"] for d in sent["documents"]] == [
        "Macwin Audited Fins FY 2024-25 signed.pdf", "Debtors Aging 30.6.25.csv",
        "Macwin_CMA Axis - Company.xls"]
    assert sent["documents"][1]["text"].startswith("Debtor,Days")
    # PULSE, server-side: the firm and its key person; the dialog's news is not needed.
    assert [n["headline"] for n in sent["news"]] == [
        "macwin solar energy in the news", "R. Kumar in the news"]
    assert sent["news"][0]["severity"] == "GREEN" and sent["news"][1]["about"] == "R. Kumar"
    assert len(sent["panorama"]["interactions"]) == 30
    fates = {d["name"]: d for d in g["inputs"]["documents"]}
    assert all(f["used"] for f in fates.values()) and len(fates) == 3
    assert fates["Macwin_CMA Axis - Company.xls"]["section"] == "Financials"
    assert sent["documents"][0]["section"] == "Financials"

    again = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert again.json()["cached"] is True and len(ai.grades) == 1
    got = await _call(app, "GET", "/v1/panorama/risk-grade",
                      params={"company": BODY["company"], "entity_id": "e-1"})
    assert got.status_code == 200 and got.json()["score"] == 52
    # Regrade re-reads everything; with nothing new it is the same grade (next test).
    fresh = await _call(app, "POST", "/v1/panorama/risk-grade", json={**BODY, "refresh": True})
    assert fresh.json()["unchanged"] is True and len(ai.grades) == 1


async def test_without_pulse_the_dialogs_news_is_the_fallback(monkeypatch):
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai, pulse=False)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200
    assert [n["headline"] for n in ai.grades[0][1]["json"]["news"]] == ["From the dialog"]
    assert "PULSE not configured" in r.json()["inputs"]["news_note"]


async def test_the_report_downloads_as_word(monkeypatch):
    app = _grading_app(monkeypatch)
    none = await _call(app, "GET", "/v1/panorama/risk-grade/report",
                       params={"company": BODY["company"], "entity_id": "e-1"})
    assert none.status_code == 404
    await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    r = await _call(app, "GET", "/v1/panorama/risk-grade/report",
                    params={"company": BODY["company"], "entity_id": "e-1"})
    assert r.status_code == 200
    assert "Risk grade - Macwin Solar Energy Private Limited - 2026-09-29.docx" in \
        r.headers["content-disposition"]
    xml = zipfile.ZipFile(io.BytesIO(r.content)).read("word/document.xml").decode()
    assert "AMBER – PROVISIONAL" in xml and "64%" in xml and "Macwin Audited Fins" in xml


def test_the_report_explains_the_confidence_and_the_evidence():
    md = rg.report_markdown({**GRADE, "generated_by": "a@b", "inputs": {
        "documents": [{"name": "x.pdf", "section": "Financials", "engines": ["sarvam_docai"],
                       "used": True}]}})
    assert "64% = 60% × data coverage (70%" in md and "spread 9 points" in md
    assert "| A. Financial strength | 30% | 6/10 | 18 | Revenue ₹61 Cr / FY25 |" in md
    assert "| x.pdf | Financials | sarvam_docai | yes |" in md


def test_a_file_in_two_slots_is_read_once_and_actuals_come_first():
    rows = [{"id": "a", "title": "Projections / CMA data", "original_filename": "CMA.xls",
             "size_bytes": 10},
            {"id": "b", "title": "Audited financials", "original_filename": "BS FY25.pdf",
             "size_bytes": 20},
            {"id": "c", "title": "Debt profile", "original_filename": "Debt profile.xlsx",
             "size_bytes": 30}]
    assert [r["id"] for r in sorted(rows, key=rg.read_order)] == ["b", "c", "a"]


def test_news_must_name_the_firm_or_the_person():
    assert rg.name_core("Fractal Energy Private Limited") == "fractal energy"
    assert not rg.mentions("A fractal universe connects us all", "Fractal Energy")
    assert rg.mentions("Fractal Energy bags 40 MW EPC order", "Fractal Energy Pvt. Ltd.")
    assert rg.mentions("R. Kumar steps down as director", "R. Kumar", person=True)


def test_only_a_real_workbook_goes_to_docrag_as_xls():
    assert rg.docrag_suffix("application/vnd.ms-excel", OLE2 + b"x", "cma.xls") == ".xls"
    assert rg.docrag_suffix("application/vnd.ms-excel", b"a,b\n1,2", "cma.xls") is None
    assert rg.docrag_suffix("application/pdf", b"%PDF-1.4", "a.pdf") == ".pdf"


async def test_a_role_with_fewer_sections_never_gets_anothers_grade(monkeypatch):
    app = _grading_app(monkeypatch)
    await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    app.state.http = _Register(restricted=["lending"])
    got = await _call(app, "GET", "/v1/panorama/risk-grade",
                      params={"company": BODY["company"], "entity_id": "e-1"})
    assert got.status_code == 404


async def test_ai_host_errors_are_passed_on_by_name(monkeypatch):
    ai = _AiHost(502, {"error": {"title": "Grading failed",
                                 "detail": "the model did not return the rubric's JSON"}})
    app = _grading_app(monkeypatch, ai)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 502 and "rubric's JSON" in r.json()["error"]["detail"]
    r2 = await _call(app, "GET", "/v1/panorama/risk-grade",
                     params={"company": BODY["company"], "entity_id": "e-1"})
    assert r2.status_code == 404                     # a failure is not cached


async def test_without_the_ai_host_there_is_no_grade(monkeypatch):
    app = _app(monkeypatch)
    app.state.http = _Register()
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 409 and "WORKFLOWS_CHITTI_URL" in r.json()["error"]["detail"]


def test_trim_keeps_the_panorama_shape():
    t = rg.trim_panorama({"anchor": {"name": "X"}, "lending": [{"a": 1}] * 40})
    assert t["anchor"] == {"name": "X"} and len(t["lending"]) == 25


# --------------------------------------------------------------------------- bridge TLS
async def test_the_index_bridge_uploads_through_the_client_that_trusts_the_ai_host(monkeypatch):
    """DocRAG sits behind the AI host's private-CA edge; the default client fails every
    upload on TLS. The bridge must use app.state.docrag_http (the CAM's)."""
    monkeypatch.setenv("WORKFLOWS_DOCRAG_URL", "https://ai-host:8443/docrag")
    app = _app(monkeypatch)

    class _Reg:
        async def get(self, url, **kw):
            u = str(url)
            if u.endswith("/content"):
                return httpx.Response(200, content=b"%PDF-1.4",
                                      headers={"content-type": "application/pdf"},
                                      request=httpx.Request("GET", u))
            return httpx.Response(200, json={"items": [
                {"id": "d1", "original_filename": "GST.pdf"}]}, request=httpx.Request("GET", u))

        async def post(self, url, **kw):
            raise AssertionError("DocRAG upload went through the default client")

    class _DocRag:
        posted: list[str] = []

        async def post(self, url, **kw):
            self.posted.append(str(url))
            return httpx.Response(200, json={"id": "x", "duplicate": False},
                                  request=httpx.Request("POST", str(url)))

    app.state.http, app.state.docrag_http = _Reg(), _DocRag()
    r = await _call(app, "POST", "/v1/panorama/index-documents",
                    json={"entity_id": "entity-0001", "company": "Aapaavani"})
    assert r.status_code == 200, r.text
    assert [i["file"] for i in r.json()["indexed"]] == ["GST.pdf"]
    assert app.state.docrag_http.posted == ["https://ai-host:8443/docrag/v1/documents"]


async def test_the_report_renders_the_grade_on_screen_even_after_a_restart(monkeypatch):
    # A restart empties the saved grades; the screen still holds its grade and sends it.
    app = _grading_app(monkeypatch)
    r = await _call(app, "POST", "/v1/panorama/risk-grade/report",
                    json={"grade": {**GRADE, "generated_by": "bhavana@evamfinance.com"}})
    assert r.status_code == 200
    assert "Risk grade - Macwin Solar Energy Private Limited - 2026-09-29.docx" in \
        r.headers["content-disposition"]
    xml = zipfile.ZipFile(io.BytesIO(r.content)).read("word/document.xml").decode()
    assert "AMBER – PROVISIONAL" in xml and "64%" in xml
    bad = await _call(app, "POST", "/v1/panorama/risk-grade/report", json={"grade": {"x": 1}})
    assert bad.status_code == 422


async def test_a_regrade_with_nothing_new_returns_the_same_grade(monkeypatch):
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai)
    first = (await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)).json()
    again = (await _call(app, "POST", "/v1/panorama/risk-grade",
                         json={**BODY, "refresh": True})).json()
    # Same records, files and news: no second model run, the same grade, said plainly.
    assert len(ai.grades) == 1 and again["unchanged"] is True
    assert again["score"] == first["score"] and first["unchanged"] is False
    # A new file on the Data Register is new information: graded afresh.
    app.state.http = _Register(docs=[*DOCS, {"id": "d1", "section": "bank",
                                             "status": "On File", "title": "Bank statements",
                                             "original_filename": "HDFC 12m.pdf"}])
    fresh = (await _call(app, "POST", "/v1/panorama/risk-grade",
                         json={**BODY, "refresh": True})).json()
    assert len(ai.grades) == 2 and fresh["unchanged"] is False


def test_newest_audit_report_is_read_first():
    rows = [{"title": "Audited financials", "original_filename": "Audit Report F.Y. 2022'23.pdf"},
            {"title": "", "original_filename": "Audit Report F.Y. 2024'25_.pdf"},
            {"title": "", "original_filename": "Audit Report F.Y. 2023'24_.pdf"},
            {"title": "ITR acknowledgements", "original_filename": "USP_ITR F.Y.2024'25.pdf"}]
    assert [r["original_filename"][:24] for r in sorted(rows, key=rg.read_order)] == [
        "Audit Report F.Y. 2024'2", "Audit Report F.Y. 2023'2", "Audit Report F.Y. 2022'2",
        "USP_ITR F.Y.2024'25.pdf"]
