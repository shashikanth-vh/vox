"""The ledger's "documents that disagree" (Oct 2026, Pulze's second draft): statement
facts carried no year, so FY23's and FY24's accounts — same group names, different
years — were named as three conflicts, two of them lines carried from one year's account
to the next ("Gross Profit b/d"). Every name and figure here is made up."""

from __future__ import annotations

from types import SimpleNamespace

from app import cam_facts
from app.cam_facts import Fact, Ledger, statement_period
from app.statement_math import check

TRADING = """| Particulars | Amount |
|---|---|
| Gross Profit b/d | 3,00,000.00 |
| Margin A | 1,00,000.00 |
| Margin B | 2,00,000.00 |
| Capital Account | {cap} |
| Accounts (CA) - A | 25,00,000.00 |
| Accounts (CA) - B | {b} |
"""


def _doc(name: str, text: str):
    return SimpleNamespace(name=name, text=text)


def test_a_statement_s_year_comes_from_its_date_or_its_name():
    assert statement_period("x", "Balance Sheet as at 31st March, 2025") == "FY25"
    assert statement_period("x", "1-Apr-2023 to 31-Mar-2024") == "FY24"
    assert statement_period("FY 25-26 BSheet") == "FY26"
    assert statement_period("P&L 2023-24") == "FY24"
    assert statement_period("Trading account") == ""


def test_a_carried_forward_line_is_no_fact():
    facts = cam_facts.from_statement(check(TRADING.format(cap="60,00,000.00",
                                                          b="35,00,000.00"), "x"), "FY24")
    assert [f.key for f in facts] == ["stmt.capital account"]
    assert facts[0].period == "FY24"


def test_two_years_accounts_do_not_disagree_and_the_same_year_does():
    fy23 = TRADING.format(cap="50,00,000.00", b="25,00,000.00") + "\nas at 31st March 2023"
    fy24 = TRADING.format(cap="60,00,000.00", b="35,00,000.00") + "\nas at 31st March 2024"
    docs = [_doc("Accounts A", fy23), _doc("Accounts B", fy24)]
    led = cam_facts.build(docs, [check(d.text, d.name) for d in docs])
    assert led.conflicts() == []
    again = _doc("Accounts C", fy24.replace("60,00,000.00", "61,00,000.00")
                 .replace("35,00,000.00", "36,00,000.00"))
    docs.append(again)
    led = cam_facts.build(docs, [check(d.text, d.name) for d in docs])
    assert [k for k, _fs in led.conflicts()] == ["stmt.capital account"]


def test_an_undated_statement_is_not_set_against_another():
    led = Ledger([Fact("stmt.net profit", "Net Profit", 21.7, "Rs lakh", "A"),
                  Fact("stmt.net profit", "Net Profit", 68.59, "Rs lakh", "B")])
    assert led.conflicts() == []
    led = Ledger([Fact("limit.cc_od.bank", "CC", 250.0, "Rs lakh", "A"),
                  Fact("limit.cc_od.bank", "CC", 200.0, "Rs lakh", "B")])
    assert len(led.conflicts()) == 1
