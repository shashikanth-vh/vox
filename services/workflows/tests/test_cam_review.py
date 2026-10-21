"""The credit team's review of the first real staged CAM (Pulze Batteries, Oct 2026).

Each test pins one finding: the bank-statement analyses read as "empty templates", the
locked figures that broke the balance sheet, terms that drifted from the term sheet, a
citation on every cell, annexures of "not on record" rows, figures no source has, the
Word tables, and the move to Sarvam glm5.3 at reasoning effort high.
"""

from __future__ import annotations

import io
import re
import zipfile

from test_cam_glm_docrag import _fake_http
from test_cam_staged import MASTER_V2, _Fake

from app import cam as cam_mod
from app.cam_pipeline import (
    DigestCache,
    SourceDoc,
    compact_checks,
    doc_type,
    draft_staged,
    figure_index,
    fit_for,
    focus_workbook,
    summarise_ledgers,
    claims_missing,
    parse_master_prompt,
    prune_annexure,
    terms_drift,
    tidy_citations,
    untraced_figures,
    wanted,
)
from app.cam_ratios import Figures, Period, consistency
from app.config import get_settings


# ------------------------------------------------------------- bank-statement analyses
def _bsa(rows: int = 3000) -> str:
    ledger = "\n".join(f"| 2026-04-{n % 28 + 1:02d} | NEFT CR CUSTOMER {n} | 1,000.00 | |"
                       for n in range(rows))
    return ("# BOI - BSA_Report\n\n## Sheet: Account Details\n| Bank | BOI |\n| Limit | 2,50,00,000 |\n"
            f"\n## Sheet: Bank Statement\n| Date | Narration | Credit | Debit |\n|---|---|---|---|\n"
            f"{ledger}\n"
            "\n## Sheet: Overview-Month Wise\n| Month | Credits | Debits |\n| Apr-25 | 2,12,00,000 | 2,10,00,000 |\n"
            "\n## Sheet: Cheque Return\n| Date | Amount |\n| 12-May-25 | 4,50,000 |\n")


def test_a_bank_statement_analysis_is_summarised_by_prism_not_read_row_by_row():
    # Pulze: the BSA's 6,000-row ledger filled every digest part, the month-wise overview
    # was never reached, and the CAM called three real reports "empty templates".
    text, note = summarise_ledgers(_bsa())
    assert len(text) < 20_000
    assert "### PRISM ledger summary — Bank Statement (computed by PRISM from 3,000 rows" in text
    assert "| 2026-04 | 3000 | 3,000,000.00 | 0.00 |" in text      # exact sums, in code
    assert "Overview-Month Wise" in text and "Cheque Return" in text and "4,50,000" in text
    assert "2,960 more rows summarised above, not read" in text
    assert note == "3,000 rows of 'Bank Statement' summarised month by month by PRISM"


def _pdf_statement(pages: int = 6, per_page: int = 40) -> str:
    """Any bank's PDF: the header repeats on every page, balances as 'Dr', dd/mm/yyyy."""
    out, bal = [], -10_000_000.0
    for p in range(pages):
        out += [f"[page {p + 1}]", "", "|SNO|TRAN DATE|DESCRIPTION|DEBITS|CREDITS|BALANCE|",
                "|---|---|---|---|---|---|"]
        for r in range(per_page):
            n = p * per_page + r
            mon = 4 + n // 120
            cr, dr = (50000.0, 0.0) if n % 2 else (0.0, 20000.0)
            bal += cr - dr
            narr = "CHQ RETURN INSUFF FUNDS" if n == 7 else f"NEFT/CUSTOMER {n}"
            out.append(f"|{n}|{n % 28 + 1:02d}/{mon:02d}/2025|{narr}|{dr:.2f}|{cr:.2f}|"
                       f"{abs(bal):.2f} Dr.|")
        out.append("")
    return "\n".join(out)


def test_any_banks_statement_is_one_ledger_across_its_pages():
    text, note = summarise_ledgers(_pdf_statement())
    assert note == "240 rows of 'transactions' summarised month by month by PRISM"
    m = re.search(r"\| 2025-04 \| 120 \| ([\d,.]+) \| ([\d,.]+) \|", text)
    assert m and m.group(1) == "3,000,000.00" and m.group(2) == "1,200,000.00"
    assert "| 2025-05 | 120 |" in text
    april = next(ln for ln in text.splitlines() if ln.startswith("| 2025-04"))
    assert april.endswith("| 1 |")                               # the cheque return
    assert "-" in april.split("|")[5]                            # Dr balance is negative
    assert "[page 6]" in text                                    # page markers survive


