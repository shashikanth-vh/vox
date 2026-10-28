"""The fact ledger — the figures a CAM may state, read by code once, each with its source.

WHY. The CAM engine the desk runs (Sarvam glm) is a capable writer and an unreliable
reader: given 40 pages of fact sheets it typed a sub-line for the year's sales, left the
year's profit out of net worth, and read four guarantors' asset declarations as "no asset
side". A model that is never asked to find a figure cannot misread one. So the harness
(``WORKFLOWS_CAM_HARNESS=v2``) reads the documents whose layout is known — accounting
statements, net-worth forms, debt-profile tables, bank-analysis limits, bureau ranks — in
code, keeps one ledger of what they say, names where two documents disagree, and renders
the tables that carry those figures itself. The model writes around them.

Everything here is a pure function of the documents' text: no model, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.cam_ratios import Figures, compute
from app.statement_math import Arithmetic, networth_values

_LAKH = Decimal(100000)


@dataclass(frozen=True)
class Fact:
    key: str                 # stable identity: "limit.cc_od.bank of india"
    label: str               # what a reader sees: "CC/OD limit — Bank of India"
    value: float | str
    unit: str                # "Rs lakh" | "text"
    source: str              # the document it was read from
    method: str = "parsed"   # parsed (code) | declared (a self-declared form)
    period: str = ""


@dataclass
class DebtRow:
    lender: str
    facility: str
    sanctioned: float | None     # Rs lakh
    outstanding: float | None    # Rs lakh
    emi: float | None            # Rs lakh
    rate: str
    security: str
    source: str


@dataclass
class Guarantor:
    person: str
    total_assets: float | None
    liabilities: float | None
    net_assets: float | None
    source: str
    checks: list[str] = field(default_factory=list)   # where the form's own sums disagree
    bureau: float | None = None   # the person's bureau balances now (lakh), when on file

    @property
    def net_after_bureau(self) -> float | None:
        """Net assets with liabilities taken as the larger of declared and bureau-reported:
        a partner who declared 26.5 lakh owed showed 4.45 crore on his own bureau report."""
        if self.bureau is None or self.total_assets is None:
            return self.net_assets
        owed = max(self.liabilities or 0.0, self.bureau)
        return self.total_assets - owed


@dataclass
class Ledger:
    facts: list[Fact] = field(default_factory=list)
    debts: dict[str, list[DebtRow]] = field(default_factory=dict)     # by source document
    guarantors: list[Guarantor] = field(default_factory=list)

    def add(self, fact: Fact) -> None:
        if fact not in self.facts:
            self.facts.append(fact)

    def conflicts(self) -> list[tuple[str, list[Fact]]]:
        """Keys two documents give different numbers for (beyond 0.5%) — the reconciliation
        an analyst must close, named instead of silently picked."""
        by: dict[tuple[str, str], list[Fact]] = {}
        for f in self.facts:
            if isinstance(f.value, float):
                by.setdefault((f.key, f.period), []).append(f)
        out = []
        for (key, period), fs in by.items():
            # Statements of different years print the same group names: without the year
            # on both, two documents' "net profit" are not the same figure.
            if key.startswith("stmt.") and not period:
                continue
            vals = [f.value for f in fs if isinstance(f.value, float)]
            srcs = {f.source for f in fs}
            if len(srcs) > 1 and vals and max(vals) - min(vals) > 0.005 * max(abs(v) for v in vals):
                out.append((key, fs))
        return out

    def render(self) -> str:
        """The block every writer and the dataset step is given."""
        if not (self.facts or self.debts or self.guarantors):
            return ""
        out = ["===== PRISM FACT LEDGER (read by code from the documents — exact; quote these "
               "figures, never re-derive them) ====="]
        for f in self.facts:
            v = f"{f.value:,.2f} {f.unit}" if isinstance(f.value, float) else str(f.value)
            out.append(f"- {f.label}{f' ({f.period})' if f.period else ''}: {v}"
                       f"{' — as declared' if f.method == 'declared' else ''} [{f.source}]")
        for key, fs in self.conflicts():
            out.append(f"- CONFLICT {key}: " + "; ".join(
                f"{f.value:,.2f} per {f.source}" for f in fs if isinstance(f.value, float))
                + " — state both and the one adopted, or leave it to the analyst")
        return "\n".join(out)


# ------------------------------------------------------------------- number reading
def _num(cell: str) -> float | None:
    s = re.sub(r"[₹,\s]|rs\.?|inr", "", (cell or "").strip(), flags=re.I)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    try:
        return float(Decimal(s))
    except InvalidOperation:
        return None


def _lakh(value: str) -> float | None:
    """A declared amount as printed on a form: "55L", "1.43", "100L", "2.5 cr"."""
    m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)\s*(L\b|lakhs?|cr\b|crores?)?", value or "", re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return v * 100 if unit.startswith("cr") else v


# ------------------------------------------------------------------- the readers
# A line carried from one year's account to the next ("Gross Profit b/d", "Net Profit
# c/d") repeats a figure, so set beside the next year's it reads as a conflict.
_CARRIED = re.compile(r"\b[bcf]\s*/\s*[df]\b|\b(?:brought|carried)\s+(?:down|forward)\b", re.I)
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
# "as at 31st March 2024", "year ended 31-03-2024", "31.03.2024".
_YEAR_END = re.compile(r"\b31(?:st)?[\s\-./]*(?:march|mar|0?3)[\s\-.,/]*(20\d{2})\b", re.I)
_FY_NAME = re.compile(r"\bFY\s*'?(?:20)?\d{2}\s*[-–/]\s*(?:20)?(\d{2})\b|\bFY\s*'?(?:20)?(\d{2})\b"
                      r"|\b20\d{2}\s*[-–/]\s*(?:20)?(\d{2})\b", re.I)


def statement_period(name: str, text: str = "") -> str:
    """The financial year a statement is for, "FY24", from its year-end date or failing
    that its name; "" when neither says."""
    m = _YEAR_END.search(text or "")
    if m:
        return f"FY{m.group(1)[2:]}"
    m = _FY_NAME.search(name or "")
    if m:
        return f"FY{next(g for g in m.groups() if g)}"
    return ""


_AUDITED = re.compile(r"\baudited\b|\bUDIN\b|auditor'?s'?\s+report", re.I)
_PROVISIONAL = re.compile(r"\bprovisional\b|\bun-?audited\b|\bmanagement\s+accounts\b", re.I)


def provisional_periods(docs: list[Any]) -> set[str]:
    """The years whose accounts on file are titled provisional or unaudited ("FY 26 -
    BSheet - Provisional"). The dataset step locked Pulze's FY26 as audited, and the
    audited growth rate the base case is capped at then took in the unaudited jump."""
    prov, audited = set(), set()
    for d in docs:
        head = (d.text or "")[:3000]
        year = statement_period(d.name, d.text or "")
        if not year:
            continue
        if _PROVISIONAL.search(d.name) or _PROVISIONAL.search(head):
            prov.add(year)
        elif _AUDITED.search(d.name) or _AUDITED.search(d.text or ""):
            audited.add(year)
    # An auditor's report for the year on file outranks a provisional draft of it.
    return prov - audited


def from_statement(arith: Arithmetic, period: str = "") -> list[Fact]:
    """A statement's groups and net worth, in lakh (statements print rupees)."""
    out = []
    for g in arith.groups:
        head = arith.amounts[g.head]
        # A group is named by words; markup or a bare symbol is no label to key a fact by.
        if re.search(r"[A-Za-z]{3}", head.label) and "<" not in head.label \
                and not _CARRIED.search(head.label):
            out.append(Fact(f"stmt.{head.label.casefold()}", head.label,
                            round(float(head.value / _LAKH), 2), "Rs lakh", arith.name,
                            period=period))
    nw = arith.net_worth()
    if nw is not None:
        out.append(Fact("stmt.net worth", "Net worth (capital + profit & loss balance)",
                        round(float((nw[0] + nw[1]) / _LAKH), 2), "Rs lakh", arith.name,
                        period=period))
    return out


def from_networth(text: str, name: str) -> Guarantor | None:
    vals = networth_values(text)
    if vals is None:
        return None
    summ = vals["summary"]
    assert isinstance(summ, dict)
    lines = vals["lines"]
    assert isinstance(lines, list)
    total = _lakh(str(summ.get("total assets", "")))
    if total is None and lines:
        parts = [x for _l, v in lines for x in [_lakh(v)] if x is not None]
        total = sum(parts) if parts else None
    liab = _lakh(str(summ.get("total liabilities", "")))
    net = _lakh(str(summ.get("net assets", "")))
    if net is None and total is not None and liab is not None:
        net = total - liab
    # The form's own sums: one Pulze partner's printed 27.5 + 450 as a total of 470 and
    # 470 − 200 as 272.5. Reported as declared, and named.
    checks = []
    mov, imm = _lakh(str(summ.get("movable assets", ""))), _lakh(str(summ.get("immovable property", "")))
    if total is not None and mov is not None and imm is not None and abs(mov + imm - total) > 0.5:
        checks.append(f"movable {mov:g} + immovable {imm:g} = {mov + imm:g}, but the form's total "
                      f"is {total:g}")
    if net is not None and total is not None and liab is not None and abs(total - liab - net) > 0.5:
        checks.append(f"total {total:g} − liabilities {liab:g} = {total - liab:g}, but the form's "
                      f"net assets are {net:g}")
    return Guarantor(str(vals["person"]) or name, total, liab, net, name, checks)


# A personal (consumer) bureau report: whose it is, and its balances now.
# Read from a PDF the rows open with "|"; from the portal's HTML they do not, and the
# name ends its line: "CONSUMER NAME | : | G Example Rajan".
_CONSUMER = re.compile(r"CONSUMER\s+NAME\s*\|?\s*:\s*\|?\s*([A-Za-z][A-Za-z .]+?)\s*(?:\||$)",
                       re.I | re.M)
_CONSUMER_NOW = re.compile(r"(?:^|\|)\s*Current\s*\|\s*:\s*\|\s*₹\s*([\d,]+)", re.I | re.M)


def consumer_bureau(text: str) -> tuple[str, float] | None:
    """(the consumer's name, current balances in lakh) from a personal bureau report."""
    who, now = _CONSUMER.search(text or ""), _CONSUMER_NOW.search(text or "")
    if not who or not now or (v := _num(now.group(1))) is None:
        return None
    return who.group(1).strip(), round(v / 100000, 2)


def _same_person(a: str, b: str) -> bool:
    """Two spellings of one name share a word of four letters or more ("Shivrane G" and
    "G Shivrane Gowrisankar")."""
    words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s.lower())}  # noqa: E731
    return bool(words(a) & words(b))


