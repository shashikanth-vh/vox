"""PRISM's own reading of the fixed-format files a risk grade rests on. The snippets
copy the real layouts (the desk's BSA workbook, TransUnion CIBIL consumer and
commercial reports, the ScoreMe legal report, the CMA grid) with fictional figures."""

from __future__ import annotations

from app import risk_facts as rf

BSA = """# BOI - 1a2b_BSA_Report
## Sheet: Account Details
|  | Bank Name | Bank of India |
|  | CC/OD Limit | 25000000.0 |
|  | - Analysed by Evam Finance Private Limited |  |
## Sheet: Bank Statement
|  | 12.0 | 2025-05-20 00:00:00 | REJECT:408812:FUNDS INSUFFICIENT |  | 119.0 |
## Sheet: Overview-Month Wise
|  | Average OD & CC Limit Utilization (%) |  | 0.82 | 0.77 | 1.02 |
|  | No. of days limit over-drawn |  | 15.0 | - | 3.0 | 12.0 |
|  | Peak overdrawing amount |  | -26712345.50 | - | -25990120.00 | -26712345.50 |
|  | Peak overdrawing date |  | 14-08-2026 |  | 31-07-2026 | 14-08-2026 |
## Sheet: Cheque Return
| Date | Particulars | Reason | Outward Cheque Return | Inward Cheque Return |
| 2026-03-06 00:00:00 | REJECT:000215:70:Advice not received | Advice Not Received | 200000.0 | - |
| 2025-05-20 00:00:00 | REJECT:408812:FUNDS INSUFFICIENT | Funds Insufficient | 252000.0 | - |
## Sheet: EOD
"""

CIR = """| CIBIL Transunion Score(s) |
| ENHANCED CREDITVISION® Score  Ranges from: 300 (high risk) to 900 (low risk) | 300900742 | Scoring Factors |
| Grameen Score  Ranges from: 300 (high risk) to 900 (low risk) | 300900611 | Scoring Factors |
| CONSUMER ACCOUNT SUMMARY |  | ACCOUNTS | BALANCES |
| Total | : | 51 | | Zero balance | : | 30 | | Overdue | : | 2 |
| High Cr/Sanc. Amt | : | ₹1,20,00,000 | | Current | : | ₹90,00,000 | | Overdue | : | ₹41,500 |
| Maximum delinquency across all trades | (G311S) | | 60-89 DPD |
| Months since most recent delinquency (30+ DPD) | (AT36S) | | 1 M |
| | CONSUMER NAME:RAVI KUMAR RAVI KUMAR | D.O.B:01/01/1990 | GENDER:Male | CREDITVISION® Score  : 742 |
"""

CMA = """| Year / INR Lacs | 2023 | 2024 | 2025 | 2026 (Prov) | 2027 (P) |  |  |  | Ratios |  | 2023 | 2024 | 2025 | 2026 (Prov) | 2027 (P) |
| Revenue |  | 1210.40 | 1102.55 | 1876.30 | 2500 |  |  |  | Growth |  | #DIV/0! | -8.91 | 70.18 | 33.2 |
| EBITDA | 0 | 52.10 | 49.80 | 96.40 | 140.0 |  |  |  | Current ratio |  | 1.05 | 1.07 | 1.12 | 1.10 |
| PAT |  | 18.2 | 14.5 | 51.3 | 70.0 |  |  |  | TOL/TNW |  | 3.70 | 4.20 | 3.40 | 3.90 |
| TNW | 0 | 55.0 | 61.2 | 112.6 | 150.0 |  |  |  | DSCR |  | 2.10 | 1.90 | 3.20 | 2.60 |
"""


def test_a_bank_statement_analysis_gives_overdrawing_and_bounces():
    f = rf.bsa_facts("BOI - 1a2b_BSA_Report.xlsx", BSA)
    assert f["bank"] == "BOI" and f["limit"] == 25_000_000
    assert f["overdrawn_days"] == 15 and f["peak"] == 26_712_345.50
    assert f["peak_date"] == "14-08-2026" and f["utilisation_latest"] == 1.02
    # Only the RETURN sheet counts, and the amount is not the cheque number (408812);
    # an "advice not received" reject is not an insufficient-funds bounce.
    assert f["insufficient_funds"] == [{"date": "2025-05-20", "amount": 252_000.0}]