def test_a_subset_sheet_is_its_own_ledger_and_a_short_listing_is_left_as_read():
    # A BSA's "Loan and WC Tranx" lists rows the statement already has: never summed in.
    bsa = _bsa() + ("\n## Sheet: Loan and WC Tranx\n| Date | Narration | Debit | Credit |\n"
                    "|---|---|---|---|\n| 2026-04-05 | TL EMI | 1,10,000.00 | |\n")
    text, _note = summarise_ledgers(bsa)
    assert "| 2026-04 | 3000 | 3,000,000.00 | 0.00 |" in text
    assert "| 2026-04-05 | TL EMI | 1,10,000.00 | |" in text    # short: kept whole
    assert summarise_ledgers("| Date | Debit |\n|---|---|\n| 01-04-2025 | 5 |") == (
        "| Date | Debit |\n|---|---|\n| 01-04-2025 | 5 |", None)


def test_a_workbook_puts_short_sheets_before_its_bulk_listings_whatever_their_names():
    rows = "\n".join(f"| item {n} | {n} | some long description of the row {n} |"
                      for n in range(1200))
    wb = ("# Vendor X export\n## Sheet: Sheet1\n| a | b | c |\n|---|---|---|\n" + rows
          + "\n## Sheet: Sheet2\n| Month | Total |\n| Apr | 9,000 |\n")
    text, note = focus_workbook(wb)
    assert text.index("## Sheet: Sheet2") < text.index("## Sheet: Sheet1")
    assert "more rows not read" in text and "| Apr | 9,000 |" in text
    assert note and "sheet 'Sheet1' (1,203 rows) not read line by line" in note
    whole = "## Sheet: A\n" + "x\n" * 30_000 + "## Sheet: B\n| a |"
    assert focus_workbook(whole) == (whole, None)                # no listing: untouched
    assert focus_workbook("[page 1]\nRevenue 120") == ("[page 1]\nRevenue 120", None)


async def test_the_summary_is_named_on_the_documents_fate_and_a_duplicate_is_read_once():
    fake = _Fake()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                             docs=[SourceDoc("b1", "BOI BSA", _bsa(), "", 22),
                                   SourceDoc("b2", "BOI BSA (copy)", _bsa(), "", 22)],
                             analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    notes = {n["doc_id"]: n for n in res.notes if n.get("doc_id")}
    assert "summarised month by month by PRISM" in notes["b1"]["note"]
    assert notes["b2"]["note"] == "identical to 'BOI BSA' — read once"
    assert sum("Produce the FACT SHEET" in u for _s, u in fake.calls) == 1


# ---------------------------------------------------- documents the section is about
def test_fact_sheets_say_what_they_are_and_sections_get_their_own_whole():
    assert doc_type("DOCUMENT TYPE: bank statement analysis\n| x |") == "bank statement analysis"
    assert doc_type("**DOCUMENT TYPE:** GST return") == "GST return"
    assert doc_type("no type line", "IP Term Sheet_Pulze Batteries") == "term sheet"
    assert doc_type("DOCUMENT TYPE: other", "PLP Company Profile") == "company profile"
    assert "bank statement analysis" in wanted("SECTION 7: BANK STATEMENTS, GST & ITR ANALYSIS")
    assert "term sheet" in wanted("APPENDIX: TERMS AND CONDITIONS")
    sheets = [("other", "o" * 50_000), ("bank statement analysis", "b" * 30_000)]
    bank, other = fit_for(sheets, 60_000, {"bank statement analysis"})
    assert bank == "b" * 30_000                       # the section's own sheet stays whole
    assert "clipped to fit" in other
    plain = fit_for(sheets, 60_000, set())
    assert "clipped" in plain[0] and "clipped" in plain[1]   # longest-first, as before


# --------------------------------------------------------------------- citations
NAMES = ["IT FY 22-23 FINAL", "W-630215792", "IP Term Sheet_Pulze Batteries",
         "App_3998679368_3998679368_Cons_CIR_SiddhaarthAB"]


def test_tables_get_one_source_line_instead_of_a_tag_per_cell():
    md = ("| Facility | Lender | DPD | Source |\n|---|---|---|---|\n"
          "| CC | BOI | 0 | [W-630215792, p.14] |\n"
          "| TL | BOI | 0 [source: W-630215792, page 11] | W-630215792, p.11 |\n"
          "| CIR | — | 775 [App_3998679368_Cons_CIR_SiddhaarthAB, page 1] | — |")
    out = tidy_citations(md, NAMES)
    assert "Source |" not in out and "[" not in out
    assert out.splitlines()[0] == "| Facility | Lender | DPD |"
    assert out.splitlines()[3] == "| TL | BOI | 0 |"
    assert out.splitlines()[-1] == ("*Source: W-630215792; "
                                    "App_3998679368_Cons_CIR_SiddhaarthAB.*")


