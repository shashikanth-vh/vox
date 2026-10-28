"""The credit team's second review of a real CAM (Oct 2026): "too negative, as if nothing
is good"; management missing although a profile was supplied; news without clean
context. Each test pins one fix. Every firm, person and figure here is made up."""

from __future__ import annotations

import asyncio
import re

from test_cam_staged import LENDING, MASTER, _call, _Engine, _job_app, _poll

from app import cam as cam_mod
from app import risk_grade as rg
from app.cam_pipeline import (
    DigestCache,
    SourceDoc,
    doc_type,
    draft_staged,
    machine_qa,
    parse_master_prompt,
    section_task,
    wanted,
)
from app.docx_out import markdown_to_docx
from app.statement_math import misread_claims, statement_notes

# A provisional balance sheet and P&L as DocRAG reads Tally's layout (group totals with
# their ledger lines beneath, two sides in two cells) — the shape the review's misreads
# came from, with made-up figures.
BSHEET = """# Balance Sheet

1-Apr-25 to 31-Mar-26

|L i a b i l i t i e s as at 31-Mar-26|A s s e t s as at 31-Mar-26|
|---|---|
|Capital Account 60,00,000.00<br><br>Accounts (CA) - A 25,00,000.00 Accounts (CA) - B 35,00,000.00<br><br>Loans (Liability) 2,10,00,000.50<br><br>Bank OD A/c 1,20,00,000.25 Secured Loans 40,00,000.00 Unsecured Loan 50,00,000.25<br><br>Current Liabilities 1,60,50,000.00<br><br>Duties &amp; Taxes 50,000.00 Sundry Creditors 1,50,00,000.00 Provisions 10,00,000.00<br><br>Profit &amp; Loss A/c 30,00,000.00 Opening Balance Current Period 30,00,000.00<br><br>|Fixed Assets 90,50,000.50<br><br>Depreciation Reserve (-)10,00,000.00 Fixed Asset - Machinery 80,00,000.00 Fixed Asset - Pickup 6,00,000.50 Fixed Asset - Software 4,00,000.00 Fixed Asset - Others 10,50,000.00<br><br>Current Assets 3,70,00,000.00<br><br>Closing Stock 2,00,00,000.00 Deposits (Asset) 5,00,000.00 Sundry Debtors 1,50,00,000.00 Cash-in-Hand 5,00,000.00 Bank Accounts 10,00,000.00<br><br>|
|T o t a l 4,60,50,000.50|T o t a l 4,60,50,000.50|
"""
PANDL = """# Profit & Loss A/c

|Particulars|Particulars|
|---|---|
|Opening Stock 1,50,00,000.00|Sales Accounts 20,10,00,000.00<br><br>Export Sales 10,00,000.00 Accounts - Sales 20,00,00,000.00<br><br>Closing Stock 2,00,00,000.00<br><br>Closing Stock 1,75,00,000.00 Goods in Transit 25,00,000.00|
"""


# ------------------------------------------------------------ statement arithmetic
def test_a_tally_balance_sheet_is_read_as_groups_and_shown_to_tie():
    block, proof = statement_notes(BSHEET, "FY26 BSheet")
    assert proof is not None
    assert ("Loans (Liability) 2,10,00,000.50 = Bank OD A/c 1,20,00,000.25 + Secured Loans "
            "40,00,000.00 + Unsecured Loan 50,00,000.25") in block
    # A negative line ("(-)" depreciation reserve) still sums into its group.
    assert "Fixed Assets 90,50,000.50 = Depreciation Reserve (-)10,00,000.00" in block
    # Each printed total is matched to its own side, and the statement ties.
    ties = [ln for ln in block.splitlines() if "TIES" in ln]
    assert len(ties) == 2
    assert any("Fixed Assets" in t and "Current Assets" in t for t in ties)
    assert any("Capital Account" in t and "Profit & Loss A/c 30,00,000.00" in t for t in ties)


def test_the_sales_group_and_the_closing_stock_group_are_one_item_each():
    block, _proof = statement_notes(PANDL, "FY26 P&L")
    assert ("Sales Accounts 20,10,00,000.00 = Export Sales 10,00,000.00 + Accounts - Sales "
            "20,00,00,000.00") in block
    assert ("Closing Stock 2,00,00,000.00 = Closing Stock 1,75,00,000.00 + Goods in Transit "
            "25,00,000.00") in block