def test_a_consumer_report_gives_both_scores_and_the_worst_delinquency():
    f = rf.cir_facts("App_1_Cons_CIR_RaviKumar.pdf", CIR)
    assert f["person"] == "Ravi Kumar" and f["score"] == 742
    assert f["other_scores"] == [{"name": "Grameen Score", "score": 611}]
    assert f["worst_delinquency"] == "60-89 DPD" and f["months_since_30dpd"] == 1
    assert f["overdue_accounts"] == 2 and f["overdue_amount"] == 41_500


def test_the_cma_grid_is_read_by_year_and_projections_are_not_facts():
    f = rf.cma_facts("CMA.xlsx", CMA)
    assert f["years"] == ["2023", "2024", "2025", "2026 (Prov)"]
    assert f["rows"]["revenue"] == {"2024": 1210.40, "2025": 1102.55, "2026 (Prov)": 1876.30}
    assert "2023" not in f["rows"]["growth"]          # '#DIV/0!' is not a number
    assert f["rows"]["tol_tnw"]["2026 (Prov)"] == 3.40
    assert all("(P)" not in y for row in f["rows"].values() for y in row)


def test_flags_say_what_is_wrong_with_the_source():
    facts = [rf.bsa_facts("BOI - x_BSA_Report.xlsx", BSA),
             rf.cir_facts("Cons_CIR_RaviKumar.pdf", CIR),
             rf.cma_facts("CMA.xlsx", CMA),
             rf.commercial_facts("W-1.pdf", "CIBIL COMMERCIAL RANK ... CMR-9 Ranges from 1 to 10"),
             rf.legal_facts("Legal.xlsx", "| Total cases in latest 5 years | | 0.0 |\n"
                            "| Total cases in latest 5 years as a Respondent | | 2.0 |\n"
                            "| Total criminal cases in all years | | 0.0 |")]
    flags = rf.flags_from(facts)
    titles = [f["title"] for f in flags]
    assert flags[0]["severity"] == "red"                       # reds come first
    assert "BOI CC/OD overdrawn beyond its limit" in titles
    assert "Promoter Ravi Kumar: delinquency up to 60-89 DPD" in titles
    assert "Promoter Ravi Kumar: Grameen Score 611 (< 650)" in titles
    assert any(f["trigger"] == "commercial rank 8–10" for f in flags if f.get("trigger"))
    # CreditVision 742 is above 650: the consumer-score trigger is NOT raised.
    assert not any(f.get("trigger") == "consumer score < 650" for f in flags)
    assert "High leverage: TOL/TNW 3.40× (2026 (Prov))" in titles
    assert "Revenue fell 8.9% in 2025" in titles
    assert "2 case(s) against the firm (5 years)" in titles
    assert all(f["source"] for f in flags)


def test_an_unrecognised_file_gives_no_facts():
    assert rf.facts_of("notes.pdf", "Minutes of the meeting. Revenue looked fine.") == []


def test_id_numbers_are_masked_before_text_leaves_prism():
    out = rf.mask_ids("Aadhaar 1234 5678 9012; PAN ABCPK1234D; GSTIN 33ABCPK1234D1Z5; "
                      "cheque 3,64,001")
    assert "1234 5678 9012" not in out and "ABCPK1234D;" not in out
    assert "33ABCPK1234D1Z5" in out and "3,64,001" in out


def test_coverage_names_what_was_read_and_what_was_not():
    fates = [{"name": "BOI - x_BSA_Report.xlsx", "section": "Banking & Debt", "used": True},
             {"name": "Bank Statements.zip / notes.txt", "section": "Banking & Debt",
              "used": False, "reason": ".txt is not readable"},
             {"name": "W-100000001.pdf", "section": "Compliance & Bureau", "used": False,
              "reason": "no text could be read"}]
    cov = {r["area"]: r for r in rf.coverage(fates, [rf.bsa_facts("BOI - x_BSA", BSA)], 0, None)}
    assert cov["Banking"]["status"] == "partly read" and cov["Banking"]["checked_by_prism"]
    assert cov["Bureau"]["status"] == "not read"
    assert cov["Legal"]["status"] == "no file"
    assert cov["News (PULSE)"]["status"] == "none found"
