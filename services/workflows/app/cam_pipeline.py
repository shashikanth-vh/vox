"""The staged CAM drafter: the credit team's master prompt run as its own workflow.

One request holding the whole prompt, every document and a ~40k-token CAM overflows the
model's context on a real deal and spreads its attention thin. The EVAM master prompt
already prescribes the fix — extract to a dataset, lock the financial table, then write
the memo — so this module runs exactly that, as bounded calls:

  1. DIGEST       each document (split at page boundaries when large) → a source-tagged
                  fact sheet. Cached per (model, rules, document), run in parallel.
  2. CONSOLIDATE  all fact sheets → the LOCKED financial dataset workpaper; in parallel,
                  the public checks the prompt requires, listed as NOT PERFORMED.
  3. BODY         every section except the summary sections, in parallel, each with the
                  rules, the fact sheets, the locked dataset and ITS specification.
  4. FINAL        the Proposal Summary (page 1), Executive Summary, Recommendations and
                  the title page, written last from the finished body so they agree with it.
  5. ASSEMBLE     the sections in the prompt's order.
  6. MACHINE QA   the prompt's Step 7 checks a program can make (sections present and
                  not duplicated, Financial Analysis figures traced to the locked
                  dataset, lengths) — reported with the draft, appended to the workpaper.

Every stage also sees the deal as the Register holds it (borrower identity, the line's
amount, product, RM, analyst), so page 1 and Annexure I are not left "not on record".

A prompt that does not have the master prompt's structure (no SECTION/ANNEXURE
specifications) is not forced through this: ``parse_master_prompt`` returns None and the
caller drafts in a single call as before.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.cam_ratios import FIGURES_TASK, compute, parse_figures, plausibility, ratios_markdown, trace
from app.cam_telemetry import stage

# ----------------------------------------------------------------------------- prompt
_BLOCK = re.compile(r"^BLOCK\s+([A-Z])\b")
_PHASE = re.compile(r"^PHASE\s+([A-Z])\b")
# Case-sensitive. The Proposal Summary (page 1) and the Term Sheet appendix are parts of
# the CAM too: without them here, page 1 was folded into Phase C's instructions and the
# appendix into Section 11's, and neither was ever written.
_SECTION = re.compile(r"^(SECTION\s+\d+\s*:|ANNEXURE\s+[IVXL]+\s*:|APPENDIX\s*:"
                      r"|PROPOSAL SUMMARY\b)")
_QA = re.compile(r"^(JUDGMENT QA|QA CHECKLIST|READ-THROUGH CHECKLIST)\b", re.I)
# Written last, from the finished body: the summaries, and page 1 (its Evam Strategy and
# proposal tables restate what the body establishes).
_FINAL = re.compile(r"EXECUTIVE SUMMARY|RECOMMENDATION|PROPOSAL SUMMARY", re.I)
# "(350–500 words + snapshot table)", "(Page 1)": guidance for the writer, not heading.
_HINT = re.compile(r"\s*\((?=[^()]*(?:\bwords?\b|\btables?\b|prose|\bpage\s*\d))[^()]*\)\s*$",
                   re.I)
_WORDS = re.compile(r"\(([\d,]+)\s*[–-]\s*([\d,]+)\s*words", re.I)


@dataclass
class SectionSpec:
    title: str          # the heading as the prompt writes it, e.g. "SECTION 5: FINANCIAL ..."
    spec: str           # the prompt's instructions for this section
    order: int
    final: bool         # written last, from the finished body

    @property
    def heading(self) -> str:
        """The heading as it appears in the CAM (writing hints removed)."""
        return _HINT.sub("", self.title).strip()

    @property
    def word_range(self) -> tuple[int, int] | None:
        """The prompt's length for this section, e.g. (350, 500), when it sets one."""
        m = _WORDS.search(self.title)
        return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else None


@dataclass
class MasterPrompt:
    rules: str                          # Block A: persona, analytical rules, scales, tone
    phases: dict[str, str]              # "A" extraction, "B" public checks, "C" forensic
    sections: list[SectionSpec]
    qa: str                             # the read-through checklist (quality bar)
    preamble: str = ""


def parse_master_prompt(text: str) -> MasterPrompt | None:
    """Split a master prompt into its parts, or None when it has no section specs."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    blocks = {m.group(1): i for i, ln in enumerate(lines) if (m := _BLOCK.match(ln.strip()))}
    heads = [i for i, ln in enumerate(lines) if _SECTION.match(ln.strip())]
    if len(heads) < 3:
        return None
    end_b = blocks.get("C", len(lines))
    qa_at = next((i for i in range(heads[-1] + 1, end_b) if _QA.match(lines[i].strip())), end_b)

    def span(a: int, b: int) -> str:
        return "\n".join(ln for ln in lines[a:b] if ln.strip()).strip()

    start_a = blocks.get("A", 0)
    rules = span(start_a, blocks.get("B", heads[0]))
    phase_at = [(m.group(1), i) for i, ln in enumerate(lines[:heads[0]])
                if (m := _PHASE.match(ln.strip()))]
    phases = {}
    for n, (name, i) in enumerate(phase_at):
        nxt = phase_at[n + 1][1] if n + 1 < len(phase_at) else heads[0]
        phases[name] = span(i, nxt)
    sections = []
    for n, i in enumerate(heads):
        nxt = heads[n + 1] if n + 1 < len(heads) else qa_at
        title = lines[i].strip()
        sections.append(SectionSpec(title=title, spec=span(i + 1, nxt), order=n,
                                    final=bool(_FINAL.search(title))))
    return MasterPrompt(rules=rules, phases=phases, sections=sections,
                        qa=span(qa_at, end_b) if qa_at < end_b else "",
                        preamble=span(0, start_a))


# ------------------------------------------------------------------------- documents
@dataclass
class SourceDoc:
    doc_id: str
    name: str
    text: str
    doc_type: str = ""
    pages: int | None = None
    # Content blocks for an engine that reads files itself (a scan with no text layer,
    # handed to Anthropic as the PDF); the fact sheet is then read from the attachment.
    blocks: list[dict[str, Any]] | None = None


def split_pages(text: str, max_chars: int) -> list[str]:
    """Split at ``[page N]`` markers (DocRAG) or paragraph breaks, never mid-page when a
    page fits; each part at most max_chars."""
    if len(text) <= max_chars:
        return [text]
    units = re.split(r"(?=^\[page \d+\]$)", text, flags=re.M)
    if len(units) <= 1:
        units = text.split("\n\n")
    parts: list[str] = []
    cur = ""
    for unit in units:
        while len(unit) > max_chars:                    # one oversized page: hard cut
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(unit[:max_chars])
            unit = unit[max_chars:]
        if len(cur) + len(unit) > max_chars and cur:
            parts.append(cur)
            cur = ""
        cur += unit if not cur else ("\n\n" + unit if not unit.startswith("[page") else unit)
    if cur.strip():
        parts.append(cur)
    return parts


def manifest(docs: list[SourceDoc]) -> str:
    rows = ["| # | Document | Type | Pages |", "| --- | --- | --- | --- |"]
    for n, d in enumerate(docs, 1):
        rows.append(f"| {n} | {d.name} | {d.doc_type or '—'} | {d.pages or '—'} |")
    return "DOCUMENT MANIFEST (what the analyst supplied)\n" + "\n".join(rows)


def fit(parts: list[str], budget: int) -> list[str]:
    """Shrink the LONGEST parts first until the total fits the budget — short fact
    sheets survive whole; a clipped part says so."""
    total = sum(len(p) for p in parts)
    if total <= budget:
        return parts
    out = list(parts)
    cap = max(len(p) for p in out)
    while sum(len(p) for p in out) > budget and cap > 2000:
        cap = int(cap * 0.85)
        out = [p if len(p) <= cap else p[:cap] + "\n[… clipped to fit the context budget]"
               for p in parts]
    return out


# ------------------------------------------------------------------ ledgers & workbooks
# A transaction list — a bank statement PDF, a BSA's raw sheet, a GST ledger, a
# debtor ledger — is thousands of rows a model reads page by page and summarises
# badly (Pulze: three bank-statement analyses called "empty templates"). PRISM
# summarises it ITSELF, month by month, from the columns the table names: exact sums
# the CAM can quote and the figure check can trace. Detected by its header (a date
# column and a debit/credit column), never by a vendor's sheet name.
_LEDGER_MIN_ROWS = 150     # fewer rows: the model reads them as they are
_LEDGER_KEEP = 40          # rows kept as read, so the columns and period still show
_DATE = re.compile(r"(\d{1,2})[-/. ](\d{1,2}|[A-Za-z]{3})[-/. ](\d{2,4})|(\d{4})-(\d{2})-(\d{2})")
_MONTHS = {m: n for n, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                       "aug", "sep", "oct", "nov", "dec"), 1)}
_RETURN = re.compile(r"\b(return|rtn|bounce|reject|insuff|unpaid|dishono|chq ret|ecs ret)", re.I)


def _cells(line: str) -> list[str]:
    return [re.sub(r"<br\s*/?>", " ", c).strip() for c in line.strip().strip("|").split("|")]


def _ledger_cols(cells: list[str]) -> dict[str, int] | None:
    """Column roles from a header row, or None when it is not a ledger's header."""
    low = [c.casefold() for c in cells]
    pick: dict[str, int] = {}
    for role, rx in (("date", r"\bdate\b|txn dt|tran dt"),
                     ("debit", r"debit|withdrawal|\bdr\b|paid out|payments?"),
                     ("credit", r"credit|deposit|\bcr\b|paid in|receipts?"),
                     ("balance", r"balance"),
                     ("narration", r"narration|description|particulars|remarks|details")):
        for n, c in enumerate(low):
            if n not in pick.values() and re.search(rx, c):
                pick[role] = n
                break
    return pick if "date" in pick and ("debit" in pick or "credit" in pick) else None


def _month(cell: str) -> str | None:
    m = _DATE.search(cell)
    if not m:
        return None
    if m.group(4):
        y, mo = int(m.group(4)), int(m.group(5))
    else:
        mo_s, y_s = m.group(2), m.group(3)
        mo = int(mo_s) if mo_s.isdigit() else _MONTHS.get(mo_s[:3].casefold(), 0)
        y = int(y_s) + (2000 if len(y_s) == 2 else 0)
    return f"{y:04d}-{mo:02d}" if 1 <= mo <= 12 and 1990 < y < 2100 else None


_AMOUNT_CELL = re.compile(r"(?:rs\.?|inr)?\s*(-)?\s*(\d+(?:\.\d{1,2})?)\s*(cr|dr)?\.?", re.I)