def test_a_document_without_groups_gets_no_arithmetic():
    # An ITR's many "Total" rows, a bank statement, a profile: nothing proven, nothing said.
    itr = "Balance Sheet\nTotal 1,20,000.00\nTotal 3,40,000.00\nGross receipts 12,500.00"
    assert statement_notes(itr, "ITR") == ("", None)
    assert statement_notes("Company profile. 5 MWh a month.", "profile") == ("", None)


def test_a_misread_is_caught_and_a_fair_point_is_not():
    statements = [statement_notes(BSHEET, "FY26 BSheet")[1],
                  statement_notes(PANDL, "FY26 P&L")[1]]
    draft = (
        "The provisional balance sheet does not balance: the lines sum to Rs. 660.5 lakh. "
        "Closing stock is printed twice, with a Rs. 25.0 lakh gap — a double-count. "
        "An unexplained Loans (Liability) line of Rs. 210.0 lakh appears. "
        # Fair: the unsecured loan's LENDER is not named; another schedule's footing.
        "The unsecured loan of Rs. 50.0 lakh is unattributed to any lender. "
        "The prior-year depreciation schedule does not foot to the balance sheet. "
        "Collections are concentrated in two buyers.")
    said = misread_claims(draft, [s for s in statements if s])
    assert len(said) == 3
    assert "does not balance" in said[0] and "ties" in said[0]
    assert "printed twice" in said[1] and "double count" in said[1]
    assert "210.0" in said[2] and "not unexplained" in said[2]


class _StatementFake:
    """Writes the review's misread into the workpaper and the financial section until it is
    told, by PRISM's arithmetic, that the statement ties."""

    def __init__(self, stubborn: bool = False) -> None:
        self.calls: list[str] = []
        self.stubborn = stubborn

    async def __call__(self, system: str, turns, **_kw):  # noqa: ANN001, ANN003
        user = turns[0]["content"]
        self.calls.append(user)
        await asyncio.sleep(0)
        wrong = "The provisional balance sheet does not balance."
        if "Produce the FACT SHEET" in user:
            if "DOCUMENT: Promoter" in user:
                return "DOCUMENT TYPE: other\nFour partners."
            return "DOCUMENT TYPE: financial statements\n| Revenue | FY26 | 3080.69 |"
        if "Write ONLY the CAM's TITLE PAGE" in user:
            return "Evam Finance Private Limited"
        if "Public information checks" in user and "Write ONLY" not in user:
            return "## Public information checks\n| Check | Source | Status | Finding |"
        if "Write ONLY this part of the CAM:" in user:
            title = re.search(r"Write ONLY this part of the CAM: (.+)", user).group(1)
            head = title.split(" (")[0]
            fixed = "statement misread" in user and not self.stubborn
            if "FINANCIAL ANALYSIS" in head and not fixed:
                return f"## {head}\n\n{wrong} Margins are thin."
            return f"## {head}\n\nThe balance sheet ties. Margins are thin."
        if "statement misread" in user and not self.stubborn:
            return "## Financial dataset workpaper\nThe statement ties as printed."
        return f"## Financial dataset workpaper\n{wrong}"


async def test_the_pipeline_shows_the_arithmetic_and_sends_a_misread_back():
    fake = _StatementFake()
    docs = [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1)]
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=docs, analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    digest = next(u for u in fake.calls if "Produce the FACT SHEET" in u)
    assert "===== PRISM ARITHMETIC: FY 26 - BSheet - Provisional.pdf" in digest
    # Every later reader gets the proof beside the fact sheet.
    section = next(u for u in fake.calls if "Write ONLY this part of the CAM: SECTION 3" in u)
    assert "the statement TIES to its printed total" in section
    # The dataset and the section that called the statement unbalanced were sent back.
    assert any("statement misread:" in u and "THE WORKPAPER" in u for u in fake.calls)
    assert any("statement misread:" in u and "YOUR DRAFT" in u for u in fake.calls)
    assert "does not balance" not in res.draft_md
    by = {c["check"]: c for c in res.qa["checks"]}
    assert by["Statements read as printed (PRISM's arithmetic)"]["status"] == "pass"


async def test_a_misread_that_survives_the_repair_fails_machine_qa():
    fake = _StatementFake(stubborn=True)
    docs = [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1)]
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=docs, analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    check = next(c for c in res.qa["checks"]
                 if c["check"] == "Statements read as printed (PRISM's arithmetic)")
    assert check["status"] == "fail" and "does not balance" in check["detail"]
    assert res.qa["passed"] is False


