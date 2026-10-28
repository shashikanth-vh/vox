"""What a committee review of Pulze's third draft (Oct 2026) found PRISM's own code or
checks let through: guarantor cover graded on declared liabilities a partner's bureau
report contradicts, a GREEN for "no conflicts" that only meant few figures were compared,
a DSCR without this facility's own interest, a sizing that looked only backward, two
recommended amounts, a mistyped PAN, an unsourced market figure, and conditions that
cannot be met. Every name and figure here is made up."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from test_cam_harness import _HarnessFake
from test_cam_sizing import _fig
from test_cam_staged import MASTER

from app import cam_facts, cam_findings
from app.cam_facts import DebtRow, Guarantor, Ledger
from app.cam_pipeline import (DigestCache, SourceDoc, condition_faults, draft_staged,
                              off_amount, parse_master_prompt, unsupported)

FORM = """Networth of Promoters / Guarantors
1. Name of the promoter/ Guarantor : R EXAMPLE
6. DETAILS OF ASSETS (Rs. lakh)
7. DETAILS OF LIABILITIES
8. SUMMARY Value of Movable assets : 27.5L Value of immovable properties : 450L Total Assets (Rs. Lakh) : 470L Less: Total liabilities (Rs. Lakh) : 200L Net Assets (Rs. Lakh) : 272.5
"""
CIR = ("| CONSUMER NAME | : | Example Rajan | | DOB | : | 01/01/1990 |\n"
       "| High Cr/Sanc. Amt | : | ₹5,26,86,524 | | Current | : | ₹4,44,90,253 | | Overdue | : | ₹0 |\n")


def _by(led: Ledger, **kw) -> dict:  # noqa: ANN003
    return {f.id: f for f in cam_findings.evaluate(cam_findings.load_rubric(), led, _fig(),
                                                   facility_lakh=100.0, **kw)}


def test_a_form_that_does_not_add_up_is_named_and_reported_as_declared():
    g = cam_facts.from_networth(FORM, "Example Networth")
    assert g is not None and g.net_assets == 272.5
    assert g.checks == ["movable 27.5 + immovable 450 = 477.5, but the form's total is 470",
                        "total 470 − liabilities 200 = 270, but the form's net assets are 272.5"]


def test_guarantor_cover_is_graded_after_the_person_s_own_bureau_balances():
    docs = [SimpleNamespace(name="Example Networth", text=FORM),
            SimpleNamespace(name="CIR Example", text=CIR)]
    led = cam_facts.build(docs, [])
    g = led.guarantors[0]
    assert g.bureau == 444.9 and round(g.net_after_bureau or 0, 1) == 25.1    # 470 − 444.9
    gc = _by(led)["guarantor_cover"]
    assert gc.signal == "AMBER" and "Rs. 25.1 lakh" in gc.evidence
    assert "Rs. 272.5 lakh as declared" in gc.evidence
    table = cam_facts.guarantor_table(led)
    assert "| 444.90 | 25.10 |" in table and "does not add up" in table
    # Liabilities above assets: RED.
    poor = Ledger(guarantors=[Guarantor("A", 100.0, 10.0, 90.0, "x", bureau=300.0)])
    assert _by(poor)["guarantor_cover"].signal == "RED"
    # A clean form and no bureau: as declared; GREEN at 2x and more.
    rich = Ledger(guarantors=[Guarantor("A", 400.0, 50.0, 350.0, "x")])
    assert _by(rich)["guarantor_cover"].signal == "GREEN"


def test_a_bureau_report_read_from_the_portal_s_html_is_matched_too():
    html_rows = ("CONSUMER NAME | : | G Example Rajan\nDOB | : | 01/01/1990\n"
                 "Current | : | ₹ 4,44,90,253\ncurrent balance | : | ₹ 20,00,000\n")
    assert cam_facts.consumer_bureau(html_rows) == ("G Example Rajan", 444.9)


def test_no_conflict_found_is_no_signal():
    assert "document_conflicts" not in _by(Ledger())


def test_dscr_carries_this_facility_s_interest_and_the_snapshot_shows_the_estimate():
    led = Ledger()
    led.debts["Debt Profile June 2026"] = [DebtRow("L", "TL", 100.0, 80.0, 3.0, "", "", "dp")]
    a = cam_findings.assess(led, _fig(), 100.0, None, 15.0)
    # (70 + 10 + 30) / (36 + 30 + 15)
    assert round(a["dscr"], 2) == 1.36 and a["facility_interest"] == 15.0
    assert "plus Rs. 15.0 lakh a year on this facility at 15%" in _by(
        led, facility_rate=15.0)["dscr"].evidence
    snap = cam_facts.snapshot_table(_fig(), {"DSCR": ("FY26", a["dscr"])})
    assert "| DSCR | not computable | 1.36x (est.) |" in snap
    term = "|Interest Rate|\n\n<br><br>|Fixed interest rate of 15.00 % P.A.|"
    assert cam_facts.facility_rate(term) == 15.0


def test_the_gap_is_shown_on_the_projected_year_too():
    from app.cam_ratios import Figures, Period
    from test_cam_sizing import BASE
    fig = Figures([*_fig().periods, Period("FY27", "projected",
                                           {**BASE, "current_assets": 1300.0,
                                            "current_liabilities": 650.0, "wc_debt": 180.0})])
    led = Ledger(cam_facts.key_values("|  | CC/OD Limit | 25000000.0 |", "BSA", "Bank X"))
    a = cam_findings.assess(led, fig, 100.0)
    assert round(a["proj_gap"], 1) == 0.75 * 1300 - 470 - 250
    assert "| **Working-capital gap (FY27, projected)** | **255.0** |" in \
        cam_findings.limit_table(a)
    ev = {f.id: f for f in cam_findings.evaluate(cam_findings.load_rubric(), led, fig,
                                                 facility_lakh=100.0)}["wc_gap"].evidence
    assert "on the FY27 projection the gap is Rs. 255.0 lakh" in ev


def test_the_writing_checks():
    assert off_amount("We recommend a limit of Rs. 60 lakh.", 100.0) == ["Rs. 60 lakh"]
    assert off_amount("We recommend Rs. 100 lakh.", 100.0) == []
    src = "PAN: KWDPS2099A. Revenue grew."
    said = unsupported("Partner PAN KWOPS2099A and KWDPS2099A. India's lithium battery demand "
                       "is projected to grow at ~30% CAGR.", src)
    assert said[0] == "PAN KWOPS2099A is in no document" and len(said) == 2
    assert "~30% CAGR" in said[1]
    faults = condition_faults("Audited FY 2026-27 statements within 6 months. Audited FY26 "
                              "accounts. BOI's NOC within 90 days of disbursement.", {"FY27"})
    assert faults[0] == "asks for audited accounts for FY 2026-27, a year not yet ended"
    assert "consent follow disbursement" in faults[1] and len(faults) == 2


async def test_the_recommendation_is_written_first_and_its_amount_is_everyone_s():
    order: list[str] = []

    class _Fake(_HarnessFake):
        async def __call__(self, system, turns, **kw):  # noqa: ANN001, ANN003
            user = turns[0]["content"]
            for part in ("SECTION 4: RECOMMENDATIONS", "SECTION 1: EXECUTIVE SUMMARY"):
                if f"THE PART'S RULES — {part}" in user or f"Write ONLY this part of the CAM: {part}" in user:
                    order.append(part)
            if "Write ONLY the sub-section" in user and "SECTION 4: RECOMMENDATIONS" in user:
                self.calls.append(user)
                await asyncio.sleep(0)
                return "### Decision\n\nWe recommend a limit of Rs. 80 lakh."
            return await super().__call__(system, turns, **kw)

    fake = _Fake()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=[SourceDoc("a", "A.pdf", "Text.", "other", 1)],
                             analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None, harness="v2")
    assert order.index("SECTION 4: RECOMMENDATIONS") < order.index("SECTION 1: EXECUTIVE SUMMARY")
    exe = [u for u in fake.calls if "THE PART'S RULES — SECTION 1: EXECUTIVE SUMMARY" in u
           or "Write ONLY this part of the CAM: SECTION 1: EXECUTIVE SUMMARY" in u]
    assert exe and all("THE RECOMMENDATION AS WRITTEN names Rs. 80.0 lakh" in u for u in exe)
    assert "Rs. 80 lakh" in res.draft_md


def test_the_most_relevant_own_sheet_survives_whole_when_own_sheets_overflow():
    """A dozen bank-type sheets cut alike lost the BOI analysis's month-wise table (Pulze's
    third draft wrote the CC's quarterly flows "not on record" again). The one the
    sub-section names keeps its tail; every other keeps a floor."""
    from app.cam_pipeline import fit_relevant
    boi = ("===== FACT SHEET: BOI analysis =====\nIntro " + "a" * 3000
           + "\n## Month-wise ledger (BOI CC)\n| Apr 2025 | 154 |")
    others = [("bank statement analysis", f"===== FACT SHEET: Other {i} =====\n" + "b" * 9000)
              for i in range(12)]
    got = fit_relevant([*others, ("bank statement analysis", boi)], 60_000,
                       {"bank statement analysis"}, "Quarterly utilisation of the BOI CC")
    assert got[0].startswith("===== FACT SHEET: BOI analysis") and got[0] == boi
    assert all(any(f"Other {i} " in s for s in got) for i in range(12))
    assert sum(len(s) for s in got) <= 60_000 * 0.6 + 13 * 60