class _Shifted(ValueError):
    """A cell in an amount column that is not an amount: the row's columns slipped."""


def _amount(cell: str) -> float | None:
    s = cell.replace(",", "").replace("₹", "").strip()
    if not s or re.fullmatch(r"[-–—]+", s):
        return None
    m = _AMOUNT_CELL.fullmatch(s)
    if not m:
        raise _Shifted(cell)
    v = float(m.group(2))
    return -v if m.group(1) or (m.group(3) or "").lower() == "dr" else v


def _summary(rows: list[tuple[str, dict[str, float | None], str, str]], title: str,
             bad: int = 0) -> str:
    months: dict[str, dict[str, Any]] = {}
    for mon, vals, date, narr in rows:
        m = months.setdefault(mon, {"n": 0, "cr": 0.0, "dr": 0.0, "bal": [], "ret": 0})
        m["n"] += 1
        m["cr"] += abs(vals.get("credit") or 0)
        m["dr"] += abs(vals.get("debit") or 0)
        if vals.get("balance") is not None:
            m["bal"].append(vals["balance"])
        if _RETURN.search(narr):
            m["ret"] += 1
    order = sorted(months)

    def fmt(v: float) -> str:
        return f"{v:,.2f}"

    # Rows PRISM could not read column by column are left out, and the heading says so:
    # a partial sum quoted as the month's credits would be a silent understatement.
    partial = (f" — PARTIAL: {bad:,} row(s) with shifted columns are not in these sums; "
               "they are understated" if bad else "")
    out = [f"### PRISM ledger summary — {title} (computed by PRISM from {len(rows):,} rows, "
           f"{order[0]} to {order[-1]}; quote these, not the sample rows){partial}", "",
           "| Month | Transactions | Credits (Rs.) | Debits (Rs.) | Closing balance (Rs.) "
           "| Lowest balance (Rs.) | Highest balance (Rs.) | Returns / bounces |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k in order:
        m = months[k]
        b = m["bal"]
        out.append(f"| {k} | {m['n']} | {fmt(m['cr'])} | {fmt(m['dr'])} | "
                   + (f"{fmt(b[-1])} | {fmt(min(b))} | {fmt(max(b))}" if b else "— | — | —")
                   + f" | {m['ret']} |")
    big = sorted(((abs(v.get("credit") or 0), d, n) for _m, v, d, n in rows
                  if v.get("credit")), reverse=True)[:10]
    out += ["", f"Total credits Rs. {fmt(sum(m['cr'] for m in months.values()))}; total "
            f"debits Rs. {fmt(sum(m['dr'] for m in months.values()))}; rows naming a return "
            f"or bounce: {sum(m['ret'] for m in months.values())}. Closing balance is the "
            "month's last row as listed (negative = overdrawn / debit balance)."]
    if big:
        out += ["", "| Largest credits (Rs.) | Date | Narration |", "| --- | --- | --- |"]
        out += [f"| {fmt(v)} | {d} | {n} |" for v, d, n in big]
    return "\n".join(out)


def summarise_ledgers(text: str) -> tuple[str, str | None]:
    """(text with each long ledger cut to its head and PRISM's month-wise summary in its
    place, note). One ledger is one header's rows — continued across the repeated
    header of every PDF page, ended by a new sheet or a different header — so a BSA's
    subset sheets (loan, interest, exceptional transactions) are never added to the
    statement's totals."""
    lines = (text or "").splitlines()
    out: list[str] = []
    notes: list[str] = []
    sheet = ""
    run: dict[str, Any] | None = None

    def close() -> None:
        nonlocal run
        if run is None:
            return
        title = run["sheet"] or "transactions"
        seen = len(run["rows"]) + run["bad"]
        if seen >= _LEDGER_MIN_ROWS and run["bad"] > 0.05 * seen:
            # Too many rows PRISM cannot read column by column: no sums it cannot stand
            # behind — the model reads the listing as before, and the fate says why.
            notes.append(f"'{title}' not summarised: {run['bad']:,} of {seen:,} rows have "
                         "shifted columns")
            out.extend(run["lines"])
        elif len(run["rows"]) >= _LEDGER_MIN_ROWS:
            # The sample rows, then whatever prose the listing carried (page markers,
            # a statement's own opening/closing summary) — only the rows are cut.
            prose = [x for x in run["lines"][run["keep_to"]:] if not x.lstrip().startswith("|")]
            summary = _summary(run["rows"], title, run["bad"])
            out.extend(["", summary, "", *run["lines"][:run["keep_to"]],
                        f"[… {len(run['rows']) - _LEDGER_KEEP:,} more rows summarised above, "
                        "not read]", *prose, ""])
            notes.append(f"{len(run['rows']):,} rows of '{title}' summarised month by month "
                         "by PRISM")
        else:
            out.extend(run["lines"])
        run = None

    for ln in lines:
        if (m := _SHEET.match(ln)):
            close()
            sheet = m.group(1).strip()
            out.append(ln)
            continue
        if not ln.lstrip().startswith("|"):
            (run["lines"] if run is not None else out).append(ln)
            continue
        cells = _cells(ln)
        head = _ledger_cols(cells)
        if head is not None:
            key = "|".join(c.casefold() for c in cells)
            if run is not None and run["key"] != key:
                close()
            if run is None:
                run = {"key": key, "cols": head, "rows": [], "lines": [], "keep_to": 0,
                       "sheet": sheet, "width": len(cells), "bad": 0}
            run["lines"].append(ln)
            continue
        if run is None:
            out.append(ln)
            continue
        run["lines"].append(ln)
        cols = run["cols"]
        mon = _month(cells[cols["date"]]) if cols["date"] < len(cells) else None
        if mon is None:
            continue
        try:
            if len(cells) != run["width"]:
                raise _Shifted(ln)
            vals = {k: _amount(cells[i]) for k, i in cols.items()
                    if k in ("debit", "credit", "balance")}
        except _Shifted:
            run["bad"] += 1          # a narration with a "|" in it: never summed shifted
            continue
        narr = (cells[cols["narration"]] if "narration" in cols and cols["narration"] < len(cells)
                else " ".join(cells))
        run["rows"].append((mon, vals, cells[cols["date"]], narr[:60]))
        if len(run["rows"]) <= _LEDGER_KEEP:
            run["keep_to"] = len(run["lines"])
    close()
    if not notes:
        return text, None
    return "\n".join(out), "; ".join(notes)


# A workbook's sheets, as DocRAG writes them ("## Sheet: Overview-Month Wise").
_SHEET = re.compile(r"(?m)^## Sheet: (.+)$")
_LEDGER_ROWS = 40          # rows of a bulk sheet kept, so its columns and period show
_SHEET_CAP = 30_000        # one sheet's share of the document
_BULK_ROWS = 300           # a sheet past the cap with this many rows is a listing


def focus_workbook(text: str) -> tuple[str, str | None]:
    """A workbook's short sheets first, its bulk listings cut to their head.

    An analysed workbook (a bank-statement analysis, a CMA, a debtor ageing) holds its
    analysis in short sheets and the raw rows in long ones — and the long ones often
    come first, so a part-by-part read never reached the analysis. Sorted by SHAPE,
    not by sheet names (every vendor names them differently); what was cut is named."""
    heads = list(_SHEET.finditer(text or ""))
    if len(heads) < 2:
        return text, None
    sheets = [(m.group(1).strip(), text[m.start():heads[n + 1].start() if n + 1 < len(heads)
                                          else len(text)])
              for n, m in enumerate(heads)]

    def bulk(body: str) -> bool:
        return len(body) > _SHEET_CAP and body.count("\n|") > _BULK_ROWS

    if all(bulk(b) for _n, b in sheets) or not any(bulk(b) for _n, b in sheets):
        return text, None
    keep, cut, notes = [], [], []
    for name, body in sheets:
        if bulk(body):
            lines = body.splitlines()
            notes.append(f"sheet '{name}' ({len(lines):,} rows) not read line by line — "
                         "the workbook's shorter sheets (and PRISM's ledger summary) cover it")
            cut.append("\n".join(lines[:_LEDGER_ROWS])
                       + f"\n[… {len(lines) - _LEDGER_ROWS:,} more rows not read]\n")
        elif len(body) > _SHEET_CAP:
            notes.append(f"sheet '{name}' read to its first {_SHEET_CAP:,} characters "
                         f"of {len(body):,}")
            keep.append(body[:_SHEET_CAP] + "\n[… rest of this sheet not read]\n")
        else:
            keep.append(body)
    head = text[:heads[0].start()]
    return head + "".join(keep) + "".join(cut), ("; ".join(notes) or None)


# ------------------------------------------------------------- which documents to read
# A real file repeats itself: a bank-statement analysis and the raw statements it was
# built from, an "older extract" beside the newer one, five consent forms. Pulze: 68
# files, 83 parts to read, a third of it saying again what another file says. A file is
# left out only when that is PROVABLE, and named with the reason; everything that
# carries the assessment (financials, ITRs, CMA, analyses, bureau, sanction letters,
# term sheet, legal, ageing, orders, profile, KYC) is always read.
_BANK_NAMES = {
    "Bank of India": r"\bboi\b|bank of india|\bbkid",
    "Bank of Baroda": r"\bbob\b|bank of baroda|\bbarb\b",
    "Kotak": r"kotak|\bkkbk", "HDFC": r"\bhdfc", "ICICI": r"\bicici", "Axis": r"\baxis\b|\butib",
    "SBI": r"\bsbi\b|state bank", "Canara": r"canara|\bcnrb", "PNB": r"\bpnb\b|punjab national",
    "Union Bank": r"union bank|\bubin", "IDFC": r"\bidfc", "IndusInd": r"indusind|\bindb",
    "Yes Bank": r"\byes bank\b|\byesb", "Federal": r"federal bank|\bfdrl",
}
_ANALYSIS = re.compile(r"\bbsa\b|statement analysis|banking analysis", re.I)
_OLDER = re.compile(r"older extract|\bold(?:er)?\b|previous|superseded", re.I)
_CONSENT = re.compile(r"\bconsent\b", re.I)


def _words(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9&]+", " ", s.lower()) + " "


def bank_of(name: str, text: str = "") -> str | None:
    """The bank a statement is of: from its name, else its first page (bank name, IFSC
    prefix) — Pulze's BOI statements are titled "REPORT (2)"."""
    for src in (name, (text or "")[:4000]):
        w = _words(src)
        if (b := next((b for b, rx in _BANK_NAMES.items() if re.search(rx, w)), None)):
            return b
    return None


def ledger_months(text: str) -> set[str]:
    """The months PRISM's own ledger summaries in a text cover: their rows are the only
    ones shaped "| 2025-04 | 315 | …" (a month, then a count of transactions)."""
    return set(re.findall(r"(?m)^\| (\d{4}-\d{2}) \| \d+ \|", text or ""))


_ACCOUNT_NO = re.compile(r"(?:a/?c|account)\s*(?:no\.?|number|num)?\s*[:.\-]?\s*(?:x+|\*+)?"
                         r"(\d[\d\s]{3,20}\d)|(?:x{2,}|\*{2,})(\d{4})\b", re.I)


def accounts_of(name: str, text: str = "") -> set[str]:
    """The last four digits of the account numbers a statement NAMES as such ("A/C No",
    "Account Number", "XX6885") — so a bank's SECOND account is never taken as covered
    by the first's analysis. Transaction references are not account numbers; masked
    all-zero numbers say nothing."""
    out = set()
    for src in (name, text or ""):
        for m in _ACCOUNT_NO.finditer(src):
            num = re.sub(r"\s", "", m.group(1) or m.group(2) or "")
            if len(num) >= 4 and set(num) != {"0"}:
                out.add(num[-4:])
    return out


def select_documents(docs: list[SourceDoc]) -> tuple[list[SourceDoc], list[dict[str, Any]]]:
    """(the documents to read, a note for each one left out). Left out only when the
    file provably adds nothing: a raw statement whose every month a same-bank analysis
    covers; an older extract of a bank another statement covers; a consent form."""
    info = [(d, bank_of(d.name, d.text), ledger_months(d.text),
             bool(_ANALYSIS.search(_words(d.name)))) for d in docs]
    analyses = [(d, b, m) for d, b, m, a in info if a and b and m]
    accts = {d.doc_id: accounts_of(d.name, d.text) for d in docs}

    def same_account(x: SourceDoc, y: SourceDoc) -> bool:
        ax, ay = accts[x.doc_id], accts[y.doc_id]
        return not ax or not ay or bool(ax & ay)        # unknown on a side: by bank
    keep: list[SourceDoc] = []
    notes: list[dict[str, Any]] = []
    for d, bank, months, is_analysis in info:
        why = None
        if _CONSENT.search(d.name) and len(d.text) < 20_000:
            why = "a consent form — on record, nothing to extract"
        elif bank and not is_analysis:
            cover = next((a for a, b, m in analyses if b == bank and months and months <= m
                          and same_account(d, a)), None)
            if cover is not None:
                why = (f"covered by '{cover.name}' (same bank, every month of this statement "
                       f"— {min(months)} to {max(months)} — is in the analysis)")
            elif _OLDER.search(d.name) and any(
                    o is not d and b == bank and not _OLDER.search(o.name) and same_account(d, o)
                    for o, b, _m, _a in info):
                why = f"an older extract of a {bank} account a newer statement covers"
        if why:
            notes.append({"doc_id": d.doc_id, "document": d.name, "note": f"not read: {why}",
                          "skipped": True})
        else:
            keep.append(d)
    return keep, notes


# What a fact sheet says it is (its first line, per DIGEST_TASK). The sections are given
# the documents they are about in full, before the rest is clipped to the budget.
DOC_TYPES = ("financial statements", "income tax return", "CMA / projections",
             "bank statement", "bank statement analysis", "GST return", "term sheet",
             "sanction letter", "debt profile", "credit bureau report", "KYC",
             "net worth statement", "legal / litigation report", "company profile",
             "order / PO", "debtor / creditor ageing", "constitution / registration", "other")
_TYPE_LINE = re.compile(r"DOCUMENT TYPE:\s*\**\s*([^\n*|]+)", re.I)
_FIN = {"financial statements", "income tax return", "CMA / projections",
        "debtor / creditor ageing", "debt profile"}
_BANK = {"bank statement", "bank statement analysis", "GST return", "income tax return"}
_PEOPLE = {"KYC", "credit bureau report", "net worth statement", "constitution / registration",
           "company profile"}
_BUSINESS = {"company profile", "order / PO", "debtor / creditor ageing", "CMA / projections"}
_TERMS = {"term sheet", "sanction letter", "debt profile"}
_LEGAL = {"legal / litigation report", "credit bureau report", "constitution / registration",
          "KYC", "income tax return"}
_WANTS: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(r"BANK|GST|ITR"), _BANK),
    (re.compile(r"FINANCIAL|WORKING CAPITAL|EXECUTIVE"), _FIN | {"bank statement analysis"}),
    (re.compile(r"MANAGEMENT|BORROWER PROFILE"), _PEOPLE),
    (re.compile(r"BUSINESS MODEL|INDUSTRY|ORDER BOOK|PROPOSAL SUMMARY"), _BUSINESS),
    (re.compile(r"SECURITY|APPENDIX|RECOMMENDATION|PROPOSAL SUMMARY|DEBT PROFILE"), _TERMS),
    (re.compile(r"COMPLIANCE"), _LEGAL),
    (re.compile(r"BUREAU|CIBIL"), {"credit bureau report"}),
]