def test_prose_tags_merge_into_one_and_other_brackets_stay():
    line = ("Revenue was Rs. 1,372.27 lakh [IT FY 22-23 FINAL, p.4] and the CMR is 3 "
            "[source: W-630215792, page 1] [analyst to confirm].")
    out = tidy_citations(line, NAMES)
    assert out == ("Revenue was Rs. 1,372.27 lakh and the CMR is 3 [analyst to confirm] "
                   "[source: IT FY 22-23 FINAL, p.4; W-630215792, page 1].")
    assert tidy_citations("## SECTION 7 [source: W-630215792]", NAMES).startswith("## SECTION 7 [")


# --------------------------------------------------------------------- annexures
def test_annexures_drop_empty_rows_and_name_what_does_not_apply():
    md = ("## ANNEXURE I: BORROWER PROFILE\n\n### Company Information\n\n"
          "| Parameter | Details |\n|---|---|\n| Legal Name | Pinnacle Lithium Power |\n"
          "| Branch (Evam) | Not on record |\n| Group Name | — |\n| Email | NA (not provided) |\n\n"
          "### Shareholding Pattern\n\n*Not applicable — the borrower is a partnership firm.*\n\n"
          "### Directors\n\n| Name | DIN |\n|---|---|\n| A | Not applicable |\n")
    out = prune_annexure(md)
    assert "Legal Name" in out and "Branch" not in out and "Group Name" not in out
    assert "Email" not in out and "| A |" not in out
    assert "### Shareholding Pattern" not in out and "### Directors" not in out
    # A table of nothing but "not applicable" goes the same way as the one-line note.
    assert out.rstrip().endswith(
        "*Not applicable to this borrower: Shareholding Pattern; Directors.*")


def test_public_checks_not_performed_become_one_line_for_the_analyst():
    table = ("## Public information checks\n\n| Check | Source / query | Status | Finding |\n"
             "| --- | --- | --- | --- |\n"
             "| News search — borrower | PULSE | Verified via PULSE, 2026-10-13 | 2 stories |\n"
             "| Litigation — borrower entity | eCourts | NOT PERFORMED — to be completed by the analyst | |\n"
             "| Litigation — Promoter 1 (A) | eCourts | NOT PERFORMED — to be completed by the analyst | |\n"
             "| Listed peer 2 — results | NSE | NOT PERFORMED — to be completed by the analyst | |\n"
             "| Tracxn | Tracxn | Not available — not configured | |")
    out = compact_checks(table)
    assert "| News search — borrower | PULSE | Verified via PULSE" in out
    assert "NOT PERFORMED —" not in out and "| Tracxn" not in out
    assert out.endswith("**Not performed by PRISM — for the analyst to complete:** "
                        "Litigation ×2; Listed peer; Tracxn.")


# ------------------------------------------------------------- figures no source has
def test_figures_are_traced_with_unit_conversions_and_totals_are_the_sections_own():
    idx = figure_index("Revenue 1,37,22,700 (rupees)", "| FY26 | 3,080.69 |",
                       "Facility amount: INR 1,00,00,000")
    md = ("## SECTION 6\n\nRevenue of Rs. 137.23 lakh and Rs. 30.81 crore; the line is "
          "Rs. 1 crore; capacity of 2,500 units.\n\n| Customer | Value |\n|---|---|\n"
          "| Haul | 1,391.04 |\n| **Total** | 2,414.20 |\n")
    assert untraced_figures(md, idx) == ["1,391.04"]


# ------------------------------------------------------------------ term-sheet terms
TERM_SHEET = ("Interest Rate | Fixed interest rate of 15.00 % P.A.\n"
              "Principal repayment | Bullet repayment on the maturity date of each drawdown/tranche.\n"
              "Governing Law / Jurisdiction | Indian law; jurisdiction at Bengaluru/Mumbai\n"
              "Financial Covenants | To be decided upon final sanction")


def test_appendix_terms_that_are_not_the_term_sheets_words_are_named():
    appendix = ("## APPENDIX: TERMS AND CONDITIONS\n\n| Head | Terms |\n|---|---|\n"
                "| Interest Rate | Fixed interest rate of 15.00 % P.A. [source: TS, page 1] |\n"
                "| Principal repayment | Payable on demand; no scheduled amortisation |\n"
                "| Governing Law | Laws of India; courts in Mumbai / Tamil Nadu (as agreed) |\n"
                "| Effective Yield | ~16.5–17.0% p.a. |\n"
                "| Financial Covenants | To be decided upon final sanction |")
    drift = terms_drift(appendix, TERM_SHEET)
    assert [d.split(":")[0] for d in drift] == ["Principal repayment", "Governing Law",
                                                 "Effective Yield"]
    assert "16.5, 17.0 not in the term sheet" in drift[2]


