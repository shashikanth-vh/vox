"""The IRG reads everything on record (Oct 2026): every Data Register section, every page
of the listing, the Register's own records (interactions with their notes, the news
monitor's intelligence, financials, monitoring), a year of news — and the files are
prepared the way the CAM prepares them, with the same "called missing" check."""

from __future__ import annotations

import httpx
from test_cam_review import _pdf_statement
from test_cam_workbench import _call
from test_risk_grade import BODY, CONTENT, DOCS, GRADE, RECORDS, _AiHost, _grading_app, _Register

from app.risk_grade import clean_record, fit_records, kind_of, kinds_read


def _sent(ai: _AiHost) -> dict:
    [(_url, kw)] = ai.grades
    return kw["json"]


async def test_every_section_is_read_including_deal_and_project_documents(monkeypatch):
    docs = [*DOCS,
            {"id": "d6", "section": "deal", "status": "On File", "title": "Term sheet",
             "original_filename": "IP Term Sheet_Pulze Batteries.pdf"},
            {"id": "d7", "section": "proj", "status": "On File", "title": "Orders",
             "original_filename": "Book Orders.pdf"}]
    monkeypatch.setitem(CONTENT, "d6", ("application/pdf", b"%PDF-1.4 term sheet"))
    monkeypatch.setitem(CONTENT, "d7", ("application/pdf", b"%PDF-1.4 orders"))
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai, _Register(docs=docs))
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200, r.text
    names = {d["name"] for d in _sent(ai)["documents"]}
    assert {"IP Term Sheet_Pulze Batteries.pdf", "Book Orders.pdf"} <= names


class _Paged(_Register):
    """A Register whose document list runs past one page (the API's 100)."""

    async def get(self, url, **kw):  # noqa: ANN001, ANN003
        params = kw.get("params") or {}
        if str(url).endswith("/v1/documents") and params.get("entity_id") == "e-1":
            req = httpx.Request("GET", str(url))
            if params.get("cursor") == "p2":
                return httpx.Response(200, json={"items": DOCS[3:], "next_cursor": None},
                                      request=req)
            return httpx.Response(200, json={"items": DOCS[:3], "next_cursor": "p2"},
                                  request=req)
        return await super().get(url, **kw)


async def test_the_listing_is_read_to_its_last_page(monkeypatch):
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai, _Paged())
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200, r.text
    names = {d["name"] for d in _sent(ai)["documents"]}
    assert "Debtors Aging 30.6.25.csv" in names            # on the second page


async def test_the_registers_own_records_reach_the_grader_in_full(monkeypatch):
    ai = _AiHost()
    reg = _Register()
    app = _grading_app(monkeypatch, ai, reg)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200, r.text
    rec = _sent(ai)["records"]
    [meeting] = rec["interactions"]
    # The desk's notes, 1,500 characters — not the panorama's 300 — and no plumbing.
    assert len(meeting["notes"]) == 1501 and "id" not in meeting and "tenant_id" not in meeting
    # The news monitor's findings; a dismissed one is not evidence.
    assert [x["title"] for x in rec["intelligence"]] == ["CIRP admitted against a group company"]
    assert rec["financials"][0]["fiscal_year"] == "FY25"
    assert r.json()["inputs"]["records"] == {"interactions": 1, "intelligence": 1,
                                             "financials": 1, "monitoring": 0}
    # Financials are read as their current versions, each list for this company only.
    fin = [u for u in reg.reads if u.endswith("/v1/financials")]
    assert fin


async def test_news_is_a_year_wide_and_reads_more_people(monkeypatch):
    seen: list[dict] = []

    class _Pulse(_Register):
        async def get(self, url, **kw):  # noqa: ANN001, ANN003
            if "/v1/news/search" in str(url):
                seen.append(kw["params"])
            return await super().get(url, **kw)

    app = _grading_app(monkeypatch, _AiHost(), _Pulse())
    await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    from datetime import date, timedelta
    assert seen and all(p["from"] <= (date.today() - timedelta(days=360)).isoformat()
                        for p in seen)


async def test_a_new_interaction_is_a_reason_to_regrade(monkeypatch):
    ai = _AiHost()
    reg = _Register(records={k: list(v) for k, v in RECORDS.items()})
    app = _grading_app(monkeypatch, ai, reg)
    await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    again = await _call(app, "POST", "/v1/panorama/risk-grade", json={**BODY, "refresh": True})
    assert again.json()["unchanged"] is True
    reg.records["/v1/interactions"].insert(0, {"occurred_at": "2026-10-18", "summary": "Call",
                                               "notes": "BOI has issued a recall notice."})
    third = await _call(app, "POST", "/v1/panorama/risk-grade", json={**BODY, "refresh": True})
    assert third.json()["unchanged"] is False and len(ai.grades) == 2