# ----------------------------------------------------------------- the writing rules
def test_every_narrative_section_is_told_to_analyse_not_accuse():
    mp = parse_master_prompt(MASTER)
    specs = {s.heading: s for s in mp.sections}
    mgmt = section_task(specs["SECTION 2: MANAGEMENT PROFILE"], True)
    assert "balanced credit analyst, not a prosecutor" in mgmt
    assert "four beats" in mgmt and "GREEN" in mgmt and "AMBER" in mgmt and "RED" in mgmt
    assert "Argue each concern ONCE" in mgmt
    assert "never treat age by itself as a risk" in mgmt
    assert "public-checks log, never in the risk narrative" in mgmt
    # Only the executive summary opens with the credit picture.
    assert "Credit picture at a glance" not in mgmt
    assert "Credit picture at a glance" in section_task(specs["SECTION 1: EXECUTIVE SUMMARY"])
    # Reference tables and the transcribed term sheet are not argued at all.
    annex = section_task(specs["ANNEXURE I: DEBT PROFILE"])
    assert "prosecutor" not in annex and "four beats" not in annex


async def test_every_document_is_read_for_strengths_as_well_as_gaps():
    fake = _StatementFake()
    docs = [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1)]
    await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                       docs=docs, analyst_notes="", cache=DigestCache(), model_id="m",
                       progress=lambda *a: None)
    digest = next(u for u in fake.calls if "Produce the FACT SHEET" in u)
    assert "Strengths & positive evidence" in digest
    assert "not a fault of the borrower" in digest


def test_qa_names_a_draft_with_red_and_no_green():
    mp = parse_master_prompt(MASTER)
    gloomy = ("## SECTION 1: EXECUTIVE SUMMARY\n\n| Factor | Signal |\n| --- | --- |\n"
              "| Leverage | RED — high |\n| Bureau | RED |\n\n| Risk | Severity |\n| --- | --- |\n"
              "| Concentration | High |\n")
    by = {c["check"]: c for c in machine_qa(mp, gloomy, "")["checks"]}
    sig = by["Signals (the credit picture's colours)"]
    assert sig["status"] == "warn" and "RED 2" in sig["detail"] and "no GREEN" in sig["detail"]
    assert "1 risk(s) rated High" in sig["detail"]
    fair = gloomy.replace("| Bureau | RED |", "| Bureau | GREEN — CMR-3, 0 DPD |")
    by = {c["check"]: c for c in machine_qa(mp, fair, "")["checks"]}
    assert by["Signals (the credit picture's colours)"]["status"] == "info"


def test_signal_cells_are_shaded_in_the_word_cam():
    md = ("| Factor | Signal | Evidence |\n| --- | --- | --- |\n"
          "| Bureau | GREEN — clean | CMR-3 |\n| Leverage | AMBER | 3.1x |\n"
          "| Concentration | RED | 78% |\n\n| Risk Area | Severity |\n| --- | --- |\n"
          "| Collateral | High |\n| Key-man | Moderate |\n")
    import io
    import zipfile
    # The CAM's own table style, and the plain one the risk-grade report uses.
    for cam in (True, False):
        xml = zipfile.ZipFile(io.BytesIO(markdown_to_docx(md, None, cam=cam))).read(
            "word/document.xml").decode()
        for fill in ("C6EFCE", "FFEB9C", "FFC7CE"):       # green, amber, red
            assert f'w:fill="{fill}"' in xml
        # A severity is coloured by its column; the factor names are not.
        assert xml.count('w:fill="FFC7CE"') == 2 and xml.count('w:fill="FFEB9C"') == 2


# ---------------------------------------------------------------------- management
def test_a_promoter_profile_is_its_own_kind_and_reaches_the_management_section():
    for name in ("Promoter Profile.pdf", "Management Team.pdf", "CVs of Directors.pdf",
                 "Founder bios.pdf"):
        assert doc_type("DOCUMENT TYPE: other", name) == "management profile", name
    assert doc_type("DOCUMENT TYPE: management profile", "x.pdf") == "management profile"
    assert doc_type("DOCUMENT TYPE: other", "PLP Company Profile.pdf") == "company profile"
    assert doc_type("DOCUMENT TYPE: other", "Debt Profile Mar 2026.xlsx") == "other"
    assert "management profile" in wanted("SECTION 2: MANAGEMENT PROFILE")