_HEAD = {"lender": r"lender|bank|institution|financier",
         "facility": r"type|nature|facility|loan",
         "sanctioned": r"sanction|limit",
         "outstanding": r"o/?s\b|outstanding|balance",
         "emi": r"\bemi\b|instal",
         "rate": r"roi|rate|interest",
         "security": r"security|collateral|secured"}


def _rupees(header: str) -> bool:
    return bool(re.search(r"\(\s*(?:inr|rs\.?|₹|rupees)\s*\)", header, re.I)) and \
        not re.search(r"lakh|crore", header, re.I)


def debt_tables(text: str, name: str) -> list[DebtRow]:
    """Every table whose header names a lender and an outstanding amount — a debt profile,
    a loan schedule — one row per facility, amounts in lakh."""
    rows: list[DebtRow] = []
    lines = [ln for ln in (text or "").splitlines() if ln.lstrip().startswith("|")]
    cols: dict[str, int] | None = None
    scale: dict[str, float] = {}
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        low = [c.casefold() for c in cells]
        if cols is None or re.fullmatch(r"[\s|:\-]*", ln):
            found = {k: next((i for i, c in enumerate(low) if c and re.search(rx, c)), -1)
                     for k, rx in _HEAD.items()}
            if found["lender"] >= 0 and found["outstanding"] >= 0 and found["sanctioned"] >= 0:
                cols = found
                scale = {k: (1 / 100000 if _rupees(cells[i]) else 1.0)
                         for k, i in found.items() if i >= 0}
            continue
        lender = cells[cols["lender"]] if cols["lender"] < len(cells) else ""
        if not lender or re.search(r"\btotal\b", lender, re.I):
            continue

        def money(k: str) -> float | None:
            i = cols[k] if cols else -1
            v = _num(cells[i]) if 0 <= i < len(cells) else None
            return None if v is None else round(v * scale.get(k, 1.0), 2)

        sanctioned, outstanding = money("sanctioned"), money("outstanding")
        if sanctioned is None and outstanding is None:
            continue

        def text_of(k: str) -> str:
            i = cols[k] if cols else -1
            return cells[i] if 0 <= i < len(cells) else ""

        rows.append(DebtRow(lender, text_of("facility"), sanctioned, outstanding, money("emi"),
                            text_of("rate"), text_of("security"), name))
    return rows