def doc_type(sheet: str, name: str = "") -> str:
    """The fact sheet's own DOCUMENT TYPE line, mapped onto DOC_TYPES; a term sheet or
    company profile is also known by its name (the two documents quoted verbatim)."""
    if re.search(r"term\s*sheet", name, re.I):
        return "term sheet"
    m = _TYPE_LINE.search(sheet[:600])
    said = (m.group(1).strip().rstrip(".").casefold() if m else "")
    for t in sorted(DOC_TYPES, key=len, reverse=True):     # "bank statement analysis" first
        if t != "other" and said and (said == t.casefold() or said.startswith(t.casefold())):
            return t
    if re.search(r"company profile|pitch|brochure", name, re.I):
        return "company profile"
    return "other"


def wanted(heading: str) -> set[str]:
    out: set[str] = set()
    for rx, types in _WANTS:
        if rx.search(heading.upper()):
            out |= types
    return out


def fit_for(sheets: list[tuple[str, str]], budget: int, want: set[str]) -> list[str]:
    """``fit`` with the section's own documents first: they keep up to 60% of the
    budget whole; the rest shares what is left, longest clipped first."""
    if not want:
        return fit([s for _t, s in sheets], budget)
    mine = [s for t, s in sheets if t in want]
    rest = [s for t, s in sheets if t not in want]
    mine_fit = fit(mine, int(budget * 0.6))
    rest_fit = fit(rest, max(budget - sum(len(s) for s in mine_fit), 2000 * len(rest)))
    return mine_fit + rest_fit


# ------------------------------------------------------------------------- prompts
DIGEST_TASK = (
    "Produce the FACT SHEET for the document above — extraction only, no credit opinion. "
    "Its FIRST line is exactly 'DOCUMENT TYPE: <one of: " + "; ".join(DOC_TYPES) + ">'. "
    "Then use these headings, omitting any with nothing to report: Document (type, entity, "
    "period covered, date); Key figures (a Markdown table: Item | Period | Value | Unit | "
    "Source); Borrowings & debt; Debtors, creditors & order book; Banking, GST & ITR; "
    "Management, ownership & group; Business (what it makes or sells, products, "
    "capacity, plants and locations, certifications, customers, suppliers, history, "
    "people — every specific the document gives); Legal, compliance & security; "
    "Qualitative observations; Gaps & red flags (what a CAM would need that this document "
    "lacks, internal inconsistencies). "
    "A bank statement or bank-statement analysis: give a MONTH-WISE table (Month | "
    "Credits | Debits | Closing / average / lowest balance | Inward and outward returns) "
    "with the account, bank, limit and period, then overdrawings or limit breaches, "
    "cheque/ECS/NACH returns with dates and amounts, loan EMIs and interest debits, GST and "
    "tax payments, related-party and cash transactions, and the largest counterparties — "
    "never transactions one by one. A GST return or GST analysis: a month-wise table of "
    "taxable turnover and tax paid, with filing dates and delays. Financial statements: "
    "every P&L and balance-sheet line as the statement prints it (do not merge lines, keep "
    "current and non-current apart, keep term loans and working-capital borrowings apart). "
    "Copy every figure exactly as stated, with its unit and period, and tag it [source: "
    "<document name>, page N]. Never compute, estimate or infer a figure that is not "
    "written in the document.")

DATASET_TASK = (
    "Build the FINANCIAL DATASET WORKPAPER from the fact sheets above, as the prompt's "
    "Steps 1 and 4 require. It is LOCKED once written: every CAM section will quote it. "
    "Include: (1) the multi-year P&L and balance sheet table in the prompt's currency "
    "convention, one column per period, each figure source-tagged; (2) the inputs each "
    "A3.5 ratio needs, source-tagged (the ratios themselves are computed by PRISM from "
    "your figures block — do not compute them); (3) the debt structure map and debtor "
    "book map; (4) triangulation across "
    "documents against the prompt's variance thresholds, naming every conflict; (5) the "
    "stress tests the prompt specifies, with post-stress DSCR; (6) the Analyst Base Case "
    "if the prompt's trigger applies. Where documents disagree, show both values and the "
    "one you adopt with the reason. Mark any figure you could not source 'not on record'. "
    "Periods: an ITR or tax audit names an ASSESSMENT year — its financial year is the "
    "year before (AY 2024-25 = FY 2023-24 = FY24); label every column by financial year "
    "and say which documents gave it. Do not mix standalone and consolidated, or audited "
    "and provisional, figures in one column. Totals are the statement's own totals, never "
    "a subtotal (total current liabilities is not total outside liabilities; a term loan "
    "is not working-capital debt). PRISM ledger summaries in the fact sheets are exact "
    "sums of the rows — prefer them for bank credits and debits. "
    "Output Markdown, headed '## Financial dataset workpaper'.")

# The IRG's dataset when no CAM dataset is cached: the statements, the locked figures and
# the reconciliation the grade needs — not the CAM's stress tests, base case and maps,
# which made the step ten minutes. Same reasoning effort, a fraction of the answer.
LIGHT_DATASET_TASK = (
    "Build the FINANCIAL DATASET WORKPAPER from the fact sheets above — CONCISE: (1) the "
    "multi-year P&L and balance sheet table in the prompt's currency convention, one column "
    "per period, basis labelled (audited / provisional / projected), each figure "
    "source-tagged; (2) a short debt table (lender, facility, sanctioned, outstanding); (3) "
    "where documents disagree on a figure, both values and the one you adopt, in one line "
    "each. No stress tests, base case, debtor map or narrative. Mark any figure you could "
    "not source 'not on record'. Output Markdown, headed '## Financial dataset workpaper'.")