async def test_the_management_section_quotes_the_profile_verbatim():
    fake = _StatementFake()
    bios = ("[page 1]\nMeera J Raghavan — B.E. Electrical, 6 years in battery design at a "
            "cell maker; heads production.")
    docs = [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1),
            SourceDoc("mp", "Promoter Profile.pdf", bios, "other", 1)]
    await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                       docs=docs, analyst_notes="", cache=DigestCache(), model_id="m",
                       progress=lambda *a: None)
    mgmt = next(u for u in fake.calls if "Write ONLY this part of the CAM: SECTION 2" in u)
    assert "===== COMPANY AND MANAGEMENT PROFILES, DECKS AND CALL NOTES (full text, as supplied) =====" in mgmt
    assert "6 years in battery design at a cell maker" in mgmt


# ---------------------------------------------------------------------------- news
def test_news_must_name_a_person_in_full_and_a_firm_as_a_word():
    # A surname alone names thousands: the CAM took their news as the promoter's.
    assert not rg.mentions("Raghavan wins state election", "Meera J Raghavan", person=True)
    assert rg.mentions("Meera Raghavan of Voltara on LFP packs", "Meera J Raghavan",
                       person=True)
    assert not rg.mentions("Revoltara Robotics raises seed", "Voltara")
    assert rg.mentions("Voltara Batteries ships 45 forklift packs", "Voltara Batteries")


def test_the_monitors_findings_are_listed_with_who_and_why():
    rows = [
        {"intel_type": "News", "title": "Voltara Batteries wins Northline order", "signal": "GREEN",
         "source": "PULSE:ET", "payload": {"when": "20260920", "severity": "GOOD",
                                           "about": "Voltara Batteries", "matched": "Voltara"}},
        {"intel_type": "Regulatory", "title": "NCLT admits plea against Ardent Cell Works",
         "signal": "RED", "payload": {"when": "20260601", "severity": "UGLY",
                                      "publisher": "IBBI",
                                      "evidence": {"nature": "action", "stage": "pending",
                                                   "forums": ["NCLT"]}}},
        {"intel_type": "News", "title": "Dismissed namesake story", "is_dismissed": True,
         "payload": {"when": "20260901"}},
        {"intel_type": "News", "title": "Too old", "payload": {"when": "20240101"}},
        {"intel_type": "Funding", "title": "Not news", "payload": {"when": "20260901"}}]
    lines = cam_mod.monitor_lines(rows, "2025-10-26")
    assert [t for t, _l in lines] == ["voltara batteries wins northline order",
                                      "nclt admits plea against ardent cell works"]
    good, bad = lines[0][1], lines[1][1]
    assert good.startswith("2026-09-20 · Voltara Batteries wins Northline order — ET")
    assert "about Voltara Batteries" in good and "found under 'Voltara'" in good
    assert "official record" in bad and "evidence: action, pending, forum NCLT" in bad