# ----------------------------------------------------------- balance-sheet identities
def test_the_pulze_locked_figures_break_the_balance_sheet():
    fy24 = Period("FY24", "audited", {"total_outside_liabilities": 271.12,
                                      "current_liabilities": 271.12, "term_debt": 128.45,
                                      "wc_debt": 128.45, "promoter_unsecured_loans": 0.0})
    flags = consistency(Figures([fy24]))
    assert any("total outside liabilities equal current liabilities (271.12)" in f for f in flags)
    assert any("same figure (128.45)" in f for f in flags)
    ok = Period("FY24", "audited", {"total_outside_liabilities": 400, "current_liabilities": 271,
                                    "term_debt": 128, "wc_debt": 100, "pat": 10, "pbdit": 50,
                                    "current_assets": 300, "trade_receivables": 100,
                                    "inventory": 150})
    assert consistency(Figures([ok])) == []


BROKEN = ('```locked-figures\n{"periods": [{"period": "FY24", "basis": "audited", '
          '"net_sales": 1843.62, "term_debt": 128.45, "wc_debt": 128.45}]}\n```')
FIXED = ('```locked-figures\n{"periods": [{"period": "FY24", "basis": "audited", '
         '"net_sales": 1843.62, "term_debt": 128.45, "wc_debt": 96.1}]}\n```')


class _Reviewing(_Fake):
    """A dataset step that misreads once, and a section that states a figure no
    source has until it is told which one."""

    def __init__(self) -> None:
        super().__init__()
        self.fixes = 0

    async def __call__(self, system: str, turns):  # noqa: ANN001
        user = turns[0]["content"]
        if "PRISM checked the locked figures" in user:
            self.fixes += 1
            self.calls.append((system, user))
            return ("## Financial dataset workpaper\n| Net sales | FY24 | 1,843.62 |\n"
                    "| Term debt | FY24 | 128.45 |\n| WC debt | FY24 | 96.1 |\n\n" + FIXED)
        if "Build the FINANCIAL DATASET" in user:
            self.calls.append((system, user))
            return ("## Financial dataset workpaper\n| Net sales | FY24 | 1,843.62 |\n"
                    "| Term debt | FY24 | 128.45 |\n\n" + BROKEN)
        if "figures in no source: 9,999.99" in user:
            self.calls.append((system, user))
            return "## SECTION 2: MANAGEMENT PROFILE\n\nCapital of Rs. 154.18 lakh."
        if "this part of the CAM: SECTION 2" in user:
            self.calls.append((system, user))
            return "## SECTION 2: MANAGEMENT PROFILE\n\nCapital of Rs. 9,999.99 lakh."
        return await super().__call__(system, turns)


def _pulze_docs() -> list[SourceDoc]:
    return [SourceDoc("ts", "IP Term Sheet_Pulze Batteries", TERM_SHEET, "", 3),
            SourceDoc("bs", "FY26 BSheet", "[page 1]\nPartners capital 154.18", "", 1)]


async def test_a_broken_identity_is_sent_back_once_and_a_wrong_figure_is_repaired():
    fake = _Reviewing()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                             docs=_pulze_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    assert fake.fixes == 1
    assert res.figures["periods"][0]["wc_debt"] == 96.1           # the correction was kept
    assert "9,999.99" not in res.draft_md and "154.18 lakh" in res.draft_md
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["Figures traced to the sources (every section)"]["status"] == "pass", by["Figures traced to the sources (every section)"]
    fix_ask = next(u for _s, u in fake.calls if "PRISM checked the locked figures" in u)
    assert "same figure (128.45)" in fix_ask


async def test_the_term_sheet_is_quoted_verbatim_where_terms_are_written():
    fake = _Fake()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                             docs=_pulze_docs(), analyst_notes="", cache=DigestCache(),
                             model_id="m", progress=lambda *a: None)
    asks = {re.search(r"this part of the CAM: (.+)", u).group(1).split(" (")[0]: u
            for _s, u in fake.calls if "Write ONLY this part" in u}
    appendix = asks["APPENDIX: TERMS AND CONDITIONS"]
    assert "THE TERM SHEET (verbatim" in appendix and "Bengaluru/Mumbai" in appendix
    assert "The appendix is the TERM SHEET, transcribed" in appendix
    assert "Term sheet table (Head | Terms)." not in appendix      # its spec is replaced
    assert "quoted from the TERM SHEET exactly" in asks["SECTION 4: RECOMMENDATIONS"]
    assert "THE TERM SHEET (verbatim" not in asks["SECTION 2: MANAGEMENT PROFILE"]
    assert all("Cite sparingly" in u for u in asks.values())
    assert any(c["check"] == "Appendix terms match the term sheet" for c in res.qa["checks"])