_LIMIT = re.compile(r"\|\s*(CC\s*/\s*OD\s+Limit|Sanctioned\s+Limit|Drawing\s+Power)\s*\|\s*"
                    r"([\d.,]+)\s*\|", re.I)
_CMR = re.compile(r"\bCMR[\s-]*(\d{1,2})\b")
# A commercial bureau report's outstanding total: "| TOTAL | | 11 | ₹ 52,835,612 |".
_BUREAU_TOTAL = re.compile(r"^\|\s*TOTAL\s*\|\s*\|\s*\d+\s*\|\s*₹\s*([\d,]+)\s*\|", re.I | re.M)
# A bank statement analysis's month-by-month row; its first figure is the window's total,
# its last the latest month's.
_OVERDRAWN = re.compile(r"^\|[^|\n]*\|\s*No\.?\s*of\s*days\s*limit\s*over-?\s*drawn\s*\|(.*)$",
                        re.I | re.M)


def key_values(text: str, name: str, bank: str | None) -> list[Fact]:
    """Labelled figures a bank analysis or bureau report prints: the CC/OD limit (rupees
    in a BSA), the company bureau rank."""
    out = []
    for m in _LIMIT.finditer(text or ""):
        v = _num(m.group(2))
        if not v:                      # a current account's "limit" of 0 is no limit
            continue
        lakh = v / 100000 if v >= 100000 else v
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        who = f" — {bank}" if bank else ""
        # Keyed by bank, so two documents on the same account can be compared; an
        # account whose bank is not known is its document's own.
        out.append(Fact(f"limit.{label.casefold()}.{(bank or name).casefold()}", f"{label}{who}",
                        round(lakh, 2), "Rs lakh", name))
    ranks = sorted({int(m.group(1)) for m in _CMR.finditer(text or "")})
    if len(ranks) == 1:
        out.append(Fact("bureau.cmr", "Company bureau rank (CMR)", f"CMR-{ranks[0]}", "text",
                        name))
        # What the company owes now — newer than any balance sheet: Pulze's July report
        # showed 528 lakh against the 321 lakh the March statements carried.
        total = _BUREAU_TOTAL.search(text or "")
        if total and (v := _num(total.group(1))):
            out.append(Fact("bureau.total outstanding", "Total outstanding (company bureau "
                            "report)", round(v / 100000, 2), "Rs lakh", name))
    for m in _OVERDRAWN.finditer(text or ""):
        # "-" is a month with none; a blank cell is no month at all.
        cells = [c.strip() for c in m.group(1).split("|") if c.strip()]
        days = [d for d in (0.0 if c == "-" else _num(c) for c in cells) if d is not None]
        if len(days) >= 2 and days[0]:
            who = f" — {bank}" if bank else ""
            out.append(Fact(f"bank.overdrawn days.{(bank or name).casefold()}",
                            f"Days the CC/OD was over its limit{who}", days[0], "days", name))
            out.append(Fact(f"bank.overdrawn days latest month.{(bank or name).casefold()}",
                            f"Days over the limit in the latest month{who}", days[-1], "days",
                            name))
    return out


