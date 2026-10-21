"""The grade reads everything PRISM sends (Oct 2026): the Register's records in full,
a year of news, the kinds of document read, and up to 150 files."""

from __future__ import annotations

from test_risk_grade import PANORAMA

from app import risk_grade as rg

RECORDS = {
    "interactions": [{"occurred_at": "2026-09-01", "interaction_type": "Meeting",
                      "notes": "Promoter: BOI CC fully drawn; recall threatened. " * 40}],
    "intelligence": [{"source": "IBBI", "signal": "RED", "observed_at": "2026-07-01",
                      "title": "CIRP admitted against a group company"}],
    "financials": [{"statement_type": "PnL", "fiscal_year": "FY25", "is_audited": True}],
    "monitoring": [{"record_type": "Stock statement", "delay_days": 45}],
}


def test_the_registers_records_are_in_the_client_data():
    text = rg.build_client_data(PANORAMA, [], [], rg.date(2026, 10, 18), records=RECORDS)
    assert "Interactions & VOX, full notes" in text and "recall threatened" in text
    # The desk's notes reach 1,500 characters, not the panorama's 300.
    assert text.count("recall threatened") >= 25
    assert "CIRP admitted against a group company" in text and "news monitor" in text
    assert '"fiscal_year": "FY25"' in text and '"delay_days": 45' in text
    assert "last 12 months" in text and "last 90 days" not in text


def test_without_records_the_panorama_summary_is_used():
    text = rg.build_client_data(PANORAMA, [], [], rg.date(2026, 10, 18))
    assert "Interactions & VOX (most recent first)" in text
    assert "Intelligence on the Register" in text and "Not available" in text


def test_the_grader_is_told_the_kinds_it_read_and_never_to_call_them_missing():
    text = rg.build_client_data(PANORAMA, [], [], rg.date(2026, 10, 18), facts={
        "kinds_read": {"bank statement analysis": ["BOI BSA.xlsx"],
                       "credit bureau report": ["W-630215792"]}})
    assert "Kinds of document READ for this grade" in text
    assert "never call one missing, empty or not provided" in text
    assert "bank statement analysis: BOI BSA.xlsx" in text


def test_a_real_credit_file_fits_one_grade():
    body = rg.RiskGradeIn(company="Pulze", panorama={},
                          documents=[{"name": f"f{n}", "text": "x"} for n in range(150)],
                          news=[{"headline": f"h{n}"} for n in range(100)], records=RECORDS)
    assert len(body.documents) == 150 and body.records["intelligence"]