CHECKS_TASK = (
    "From the public-information phase above, list every public check the prompt requires "
    "for this borrower (at least the minimum it sets), as one Markdown table: Check | "
    "Source / query | Status | Finding. Where the PUBLIC SOURCES PRISM HOLDS block "
    "answers a check, Status is 'Verified via <source>, <date>' (or 'Partly verified via "
    "…') and Finding states ONLY what that source shows, cited. Every other row: Status "
    "'NOT PERFORMED — to be completed by the analyst' (this environment has no web or "
    "registry access), Finding blank. Never state a public fact that is not in that "
    "block. Head it '## Public information checks'. Output only that section.")


TITLE_TASK = (
    "Write ONLY the CAM's TITLE PAGE block, as plain lines (no heading, no table, no "
    "blank lines), in this order: 'Evam Finance Private Limited'; the borrower's legal "
    "name; its constitution and location; 'Proposed Facility: <facility type> — Rs. "
    "<amount> lakh' (write 'facility type not on record' when the type is unknown); the "
    "key terms on one line if on record (rate | tenor | availability), in the term sheet's "
    "own words; 'Document date: "
    "{today}'; 'Financial data as of <the latest period in the locked dataset, and "
    "whether audited or provisional>'; the dates the banking, debtor and debt data run "
    "to, where on record; 'Prepared by: <the analyst on the Register, else Credit Risk "
    "Group>'. Take each fact from the deal on the Register, the locked dataset or the "
    "CAM body; write 'not on record' for any that is missing — never invent one.")


RATIOS_RULE = (
    " Ratios, margins, growth rates, day counts and coverage figures: quote ONLY the "
    "'Computed ratios' table in the locked dataset — never compute one yourself; if a "
    "ratio you need is 'not computable' or absent, say so and name the missing input.")

# The credit team's review of the first real CAM (Pulze, Oct 2026): a tag on every data
# point cluttered it; terms were paraphrased and extended; annexures restated the body.
CITATION_RULE = (
    " Cite sparingly: tag only the key figures and findings a reviewer would check (a "
    "bureau score, a limit breach, a covenant, a figure that conflicts between documents) "
    "as [source: <document>, page N] — at most one tag per paragraph, none in table cells. "
    "Never put a 'Source' column in a table.")
EXEMPLAR_RULE = (
    " The prompt's examples and exemplar sentences set tone and depth only: never reuse "
    "their figures, amounts, company names or places.")
TERMS_RULE = (
    " Facility terms (amount, rate, fees, charges, tenor, availability, drawdown, repayment, "
    "security, guarantees, covenants, conditions, jurisdiction) are quoted from the TERM "
    "SHEET exactly as it words them — never paraphrased, rounded, interpreted, extended or "
    "added to. Where the term sheet defers or omits a term, say so in its own words (e.g. "
    "'To be decided upon final sanction'); never supply a market-standard term instead.")
APPENDIX_TASK = (
    "The appendix is the TERM SHEET, transcribed: one 2-column table (Head | Terms), one "
    "row per head in the order the term sheet gives them, each Terms cell the term sheet's "
    "own text verbatim (only line breaks become '; '). Nothing else: no pricing summary, "
    "covenants, reporting schedule, events of default or general terms that the term sheet "
    "does not itself state.")
ANNEX_RULE = (
    " Annexures are reference tables, not narrative: a row appears only when the documents "
    "give its value or it needs the analyst's action; do not restate the body's analysis; "
    "no notes paragraphs. Sub-sections that cannot apply to this borrower's constitution "
    "(e.g. shareholding or DINs for a partnership) are not written — name them once in one "
    "line.")
_TERMS_PART = re.compile(r"PROPOSAL SUMMARY|SECURITY|RECOMMENDATION|APPENDIX|EXECUTIVE", re.I)


def section_task(spec: SectionSpec, computed: bool = False, *, terms: bool = False) -> str:
    appendix = terms and spec.heading.upper().startswith("APPENDIX")
    return (f"Write ONLY this part of the CAM: {spec.title}\n\nIts specification:\n"
            + (APPENDIX_TASK if appendix else spec.spec)
            + f"\n\nStart with the heading '## {spec.heading}', use '###' for sub-sections and "
            "Markdown tables with a header row. Quote figures from the LOCKED financial "
            "dataset exactly — never recompute or alter them."
            + (RATIOS_RULE if computed else "")
            + CITATION_RULE + EXEMPLAR_RULE
            + (TERMS_RULE if terms and _TERMS_PART.search(spec.heading) else "")
            + (ANNEX_RULE if spec.heading.upper().startswith("ANNEXURE") else "")
            + " Where the documents are silent, say 'not on record'. Do not write any "
            "other section.")


REPAIR_TASK = (
    "Your draft of this part of the CAM is below. PRISM checked it against every source "
    "above (the fact sheets, the locked dataset, the term sheet and the deal on record) "
    "and found:\n{issues}\n\nFix each. A figure no source has: find the right figure in the "
    "sources and correct it, or remove the statement (a lakh/crore conversion of a source "
    "figure, or a share or difference whose inputs the sentence shows, may stay). A "
    "document called missing or empty that IS on record: use its fact sheet above — if "
    "the claim is about a period or item that document truly lacks, say exactly what it "
    "does cover. Change nothing else. Return the whole part, heading included.\n\n"
    "===== YOUR DRAFT =====\n{draft}")
DATASET_FIX_TASK = (
    "PRISM checked the locked figures of the workpaper below and found these breaks of "
    "balance-sheet identities or plausibility: {flags}. Each is almost always a mis-read "
    "line (a subtotal taken for a total, a figure copied into the wrong line, a unit slip). "
    "Re-read the fact sheets above, correct the workpaper's tables and the locked-figures "
    "block, and return the WHOLE corrected workpaper ending with the block. Where the "
    "source really states a figure that breaks a check, keep it and say why in the "
    "triangulation notes.\n\n===== THE WORKPAPER =====\n{workpaper}")


# ----------------------------------------------------------------------- machine QA
_EMPTY_CLAIM = re.compile(r"empty template|\b(?:is|are|appears?|seems?) (?:to be )?(?:empty|"
                          r"blank)\b|no (?:populated |transaction )?data|contains? no (?:data|"
                          r"figures|transactions|entries)|only (?:section )?headers", re.I)
# A figure, also as a multiple ("2.5x") or a percentage ("12.5%").
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?=x\b|[^\w]|$)")
_FAILED = "This section could not be drafted"


def _canon(value: str) -> str | None:
    try:
        f = abs(float(value.replace(",", "")))
    except ValueError:
        return None
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _figures(text: str) -> set[str]:
    return {c for m in _NUM.finditer(text) if (c := _canon(m.group(0)))}


def _traced(raw: str, locked: list[float]) -> bool:
    """A table figure is the dataset's when some dataset value, rounded to the
    figure's own precision, equals it — "1,048.4" in Section 5 is "1,048.43" in the
    dataset shown to one decimal, not a new number."""
    from decimal import ROUND_HALF_UP, Decimal

    places = len(raw.split(".")[1]) if "." in raw else 0
    step = Decimal(1).scaleb(-places)
    shown = Decimal(raw.replace(",", ""))
    # Half-up, as figures are rounded on paper: Python's round() takes 1204.5 to 1204.
    return any(Decimal(str(x)).quantize(step, ROUND_HALF_UP) == shown for x in locked)


def _blocks(draft_md: str) -> list[tuple[str, str]]:
    """(heading, body) for every '## ' part of the draft, in order."""
    out: list[tuple[str, str]] = []
    for part in re.split(r"(?m)^(?=## )", draft_md):
        if part.startswith("## "):
            head, _, body = part.partition("\n")
            out.append((head[3:].strip(), body))
    return out


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.casefold().replace("&", " and ")).strip()  # noqa: E731
    return norm(a) == norm(b)