def build(docs: list[Any], statements: list[Arithmetic],
          bank_of: Any = None) -> Ledger:
    """The ledger for one CAM: ``docs`` are SourceDocs (name, text); ``statements`` the
    arithmetic statement_math proved; ``bank_of(name, text)`` names a bank account's bank."""
    led = Ledger()
    bureaus: list[tuple[str, float]] = []
    text_of = {d.name: d.text for d in docs}
    for s in statements:
        for f in from_statement(s, statement_period(s.name, text_of.get(s.name, ""))):
            led.add(f)
    for d in docs:
        g = from_networth(d.text, d.name)
        if g is not None:
            led.guarantors.append(g)
            if g.net_assets is not None:
                led.add(Fact(f"guarantor.{g.person.casefold()}.net assets",
                             f"Net assets — {g.person}", float(g.net_assets), "Rs lakh",
                             d.name, method="declared"))
        rows = debt_tables(d.text, d.name)
        if rows:
            led.debts[d.name] = rows
        if (cb := consumer_bureau(d.text)) is not None:
            bureaus.append(cb)
        for f in key_values(d.text, d.name, bank_of(d.name, d.text) if bank_of else None):
            led.add(f)
    for g in led.guarantors:
        g.bureau = next((v for who, v in bureaus if _same_person(who, g.person)), None)
    return led