async def test_the_cam_reads_the_monitor_and_searches_every_name_and_person(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_PULSE_URL", "http://pulse:8000")
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    stub.records = {
        f"/v1/lending/{LENDING}": {"id": LENDING, "entity_id": "e-1"},
        "/v1/entities/e-1": {"legal_name": "Ardent Cell Works",
                             "display_name": "Voltara Batteries", "news_aliases": ["Voltara"],
                             "key_people": [{"name": "Meera J Raghavan", "role": "Partner"}]},
        "/v1/panorama/financials": 409,
        "/v1/external-intelligence": {"items": [
            {"intel_type": "News", "title": "Voltara Batteries wins Northline order",
             "signal": "GREEN", "payload": {"when": "20260920", "severity": "GOOD",
                                            "about": "Voltara Batteries"}}]},
        "/v1/news/search": {"articles": [
            {"title": "Voltara Batteries wins Northline order", "source": "ET", "severity": "GOOD"},
            {"title": "Raghavan wins state election", "source": "TOI", "severity": "GOOD"},
            {"title": "Meera J Raghavan speaks at battery expo", "source": "ET",
             "severity": "GOOD"}]}}
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    # Read AS the caller, from the Register, for this firm.
    reads = [p for u, p in stub.gets if u.endswith("/v1/external-intelligence")]
    assert reads and reads[0]["entity_id"] == "e-1"
    checks = next(u for _s, u in eng.fake.calls if "Public information checks" in u)
    assert "PULSE monitor findings filed in the Register (last 12 months, 1;" in checks
    # Every name the firm goes by, and its people, were searched.
    queries = {p.get("q") for u, p in stub.gets if "news/search" in u}
    assert {'"ardent cell works"', '"voltara batteries"', '"voltara"',
            '"Meera J Raghavan"'} <= queries
    assert "searched under Ardent Cell Works; Voltara Batteries; Voltara · people: " \
           "Meera J Raghavan (Partner)" in checks
    # A filed story is not listed twice; a surname-only namesake is not listed at all.
    assert checks.count("Voltara Batteries wins Northline order") == 1
    assert "Raghavan wins state election" not in checks
    assert "about Partner Meera J Raghavan" in checks
    assert "an allegation is not a finding" in checks


# ----------------------------------- the review sheet: what can't be missed, and how
def test_the_memo_quotes_no_sources():
    from app.cam_pipeline import strip_sources
    md = ("## SECTION 4\n\nCells are imported [source: PO 26-27, p.1]. Margins are thin.\n\n"
          "| A | B |\n| --- | --- |\n| x | 1 |\n\n*Source: Book Orders; CMA.*\n\nDone.")
    out = strip_sources(md)
    assert "[source" not in out and "Source:" not in out
    assert "Cells are imported. Margins are thin." in out and "| x | 1 |" in out


def test_rule_codes_and_tool_talk_are_named():
    from app.cam_pipeline import internal_terms
    said = internal_terms("## S\n\nUnder the A3.6 revenue stress, per Rule 9, coverage falls. "
                          "Tracxn is not configured in this deployment. Honest assessment: "
                          "depth is thin.")
    joined = " ".join(said)
    for term in ("A3.6", "Rule 9", "not configured", "this deployment", "Honest assessment"):
        assert term in joined
    assert internal_terms("## S\n\nInterest coverage is 2.6x under the revenue stress.") == []


def test_net_worth_includes_the_years_profit_not_yet_in_capital():
    from app.statement_math import tnw_misreads
    _block, proof = statement_notes(BSHEET, "FY26 BSheet")
    assert proof is not None
    assert ("Net worth = capital 60,00,000.00 + profit & loss balance 30,00,000.00 = "
            "90,00,000.00") in proof.render()
    # Capital alone locked as TNW is sent back; the full figure is not.
    said = tnw_misreads([("FY26", 60.0)], [proof])
    assert said and "FY26 TNW 60 is the partners' capital alone" in said[0] and "90.00" in said[0]
    assert tnw_misreads([("FY26", 90.0)], [proof]) == []


NETWORTH = ("Annexure 2 Networth of Promoters / Guarantors<br>1. Name of the promoter/ Guarantor : "
            "Asha R Menon<br>"
            "6. DETAILS OF ASSETS (Rs. lakh)<br>i) MOVABLE ASSETS<br>"
            "a) Cash and Bank (Savings/ FD) : 2.10<br>b) Liquid Shares/ Bonds : <br>"
            "e) Vehicles/ Jewellery : 40L<br>f) Others : Total :42.10<br>"
            "7. DETAILS OF LIABILITIES<br>|1|Bank|Home|House|30|<br>"
            "8. SUMMARY Value of Movable assets : 42.10 Value of immovable properties : 80L "
            "Total Assets (Rs. Lakh) : 122.10 Less: Total liabilities (Rs. Lakh) : 30 "
            "Net Assets (Rs. Lakh) : 92.10")


def test_a_guarantors_declared_assets_are_read_and_never_called_missing():
    from app.statement_math import asset_claims, networth_notes
    block, has_assets = networth_notes(NETWORTH, "ASHA Networth")
    assert has_assets
    assert "- Cash and Bank (Savings/ FD): 2.10" in block and "- Vehicles/ Jewellery: 40L" in block
    assert ("Summary as declared: movable assets 42.10; immovable property 80L; total assets "
            "122.10; total liabilities 30; net assets 92.10") in block
    said = asset_claims("The net-worth annexures show liabilities only, with no asset side. "
                        "Bureau conduct is clean.", ["ASHA Networth"])
    assert len(said) == 1 and "declare assets: ASHA Networth" in said[0]
    assert asset_claims("Liabilities only.", []) == []          # no form read, no claim
    assert networth_notes("Balance Sheet 1,00,000.00", "BS") == ("", False)


def test_a_word_97_document_is_read():
    from app.cam import extract_text
    body = "Networth of Promoters. Vehicles / Jewellery : 60L. Net Assets : 92.10"
    blob = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + "Root Entry".encode("utf-16-le")
            + b"\x00" * 8 + "WordDocument".encode("utf-16-le") + b"\x00" * 32
            + body.encode("utf-16-le") + b"\x00" * 16 + "Times New Roman".encode("utf-16-le"))
    text, reason = extract_text("application/msword", blob)
    assert reason is None
    assert "Vehicles / Jewellery : 60L" in text and "Net Assets : 92.10" in text
    assert "Times New Roman" not in text and "WordDocument" not in text
    assert text.startswith("[Word 97–2003 .doc — text recovered without its layout]")
    # An Excel 97 file is OLE2 too, but not a Word document.
    xls = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "Workbook".encode("utf-16-le") + b"\x00" * 64
    assert extract_text("application/vnd.ms-excel", xls)[1] is not None