def machine_qa(prompt: MasterPrompt, draft_md: str, workpaper_md: str, *,
               ratios_from_code: bool | None = None, ratios_note: str | None = None,
               implausible: list[str] | None = None, untraced: list[str] | None = None,
               traced_off: dict[str, list[str]] | None = None,
               terms_off: list[str] | None = None,
               missing_claims: dict[str, list[str]] | None = None,
               term_sheet: bool | None = None) -> dict[str, Any]:
    """The master prompt's Step 7, the part a program can check — before an analyst
    reads the draft. Every check is named with what it found; a failure is reported,
    never silently fixed, so the analyst knows exactly what to look at.

    - every section and annexure of the prompt is present, and none twice;
    - no section was left as a "could not be drafted" placeholder;
    - every figure in the Financial Analysis tables is in the locked dataset;
    - each section's length is near the prompt's word range."""
    checks: list[dict[str, Any]] = []
    blocks = _blocks(draft_md)

    missing = [s.heading for s in prompt.sections
               if not any(_same(h, s.heading) for h, _b in blocks)]
    checks.append({"check": "All sections and annexures present",
                   "status": "fail" if missing else "pass",
                   "detail": ("missing: " + "; ".join(missing)) if missing
                   else f"{len(prompt.sections)} of {len(prompt.sections)}"})

    dupes = sorted({s.heading for s in prompt.sections
                    if sum(_same(h, s.heading) for h, _b in blocks) > 1})
    checks.append({"check": "No section duplicated", "status": "fail" if dupes else "pass",
                   "detail": ("twice: " + "; ".join(dupes)) if dupes else "none"})

    failed = [h for h, b in blocks if _FAILED in b]
    checks.append({"check": "Every section drafted", "status": "fail" if failed else "pass",
                   "detail": ("not drafted: " + "; ".join(failed)) if failed else "all"})

    fin = next((b for h, b in blocks if "FINANCIAL ANALYSIS" in h.upper()), None)
    if fin is None:
        checks.append({"check": "Financial Analysis figures match the locked dataset",
                       "status": "warn", "detail": "no Financial Analysis section"})
    else:
        locked = [float(v) for v in _figures(workpaper_md)]
        cells = [c for ln in fin.splitlines() if ln.lstrip().startswith("|")
                 and not re.fullmatch(r"[\s|:\-]+", ln) for c in ln.split("|")]
        figs = []
        for c in cells:
            for m in _NUM.finditer(c):
                raw = m.group(0)
                v = _canon(raw)
                # Years and small counts are labels, not figures.
                if v is None or re.fullmatch(r"(19|20)\d\d", raw) or ("." not in raw
                                                                       and float(v) < 100):
                    continue
                figs.append((raw, v))
        off = sorted({raw for raw, _v in figs if not _traced(raw, locked)})
        checks.append({
            "check": "Financial Analysis figures match the locked dataset",
            "status": "fail" if off else ("pass" if figs else "warn"),
            "detail": (f"{len(off)} of {len(figs)} not in the dataset (computed in the "
                       "section, not quoted): " + ", ".join(off[:12])
                       + (" …" if len(off) > 12 else "")) if off
            else (f"all {len(figs)} traced" if figs else "no table figures found")})

    lengths = []
    for s in prompt.sections:
        rng = s.word_range
        body = next((b for h, b in blocks if _same(h, s.heading)), None)
        if not rng or body is None or _FAILED in body:
            continue
        words = len(re.findall(r"\w+", "\n".join(ln for ln in body.splitlines()
                                                 if not ln.lstrip().startswith("|"))))
        if words < rng[0] * 0.7 or words > rng[1] * 1.3:
            lengths.append(f"{s.heading.split(':')[0]}: {words} words "
                           f"(asked {rng[0]}–{rng[1]})")
    checks.append({"check": "Section lengths near the prompt's ranges",
                   "status": "warn" if lengths else "pass",
                   "detail": "; ".join(lengths) if lengths else "within range"})

    if ratios_from_code is not None:
        checks.append({"check": "Ratios computed by PRISM (A3.5 locked formulas)",
                       "status": "pass" if ratios_from_code else "warn",
                       "detail": "from the locked figures" if ratios_from_code else
                       f"{ratios_note or 'no figures block'} — ratios in this draft are "
                       "the model's own arithmetic; verify them"})
    if ratios_from_code:
        flags = implausible or []
        # Growth can be real (a first full year, a start-up) as easily as a unit slip:
        # it is flagged for a look. Days, leverage, coverage and liquidity beyond any
        # working business are an extraction problem until shown otherwise: they fail.
        growth = [f for f in flags if f.startswith(("Revenue growth", "Revenue CAGR"))]
        bad = [f for f in flags if f not in growth]
        checks.append({"check": "Ratios plausible (no likely unit or period slip)",
                       "status": "fail" if bad else ("warn" if growth else "pass"),
                       "detail": "; ".join(filter(None, [
                           ("check the inputs' unit and period: " + "; ".join(bad[:8])
                            + (" …" if len(bad) > 8 else "")) if bad else "",
                           ("growth to confirm — a first year, or a unit slip? "
                            + "; ".join(growth)) if growth else ""])) or "all in range"})
        checks.append({"check": "Extracted figures traced to the workpaper tables",
                       "status": "warn" if untraced else "pass",
                       "detail": ("dropped, not in the tables: " + "; ".join((untraced or [])[:8]))
                       if untraced else "all traced"})

    if traced_off is not None:
        off = [f"{h.split(':')[0]}: {', '.join(v[:6])}" + (" …" if len(v) > 6 else "")
               for h, v in traced_off.items() if v]
        checks.append({"check": "Figures traced to the sources (every section)",
                       "status": "warn" if off else "pass",
                       "detail": ("in no source after one correction pass — verify: "
                                  + "; ".join(off)) if off else "all traced"})
    if missing_claims is not None:
        said = [f"{h.split(':')[0]}: {v[0]}" for h, v in missing_claims.items() if v]
        checks.append({"check": "No supplied document called missing",
                       "status": "fail" if said else "pass",
                       "detail": "; ".join(said[:6]) if said else "none"})
    if terms_off is not None:
        checks.append({"check": "Appendix terms match the term sheet",
                       "status": "fail" if terms_off else "pass",
                       "detail": "; ".join(terms_off[:10]) if terms_off else "verbatim"})
    elif term_sheet is False:
        checks.append({"check": "Appendix terms match the term sheet", "status": "warn",
                       "detail": "no term sheet among the documents — the terms come from "
                       "the Register and the analyst's notes; attach the term sheet"})

    gaps = len(re.findall(r"not on record", draft_md, re.I))
    checks.append({"check": "Facts marked 'not on record'", "status": "info",
                   "detail": f"{gaps} place(s) — the documents were silent there"})
    return {"passed": not any(c["status"] == "fail" for c in checks), "checks": checks}


_NOT_HELD = re.compile(r"not available|not configured|unreachable|not checked|none found"
                       r"|HTTP [45]\d\d|no answer", re.I)


def unverify_unavailable(table_md: str) -> str:
    """A check cannot be "verified" by a source PRISM did not have: a row marked
    Verified / Partly verified whose finding says the source was not available is
    put back to NOT PERFORMED — enforced here, not left to the model."""
    out = []
    for ln in table_md.splitlines():
        cells = ln.split("|")
        if len(cells) >= 6 and re.search(r"\bverified\b", cells[3], re.I) \
                and _NOT_HELD.search(cells[4]):
            cells[3] = " NOT PERFORMED — the source was not available to PRISM "
            ln = "|".join(cells)
        out.append(ln)
    return "\n".join(out)


# ------------------------------------------------------------ deterministic tidy-up
# Done in code, after each section is written: a model asked to cite sparingly still
# tags most cells, and asked to keep annexures lean still writes "not on record" rows.
_BRACKET = re.compile(r"\s*\[([^\[\]\n]{2,300})\](?!\()")
_PREFIX = re.compile(r"^\s*sources?\s*:\s*", re.I)
_PAGE = re.compile(r",\s*(?:p\.|pp\.|pages?\b|sheet\b).*$|\s+pp?\.\s*\d.*$", re.I)


def _cited(inner: str, names: list[str]) -> list[str] | None:
    """The references in one bracket ("source: A, page 2; B"), or None when the bracket
    is not a citation (a placeholder, a note)."""
    prefixed = bool(_PREFIX.match(inner))
    refs = [r.strip() for r in _PREFIX.sub("", inner).split(";") if r.strip()]
    if not refs:
        return None
    if prefixed:
        return refs
    low = refs[0].casefold()
    doc = _doc_of(refs[0]).casefold()
    for n in names:
        nl = n.casefold()
        if low == nl or low.startswith(nl + ",") or low.startswith(nl + " ,"):
            return refs
        # A shortened name ("App_3998679368_Cons_CIR_SiddhaarthAB" for the CIR's full id).
        if len(doc) >= 8 and (doc in nl or nl in doc
                              or _squash(doc) in _squash(nl) or _squash(nl) in _squash(doc)):
            return refs
    return None


def _squash(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.casefold())


def _doc_of(ref: str) -> str:
    return _PAGE.sub("", ref).strip(" ,")


def _strip_tags(text: str, names: list[str]) -> tuple[str, list[str]]:
    refs: list[str] = []

    def drop(m: re.Match[str]) -> str:
        got = _cited(m.group(1), names)
        if got is None:
            return m.group(0)
        refs.extend(got)
        return ""

    return _BRACKET.sub(drop, text), refs


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        if i and i.casefold() not in seen:
            seen.add(i.casefold())
            out.append(i)
    return out


def _source_line(docs: list[str]) -> str:
    docs = _uniq(docs)
    shown = "; ".join(docs[:6]) + (f"; and {len(docs) - 6} more" if len(docs) > 6 else "")
    return f"*Source: {shown}.*"


def tidy_citations(md: str, names: list[str]) -> str:
    """One source line under each table instead of a tag in every cell (and no Source
    column); in prose, the tags of one line merged into one at its end."""
    names = sorted({n for n in names if n}, key=len, reverse=True)
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            rows = [_split_cells(ln) for ln in lines[i:j]]
            docs: list[str] = []
            src_col = next((c for c, h in enumerate(rows[0])
                            if re.fullmatch(r"\**\s*sources?\s*\**", h.strip(), re.I)), None)
            table = []
            for r, cells in enumerate(rows):
                if src_col is not None and src_col < len(cells):
                    if r > 0 and not re.fullmatch(r"[\s:\-]*", cells[src_col]):
                        _t, refs = _strip_tags(f"[source: {cells[src_col]}]", names)
                        docs += [_doc_of(x) for x in refs]
                    cells = cells[:src_col] + cells[src_col + 1:]
                clean = []
                for c in cells:
                    c, refs = _strip_tags(c, names)
                    docs += [_doc_of(x) for x in refs]
                    clean.append(c.strip())
                table.append("| " + " | ".join(clean) + " |")
            out += table
            docs = [d for d in docs if d and not re.fullmatch(r"[—–\-]+", d)
                    and not re.search(r"not performed|not on record|pending|^n/?a$", d, re.I)]
            if docs:
                out += ["", _source_line(docs)]
            i = j
            continue
        line = lines[i]
        if not line.lstrip().startswith("#"):
            text, refs = _strip_tags(line, names)
            if refs:
                tag = " [source: " + "; ".join(_uniq(refs)) + "]"
                stripped = text.rstrip()
                line = (stripped[:-1] + tag + "." if stripped.endswith(".")
                        else stripped + tag)
        out.append(line)
        i += 1
    return "\n".join(out)


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


_EMPTY_CELL = re.compile(r"(not on record|n/?a|na \(not provided\)|not applicable\b.*|none"
                         r"|nil|not disclosed|not available|[—–\-]*)\.?", re.I)


def prune_annexure(md: str) -> str:
    """An annexure keeps the rows that say something: a row whose every value cell is
    'not on record' / 'NA' / blank goes, a table left with no rows goes, and a
    sub-section that is only a 'Not applicable' line is named once at the end."""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            block = lines[i:j]
            top, rule, rows = block[:1], block[1:2], block[2:]
            if rule and re.fullmatch(r"[\s|:\-]+", rule[0]):
                keep = [ln for ln in rows
                        if not (vals := _split_cells(ln)[1:])
                        or not all(_EMPTY_CELL.fullmatch(c.strip(" *_")) for c in vals)]
                out += (top + rule + keep) if keep else []
            else:
                out += block
            i = j
            continue
        out.append(lines[i])
        i += 1
    # Sub-sections whose whole body is one "not applicable" line (or nothing).
    text = "\n".join(out)
    parts = re.split(r"(?m)^(?=### )", text)
    kept, na = [parts[0]], []
    for part in parts[1:]:
        head, _, body = part.partition("\n")
        words = re.sub(r"[\s*_\-—]+", " ", body).strip()
        if not words or (re.match(r"not applicable", words, re.I) and len(words) < 220
                         and "|" not in body):
            na.append(head[4:].strip())
            continue
        kept.append(part)
    text = "".join(kept).rstrip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if na:
        text += "\n\n*Not applicable to this borrower: " + "; ".join(na) + ".*"
    return text + "\n"