def facility_lakh(term_text: str, deal_context: str = "") -> float | None:
    """The facility amount in lakh: the term sheet's "Facility Amount" (rupees as printed,
    "INR 1,00,00,000/-"), else the Register line's amount_cr."""
    m = re.search(r"Facility\s+Amount\W{0,40}?(?:upto\s+)?(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)",
                  term_text or "", re.I | re.S)
    if m:
        v = _num(m.group(1))
        if v:
            return v / 100000 if v >= 100000 else v
    m = re.search(r"amount_cr:\s*([\d.]+)", deal_context or "")
    if m:
        v = _num(m.group(1))
        return v * 100 if v else None
    return None


# "Interest Rate | … | Fixed interest rate of 15.00 % P.A." — the first rate after the label.
_RATE = re.compile(r"interest\s+rate.{0,120}?(\d{1,2}(?:\.\d+)?)\s*%", re.I | re.S)


def facility_rate(term_text: str) -> float | None:
    """The facility's interest rate, % a year, as the term sheet prints it."""
    m = _RATE.search(term_text or "")
    return float(m.group(1)) if m else None


# ------------------------------------------------------------- tables PRISM renders
def _f(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def _latest(debts: dict[str, list[DebtRow]]) -> tuple[str, list[DebtRow]] | None:
    """The newest debt profile, by the month and year its name carries."""
    months = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                          "aug", "sep", "oct", "nov", "dec"), 1)}

    def when(name: str) -> tuple[int, int]:
        y = re.findall(r"20\d\d", name)
        # "Format_June 2026": an underscore is a word character, so no \b before it.
        m = re.search(r"(?<![a-z])(" + "|".join(months) + r")[a-z]*(?![a-z])", name, re.I)
        return (int(y[-1]) if y else 0, months[m.group(1).lower()] if m else 0)

    if not debts:
        return None
    name = max(debts, key=lambda n: (when(n), len(debts[n])))
    return name, debts[name]