# ------------------------------------------------------------------ the Word tables
def test_cam_tables_span_the_page_with_sized_columns_and_small_type():
    from app.docx_out import markdown_to_docx

    md = ("## SECTION 5\n\n| Particulars | FY24 (A) | FY25 (A) |\n|---|---|---|\n"
          "| Net sales from operations (Rs. lakh) | 1,843.62 | 1,605.10 |\n\n"
          "*Source: IT FY 23-24 FINAL.*")
    with zipfile.ZipFile(io.BytesIO(markdown_to_docx(md, cam=True))) as z:
        doc = z.read("word/document.xml").decode()
    assert '<w:tblW w:w="5000" w:type="pct"/><w:tblLayout w:type="fixed"/>' in doc
    widths = [int(w) for w in re.findall(r'<w:gridCol w:w="(\d+)"/>', doc)]
    assert len(widths) == 3 and widths[0] > widths[1] and abs(sum(widths) - 9638) < 5
    assert "<w:tblHeader/>" in doc and "<w:cantSplit/>" in doc
    assert '<w:sz w:val="18"/>' in doc and '<w:jc w:val="right"/>' in doc
    assert 'w:after="0" w:line="240"' in doc
    assert '<w:color w:val="595959"/><w:sz w:val="16"/>' in doc     # the source line


# --------------------------------------------------------------- Sarvam glm5.3, high
async def test_glm53_is_sent_its_reasoning_effort():
    eng = cam_mod.OpenAICompatEngine("glm5.3", "https://api.sarvam.ai/v2", "sk",
                                     max_tokens=32000, provider="sarvam",
                                     token_param="max_tokens", streaming=False,
                                     extra_body={"reasoning_effort": "high"})
    seen: dict = {}
    out = await eng.generate(_fake_http(
        ctype="application/json", seen=seen,
        body=b'{"choices":[{"message":{"content":"CAM","reasoning_content":"..."},'
             b'"finish_reason":"stop"}]}'), "SYS", [{"role": "user", "content": "go"}])
    assert out == "CAM" and seen["url"] == "https://api.sarvam.ai/v2/chat/completions"
    assert seen["json"]["reasoning_effort"] == "high" and seen["json"]["max_tokens"] == 32000


async def test_a_budget_spent_on_reasoning_is_an_error_not_an_empty_answer():
    # Sarvam's glm5.3 answers HTTP 200, finish "length", content null when its reasoning
    # uses the whole budget: continuing from nothing would only spend it again.
    eng = cam_mod.OpenAICompatEngine("glm5.3", "https://api.sarvam.ai/v2", "sk",
                                     max_tokens=2000, provider="sarvam",
                                     token_param="max_tokens", streaming=False)
    http = _fake_http(ctype="application/json",
                      body=b'{"choices":[{"message":{"content":null},"finish_reason":"length"}]}')
    try:
        await eng.generate(http, "SYS", [{"role": "user", "content": "go"}])
    except RuntimeError as exc:
        assert "whole output budget (2000 tokens) on reasoning" in str(exc)
    else:
        raise AssertionError("an empty answer was accepted")


