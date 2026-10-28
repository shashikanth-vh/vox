"""What a committee asked of Pulze's second draft (Oct 2026), now set by PRISM: no GREEN
on an unaudited year or over a CC drawn past its limit, leverage on what the bureau shows
owed now, and the limit sized net of the bank already funding the same current assets.
Every name and figure here is made up."""

from __future__ import annotations

from types import SimpleNamespace

from app import cam_facts, cam_findings
from app.cam_facts import DebtRow, Fact, Ledger
from app.cam_ratios import Figures, Period

BASE = {"net_sales": 3000.0, "cogs": 2600.0, "purchases": None, "pbdit": 130.0,
        "depreciation": 10.0, "non_cash_charges": None, "interest_total": 30.0,
        "interest_term_debt": None, "principal_due": None, "pat": 70.0, "tnw": 180.0,
        "intangibles": 0.0, "related_party_advances": 0.0, "group_investments": 0.0,
        "promoter_unsecured_loans": 0.0, "term_debt": 140.0, "wc_debt": 180.0,
        "total_outside_liabilities": 700.0, "current_assets": 900.0,
        "current_liabilities": 500.0, "trade_receivables": 170.0, "trade_payables": 290.0,
        "inventory": 460.0, "partners_remuneration": None, "tax": 20.0}


def _fig(basis: str = "audited") -> Figures:
    return Figures([Period("FY25", "audited", {**BASE, "net_sales": 2000.0}),
                    Period("FY26", basis, dict(BASE))])


def _by(led: Ledger, basis: str = "audited", facility: float = 100.0) -> dict:
    return {f.id: f for f in cam_findings.evaluate(cam_findings.load_rubric(), led,
                                                   _fig(basis), facility_lakh=facility)}


BSA = ("|  | CC/OD Limit | 25000000.0 |\n"
       "|  | No. of days limit over-drawn |  | 15.0 | - | - | 3.0 | 12.0 |\n")
BUREAU = ("Rank CMR-3\n| TOTAL | | 11 | ₹ 52,835,612 | | | 0 | ₹ 0 |\n")


def test_the_ledger_reads_overdrawn_days_and_the_bureau_total():
    facts = {f.key: f.value for f in cam_facts.key_values(BSA, "BSA", "Bank of Example")}
    assert facts["limit.cc/od limit.bank of example"] == 250.0
    assert facts["bank.overdrawn days.bank of example"] == 15.0
    assert facts["bank.overdrawn days latest month.bank of example"] == 12.0
    facts = {f.key: f.value for f in cam_facts.key_values(BUREAU, "CMR report", None)}
    assert facts["bureau.total outstanding"] == 528.36


def test_no_green_on_an_unaudited_year_or_over_a_cc_drawn_past_its_limit():
    clean = Ledger()
    by = _by(clean)
    assert by["growth"].signal == "GREEN" and by["liquidity"].signal == "GREEN"
    assert _by(clean, "provisional")["growth"].signal == "AMBER"
    led = Ledger(cam_facts.key_values(BSA, "BOI BSA", "Bank of Example"))
    liq = _by(led)["liquidity"]
    assert liq.signal == "AMBER" and "over its limit on 15 day(s)" in liq.evidence
    assert "12 of them in the latest month" in liq.evidence


def test_leverage_is_read_on_what_the_bureau_shows_owed_now():
    led = Ledger([Fact("bureau.total outstanding", "Total", 528.36, "Rs lakh", "CMR report")])
    lev = _by(led)["leverage"]
    assert "2.94x" in lev.evidence and "Rs. 528.4 lakh the company bureau report" in lev.evidence
    assert "Rs. 320.0 lakh in the FY26 statements" in lev.evidence
    # A bureau total no higher than the statements' debt changes nothing.
    low = Ledger([Fact("bureau.total outstanding", "Total", 300.0, "Rs lakh", "CMR report")])
    assert "1.78x" in _by(low)["leverage"].evidence


def test_a_provisional_year_is_not_locked_as_audited():
    docs = [SimpleNamespace(name="FY 26 - BSheet - Provisional", text="as at 31st March 2026"),
            SimpleNamespace(name="Accounts FY25", text="Independent Auditor's Report ... "
                                                       "as at 31st March 2025"),
            SimpleNamespace(name="FY25 provisional draft", text="as at 31st March 2025")]
    assert cam_facts.provisional_periods(docs) == {"FY26"}


def test_the_limit_is_sized_net_of_the_bank_already_lending():
    led = Ledger(cam_facts.key_values(BSA, "BOI BSA", "Bank of Example"))
    led.debts["Debt Profile June 2026"] = [
        DebtRow("Lender A", "Term loan", 100.0, 80.0, 2.5, "10%", "", "dp"),
        DebtRow("Lender B", "Vehicle", 20.0, 12.0, 0.5, "9%", "", "dp")]
    a = cam_findings.assess(led, _fig(), 100.0)
    # 0.75 × 900 − (500 − 180) = 355; less the 250 CC = 105.
    assert round(a["mpbf"], 1) == 355.0 and round(a["gap"], 1) == 105.0
    # 460 × 0.5 + 170 × 0.75 = 357.5 against 250 + 100.
    assert round(a["cover"], 2) == 1.02
    # (70 + 10 + 30) / (12 × 3.0 + 30): all interest counted, term interest not separable.
    assert round(a["dscr"], 2) == 1.67 and a["dscr_conservative"]
    by = _by(led)
    assert by["wc_gap"].signal == "GREEN" and "Rs. 105.0 lakh" in by["wc_gap"].evidence
    assert by["ca_cover"].signal == "AMBER"
    assert by["dscr"].signal == "GREEN" and "estimated" in by["dscr"].evidence
    assert "interest_cover" not in by
    assert _by(led, facility=200.0)["wc_gap"].signal == "AMBER"
    table = cam_findings.limit_table(a)
    assert "| **Working-capital gap (FY26, actual)** | **105.0** |" in table
    assert "| Less: existing limit — Bank Of Example | 250.0 |" in table
    assert "| **Current-asset cover** | **1.02x** |" in table


def test_a_recommendation_above_the_facility_cap_is_named():
    from app.cam_pipeline import over_cap
    text = ("The Register records Rs. 200 lakh sanctioned. We recommend a limit of Rs. 200 "
            "lakh. Alternatively we recommend Rs. 1 crore. PRISM recommends ₹ 2.5 crore.")
    assert over_cap(text, 100.0) == ["Rs. 200 lakh", "Rs. 2.5 crore"]
    assert over_cap(text, None) == []


def test_each_prism_table_is_printed_once_and_later_places_refer_back():
    snap = "| Year | Sales |\n| --- | --- |\n| FY26 | 3,000.0 |"
    parts = [f"## SECTION 1: EXECUTIVE SUMMARY\n\nText.\n\n{snap}\n\nMore.\n\n{snap}",
             f"## SECTION 5: FINANCIAL ANALYSIS\n\n{snap}\n\nAnalysis.", "## SECTION 6\n\nNone."]
    out = cam_facts.once(parts, {"snapshot": snap, "debt": ""})
    assert out[0].count(snap) == 1
    assert snap not in out[1]
    assert "*The financial snapshot is under SECTION 1: EXECUTIVE SUMMARY.*" in out[1]
    assert out[2] == parts[2]
