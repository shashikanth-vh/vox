"""The v2 CAM harness: PRISM reads the known layouts into a fact ledger and renders the
tables that carry them; the model writes around them. Every firm, person and figure
here is made up."""

from __future__ import annotations

import asyncio
import re

from test_cam_balance import BSHEET, NETWORTH
from test_cam_staged import MASTER

from app import cam_facts as cf
from app.cam_eval import score
from app.cam_pipeline import DigestCache, SourceDoc, bank_of, draft_staged, parse_master_prompt
from app.cam_ratios import parse_figures
from app.statement_math import statement_notes

DEBT = """## Sheet: Sheet1

| Col1 | Col2 | Col3 | Col4 | Col5 | Col6 | Col7 |
|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |
|  | Lender Name | Type of Debt/Loan | Sanctioned Amount (INR) | O/S Balance as of Date (INR) | EMI Amount (INR) | Security offered |
|  | NORTH BANK | MACHINERY LOAN | 2000000 | 1042730 | 33798 | Hypothecation |
|  | CITY NBFC | Term Loan | 3049602 | 2875159 | 110250 | Unsecured |
|  |  |  |  |  |  |  |
"""
BSA = "| Account | x |\n|---|---|\n|  |  |  | CC/OD Limit | 25000000.0 |\n| Current | 0 |"
BUREAU = "Company Credit Report. Rank: CMR-4 as at 16-Jul-2026. CMR-4 means moderate risk."


def _docs() -> list[SourceDoc]:
    return [SourceDoc("bs", "FY 26 - BSheet - Provisional.pdf", BSHEET, "financial_report", 1),
            SourceDoc("nw", "ASHA Networth.pdf", NETWORTH, "other", 1),
            SourceDoc("dp", "Debt Profile June 2026.xlsx", DEBT, "other", 1),
            SourceDoc("dp0", "Debt Profile Mar 2026.xlsx", DEBT.replace("1042730", "1100000"),
                      "other", 1),
            SourceDoc("bsa", "BOI - BSA_Report.xlsx", BSA, "other", 1),
            SourceDoc("cir", "W-1234 commercial bureau.pdf", BUREAU, "other", 1)]


def _ledger() -> cf.Ledger:
    docs = _docs()
    statements = [p for d in docs if (p := statement_notes(d.text, d.name)[1])]
    return cf.build(docs, statements, bank_of)


# ------------------------------------------------------------------------ the ledger
def test_the_ledger_reads_statements_forms_debt_tables_limits_and_the_bureau():
    led = _ledger()
    facts = {f.key: f for f in led.facts}
    assert facts["stmt.net worth"].value == 90.0                     # capital + P&L balance
    assert facts["stmt.loans (liability)"].value == 210.0
    nw = next(f for k, f in facts.items() if k.startswith("guarantor."))
    assert nw.value == 92.10 and nw.method == "declared"
    assert facts["limit.cc/od limit.bank of india"].value == 250.0      # rupees → lakh
    assert facts["bureau.cmr"].value == "CMR-4"
    rows = led.debts["Debt Profile June 2026.xlsx"]
    assert [(r.lender, r.sanctioned, r.outstanding) for r in rows] == [
        ("NORTH BANK", 20.0, 10.43), ("CITY NBFC", 30.5, 28.75)]
    block = led.render()
    assert block.startswith("===== PRISM FACT LEDGER")
    assert "Net assets — Asha R Menon: 92.10 Rs lakh — as declared" in block


def test_two_documents_that_disagree_are_named_not_picked():
    led = _ledger()
    led.add(cf.Fact("limit.cc/od limit.bank of india", "CC/OD Limit — Bank of India", 310.0,
                    "Rs lakh", "W-1234 commercial bureau.pdf"))
    assert [k for k, _fs in led.conflicts()] == ["limit.cc/od limit.bank of india"]
    assert "CONFLICT limit.cc/od limit.bank of india: 250.00 per BOI - BSA_Report.xlsx; " \
           "310.00 per W-1234 commercial bureau.pdf" in led.render()