def test_the_sarvam_glm53_engine_follows_config(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_CAM_ENGINE", "sarvam:glm5.3")
    monkeypatch.setenv("WORKFLOWS_SARVAM_API_KEY", "sk")
    monkeypatch.setenv("WORKFLOWS_SARVAM_BASE_URL", "https://api.sarvam.ai/v2")
    monkeypatch.setenv("WORKFLOWS_SARVAM_MAX_TOKENS", "32000")
    monkeypatch.setenv("WORKFLOWS_SARVAM_REASONING_EFFORT", "HIGH")
    get_settings.cache_clear()
    eng = cam_mod.build_engine(get_settings())
    assert isinstance(eng, cam_mod.OpenAICompatEngine) and eng.name == "sarvam:glm5.3"
    assert eng.base_url == "https://api.sarvam.ai/v2" and eng.max_tokens == 32000
    assert eng.extra_body == {"reasoning_effort": "high"}
    monkeypatch.setenv("WORKFLOWS_SARVAM_REASONING_EFFORT", "")
    get_settings.cache_clear()
    assert cam_mod.build_engine(get_settings()).extra_body == {}
    get_settings.cache_clear()


# --------------------------------------------- claims that a supplied document is missing
def test_a_section_calling_a_supplied_kind_of_document_missing_is_caught():
    present = {"bank statement analysis": ["BOI BSA"], "bank statement": ["BOI BSA"],
               "legal / litigation report": ["ScoreMe_LegalReport"]}
    md = ("## SECTION 7\n\nThe BSA reports are empty templates with no populated data. "
          "GST returns (GSTR-3B) are absent.\n\n| Check | Finding |\n|---|---|\n"
          "| Legal | Legal report provided is an empty template |\n")
    found = claims_missing(md, present)
    assert len(found) == 2
    assert "but bank statement analysis on record: BOI BSA" in found[0]
    assert "but legal / litigation report on record: ScoreMe_LegalReport" in found[1]
    # GST was truly not supplied: saying so is right, not a finding.
    assert not any("GST" in f for f in found)


class _SaysMissing(_Fake):
    """A writer that calls the bank statements missing until it is told they are not."""

    async def __call__(self, system: str, turns):  # noqa: ANN001
        user = turns[0]["content"]
        if "Produce the FACT SHEET" in user and "Bank statement" in user:
            self.calls.append((system, user))
            return "DOCUMENT TYPE: bank statement\n| Credits | Apr-25 | 98.0 |"
        if "document called missing" in user:
            self.calls.append((system, user))
            return "## SECTION 2: MANAGEMENT PROFILE\n\nBank credits ran at Rs. 98.0 lakh."
        if "this part of the CAM: SECTION 2" in user:
            self.calls.append((system, user))
            return "## SECTION 2: MANAGEMENT PROFILE\n\nBank statements were not provided."
        return await super().__call__(system, turns)


async def test_the_writer_is_told_once_that_the_document_it_called_missing_was_read():
    from test_cam_staged import _docs

    fake = _SaysMissing()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    assert "not provided" not in res.draft_md and "Rs. 98.0 lakh" in res.draft_md
    ask = next(u for _s, u in fake.calls if "document called missing" in u)
    assert "but bank statement on record: Bank statement.pdf" in ask
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["No supplied document called missing"]["status"] == "pass"


class _CallsItEmpty(_Fake):
    async def __call__(self, system: str, turns):  # noqa: ANN001
        user = turns[0]["content"]
        if "Produce the FACT SHEET" in user:
            self.calls.append((system, user))
            if "this document is NOT empty" in user:
                return "DOCUMENT TYPE: legal / litigation report\n| Cases | 0 |"
            return "DOCUMENT TYPE: legal / litigation report\nThe report is an empty template."
        return await super().__call__(system, turns)


async def test_a_fact_sheet_that_calls_a_full_document_empty_is_read_again():
    full = "\n".join(f"| Court {n} | {n * 7} | {n * 13}.50 |" for n in range(1, 80))
    fake = _CallsItEmpty()
    await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                       docs=[SourceDoc("l1", "Legal report", full, "", 13)], analyst_notes="",
                       cache=DigestCache(), model_id="m", progress=lambda *a: None)
    digests = [u for _s, u in fake.calls if "Produce the FACT SHEET" in u]
    assert len(digests) == 2 and "it holds" in digests[1]


def test_a_small_amount_in_prose_is_checked_too():
    # The Pulze draft's "the first Rs. 2 crore facility" was the prompt exemplar's figure.
    idx = figure_index("Facility amount: INR 1,00,00,000")
    md = "## PAGE 1\n\nThe first Rs. 2 crore facility opens the door; ours is Rs. 1 crore."
    assert untraced_figures(md, idx) == ["2"]