def debt_table(led: Ledger) -> str:
    got = _latest(led.debts)
    if got is None:
        return ""
    name, rows = got
    head = ["| Lender | Facility | Sanctioned (Rs lakh) | Outstanding (Rs lakh) | EMI (Rs lakh) "
            "| Rate | Security |", "| --- | --- | --- | --- | --- | --- | --- |"]
    body = [f"| {r.lender} | {r.facility} | "
            f"{_f(r.sanctioned)} | {_f(r.outstanding)} | {_f(r.emi, 2)} | {r.rate} | "
            f"{r.security} |" for r in rows]
    tot_s = sum(r.sanctioned or 0 for r in rows)
    tot_o = sum(r.outstanding or 0 for r in rows)
    body.append(f"| **Total** | | **{tot_s:,.1f}** | **{tot_o:,.1f}** | | | |")
    return "\n".join(head + body + ["", f"*As per {name}.*"])


def guarantor_table(led: Ledger) -> str:
    if not led.guarantors:
        return ""
    rows = ["| Guarantor | Total assets (Rs lakh) | Liabilities declared (Rs lakh) | Net assets "
            "declared (Rs lakh) | Bureau balances now (Rs lakh) | Net after bureau (Rs lakh) |",
            "| --- | --- | --- | --- | --- | --- |"]
    rows += [f"| {g.person.title() if g.person.isupper() else g.person} | "
             f"{_f(g.total_assets, 2)} | {_f(g.liabilities, 2)} | {_f(g.net_assets, 2)} | "
             f"{_f(g.bureau, 2)} | {_f(g.net_after_bureau, 2)} |" for g in led.guarantors]
    nets = [g.net_assets for g in led.guarantors if g.net_assets is not None]
    after = [g.net_after_bureau for g in led.guarantors if g.net_after_bureau is not None]
    if len(nets) > 1:
        rows.append(f"| **Total** | | | **{sum(nets):,.2f}** | | **{sum(after):,.2f}** |")
    notes = ["", "*Assets and liabilities as declared in the guarantors' net-worth statements, "
             "to be supported by documents. Net after bureau takes the larger of the declared "
             "liabilities and the balances on the person's own bureau report — some of those "
             "may be the firm's loans they co-signed.*"]
    notes += [f"*{g.person}'s form does not add up: {c}.*" for g in led.guarantors for c in g.checks]
    return "\n".join(rows + notes)


_SNAPSHOT = (("Net Sales", "net_sales", None), ("PBDIT", "pbdit", None),
             ("PBDIT Margin", None, "PBDIT Margin"), ("PAT", "pat", None),
             ("TNW", "tnw", None), ("Adjusted TNW", None, "Adjusted TNW"),
             ("Total Debt", None, "_debt"), ("Total Debt / Adj. TNW", None,
                                              "Total Debt / Adj. TNW"),
             ("Current Ratio", None, "Current Ratio"),
             ("Interest Coverage", None, "Interest Coverage"), ("DSCR", None, "DSCR"))


def snapshot_table(fig: Figures, estimates: dict[str, tuple[str, float]] | None = None) -> str:
    """The financial snapshot every CAM opens with — the locked figures and PRISM's ratios,
    never retyped by a model. ``estimates`` fills a ratio the figures cannot give for one
    period ({"DSCR": ("FY26", 1.34)}), marked as estimated — the table said "not
    computable" beside a signal quoting the estimate."""
    ratios = {r.name: r for r in compute(fig)}
    labels = [p.label + {"audited": " (A)", "provisional": " (P)", "projected": " (E)"}
              .get(p.basis, "") for p in fig.periods]
    out = ["| Particulars (Rs lakh) | " + " | ".join(labels) + " |",
           "| --- | " + " | ".join("---" for _ in labels) + " |"]
    for title, field_, ratio in _SNAPSHOT:
        cells = []
        for p in fig.periods:
            if field_:
                v = p[field_]
                cells.append(_f(v))
            elif ratio == "_debt":
                td = None if p["term_debt"] is None and p["wc_debt"] is None else \
                    (p["term_debt"] or 0) + (p["wc_debt"] or 0)
                cells.append(_f(td))
            else:
                r = ratios.get(ratio or "")
                v = r.values.get(p.label) if r else None
                unit = r.unit if r else ""
                est = (estimates or {}).get(title)
                if v is None and est is not None and est[0] == p.label:
                    cells.append(f"{est[1]:.2f}x (est.)")
                    continue
                cells.append("not computable" if v is None else
                             f"{v:.1f}%" if unit == "%" else f"{v:.2f}x" if unit == "x"
                             else f"{v:,.1f}")
        out.append(f"| {title} | " + " | ".join(cells) + " |")
    return "\n".join(out)