def test_the_newest_debt_profile_is_rendered_with_totals():
    table = cf.debt_table(_ledger())
    assert "| NORTH BANK | MACHINERY LOAN | 20.0 | 10.4 | 0.34 |" in table   # June, not March
    assert "| **Total** | | **50.5** | **39.2** |" in table
    assert table.endswith("*As per Debt Profile June 2026.xlsx.*")


def test_the_guarantor_table_states_declared_net_worth():
    table = cf.guarantor_table(_ledger())
    assert "| Asha R Menon | 122.10 | 30.00 | 92.10 |" in table
    assert "as declared" in table.lower()


FIG = """## Financial dataset workpaper
| Item | FY25 | FY26 |
| Sales | 1605.1 | 3080.7 |
| PBDIT | 76.4 | 127.3 |
| PAT | 19.0 | 76.9 |
| TNW | 77.4 | 181.0 |
| Term debt | 47.5 | 142.2 |
| WC debt | 182.6 | 178.6 |
| Interest | 27.9 | 27.9 |
```locked-figures
{"unit": "Rs lakh", "periods": [
 {"period": "FY25", "basis": "audited", "net_sales": 1605.1, "pbdit": 76.4, "pat": 19.0,
  "tnw": 77.4, "term_debt": 47.5, "wc_debt": 182.6, "interest_total": 27.9},
 {"period": "FY26", "basis": "provisional", "net_sales": 3080.7, "pbdit": 127.3, "pat": 76.9,
  "tnw": 181.0, "term_debt": 142.2, "wc_debt": 178.6, "interest_total": 27.9}]}
```
"""


def test_the_snapshot_is_rendered_from_the_locked_figures():
    fig, _ds, _why = parse_figures(FIG)
    assert fig is not None
    table = cf.snapshot_table(fig)
    assert table.splitlines()[0] == "| Particulars (Rs lakh) | FY25 (A) | FY26 (P) |"
    assert "| Net Sales | 1,605.1 | 3,080.7 |" in table
    assert "| TNW | 77.4 | 181.0 |" in table
    assert "| Total Debt | 230.1 | 320.8 |" in table
    assert "| Interest Coverage | 2.74x | 4.56x |" in table


def test_a_table_token_becomes_the_table_and_an_unknown_one_disappears():
    md, placed = cf.place_tables("## S9\n\nThe facilities:\n\n{{table:debt}}\n\nAnd {{table:"
                                 "nope}} more.", {"debt": "| A |\n| --- |\n| 1 |"})
    assert placed == ["debt"] and "| A |\n| --- |\n| 1 |" in md and "{{" not in md