async def test_qa_warns_when_no_term_sheet_was_supplied():
    from test_cam_staged import _docs

    res = await draft_staged(generate=_Fake(), system="S", prompt=parse_master_prompt(MASTER_V2),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["Appendix terms match the term sheet"]["status"] == "warn"
    assert "no term sheet among the documents" in by["Appendix terms match the term sheet"]["detail"]


def test_rows_with_shifted_columns_are_never_summed_and_the_summary_says_partial():
    # BOI's PDF: a narration holding "|" pushed an account number into the debit column
    # and the month's debits came out at Rs. 1.2e16.
    text = _pdf_statement()
    lines = text.splitlines()
    n = next(i for i, ln in enumerate(lines) if ln.startswith("|5|"))
    lines[n] = "|5|06/04/2025|UPI/944|230693/Payme|n|0000000000000000|1.00 Dr.|"
    out, note = summarise_ledgers("\n".join(lines))
    assert "PARTIAL: 1 row(s) with shifted columns are not in these sums" in out
    assert "| 2025-04 | 119 |" in out and "e+" not in out
    many = "\n".join(ln.replace("|DEBITS|", "|DEBITS|X|") if "|SNO|" in ln else ln
                     for ln in text.splitlines())      # every row now one cell short
    same, why = summarise_ledgers(many)
    assert "PRISM ledger summary" not in same and "not summarised" in why


async def test_a_budget_spent_reasoning_at_high_is_asked_once_more_at_low():
    # Pulze's 95-page BOB statement: 32,000 tokens of reasoning at "high", no answer.
    sent: list[dict] = []

    class _Http:
        def stream(self, method, url, **kw):  # noqa: ANN001, ANN003
            sent.append(kw["json"])
            body = (b'{"choices":[{"message":{"content":null},"finish_reason":"length"}]}'
                    if len(sent) == 1 else
                    b'{"choices":[{"message":{"content":"FACT SHEET"},"finish_reason":"stop"}]}')
            return _fake_http(ctype="application/json", body=body).stream(method, url, **kw)

    eng = cam_mod.OpenAICompatEngine("glm5.3", "https://api.sarvam.ai/v2", "sk",
                                     max_tokens=32000, provider="sarvam",
                                     token_param="max_tokens", streaming=False,
                                     extra_body={"reasoning_effort": "high"})
    assert await eng.generate(_Http(), "SYS", [{"role": "user", "content": "go"}]) == "FACT SHEET"
    assert [b["reasoning_effort"] for b in sent] == ["high", "low"]
    assert eng.extra_body == {"reasoning_effort": "high"}     # the next request is "high" again


# ------------------------------------------------------------------- speed (Oct 2026)
class _Efforts(_Fake):
    """Records the effort each call was made at."""

    def __init__(self) -> None:
        super().__init__()
        self.efforts: list[tuple[str, str | None]] = []

    async def __call__(self, system: str, turns, effort=None):  # noqa: ANN001
        user = turns[0]["content"]
        kind = "digest" if "Produce the FACT SHEET" in user else "other"
        self.efforts.append((kind, effort))
        return await super().__call__(system, turns)


async def test_documents_are_read_at_low_effort_and_the_analysis_keeps_its_own():
    # 70% of a 39-minute CAM was reading documents at "high": extraction needs no
    # deliberation. The dataset, sections and repairs keep the engine's effort.
    fake = _Efforts()
    from test_cam_staged import _docs

    await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER_V2),
                       docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                       progress=lambda *a: None, digest_effort="low")
    assert {e for k, e in fake.efforts if k == "digest"} == {"low"}
    assert {e for k, e in fake.efforts if k == "other"} == {None}


async def test_fact_sheets_survive_a_restart_on_disk(tmp_path):
    from test_cam_staged import _docs

    first, second = _Fake(), _Fake()
    mp = parse_master_prompt(MASTER_V2)
    await draft_staged(generate=first, system="S", prompt=mp, docs=_docs(), analyst_notes="",
                       cache=DigestCache(directory=str(tmp_path)), model_id="m",
                       progress=lambda *a: None)
    # A new process: a new cache object over the same directory.
    await draft_staged(generate=second, system="S", prompt=mp, docs=_docs(), analyst_notes="",
                       cache=DigestCache(directory=str(tmp_path)), model_id="m",
                       progress=lambda *a: None)
    assert sum("Produce the FACT SHEET" in u for _s, u in first.calls) == 2
    assert sum("Produce the FACT SHEET" in u for _s, u in second.calls) == 0
    # Two fact sheets and the locked dataset: a second draft spends nothing on either.
    assert len(list(tmp_path.glob("*.md"))) == 3
    assert not any("Build the FINANCIAL DATASET" in u for _s, u in second.calls)