TOKENS = {"snapshot": "the financial snapshot (all periods: sales, PBDIT, margin, PAT, TNW, "
                      "debt, leverage, current ratio, coverage)",
          "debt": "the debt profile (every facility: lender, sanctioned, outstanding, EMI, "
                  "rate, security)",
          "guarantors": "the guarantors' declared net worth",
          "limits": "the limit sizing (working-capital gap after existing bank limits, and "
                    "current-asset cover after haircuts)",
          "picture": "the credit picture at a glance (each factor's signal, set by the credit "
                     "team's rubric)"}


def rendered_tables(led: Ledger, fig: Figures | None,
                    estimates: dict[str, tuple[str, float]] | None = None) -> dict[str, str]:
    out = {"debt": debt_table(led), "guarantors": guarantor_table(led),
           "snapshot": snapshot_table(fig, estimates) if fig is not None else ""}
    return {k: v for k, v in out.items() if v}


def tables_block(tables: dict[str, str]) -> str:
    """What the writers are told: the tables PRISM renders, and how to place one."""
    if not tables:
        return ""
    out = ["===== TABLES PRISM RENDERS (exact, from the documents) =====",
           "Where your section needs one of these tables, write its token alone on its own "
           "line — e.g. {{table:debt}} — and PRISM puts the table there. Never retype or "
           "re-create these tables; refer to their figures in prose as they stand."]
    for k, t in tables.items():
        out += ["", f"{{{{table:{k}}}}} — {TOKENS[k]}:", t]
    return "\n".join(out)


_TOKEN = re.compile(r"\{\{\s*table\s*:\s*([a-z]+)\s*\}\}", re.I)
_SHORT = {"snapshot": "The financial snapshot", "debt": "The debt profile",
          "guarantors": "The guarantors' declared net worth", "limits": "The limit sizing",
          "picture": "The credit picture at a glance"}


def once(parts: list[str], tables: dict[str, str]) -> list[str]:
    """Each PRISM table printed once, where it first falls; later places refer back to it.
    Pulze's draft printed the five-year table 8 times and the signal table 7 — about a
    third of a 24,000-word CAM was the same tables again."""
    seen: dict[str, str] = {}
    out = []
    for part in parts:
        head = next((ln.lstrip("# ").strip() for ln in part.splitlines()
                     if ln.startswith("## ")), "")
        for key, table in tables.items():
            if not table or table not in part:
                continue
            first = part.index(table) + len(table)
            if key not in seen:
                seen[key] = head
                part = part[:first] + part[first:].replace(table, "")
                continue
            ref = f"*{_SHORT.get(key, 'The table')} is under {seen[key] or 'an earlier section'}.*"
            part = part.replace(table, ref, 1).replace(table, "")
        out.append(re.sub(r"\n{3,}", "\n\n", part))
    return out


def place_tables(md: str, tables: dict[str, str]) -> tuple[str, list[str]]:
    """(the section with each token replaced by its table, the tokens placed). A token
    for a table PRISM does not have is removed, never left showing."""
    placed: list[str] = []

    def sub(m: re.Match[str]) -> str:
        key = m.group(1).lower()
        if key in tables:
            placed.append(key)
            return "\n\n" + tables[key] + "\n\n"
        return ""

    out = _TOKEN.sub(sub, md)
    return re.sub(r"\n{3,}", "\n\n", out).strip(), placed
