"""Documents that came through OCR (Oct 2026, cipher-test): DocRAG passes Sarvam's pages
through with their tables as raw ``<table><tr><td>`` markup. The CAM's readers took
``<td>`` for a statement group's label, called a tied balance sheet unrebuildable, keyed
"documents that disagree" by markup, and found none of the guarantors' declared assets —
so the draft called the net-worth forms "liabilities-only". Every name and figure here is
made up."""

from __future__ import annotations

from app import cam_facts
from app.cam import markdown_tables
from app.statement_math import check, networth_values, statement_notes

# A balance sheet the way an OCR page carries it: one Tally line per row.
OCR_BSHEET = """# Balance Sheet

<table><tr><th>Liabilities</th><th>Amount</th></tr>
<tr><td>Capital Account</td><td>60,00,000.00</td></tr>
<tr><td>Accounts (CA) - A</td><td>25,00,000.00</td></tr>
<tr><td>Accounts (CA) - B</td><td>35,00,000.00</td></tr>
<tr><td>Loans (Liability)</td><td>1,60,00,000.00</td></tr>
<tr><td>Bank OD A/c</td><td>1,20,00,000.00</td></tr>
<tr><td>Secured Loans</td><td>40,00,000.00</td></tr>
<tr><td><b>Total</b></td><td>2,20,00,000.00</td></tr></table>
"""

# A guarantor's net-worth form, its summary OCR'd as a two-column table.
OCR_NETWORTH = """Annexure 2 Networth of Promoters / Guarantors

1. Name of the promoter/ Guarantor : R KESAVAN
6. DETAILS OF ASSETS (Rs. lakh)

7. DETAILS OF LIABILITIES

SUMMARY
<table>
<tr><td>Value of Movable assets</td><td>18.5</td></tr>
<tr><td>Value of immovable properties</td><td>100L</td></tr>
<tr><td>Total Assets (Rs. Lakh)</td><td>118.5</td></tr>
<tr><td>Less: Total liabilities (Rs. Lakh)</td><td>30</td></tr>
<tr><td>Net Assets (Rs. Lakh)</td><td>88.5</td></tr>
</table>
"""


def test_an_ocr_table_becomes_a_markdown_table_and_the_rest_is_untouched():
    md = markdown_tables("Heading\n<table><tr><th>Particulars</th><th>Amount</th></tr>"
                         "<tr><td>Rent &amp; rates</td><td>1,000.00</td></tr></table>\nafter")
    assert "| Particulars | Amount |" in md and "| Rent & rates | 1,000.00 |" in md
    assert md.startswith("Heading") and md.rstrip().endswith("after") and "<" not in md
    plain = "| Already | Markdown |\n|---|---|\n| a | 1 |"
    assert markdown_tables(plain) == plain


def test_an_ocr_balance_sheet_is_read_by_its_words_not_its_markup():
    raw_block, _p = statement_notes(OCR_BSHEET, "FY26 BSheet")
    assert "<td" in raw_block or "<t" in raw_block          # the bug: markup as a label
    block, proof = statement_notes(markdown_tables(OCR_BSHEET), "FY26 BSheet")
    assert proof is not None and "<" not in block
    assert ("Capital Account 60,00,000.00 = Accounts (CA) - A 25,00,000.00 + Accounts (CA) "
            "- B 35,00,000.00") in block
    facts = cam_facts.from_statement(check(markdown_tables(OCR_BSHEET), "FY26 BSheet"))
    assert facts and all("<" not in f.key for f in facts)
    # Even on raw markup, no fact is keyed by a tag.
    assert all("<" not in f.key for f in cam_facts.from_statement(check(OCR_BSHEET, "x")))


def test_a_net_worth_summary_ocrd_as_a_table_is_read():
    vals = networth_values(markdown_tables(OCR_NETWORTH))
    assert vals is not None and vals["person"] == "R KESAVAN"
    assert vals["summary"] == {"movable assets": "18.5", "immovable property": "100L",
                               "total assets": "118.5", "total liabilities": "30",
                               "net assets": "88.5"}
    g = cam_facts.from_networth(markdown_tables(OCR_NETWORTH), "KESAVAN Networth")
    assert g is not None and g.net_assets == 88.5 and g.total_assets == 118.5


def test_a_bank_limit_in_an_ocr_table_is_found():
    bsa = "<table><tr><td>CC/OD Limit</td><td>25,00,000</td></tr></table>"
    facts = cam_facts.key_values(markdown_tables(bsa), "BSA", "Bank of Example")
    assert [(f.label, f.value) for f in facts] == [("CC/OD Limit — Bank of Example", 25.0)]


def test_a_long_statement_with_many_totals_is_proved_in_time():
    """Once OCR'd tables read as tables, a long statement's "Total" rows reached the
    totals search, which was cubic in the figures: one document held the service's event
    loop for minutes and the workbench's polls timed out. Bounded, it takes well under a
    second; it ran more than a minute before."""
    import time
    rows = "".join(f"| Entry {i} | {1000 + i * 7:,}.00 |\n"
                   + ("| Total | 5,000.00 |\n" if i % 25 == 0 else "") for i in range(1500))
    t = time.perf_counter()
    check(rows, "long")
    assert time.perf_counter() - t < 5