def compact_checks(table_md: str) -> str:
    """The public-checks log keeps the checks PRISM performed as rows; the ones it could
    not perform are one line for the analyst, grouped ("Litigation ×5")."""
    rows, other, pending = [], [], []
    for ln in table_md.splitlines():
        if ln.lstrip().startswith("|"):
            rows.append(ln)
        else:
            other.append(ln)
    if len(rows) < 3:
        return table_md
    head, rule, body = rows[0], rows[1], rows[2:]
    done = []
    for ln in body:
        cells = _split_cells(ln)
        status = cells[2] if len(cells) > 2 else ""
        if re.match(r"\s*(not performed|not available|not checked)", status, re.I):
            name = re.split(r"\s+[—–-]\s+|\s*\(", cells[0])[0].strip(" *")
            pending.append(re.sub(r"\s+\d+$", "", name))     # "Listed peer 2" → ×2
        else:
            done.append(ln)
    counts: dict[str, int] = {}
    for p in pending:
        counts[p] = counts.get(p, 0) + 1
    out = [ln for ln in other if ln.strip()]
    if done:
        out += ["", head, rule, *done]
    if counts:
        out += ["", "**Not performed by PRISM — for the analyst to complete:** "
                + "; ".join(k + (f" ×{n}" if n > 1 else "") for k, n in counts.items()) + "."]
    return "\n".join(out).strip()


# Figures in a section that no source has: the data errors the review found. Matched
# with lakh/crore/rupee conversions, at the precision the section writes them.
# Same figure, other unit: lakh↔crore, rupees↔lakh, rupees↔crore, thousands.
_SCALES = tuple(Decimal(x) for x in ("1", "100", "0.01", "100000", "0.00001", "10000000",
                                     "0.0000001", "1000", "0.001"))
_AMOUNT = re.compile(r"(?:Rs\.?|₹|INR)\s*(\d[\d,]*(?:\.\d+)?)"
                     r"|(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(?:lakh|crore|cr\b|L\b)", re.I)


def figure_index(*texts: str) -> set[str]:
    idx: set[str] = set()
    for t in texts:
        for m in _NUM.finditer(t or ""):
            try:
                d = Decimal(m.group(0).replace(",", ""))
            except ArithmeticError:
                continue
            for s in _SCALES:
                v = d * s
                for places in (0, 1, 2):
                    idx.add(str(v.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)))
    return idx


def _indexed(raw: str, idx: set[str]) -> bool:
    places = min(len(raw.split(".")[1]) if "." in raw else 0, 2)
    try:
        shown = Decimal(raw.replace(",", ""))
    except ArithmeticError:
        return True
    return str(shown.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)) in idx


def _label(raw: str) -> bool:
    """Years and small counts are labels, not figures."""
    return bool(re.fullmatch(r"(19|20)\d\d", raw)) or ("." not in raw
                                                        and float(raw.replace(",", "")) < 100)


def untraced_figures(section_md: str, idx: set[str],
                     names: list[str] | None = None) -> list[str]:
    """Every table figure and every prose amount (Rs./lakh/crore) of a section that is in
    none of the sources."""
    body = "\n".join(ln for ln in section_md.splitlines() if not ln.lstrip().startswith("#"))
    body, _refs = _strip_tags(body, names or [])
    body = re.sub(r"(?m)^\*Source:.*$", "", body)
    raws: list[str] = []
    amounts: list[str] = []
    for ln in body.splitlines():
        if ln.lstrip().startswith("|"):
            # A total is the section's own sum of rows it shows, not a new figure.
            if re.fullmatch(r"[\s|:\-]+", ln) or re.search(r"\btotal\b|\bsum\b",
                                                           _split_cells(ln)[0], re.I):
                continue
            raws += [m.group(0) for m in _NUM.finditer(ln)]
        else:
            # An amount in prose is checked at any size: "the first Rs. 2 crore
            # facility" (a prompt exemplar's figure) is as wrong as a big one.
            amounts += [m.group(1) or m.group(2) for m in _AMOUNT.finditer(ln)]
    picked = {r for r in raws if r and not _label(r)} | {
        r for r in amounts if r and not re.fullmatch(r"(19|20)\d\d", r)}
    return sorted(r for r in picked if not _indexed(r, idx))


# "GST returns are absent", "the BSA reports are empty templates", "no legal report":
# a section saying a KIND of document is missing while one was read. The Pulze draft
# said it of three bank-statement analyses and a legal report; any kind can be next.
_KIND_WORDS: dict[str, str] = {
    "bank statement": r"bank statements?|banking data|bank credits|statement of account",
    "bank statement analysis": r"\bBSA\b|bank statement analys[ie]s|banking analysis",
    "GST return": r"\bGST(?:R|R-?\d\w?| returns?| data| turnover| filings?)\b",
    "income tax return": r"\bITRs?\b|income[- ]tax returns?",
    "financial statements": r"audited (?:financials|accounts|statements)|financial statements"
                            r"|balance sheets?|profit (?:and|&) loss",
    "credit bureau report": r"\bCIBIL\b|bureau reports?|\bCIRs?\b|\bCMR\b",
    "legal / litigation report": r"legal (?:report|due diligence)|litigation (?:report|search)",
    "debtor / creditor ageing": r"(?:debtor|creditor|receivables?) (?:ageing|aging|list)",
    "order / PO": r"order book|purchase orders?|\bPOs?\b",
    "sanction letter": r"sanction letters?",
    "debt profile": r"debt profile|loan schedule",
    "CMA / projections": r"\bCMA\b|projections",
    "company profile": r"company profile|pitch deck",
    "KYC": r"\bKYC\b|\bPAN\b|aadhaa?r",
}
# A claim has a shape, not just two nearby words: the document IS absent ("the GST returns
# are not provided", "BSA reports are empty templates"), or absence comes before it ("no
# bank statements", "in the absence of GST returns"). "CIRs report income as 'Not
# Available'" quotes a field and "consent … but the annexure is left blank" describes
# another document — neither says the document is missing.
_IS_ABSENT = (r"[^.;|\n]{0,40}?\b(?:is|are|was|were|has been|have been|remains?|appears?|"
              r"seems?)\s+(?:(?:an?|the|only|still)\s+)?(?:not\s+(?:been\s+)?(?:provided|supplied|"
              r"available|on record|submitted|shared|populated)|missing|absent|unavailable|"
              r"empty|blank|templates?\b(?:\s+with\s+no)?)")
_ABSENT_BEFORE = (r"\b(?:no|missing|absent|without|lack of|absence of|non-?availability of)"
                  r"\s+(?:[\w-]+\s+){0,4}?")


def claims_missing(section_md: str, present: dict[str, list[str]]) -> list[str]:
    """Sentences that call a kind of document missing or empty when PRISM read one."""
    out = []
    body = "\n".join(ln for ln in section_md.splitlines() if not ln.lstrip().startswith("#"))
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+|\|", body):
        for kind, rx in _KIND_WORDS.items():
            names = present.get(kind) or []
            is_absent = [m for m in re.finditer(f"(?:{rx}){_IS_ABSENT}", sentence, re.I)
                         # "whether they are firm POs … is not on record": the answer to a
                         # question is missing, not the document.
                         if not re.search(r"\b(?:whether|if|how|why|when|which)\b",
                                          sentence[max(0, m.start() - 40):m.start()], re.I)]
            if names and (is_absent or re.search(f"{_ABSENT_BEFORE}(?:{rx})", sentence, re.I)):
                out.append(f"'{sentence.strip()[:140]}' — but {kind} on record: "
                           + "; ".join(names[:4]))
                break
    return _uniq(out)


def terms_drift(appendix_md: str, term_text: str) -> list[str]:
    """Appendix rows that are not the term sheet's words: a figure the term sheet does not
    have, or a cell most of whose words it never uses."""
    idx = figure_index(term_text)
    vocab = set(re.findall(r"[a-z]{4,}", term_text.casefold()))
    out = []
    lines = appendix_md.splitlines()
    for n, ln in enumerate(lines):
        if not ln.lstrip().startswith("|") or re.fullmatch(r"[\s|:\-]+", ln):
            continue
        if n + 1 < len(lines) and re.fullmatch(r"\s*\|[\s|:\-]+", lines[n + 1]):
            continue                                   # a header row
        cells = [re.sub(r"\[[^\]]*\]", "", c) for c in _split_cells(ln)]
        if len(cells) < 2:
            continue
        head, terms = cells[0].strip(" *"), " ".join(cells[1:])
        figs = [m.group(0) for m in _NUM.finditer(terms)
                if not re.fullmatch(r"(19|20)\d\d", m.group(0))]
        off = [f for f in figs if not _indexed(f, idx)]
        words = re.findall(r"[a-z]{4,}", terms.casefold())
        foreign = [w for w in words if w not in vocab]
        if off:
            out.append(f"{head}: {', '.join(off)} not in the term sheet")
        elif words and len(foreign) / len(words) > 0.35:
            out.append(f"{head}: reworded or added ({', '.join(_uniq(foreign)[:4])} …)")
    return out


def qa_markdown(qa: dict[str, Any]) -> str:
    rows = ["## Machine QA report", "",
            f"**Result:** {'passed' if qa.get('passed') else 'FAILED — see below'}", "",
            "| Check | Status | Detail |", "| --- | --- | --- |"]
    rows += [f"| {c['check']} | {c['status'].upper()} | {c['detail']} |"
             for c in qa.get("checks", [])]
    return "\n".join(rows)


# -------------------------------------------------------------------------- engine
# (system, turns) → text; the staged drafter may also pass ``effort=`` for one call.
Engine = Callable[..., Awaitable[str]]
# (typed fact sheets, the locked dataset, the result so far) → extra context for writers.
Interlude = Callable[[list[tuple[str, str]], str, "DraftResult"], Awaitable[str]]
Progress = Callable[[str, int, int], None]


@dataclass
class DraftResult:
    draft_md: str
    workpaper_md: str
    notes: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    qa: dict[str, Any] = field(default_factory=dict)
    figures: dict[str, Any] = field(default_factory=dict)   # what the ratios came from
    # What a caller's interlude returned (the IRG: the rubric's grade, computed between
    # the locked dataset and the sections so every section can explain it).
    extra: dict[str, Any] = field(default_factory=dict)