async def test_a_bank_statement_is_summed_by_prism_before_it_is_budgeted(monkeypatch):
    statement = _pdf_statement().encode()
    docs = [{"id": "s1", "section": "bank", "status": "On File", "title": "Bank statement",
             "original_filename": "BOI CC Apr-Sep 2025.csv"}]
    monkeypatch.setitem(CONTENT, "s1", ("text/csv", statement))
    ai = _AiHost()
    app = _grading_app(monkeypatch, ai, _Register(docs=docs), docrag=False)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    assert r.status_code == 200, r.text
    [doc] = _sent(ai)["documents"]
    assert "PRISM ledger summary" in doc["text"] and "| 2025-04 | 120 |" in doc["text"]
    fate = next(f for f in r.json()["inputs"]["documents"] if f["name"].startswith("BOI CC"))
    assert "summarised month by month by PRISM" in fate["note"]


async def test_the_grader_is_told_what_it_read_and_a_missing_claim_is_named(monkeypatch):
    said = {**GRADE, "data_gaps": ["Bank statements were not provided"],
            "risks": ["Audited financials are missing"]}
    ai = _AiHost(body=said)
    app = _grading_app(monkeypatch, ai)
    r = await _call(app, "POST", "/v1/panorama/risk-grade", json=BODY)
    kinds = _sent(ai)["facts"]["kinds_read"]
    assert kinds["financial statements"] == ["Macwin Audited Fins FY 2024-25 signed.pdf"]
    claims = r.json()["inputs"]["claims_missing"]
    # Audited financials WERE read: named. No bank statement was: saying so is right.
    assert len(claims) == 1 and "financial statements on record" in claims[0]


def test_record_helpers():
    assert kind_of("BOI - a7ba_BSA_Report.xlsx") == "bank statement analysis"
    assert kind_of("App_3998_Cons_CIR_Siddhaarth.pdf") == "credit bureau report"
    assert kind_of("GSTR3B Apr-Sep.pdf") == "GST return"
    assert kind_of("Audited Financial Statements FY25.pdf") == "financial statements"
    assert kind_of("Account_01 Apr 2026 - 21 Jul 2026_XX6885-Kotak.pdf") == "bank statement"
    assert kind_of("PulzeBattery - CMA Format.xlsx", "Financials") == "CMA / projections"
    assert kind_of("lithium pan card.pdf") == "KYC"
    # A statement's name says nothing; PRISM's own ledger summary of it does. A
    # Banking & Debt slot alone does not (it also holds sanction letters).
    assert kind_of("REPORT (2)", "bank", "### PRISM ledger summary — transactions") == \
        "bank statement"
    assert kind_of("REPORT (2)", "bank", "| SNO | TRAN DATE |") is None
    assert kind_of("Term Loan 33.8L", "bank") == "sanction letter"
    assert kind_of("W-630215792", "comp", "Commercial Credit Report … CMR-3") == \
        "credit bureau report"
    fates = [{"name": "REPORT (2)", "used": True, "kind": "bank statement"},
             {"name": "x_BSA.xlsx", "used": False}]
    assert kinds_read(fates)["bank statement analysis"] == ["REPORT (2)"]
    assert clean_record({"id": "1", "notes": "n" * 2000, "x": None})["notes"].endswith("…")
    big = {"interactions": [{"n": "a" * 500}] * 10, "financials": [{"f": 1}]}
    kept, notes = fit_records(big, total=2_000)
    assert len(kept["financials"]) == 1 and 0 < len(kept["interactions"]) < 10
    assert notes and notes[0].startswith("interactions:")


def test_the_report_names_what_else_was_read_and_any_missing_claim():
    from app.risk_grade import report_markdown

    md = report_markdown({**GRADE, "evidence": {"coverage": [{"area": "Financials",
                                                             "status": "read", "read": ["a"]}]},
                          "inputs": {"records": {"interactions": 12, "intelligence": 3},
                                     "claims_missing": ["'Audited financials are missing' — but "
                                                        "financial statements on record: a"]}})
    assert "**Also read from the Register:** 12 interactions; 3 intelligence." in md
    assert "the grade calls these missing, but PRISM read them" in md


def test_a_data_gap_naming_a_kind_that_was_read_is_flagged():
    from app.risk_grade import grade_claims_missing

    g = {"data_gaps": ["Bank of India sanction letter for the 2.5 Cr facility",
                       "GST return filing status for the last 6 months"]}
    kinds = {"sanction letter": ["Pinnacle 1.6 cr", "pinnacle 90L"]}
    [claim] = grade_claims_missing(g, kinds)
    assert "sanction letter on record: Pinnacle 1.6 cr; pinnacle 90L" in claim