def test_an_unwritable_cache_directory_falls_back_to_memory(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    cache = DigestCache(directory=str(blocked / "sub"))
    assert cache.dir is None
    cache.put("k", "v")
    assert cache.get("k") == "v"


def test_a_kind_and_an_absence_far_apart_in_one_sentence_are_not_a_claim():
    # Pulze, Section 2: KYC complete, "an executed CIBIL consent", then the net-worth
    # annexure "left blank" 200 characters on — not a claim about the bureau report.
    present = {"credit bureau report": ["App_..._CIR_PranavSKumar"]}
    md = ("His identity KYC is complete on record — PAN card, Aadhaar and an executed CIBIL "
          "consent dated 20-Jul-2026 — but the same cannot be said of his financial "
          "disclosures: his net-worth annexure is a liabilities-side declaration whose "
          "schedules are all left blank rather than marked nil.")
    assert claims_missing(md, present) == []
    assert claims_missing("The CIBIL report is not provided.", present)



# ------------------------------------------------- which documents to read (Oct 2026)
def _stmt(months: list[str], header: str = "") -> str:
    rows = "\n".join(f"| {m} | 10 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 |" for m in months)
    return f"{header}\n### PRISM ledger summary — transactions\n\n{rows}\n"


def test_a_statement_an_analysis_covers_is_left_out_and_named():
    from app.cam_pipeline import select_documents

    bsa = SourceDoc("a", "BOI - x_BSA_Report", _stmt(["2025-04", "2025-05", "2025-06"]))
    covered = SourceDoc("b", "REPORT (2)", _stmt(["2025-04", "2025-05"], "BANK OF INDIA"))
    beyond = SourceDoc("c", "BOI Jul 2025", _stmt(["2025-06", "2025-07"]))
    consent = SourceDoc("d", "CIBIL Consent ARAVIND", "I consent to the bureau check.")
    audited = SourceDoc("e", "Audited FS FY25", "[page 1]\nRevenue 120")
    keep, notes = select_documents([bsa, covered, beyond, consent, audited])
    assert [d.doc_id for d in keep] == ["a", "c", "e"]
    by = {n["doc_id"]: n["note"] for n in notes}
    assert by["b"].startswith("not read: covered by 'BOI - x_BSA_Report'")
    assert "2025-04 to 2025-05" in by["b"]
    assert by["d"] == "not read: a consent form — on record, nothing to extract"


def test_a_banks_second_account_is_never_taken_as_covered():
    from app.cam_pipeline import select_documents

    bsa = SourceDoc("a", "Kotak BSA", _stmt(["2026-01", "2026-02"], "Account No: XX6885"))
    other = SourceDoc("b", "Kotak current account", _stmt(["2026-01"], "Account No: XX1122"))
    keep, _notes = select_documents([bsa, other])
    assert [d.doc_id for d in keep] == ["a", "b"]


async def test_a_left_out_statement_still_counts_as_on_record():
    bsa = SourceDoc("a", "BOI - x_BSA_Report", _stmt(["2025-04", "2025-05"]))
    covered = SourceDoc("b", "BOI statement Apr-May", _stmt(["2025-04"]))
    res = await draft_staged(generate=_SaysMissing(), system="S",
                             prompt=parse_master_prompt(MASTER_V2), docs=[bsa, covered],
                             analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    assert any(n.get("skipped") and n["doc_id"] == "b" for n in res.notes)


def test_a_missing_claim_has_a_shape_not_just_two_nearby_words():
    present = {"credit bureau report": ["CIR Pranav"], "bank statement analysis": ["BOI BSA"],
               "bank statement": ["BOI BSA"], "GST return": ["GSTR-3B FY25"],
               "order / PO": ["Book Orders.xlsx"]}
    real = ["The BSA reports are empty templates with no populated data.",
            "GST returns (GSTR-3B) are not provided.",
            "In the absence of bank statements, conduct cannot be judged.",
            "No CIBIL report was supplied for the entity."]
    quoted = ["The four consumer CIRs on record corroborate the borrowings but report income "
              "as \"Not Available\" in every case.",
              "His KYC and an executed CIBIL consent are on record, but his net-worth "
              "annexure is left blank.",
              "The Zencore lines carry no dates, and whether they are firm POs or "
              "enquiry-stage compilations is not on record."]
    assert all(claims_missing(x, present) for x in real), [x for x in real
                                                          if not claims_missing(x, present)]
    assert not any(claims_missing(x, present) for x in quoted)


async def test_a_bureau_report_that_reads_differently_does_not_cost_the_dataset(tmp_path):
    # DocRAG re-OCRs a page Sarvam left empty; the CIR's text varies between runs.
    from test_cam_staged import _docs

    class _Typed(_Fake):
        async def __call__(self, system, turns, effort=None):  # noqa: ANN001
            user = turns[0]["content"]
            if "Produce the FACT SHEET" in user:
                self.calls.append((system, user))
                kind = "credit bureau report" if "CIR" in user else "financial statements"
                return f"DOCUMENT TYPE: {kind}\n| x | {len(user)} |"
            return await super().__call__(system, turns)

    mp = parse_master_prompt(MASTER_V2)
    base = _docs()
    for n, cir in enumerate(["[page 1]\nScore 775", "[page 1]\nScore 775\n[page 24]\n…"]):
        fake = _Typed()
        await draft_staged(generate=fake, system="S", prompt=mp,
                           docs=[*base, SourceDoc("c", "CIR Pranav", cir)], analyst_notes="",
                           cache=DigestCache(directory=str(tmp_path)), model_id="m",
                           progress=lambda *a: None)
        ran = any("Build the FINANCIAL DATASET" in u for _s, u in fake.calls)
        assert ran is (n == 0)