# -------------------------------------------------------------------- the v2 pipeline
class _V2Fake:
    """Places the debt and snapshot tables by token, as told."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, system: str, turns, **_kw):  # noqa: ANN001, ANN003
        user = turns[0]["content"]
        self.calls.append(user)
        await asyncio.sleep(0)
        if "Produce the FACT SHEET" in user:
            return "DOCUMENT TYPE: other\nNotes."
        if "Write ONLY the CAM's TITLE PAGE" in user:
            return "Evam Finance Private Limited"
        if "Public information checks" in user and "Write ONLY" not in user:
            return "## Public information checks\n| Check | Source | Status | Finding |"
        if (m := re.search(r"Write ONLY this part of the CAM: (.+)", user)):
            head = m.group(1).split(" (")[0]
            if "EXECUTIVE" in head:
                return f"## {head}\n\nSales rose.\n\n{{{{table:snapshot}}}}"
            if "ANNEXURE" in head:
                return f"## {head}\n\n{{{{table:debt}}}}"
            return f"## {head}\n\nText."
        return FIG


async def test_v2_gives_writers_the_ledger_and_renders_the_tables_they_place():
    fake = _V2Fake()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None, harness="v2")
    section = next(u for u in fake.calls if "Write ONLY this part of the CAM: SECTION 2" in u)
    assert "===== PRISM FACT LEDGER" in section and "===== TABLES PRISM RENDERS" in section
    assert "{{table:debt}}" in section and "{{table:guarantors}}" in section
    assert "| Net Sales | 1,605.1 | 3,080.7 |" in res.draft_md        # snapshot, by PRISM
    assert "| NORTH BANK | MACHINERY LOAN | 20.0 | 10.4 |" in res.draft_md
    assert "{{table" not in res.draft_md
    check = next(c for c in res.qa["checks"]
                 if c["check"] == "Tables rendered by PRISM (v2 harness)")
    assert check["status"] == "warn" and "guarantors not placed" in check["detail"]
    assert "snapshot in SECTION 1" in check["detail"] and "debt in ANNEXURE I" in check["detail"]


async def test_v1_is_unchanged_by_the_harness():
    fake = _V2Fake()
    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None)
    assert not any("PRISM FACT LEDGER" in u or "TABLES PRISM RENDERS" in u for u in fake.calls)
    assert not any(c["check"].startswith("Tables rendered") for c in res.qa["checks"])


# --------------------------------------------------------------------------- the eval
CASE = {"name": "made-up",
        "facts": [{"name": "net worth", "value": 180.99},
                  {"name": "sales", "value": 3080.69, "section": "FINANCIAL"}],
        "forbidden": [{"name": "unbalanced", "pattern": r"does not balance"},
                      {"name": "sources", "pattern": r"\[source:|^\s*[*_]?Sources?:"}],
        "required": [{"name": "glance", "pattern": "Credit picture at a glance"}]}


def test_the_eval_scores_facts_at_printed_precision_and_counts_the_tone():
    good = ("## SECTION 1\n\n### Credit picture at a glance\n| Factor | Signal |\n| --- | --- |\n"
            "| Bureau | GREEN — clean |\n\nNet worth is Rs. 181.0 lakh.\n\n"
            "## SECTION 5: FINANCIAL ANALYSIS\n\nSales were Rs. 30.81 crore.")
    r = score(good, CASE)
    assert r["score"] == "5/5" and r["metrics"]["GREEN"] == 1
    bad = ("## SECTION 1\n\nNet worth is Rs. 181.2 lakh; the balance sheet does not balance "
           "[source: BS, p.1].\n*Source: BS.*\n\n## SECTION 5: FINANCIAL ANALYSIS\n\nSales "
           "Rs. 1.5 crore.\n\n## SECTION 7\n\nSales 3,080.7.")
    r = score(bad, CASE)
    assert r["score"] == "0/5"                   # 181.2 ≠ 180.99; sales outside its section
    assert r["metrics"]["source tags"] == 1 and r["metrics"]["'Source:' lines"] == 1


# ------------------------------------------------- phase 2: findings and the rubric
def _findings(**kw):  # noqa: ANN003, ANN202
    from app import cam_findings as fd
    fig, _ds, _why = parse_figures(FIG)
    return fd.evaluate(fd.load_rubric(), _ledger(), fig, statements_tie=True,
                       facility_lakh=50.0, **kw)


def test_signals_come_from_the_rubric_not_the_model():
    by = {f.id: f for f in _findings()}
    assert by["bureau_company"].signal == "AMBER"            # CMR-4: moderate (A3.2)
    assert by["bureau_company"].evidence.startswith("CMR-4")
    assert by["interest_cover"].signal == "GREEN"            # 4.56x, no DSCR on record
    assert "dscr" not in by                                  # no metric, no guess
    assert by["leverage"].signal == "GREEN" and "1.77x" in by["leverage"].evidence
    # FY26 is provisional: growth on a year not yet audited is no GREEN.
    assert by["growth"].signal == "AMBER" and "91.9%" in by["growth"].evidence
    assert "on provisional figures, not yet audited" in by["growth"].evidence
    assert by["statements"].signal == "GREEN"
    assert by["guarantor_cover"].signal == "AMBER"           # 92.10 / 50 = 1.8x
    assert "Rs. 92.1 lakh" in by["guarantor_cover"].evidence


def test_an_analyst_can_set_a_signal_with_a_reason():
    by = {f.id: f for f in _findings(overrides={
        "bureau_company": {"signal": "green", "note": "CMR moved to 3 on the Oct refresh"}})}
    assert by["bureau_company"].signal == "GREEN"
    assert by["bureau_company"].override == "CMR moved to 3 on the Oct refresh"
    from app import cam_findings as fd
    pic = fd.picture_table(list(by.values()))
    assert pic.splitlines()[0] == "| Factor | Signal | What the evidence shows |"
    assert "(analyst: CMR moved to 3 on the Oct refresh)" in pic
    rows = pic.splitlines()[2:]
    assert all("GREEN" in r for r in rows[:3])               # strengths lead


def test_the_findings_block_names_offsets_resolutions_and_homes():
    from app import cam_findings as fd
    block = fd.block(_findings())
    assert block.startswith("===== PRISM FINDINGS")
    line = next(ln for ln in block.splitlines() if ln.startswith("- [guarantor_cover]"))
    assert "To resolve: CA-certified net-worth statements" in line and "Owned by: security" in line
    assert [f.id for f in fd.homes_of(_findings(), "SECTION 9: SECURITY")] == ["guarantor_cover"]


def test_the_facility_amount_is_read_from_the_term_sheet_or_the_register():
    assert cf.facility_lakh("|Facility Amount|\n|---|\n|Upto INR 1,00,00,000/- (Upto Rupees One "
                            "Crore only)|") == 100.0
    assert cf.facility_lakh("", "Lending line:\n  amount_cr: 2.0") == 200.0
    assert cf.facility_lakh("", "") is None


# --------------------------------------- phase 3: plan, write, review, repair
def test_a_plan_is_cleaned_and_every_owned_finding_assigned_once():
    from app.cam_pipeline import parse_outline
    raw = ('Here: {"subsections": [{"heading": "### Revenue", "covers": "sales", "findings": '
           '["growth", "nope"], "table": "snapshot"}, {"heading": "Leverage", "covers": "debt",'
           ' "findings": ["growth"], "table": "made-up"}]}')
    plan = parse_outline(raw, ["growth", "leverage"], ["snapshot", "debt"])
    assert plan is not None and [p["heading"] for p in plan] == ["Revenue", "Leverage"]
    assert plan[0]["findings"] == ["growth"] and plan[0]["table"] == "snapshot"
    assert plan[1]["findings"] == ["leverage"] and plan[1]["table"] is None
    assert parse_outline("no json here", [], []) is None
    assert parse_outline('{"subsections": [{"heading": "Only one"}]}', [], []) is None


def test_repeated_passages_are_found_across_parts():
    from app.cam_pipeline import repeated_passages
    same = "the cash credit has run at or above its sanctioned limit in most observed months"
    md = (f"## SECTION 1: A\n\n{same}.\n\n## SECTION 5: B\n\nAgain {same} as noted.\n\n"
          "## SECTION 7: C\n\nSomething else entirely is written here for the committee.")
    said = repeated_passages(md)
    assert len(said) == 1 and said[0].startswith("SECTION 1 & SECTION 5")


class _HarnessFake:
    """Plans two sub-sections, writes each (one with a rule code), reviews, repairs."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, system: str, turns, **_kw):  # noqa: ANN001, ANN003
        user = turns[0]["content"]
        self.calls.append(user)
        await asyncio.sleep(0)
        if "Produce the FACT SHEET" in user:
            return "DOCUMENT TYPE: other\nNotes."
        if "Write ONLY the CAM's TITLE PAGE" in user:
            return "Evam Finance Private Limited"
        if "Public information checks" in user and "PLAN (do not write)" not in user \
                and "Write ONLY" not in user:
            return "## Public information checks\n| Check | Source | Status | Finding |"
        if "PLAN (do not write)" in user:
            return ('{"subsections": [{"heading": "Revenue and margins", "covers": "trend", '
                    '"findings": ["growth"], "table": "snapshot"}, {"heading": "Leverage", '
                    '"covers": "debt", "findings": [], "table": null}]}')
        if "Your sub-section below was reviewed" in user:
            head = re.search(r"Write ONLY the sub-section '### (.+?)'", user).group(1)
            return f"### {head}\n\nCoverage is comfortable under the revenue stress test."
        if (m := re.search(r"Write ONLY the sub-section '### (.+?)'", user)):
            head = m.group(1)
            if head == "Leverage":
                return "### Leverage\n\nUnder the A3.6 stress, leverage holds."
            return f"### {head}\n\nSales doubled.\n\n{{{{table:snapshot}}}}"
        if (m := re.search(r"Write ONLY this part of the CAM: (.+)", user)):
            return f"## {m.group(1).split(' (')[0]}\n\nText."
        return FIG