class DigestCache:
    """Fact sheets are deterministic in (model, effort, rules, document text): reuse them.

    With a ``directory`` (a volume) they also survive restarts: a deploy no longer means
    re-reading every document, and a CAM re-drafted on the same file goes straight to
    writing. A directory that cannot be written is logged once and memory is used."""

    def __init__(self, size: int = 512, directory: str | None = None) -> None:
        self._d: OrderedDict[str, str] = OrderedDict()
        self.size = size
        self.dir: Path | None = None
        if directory:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
                probe = Path(directory) / ".probe"
                probe.write_text("ok")
                probe.unlink()
                self.dir = Path(directory)
            except OSError as exc:
                import logging
                logging.getLogger("cam.pipeline").warning(
                    "cam_digest_cache_unwritable dir=%s error=%s — fact sheets kept in "
                    "memory only", directory, exc)

    @staticmethod
    def key(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update(p.encode("utf-8", "ignore") + b"\x00")
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        if self.dir is not None:
            try:
                value = (self.dir / f"{key}.md").read_text(encoding="utf-8")
            except OSError:
                return None
            self._remember(key, value)
            return value
        return None

    def put(self, key: str, value: str) -> None:
        self._remember(key, value)
        if self.dir is not None:
            tmp = self.dir / f".{key}.tmp"
            try:
                tmp.write_text(value, encoding="utf-8")
                tmp.replace(self.dir / f"{key}.md")      # atomic: never a half sheet
            except OSError:
                pass                                       # memory still has it

    def _remember(self, key: str, value: str) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.size:
            self._d.popitem(last=False)


async def draft_staged(*, generate: Engine, system: str, prompt: MasterPrompt,
                       docs: list[SourceDoc], analyst_notes: str, cache: DigestCache,
                       model_id: str, progress: Progress, concurrency: int = 4,
                       call_budget: int = 360_000, digest_part_chars: int = 90_000,
                       deal_context: str = "", public_context: str = "",
                       today: str = "", retry_delay_s: float = 0.0,
                       digest_effort: str | None = None,
                       doc_title: str = "Credit Appraisal Memorandum",
                       interlude: Interlude | None = None,
                       section_effort: str | None = None,
                       final_effort: str | None = None,
                       light_dataset: bool = False) -> DraftResult:
    gate = asyncio.Semaphore(max(1, concurrency))
    result = DraftResult(draft_md="", workpaper_md="")
    base_system = f"{system}\n\n===== THE CREDIT TEAM'S RULES (Block A) =====\n{prompt.rules}"

    async def call(user: Any, label: str, *, sys: str = base_system,
                   effort: str | None = None) -> str:
        async with gate:
            result.calls += 1
            with stage(label):
                turns = [{"role": "user", "content": user}]
                return await (generate(sys, turns, effort=effort) if effort
                              else generate(sys, turns))

    async def call_retry(user: Any, label: str, effort: str | None = None) -> str:
        try:
            return await call(user, label, effort=effort)
        except RuntimeError:
            # One retry for transient provider errors, after a pause: a rate limit or a
            # provider blip needs time, not an immediate re-ask.
            await asyncio.sleep(retry_delay_s)
            return await call(user, label, effort=effort)

    # 0. FOCUS --------------------------------------------------------------------------
    # An analysed workbook is read summaries-first (a BSA's month-wise overview, not
    # its 6,000 raw rows); what was not read is named on the document's fate.
    # Any transaction listing (a bank statement of any bank, a BSA's raw sheet, a ledger)
    # is summarised by PRISM month by month; a document uploaded twice is read once.
    focused: list[SourceDoc] = []
    seen_text: dict[str, str] = {}
    for d in docs:
        said: list[str] = []
        if d.text:
            digest_of = hashlib.sha256(re.sub(r"\s+", " ", d.text).strip().encode()).hexdigest()
            if digest_of in seen_text:
                result.notes.append({"doc_id": d.doc_id, "document": d.name,
                                     "note": f"identical to '{seen_text[digest_of]}' — read once"})
                continue
            seen_text[digest_of] = d.name
            text, note = summarise_ledgers(d.text)
            said += [note] if note else []
            if "## Sheet:" in text:
                text, note = focus_workbook(text)
                said += [note] if note else []
            d = replace(d, text=text)
        if said:
            result.notes.append({"doc_id": d.doc_id, "document": d.name, "note": "; ".join(said)})
        focused.append(d)
    docs, skipped = select_documents(focused)
    result.notes += skipped
    names = [d.name for d in docs]

    # 1. DIGEST -------------------------------------------------------------------------
    jobs = [(d, i, part) for d in docs
            for i, part in enumerate(split_pages(d.text, digest_part_chars))]
    done = 0
    progress("Reading documents", 0, len(jobs))
    phase_a = prompt.phases.get("A", "")

    async def digest(doc: SourceDoc, idx: int, part: str) -> tuple[SourceDoc, int, str, str]:
        """(doc, part index, fact sheet or "", error or "")."""
        nonlocal done
        attached = repr(doc.blocks)[:4096] if doc.blocks else ""
        key = cache.key(model_id, digest_effort or "", prompt.rules, phase_a, DIGEST_TASK, part,
                        hashlib.sha256(attached.encode()).hexdigest() if attached else "")
        sheet, err = cache.get(key) or "", ""
        if not sheet:
            label = doc.name + (f" (part {idx + 1})" if idx else "")
            ask = (f"===== DOCUMENT: {label} =====\n"
                   + (part or "(the document is the attached scan)") + "\n\n"
                   f"===== EXTRACTION INSTRUCTIONS (Phase A) =====\n{phase_a}\n\n{DIGEST_TASK}")
            content: Any = [*doc.blocks, {"type": "text", "text": ask}] if doc.blocks else ask
            try:
                sheet = await call_retry(content, "digest", digest_effort)
                figs = len(_NUM.findall(part))
                if figs >= 100 and _EMPTY_CLAIM.search(sheet) and not doc.blocks:
                    # "An empty template" said of a part holding hundreds of figures is a
                    # misread (the Pulze BSAs, the legal report): read it once more, told so.
                    sheet = await call_retry(
                        f"{ask}\n\nNOTE: this document is NOT empty — it holds {figs} "
                        "figures. Do not describe it as empty, blank or a template; extract "
                        "what it holds.", "digest", digest_effort)
                cache.put(key, sheet)
            except RuntimeError as exc:
                sheet, err = "", str(exc)
        done += 1
        progress("Reading documents", done, len(jobs))
        return doc, idx, sheet, err

    sheets: dict[str, list[tuple[int, str]]] = {d.doc_id: [] for d in docs}
    for doc, idx, sheet, err in await asyncio.gather(*(digest(d, i, p) for d, i, p in jobs)):
        if sheet:
            sheets[doc.doc_id].append((idx, sheet))
        else:
            result.notes.append({"doc_id": doc.doc_id, "document": doc.name,
                                 "reason": f"fact sheet failed: {err}"})
    joined = {k: "\n\n".join(s for _i, s in sorted(v)) for k, v in sheets.items()}
    # Each fact sheet says what its document is: sections get their own documents whole.
    kinds = {d.doc_id: doc_type(joined.get(d.doc_id, ""), d.name) for d in docs}
    typed = [(kinds[d.doc_id], f"===== FACT SHEET: {d.name} =====\n" + joined[d.doc_id])
             for d in docs if joined.get(d.doc_id)]
    fact_sheets = [s for _t, s in typed]
    head = manifest(docs)
    notes_block = (f"===== ANALYST'S CONVERSATION & INSTRUCTIONS (the documents and the "
                   f"dataset take precedence on facts) =====\n{analyst_notes}"
                   if analyst_notes.strip() else "")
    # The borrower and the line as the Register holds them: identity (CIN, PAN, GSTIN,
    # constitution), the amount, product, RM and analyst. Page 1, the title page and
    # Annexure I were "not on record" without it.
    deal_block = (f"===== THE DEAL ON THE REGISTER (facts of record) =====\n{deal_context}"
                  if deal_context.strip() else "")
    # What PRISM itself holds from public sources — Tracxn's filings (cached, never a
    # paid fetch from here) and PULSE's news — so checks can be "verified via", cited.
    public_block = (f"===== PUBLIC SOURCES PRISM HOLDS (cite as given; documents take "
                    f"precedence on the borrower's own figures) =====\n{public_context}"
                    if public_context.strip() else "")
    notes_block = "\n\n".join(b for b in (deal_block, public_block, notes_block) if b)
    # Two documents are quoted, not summarised: the term sheet (the facility's terms of
    # record, transcribed into the appendix) and the company profile (the business, in
    # the borrower's own words — a fact sheet keeps too little of it for Sections 2 and 4).
    term_text = "\n\n".join(d.text for d in docs if kinds[d.doc_id] == "term sheet")[:40_000]
    term_block = (f"===== THE TERM SHEET (verbatim — the facility's terms of record) =====\n"
                  f"{term_text}" if term_text.strip() else "")
    profile_text = "\n\n".join(d.text for d in docs
                               if kinds[d.doc_id] == "company profile")[:60_000]
    profile_block = (f"===== COMPANY PROFILE (full text, as supplied) =====\n{profile_text}"
                     if profile_text.strip() else "")

    def primaries(heading: str) -> list[str]:
        out = []
        if term_block and (_TERMS_PART.search(heading) or "DEBT PROFILE" in heading.upper()):
            out.append(term_block)
        if profile_block and re.search(r"MANAGEMENT|BUSINESS MODEL|INDUSTRY|PROPOSAL SUMMARY"
                                       r"|BORROWER PROFILE|EXECUTIVE|ORDER BOOK",
                                       heading, re.I):
            out.append(profile_block)
        return out

    # 2. CONSOLIDATE + public checks ------------------------------------------------------
    progress("Locking the financial dataset", 0, 1)
    ctx_budget = max(call_budget - len(base_system) - 20_000, 40_000)
    dataset_want = _FIN | {"bank statement analysis", "GST return", "sanction letter"}
    dataset_ctx = "\n\n".join([head, *fit_for(typed, ctx_budget, dataset_want), notes_block])
    phase_c = prompt.phases.get("C", "")
    dataset_ask = (f"{dataset_ctx}\n\n===== FORENSIC ANALYSIS (Phase C) =====\n"
                   f"{phase_c}\n\n{DATASET_TASK}\n\n{FIGURES_TASK}")
    # The locked dataset is a function of the fact sheets (and the rules): cached like
    # them, so an IRG after a CAM, or a re-draft, does not spend ten minutes on it again.
    # Keyed on the fact sheets the dataset is built FROM (financials, ITR, CMA, bank
    # analyses, GST, sanction letters, debt, ageing): a bureau report whose OCR varies
    # between runs (DocRAG does not cache a page Sarvam left empty) must not cost the
    # dataset again.
    ds_basis = [sh for k, sh in typed if k in dataset_want] or fact_sheets
    ds_key = cache.key("dataset", model_id, prompt.rules, phase_c, DATASET_TASK,
                       FIGURES_TASK, *ds_basis)
    cached_ds = cache.get(ds_key)
    if light_dataset and not cached_ds:
        # No full (CAM) dataset for these fact sheets: the IRG builds the concise one,
        # or reuses its own.
        ds_key = cache.key("dataset-light", model_id, prompt.rules, LIGHT_DATASET_TASK,
                           FIGURES_TASK, *ds_basis)
        cached_ds = cache.get(ds_key)
        dataset_ask = (f"{dataset_ctx}\n\n{LIGHT_DATASET_TASK}\n\n{FIGURES_TASK}")

    async def dataset_call() -> str:
        if cached_ds:
            return cached_ds
        raw = await call_retry(dataset_ask, "dataset")
        cache.put(ds_key, raw)
        return raw

    # Without a cached dataset the interlude (the IRG's grade) runs BESIDE it, on the
    # fact sheets alone, instead of waiting for it.
    early: asyncio.Future[str] | None = (
        asyncio.ensure_future(interlude(typed, "", result))
        if interlude is not None and not cached_ds else None)
    checks_task = call_retry(f"{head}\n\n{public_block}\n\n===== PUBLIC INFORMATION "
                             f"(Phase B) =====\n{prompt.phases.get('B', '')}\n\n{CHECKS_TASK}",
                             "public_checks")
    dataset, checks = await asyncio.gather(dataset_call(), checks_task, return_exceptions=True)
    if isinstance(dataset, BaseException):
        if early is not None:
            early.cancel()
        raise RuntimeError(f"The financial dataset could not be built: {dataset}")
    # The ratios are PRISM's: computed from the figures the dataset step locked, and
    # added to the dataset every section quotes.
    figures, dataset, why_not = parse_figures(dataset)
    implausible: list[str] = []
    untraced: list[str] = []
    if figures is not None:
        untraced = trace(figures, dataset)
        implausible = plausibility(figures, compute(figures))
        # A broken identity is a mis-read line: the dataset step gets ONE chance to
        # re-read its sources, and the correction is kept only if it is no worse.
        broken = [f for f in implausible
                  if not f.startswith(("Revenue growth", "Revenue CAGR"))]
        if broken:
            try:
                fix_key = cache.key("dataset_fix", ds_key)
                redo = cache.get(fix_key) or await call_retry(
                    f"{dataset_ctx}\n\n{FIGURES_TASK}\n\n"
                    + DATASET_FIX_TASK.format(flags="; ".join(broken), workpaper=dataset),
                    "dataset_fix")
                cache.put(fix_key, redo)
                f2, d2, _why2 = parse_figures(redo)
                if f2 is not None:
                    u2 = trace(f2, d2)
                    i2 = plausibility(f2, compute(f2))
                    if len([f for f in i2 if not f.startswith(("Revenue growth",
                                                               "Revenue CAGR"))]) < len(broken):
                        figures, dataset, untraced, implausible = f2, d2, u2, i2
            except RuntimeError as exc:
                result.notes.append({"section": "financial dataset",
                                     "reason": f"correction pass failed: {exc}"})
        dataset = f"{dataset.rstrip()}\n\n{ratios_markdown(figures)}\n"
        if untraced:
            dataset += ("\n**Figures not in the workpaper's tables, so not used:** "
                        + "; ".join(untraced) + "\n")
        result.figures = {"subordinated": figures.subordinated,
                          "periods": [{"period": p.label, "basis": p.basis, **p.v}
                                      for p in figures.periods]}
    computed = figures is not None       # if not, Machine QA says why (why_not)
    result.workpaper_md = dataset
    progress("Locking the financial dataset", 1, 1)

    # 2b. INTERLUDE: a caller's step between the evidence and the writing — the IRG
    # grades here, on the fact sheets and the locked dataset, and its result becomes
    # context every section reads.
    if interlude is not None:
        progress("Grading", 0, 1)
        extra_ctx = await (early if early is not None else interlude(typed, dataset, result))
        if extra_ctx.strip():
            notes_block = "\n\n".join(b for b in (notes_block, extra_ctx) if b)
        progress("Grading", 1, 1)

    # 3. BODY sections ------------------------------------------------------------------
    locked = f"===== LOCKED FINANCIAL DATASET (quote exactly) =====\n{dataset}"
    qa = f"===== QUALITY BAR (read-through checklist) =====\n{prompt.qa}" if prompt.qa else ""
    body_specs = [s for s in prompt.sections if not s.final]
    final_specs = [s for s in prompt.sections if s.final]
    written: dict[int, str] = {}
    total = len(prompt.sections)
    progress("Writing sections", 0, total)
    # Every figure a section may state is in one of these; one that is not is a data
    # error until shown otherwise.
    known = figure_index(dataset, *fact_sheets, *(d.text for d in docs), deal_context,
                         public_context, analyst_notes)
    traced_off: dict[str, list[str]] = {}
    missing_claims: dict[str, list[str]] = {}
    # Which kinds of document PRISM read, by name. A statement and its analysis answer
    # the same question ("is there bank data?").
    present: dict[str, list[str]] = {}
    for d in docs:
        if joined.get(d.doc_id):
            present.setdefault(kinds[d.doc_id], []).append(d.name)
    # A statement left out because an analysis covers it is still on record.
    present.setdefault("bank statement", []).extend(
        n["document"] for n in skipped if "covered by" in n["note"] or "older extract" in n["note"])
    if not present["bank statement"]:
        present.pop("bank statement")
    banking = present.get("bank statement", []) + present.get("bank statement analysis", [])
    if banking:
        present["bank statement"] = present["bank statement analysis"] = banking

    def section_ctx(spec: SectionSpec, extra: list[str]) -> str:
        prim = primaries(spec.heading)
        spare = max(ctx_budget - sum(len(x) for x in [*extra, *prim]) - len(head), 20_000)
        return "\n\n".join([head, *prim, *fit_for(typed, spare, wanted(spec.heading)), *extra])

    def polish(spec: SectionSpec, text: str) -> str:
        if not text.lstrip().startswith("#"):
            text = f"## {spec.heading}\n\n{text}"
        text = tidy_citations(text.strip(), names)
        if spec.heading.upper().startswith("ANNEXURE"):
            text = prune_annexure(text)
        return text.strip()

    async def write(spec: SectionSpec, context: str) -> None:
        task = section_task(spec, computed, terms=bool(term_block))
        label = "final_section" if spec.final else "section"
        effort = final_effort if spec.final else section_effort
        try:
            text = polish(spec, await call_retry(f"{context}\n\n{task}", label, effort))
        except RuntimeError as exc:
            text = (f"## {spec.heading}\n\n> This section could not be drafted ({exc}). "
                    "Regenerate, or write it by hand.")
            result.notes.append({"section": spec.title, "reason": str(exc)})
        else:
            off = untraced_figures(text, known, names)
            gone = claims_missing(text, present)
            if off or gone:
                # One correction pass: figures no source has, and documents called
                # missing that were read, named back to the writer with its own draft.
                # What survives it is named in Machine QA.
                issues = "\n".join(
                    ([f"- figures in no source: {', '.join(off[:30])}"] if off else [])
                    + [f"- document called missing: {g}" for g in gone[:10]])
                try:
                    redo = polish(spec, await call_retry(
                        f"{context}\n\n{task}\n\n" + REPAIR_TASK.format(
                            issues=issues, draft=text), "repair", effort))
                    if redo.startswith("#") and len(redo) > len(text) * 0.5:
                        text = redo
                        off = untraced_figures(text, known, names)
                        gone = claims_missing(text, present)
                except RuntimeError:
                    pass                    # the first draft stands; QA names what is wrong
            if off:
                traced_off[spec.heading] = off
            if gone:
                missing_claims[spec.heading] = gone
        written[spec.order] = text
        progress("Writing sections", len(written), total)

    fixed = [locked, notes_block, qa]
    await asyncio.gather(*(write(s, section_ctx(s, fixed)) for s in body_specs))

    # 4. FINAL sections, from the finished body ------------------------------------------
    body = "\n\n".join(written[s.order] for s in body_specs)
    body_block = f"===== THE CAM BODY AS WRITTEN =====\n{body}"

    def final_ctx(heading: str) -> str:
        prim = primaries(heading)
        room = max(ctx_budget - len(locked) - sum(len(x) for x in prim), 20_000)
        return "\n\n".join([head, *prim, locked, *fit([body_block], room), notes_block, qa])

    async def title_page() -> str:
        try:
            text = await call_retry(f"{final_ctx('PROPOSAL SUMMARY')}\n\n"
                                    f"{TITLE_TASK.format(today=today or _today())}", "title")
        except RuntimeError as exc:
            result.notes.append({"section": "TITLE PAGE", "reason": str(exc)})
            return ""
        # Plain lines under the document title; a heading the model added is dropped.
        return "\n".join(ln for ln in text.strip().splitlines()
                         if not ln.lstrip().startswith("#")).strip()

    finals = await asyncio.gather(title_page(),
                                  *(write(s, final_ctx(s.heading)) for s in final_specs))
    title = tidy_citations(str(finals[0] or ""), names)

    # 5. ASSEMBLE -----------------------------------------------------------------------
    parts = [written[s.order] for s in prompt.sections]
    if isinstance(checks, str) and checks.strip():
        checks = compact_checks(unverify_unavailable(checks))
        first_annex = next((n for n, s in enumerate(prompt.sections)
                            if s.title.startswith("ANNEXURE")), len(parts))
        parts.insert(first_annex, checks.strip())
    # Two trailing spaces: a Markdown line break, so the title block stays one line each.
    title_md = "  \n".join(ln.strip() for ln in title.splitlines() if ln.strip())
    result.draft_md = (f"# {doc_title}\n\n" + (f"{title_md}\n\n" if title_md else "")
                       + "\n\n".join(parts) + "\n")

    # 6. MACHINE QA (the prompt's Step 7) -------------------------------------------------
    appendix = next((written[s.order] for s in prompt.sections
                     if s.heading.upper().startswith("APPENDIX")), None)
    result.qa = machine_qa(prompt, result.draft_md, dataset, ratios_from_code=computed,
                           ratios_note=why_not, implausible=implausible, untraced=untraced,
                           traced_off=traced_off, missing_claims=missing_claims,
                           terms_off=(terms_drift(appendix, term_text)
                                      if appendix is not None and term_text.strip() else None),
                           term_sheet=bool(term_text.strip()) if appendix is not None else None)
    result.workpaper_md = f"{dataset.rstrip()}\n\n{qa_markdown(result.qa)}\n"
    progress("Done", total, total)
    return result


def _today() -> str:
    from datetime import date
    return date.today().strftime("%d-%b-%Y")
