"""The CAM's ratios are PRISM's: A3.5 formulas over the figures the dataset step locked."""

from __future__ import annotations

import json

from app.cam_ratios import Figures, Period, assessments, cagr, compute, parse_figures, \
    ratios_markdown

FY24 = {"net_sales": 1000, "cogs": 700, "purchases": None, "pbdit": 150, "depreciation": 20,
        "non_cash_charges": None, "interest_total": 40, "interest_term_debt": 25,
        "principal_due": 50, "pat": 60, "tnw": 500, "intangibles": 20,
        "related_party_advances": 30, "group_investments": 0, "promoter_unsecured_loans": 100,
        "term_debt": 300, "wc_debt": 200, "total_outside_liabilities": 900,
        "current_assets": 600, "current_liabilities": 400, "trade_receivables": 200,
        "trade_payables": 100, "inventory": 140}
FY25 = {**FY24, "net_sales": 1210, "pbdit": 200, "interest_total": 44, "principal_due": None,
        "term_debt": 280, "wc_debt": 220}
FY26 = {**FY25, "net_sales": 2000}


def _fig(*, subordinated: bool = False) -> Figures:
    return Figures([Period("FY24", "audited", dict(FY24)), Period("FY25", "audited", dict(FY25)),
                    Period("FY26", "projected", dict(FY26))], subordinated)


def _by(fig: Figures) -> dict:
    return {r.name: r for r in compute(fig)}


def test_every_a35_ratio_is_the_locked_formula():
    r = _by(_fig())
    v = {name: ratio.values["FY24"] for name, ratio in r.items()}
    assert v["PBDIT Margin"] == 15.0 and v["PAT Margin"] == 6.0
    assert v["Interest Coverage"] == 3.75
    # (PAT + Dep + interest on term debt) / (interest on term debt + principal due)
    assert v["DSCR"] == (60 + 20 + 25) / (25 + 50)
    assert v["Cash Accruals"] == 80
    # TNW − intangibles − related-party advances − group investments; promoter loans out
    assert v["Adjusted TNW"] == 450
    assert v["Total Debt / Adj. TNW"] == (300 + 200 + 100) / 450
    assert v["TOL/TNW"] == 1.8 and v["Current Ratio"] == 1.5
    assert v["Debtor Days"] == 73.0 and v["Inventory Days"] == 73.0
    assert round(v["Creditor Days"], 3) == round(100 / 700 * 365, 3)      # COGS: no purchases
    assert round(v["Cash Conversion Cycle"], 3) == round(73 + 73 - 100 / 700 * 365, 3)
    assert round(r["Revenue growth YoY"].values["FY25"], 6) == 21.0
    assert r["Revenue growth YoY"].values["FY24"] is None
    # Blended cost: interest / average of opening and closing total debt (500, 500)
    assert round(r["Blended Cost of Debt"].values["FY25"], 6) == 8.8
    assert "COGS used" in " ".join(r["Creditor Days"].notes)


def test_a_missing_input_is_named_never_estimated():
    r = _by(_fig())
    assert r["DSCR"].values["FY25"] is None
    assert "scheduled principal due" in " ".join(r["DSCR"].notes)
    md = ratios_markdown(_fig())
    assert "| DSCR | 1.40x | not computable | not computable |" in md


def test_subordinated_promoter_loans_move_into_net_worth():
    r = _by(_fig(subordinated=True))
    assert r["Adjusted TNW"].values["FY24"] == 550
    assert r["Total Debt / Adj. TNW"].values["FY24"] == 500 / 550
    assert "ADDED (subordination on record)" in " ".join(r["Adjusted TNW"].notes)


def test_without_term_debt_interest_coverage_is_the_service_metric():
    flat = {**FY24, "term_debt": None, "interest_term_debt": None, "principal_due": None}
    fig = Figures([Period("FY24", "audited", flat)])
    r = _by(fig)
    assert r["DSCR"].values["FY24"] is None
    assert "Interest Coverage is the primary service metric" in " ".join(r["DSCR"].notes)
    assert assessments(fig, list(r.values()))[0][0].startswith("Interest Coverage FY24: 3.75x")


def test_assessments_use_the_latest_historical_year_and_the_a33_bands():
    fig = _fig()
    marks = dict(assessments(fig, compute(fig)))
    # FY26 is a projection; FY25 is the latest historical year, with no DSCR.
    assert "Interest Coverage FY25: 4.55x (DSCR not computable)" in marks
    assert marks["Current Ratio FY25: 1.50"] == "Healthy"
    assert marks["Total Debt / Adj. TNW FY25: 1.33x"] == "Conservative"
    g, span = cagr(fig)
    assert round(g, 6) == 21.0 and span == "FY24–FY25, 1 year(s)"


def test_the_figures_block_is_read_and_removed_from_the_workpaper():
    block = {"unit": "Rs lakh", "promoter_loans_subordinated": False, "periods": [
        {"period": "FY25", "basis": "Audited", "net_sales": 1210.5, "pat": True, "tnw": "500"}]}
    text = f"## Financial dataset workpaper\n| x |\n```locked-figures\n{json.dumps(block)}\n```\n"
    fig, cleaned, why = parse_figures(text)
    assert why is None and "locked-figures" not in cleaned and "| x |" in cleaned
    p = fig.periods[0]
    # A boolean or a string is not a figure: it is "not on record", never coerced.
    assert p.basis == "audited" and p["net_sales"] == 1210.5
    assert p["pat"] is None and p["tnw"] is None


def test_a_missing_or_broken_block_says_why():
    assert parse_figures("no block here")[2] == "the dataset step returned no locked-figures block"
    fig, cleaned, why = parse_figures("w\n```locked-figures\n{not json\n```")
    assert fig is None and why.startswith("the locked-figures block could not be read")
    assert parse_figures('```locked-figures\n{"periods": []}\n```')[2] == \
        "the locked-figures block has no periods"