async def test_v2_plans_writes_by_sub_section_reviews_and_repairs_only_what_failed():
    fake, critic_asks = _HarnessFake(), []

    async def critic(system, turns, **_kw):  # noqa: ANN001, ANN003, ANN202
        critic_asks.append(turns[0]["content"])
        return ('{"issues": [{"subsection": "Revenue and margins", "problem": "states the '
                'trend without its exposure", "fix": "lead with the business"}]}')

    res = await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                             docs=_docs(), analyst_notes="", cache=DigestCache(), model_id="m",
                             progress=lambda *a: None, harness="v2", critic=critic)
    fin = res.draft_md.split("## SECTION 3: FINANCIAL ANALYSIS")[1].split("\n## ")[0]
    # Planned, then written a sub-section at a time; the table placed by token.
    assert "### Revenue and margins" in fin and "### Leverage" in fin
    # Both sub-sections were sent back — one by the reviewer, one by PRISM's own check —
    # and each repaired once; the rule code is gone.
    assert "A3.6" not in fin and fin.count("revenue stress test") == 2
    sub_asks = [u for u in fake.calls if "Write ONLY the sub-section '### Leverage'" in u]
    assert any("FINDINGS" in u for u in sub_asks)
    assert any("internal code or tool talk" in u for u in fake.calls
               if "Your sub-section below was reviewed" in u)
    # The reviewer ran on the critic engine, with the standards and the findings.
    assert critic_asks and "credit committee's reviewer" in critic_asks[0]
    assert "PRISM FINDINGS" in critic_asks[0]
    assert any(r["issues"] for r in res.reviews)
    # The executive summary opens with the picture PRISM rendered from the rubric.
    exe_asks = [u for u in fake.calls if "THE PART'S RULES — SECTION 1: EXECUTIVE SUMMARY" in u]
    assert exe_asks and all("{{table:picture}}" in u and "Credit picture at a glance" in u
                            for u in exe_asks)
    assert "| Company bureau rank (CMR) | AMBER | CMR-4" in exe_asks[0]   # the table offered
    assert {f["id"] for f in res.findings} >= {"bureau_company", "leverage", "growth"}
    assert any(c["check"].startswith("Each point made once") for c in res.qa["checks"])