def test_call_memos_and_meeting_notes_are_their_own_kind():
    for name in ("Call memo 12 Oct.pdf", "Meeting minutes - promoters.docx", "Transcript.pdf",
                 "Plant visit report.pdf"):
        assert doc_type("DOCUMENT TYPE: other", name) == "call memo / meeting notes", name
    assert "call memo / meeting notes" in wanted("SECTION 4: BUSINESS MODEL ANALYSIS")
    assert "call memo / meeting notes" in wanted("SECTION 2: MANAGEMENT PROFILE")


def test_a_supplied_document_whose_data_never_reached_the_cam_is_named():
    from app.cam_pipeline import unused_documents
    docs = [SourceDoc("fin", "Audited FY25.pdf", "x"), SourceDoc("po", "PO 000076.pdf", "x"),
            SourceDoc("kyc", "PAN.pdf", "x")]
    sheets = {"fin": "| Revenue | FY25 | 1,605.10 | Rs lakh |",
              "po": "| Order | 45 packs | 1,641.40 | Rs lakh | Northline Lifts Coimbatore |",
              "kyc": "PAN ABCDE1234F"}
    kinds = {"fin": "financial statements", "po": "order / PO", "kyc": "KYC"}
    draft = "## S5\n\nRevenue fell to Rs. 1,605.1 lakh in FY25."
    assert unused_documents(docs, sheets, kinds, draft) == {
        "Order book, pipeline and business metrics": ["PO 000076.pdf"]}
    # Words only that document holds count as its data too.
    assert unused_documents(docs, sheets, kinds,
                            draft + " The Northline Lifts order from Coimbatore.") == {}


async def test_industry_news_is_read_for_the_industry_section(monkeypatch):
    monkeypatch.setenv("WORKFLOWS_PULSE_URL", "http://pulse:8000")
    eng = _Engine()
    app, stub = _job_app(monkeypatch, eng)
    stub.records = {
        f"/v1/lending/{LENDING}": {"id": LENDING, "entity_id": "e-1"},
        "/v1/entities/e-1": {"legal_name": "Ardent Cell Works",
                             "sub_sector": "Lithium-ion battery packs"},
        "/v1/panorama/financials": 409, "/v1/external-intelligence": {"items": []},
        "/v1/news/search": {"articles": [
            {"title": "Forklift electrification picks up in India", "source": "ET",
             "when": "2026-09-01"}]}}
    start = await _call(app, "POST", f"/v1/cam/{LENDING}/draft", json={
        "prompt_doc_id": "master", "source_doc_ids": ["fin-1"]})
    job = await _poll(app, start.json()["job_id"])
    assert job["status"] == "done", job
    assert any(p.get("q") == "Lithium-ion battery packs India"
               for u, p in stub.gets if "news/search" in u)
    checks = next(u for _s, u in eng.fake.calls if "Public information checks" in u)
    assert ("Industry news (Lithium-ion battery packs, last 6 months — context for the "
            "industry analysis, not facts about this borrower):") in checks
    assert "Forklift electrification picks up in India — ET" in checks


async def test_a_section_with_tool_talk_is_sent_back_and_named_if_it_stays():
    class _Talky(_StatementFake):
        async def __call__(self, system, turns, **kw):  # noqa: ANN001, ANN003
            user = turns[0]["content"]
            if "Write ONLY this part of the CAM: SECTION 3" in user:
                self.calls.append(user)
                return ("## SECTION 3: FINANCIAL ANALYSIS\n\nUnder the A3.6 stress coverage "
                        "falls [source: CMA, p.2]. Margins are thin.")
            return await super().__call__(system, turns, **kw)

    fake = _Talky()
    docs = [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1)]
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=docs, analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    assert any("internal code or tool talk" in u and "A3.6" in u for u in fake.calls)
    assert "[source" not in res.draft_md
    check = next(c for c in res.qa["checks"]
                 if c["check"] == "Plain language (no rule codes or tool talk)")
    assert check["status"] == "warn" and "A3.6" in check["detail"]