def test_a_sub_section_reads_its_own_sheets_not_every_document():
    """v2 sent every sub-section and repair the part's whole context — ~100k tokens each,
    13.3M to write one CAM. Its own kinds of document first, then those that mention what
    it covers; one with nothing to do with it is left out, not clipped to a stub."""
    from app.cam_pipeline import fit_relevant
    sheets = [("debt profile", "===== FACT SHEET: Debt Profile =====\nLender rows."),
              ("other", "===== FACT SHEET: Canteen menu =====\nLunch at noon."),
              ("other", "===== FACT SHEET: Board note =====\nThe leverage covenant holds."),
              ("other", "===== FACT SHEET: Long =====\nleverage " + "x" * 9000)]
    got = fit_relevant(sheets, 6000, {"debt profile"}, "Leverage — debt and its covenant")
    assert got[0].startswith("===== FACT SHEET: Debt Profile")      # its own kind first
    assert any("Board note" in s for s in got)                        # mentions what it covers
    assert not any("Canteen" in s for s in got)                       # nothing to do with it
    assert sum(len(s) for s in got) <= 6000 + 60 and got[-1].endswith("context budget]")


def test_every_own_sheet_reaches_the_sub_section_and_a_name_outranks_a_common_word():
    """Own-kind sheets taken whole until the budget ran out dropped some entirely: a
    banking sub-section never saw one bank's statement and wrote its CC flows "not on
    record" (Pulze, Oct 2026). Every own sheet now arrives, the longest clipped; and a
    sheet naming the customer the sub-section covers beats one sharing only a common
    word. Names are made up."""
    from app.cam_pipeline import fit_relevant
    banks = [("bank statement", f"===== FACT SHEET: Bank {i} =====\nmonthly credits "
              + "z" * 9000) for i in range(4)]
    others = [("other", "===== FACT SHEET: Receipts =====\nA receipt from Zentrac of 8 lakh."),
              *[("other", f"===== FACT SHEET: Note {i} =====\nmonthly " + "y" * 3000)
                for i in range(3)]]
    got = fit_relevant(banks + others, 20_000, {"bank statement"},
                       "Customers — monthly credits from Zentrac")
    assert all(any(f"Bank {i} " in s for s in got) for i in range(4))
    assert any("Zentrac of 8 lakh" in s for s in got)
    assert sum(len(s) for s in got) <= 20_000 + 200


async def test_v2_sub_sections_are_sent_less_than_the_whole_part():
    canteen = SourceDoc("cm", "Canteen menu.pdf", "Lunch is served at noon. " * 400, "other", 1)

    async def run(sub_chars: int) -> list[str]:
        fake = _HarnessFake()
        await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                           docs=[*_docs(), canteen], analyst_notes="", cache=DigestCache(),
                           model_id="m", progress=lambda *a: None, harness="v2",
                           sub_sheet_chars=sub_chars)
        return fake.calls

    calls = await run(60_000)
    plan = [u for u in calls if "PLAN (do not write) this part of the CAM: SECTION 3" in u]
    sub = [u for u in calls if "Write ONLY the sub-section '### Leverage'" in u]
    assert plan and sub
    assert "FACT SHEET: Canteen menu" in plan[0]          # the plan still sees every sheet
    assert "FACT SHEET: Canteen menu" not in sub[0]       # the sub-section does not
    assert "LOCKED FINANCIAL DATASET" in sub[0] and "PRISM FINDINGS" in sub[0]
    assert "FACT SHEET: Debt Profile" in sub[0]
    # 0 keeps the old behaviour: the part's whole context on every call.
    whole = [u for u in await run(0) if "Write ONLY the sub-section '### Leverage'" in u]
    assert "FACT SHEET: Canteen menu" in whole[0]


async def test_v2_repairs_are_lighter_and_run_at_the_repair_effort():
    """Every Pulze sub-section was repaired, each re-sent its whole sub-section context
    at the engine's high effort (2.05M tokens). A repair gets a third of the sheets, and
    runs at the repair effort; the writing keeps the engine's."""
    long_note = "debt covenant schedule " * 2500                      # ~57k characters

    class _Fake(_HarnessFake):
        def __init__(self) -> None:
            super().__init__()
            self.efforts: list[tuple[str, str | None]] = []

        async def __call__(self, system: str, turns, **kw):  # noqa: ANN001, ANN003
            user = turns[0]["content"]
            self.efforts.append((user, kw.get("effort")))
            if "Produce the FACT SHEET" in user and "Covenant note" in user:
                self.calls.append(user)
                return "DOCUMENT TYPE: other\n" + long_note
            return await super().__call__(system, turns, **kw)

    async def critic(system, turns, **_kw):  # noqa: ANN001, ANN003, ANN202
        return '{"issues": [{"subsection": "Leverage", "problem": "p", "fix": "f"}]}'

    note = SourceDoc("cn", "Covenant note.pdf", "Debt covenants. " * 50, "other", 1)
    fake = _Fake()
    await draft_staged(generate=fake, system="S", prompt=parse_master_prompt(MASTER),
                       docs=[*_docs(), note], analyst_notes="", cache=DigestCache(),
                       model_id="m", progress=lambda *a: None, harness="v2", critic=critic,
                       sub_sheet_chars=60_000, repair_effort="low")
    sub = [(u, e) for u, e in fake.efforts if "Write ONLY the sub-section '### Leverage'" in u
           and "Your sub-section below was reviewed" not in u]
    fix = [(u, e) for u, e in fake.efforts if "Your sub-section below was reviewed" in u
           and "### Leverage" in u]
    assert sub and fix
    assert sub[0][1] is None and fix[0][1] == "low"
    assert long_note[:40] in sub[0][0] and long_note in sub[0][0]   # whole for the writer
    assert len(fix[0][0]) < len(sub[0][0]) - 30_000                  # a third for the repair
