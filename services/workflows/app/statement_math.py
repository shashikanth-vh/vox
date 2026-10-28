"""Statement arithmetic — the sums a financial statement prints, checked by PRISM.

WHY. Tally (and most accounting packages) print a statement as GROUP totals with the
ledger lines that make them up beneath: "Loans (Liability)", then "Bank OD A/c", "Secured
Loans" and "Unsecured Loan", each with its amount.
Read flat, that is four figures. A real CAM (Oct 2026) read it so: it called the group an
"unexplained liability line", a closing-stock group (closing stock + goods in transit)
"printed twice", the domestic sales line the year's sales, and summed groups AND their
lines to declare that a balance sheet which ties to the paise "does not balance" — the
report's two worst red flags, both false.

So PRISM does the arithmetic itself, exactly, before any model reads the statement:
which figures are the sum of the run of figures printed after them (a group and its
lines), and which printed totals those groups add up to. The result is handed to every
reader as fact, and a draft that still calls a tied statement unbalanced, or a group
and its lines a double count, is sent back (``misread_claims``, used by cam_pipeline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# A statement worth checking: a balance sheet, a P&L or a trading account.
_STATEMENT = re.compile(r"balance\s*sheet|profit\s*(?:&|and|&amp;)\s*loss|trading\s+account"
                        r"|statement\s+of\s+profit", re.I)
# A printed amount: digit-grouped ("1,04,12,021.00", "3,069.30") or with two decimals
# ("104.12"). Bare integers are years, pin codes and counts, never read as amounts; a
# date ("31.03.2026") and a percentage are not amounts either. "(-)" or a leading minus
# or brackets make it negative ("(-)10,00,000.00" is a depreciation reserve).
_AMOUNT = re.compile(r"(?<![\w.,/:])(\(-\)\s*|-\s*(?=\d)|\()?"
                     r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2})(\))?"
                     r"(?![\w,/%]|\.\d|\s*%)")
# The longest run of lines one group may hold (a fixed-asset schedule runs to 25).
_MAX_LINES = 60
# A printed total is matched to a run ending within this many items before it, of at most
# this many items — a statement side, not a whole document.
_TOTAL_ENDS = 12
_TOTAL_SPAN = 300
_EPS = Decimal("0.000001")
_MAX_NOTES = 30


@dataclass
class Amount:
    value: Decimal
    raw: str
    label: str
    places: int


@dataclass
class Group:
    head: int                      # index of the group total in the amounts
    lines: list[int]               # indices of the lines that sum to it


@dataclass
class Total:
    at: int                        # index of the printed total
    groups: list[int] | None       # indices of the top-level items that sum to it


@dataclass
class Arithmetic:
    """What PRISM proved about one statement's printed figures."""
    name: str
    amounts: list[Amount]
    groups: list[Group] = field(default_factory=list)
    totals: list[Total] = field(default_factory=list)

    @property
    def tied(self) -> list[Total]:
        return [t for t in self.totals if t.groups]

    def net_worth(self) -> tuple[Decimal, Decimal] | None:
        """(capital, profit & loss balance) when both sit in the run a tied total is made
        of — a firm's year's profit not yet moved to the partners' capital accounts is
        still net worth. A real CAM locked TNW at capital alone."""
        for t in self.tied:
            labels = {i: self.amounts[i].label.casefold() for i in t.groups or []}
            cap = [i for i, lb in labels.items() if re.search(r"capital|share\s*capital", lb)]
            pl = [i for i, lb in labels.items()
                  if re.search(r"profit\s*(?:&|and)\s*loss|p\s*&\s*l\b|reserves?", lb)]
            if cap and pl:
                return self.amounts[cap[0]].value, self.amounts[pl[0]].value
        return None

    def heads(self) -> list[Decimal]:
        """The group totals — each fully explained by its lines."""
        return [self.amounts[g.head].value for g in self.groups]

    def figures(self) -> list[Decimal]:
        """Every figure that is a group total or one of its lines."""
        seen = {g.head for g in self.groups} | {i for g in self.groups for i in g.lines}
        return [self.amounts[i].value for i in sorted(seen)]

    def render(self) -> str:
        """The block a reader is given beside the statement — empty when PRISM found no
        group, so a statement it could not parse adds nothing (and claims nothing)."""
        if not self.groups:
            return ""
        a = self.amounts

        def item(i: int) -> str:
            return f"{a[i].label} {a[i].raw}".strip()

        out = [f"===== PRISM ARITHMETIC: {self.name} (exact sums of the statement's own "
               "printed figures) =====",
               "A figure followed by the lines that add up to it is ONE item — a group "
               "total and the ledger lines that make it up (accounting software prints "
               "accounts this way). Never count both, never call the pair a duplicate or "
               "a double count, and never call the group unexplained: its make-up is "
               "below. Take a group's total as the statement's figure for it (all sales "
               "lines, domestic and export, are the year's sales)."]
        for g in self.groups[:_MAX_NOTES]:
            lines = [item(i) for i in g.lines]
            shown = " + ".join(lines[:8]) + (f" + … ({len(lines)} lines)" if len(lines) > 8
                                             else "")
            out.append(f"- {item(g.head)} = {shown}")
        if len(self.groups) > _MAX_NOTES:
            out.append(f"- … and {len(self.groups) - _MAX_NOTES} more groups that sum exactly")
        nw = self.net_worth()
        if nw is not None:
            cap, pl = nw
            out.append(f"- Net worth = capital {_inr(cap)} + profit & loss balance {_inr(pl)} "
                       f"= {_inr(cap + pl)} — the profit not yet moved to capital is net "
                       "worth, not a liability.")
        for t in self.totals:
            twin = any(o.groups and a[o.at].value == a[t.at].value for o in self.totals)
            if t.groups:
                out.append(f"- {item(t.at)} = " + " + ".join(item(i) for i in t.groups)
                           + " — the statement TIES to its printed total.")
            elif twin:
                # The other side of a two-sided statement prints the same total, and that
                # side was rebuilt: the sides agree as printed.
                out.append(f"- {item(t.at)}: the same figure as the total rebuilt above — "
                           "both sides of the statement agree as printed.")
            else:
                out.append(f"- {item(t.at)}: PRISM could not rebuild this total from the "
                           "groups it found. That is as likely a reading problem as an "
                           "imbalance — check the printed statement before saying it does "
                           "not balance.")
        return "\n".join(out)


def _inr(v: Decimal) -> str:
    """Indian digit grouping, as the statements print: 1,81,00,000.00."""
    sign, v = ("-" if v < 0 else ""), abs(v).quantize(Decimal("0.01"))
    whole, frac = f"{v:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join(([head] if head else []) + groups + [tail]) + "." + frac


def tnw_misreads(tnw_lakh: list[tuple[str, float]], statements: list[Arithmetic]) -> list[str]:
    """Periods whose locked TNW is a statement's capital ALONE while that statement also
    carries a profit & loss balance on the same side (both in lakh, to 0.05)."""
    out = []
    for s in statements:
        nw = s.net_worth()
        if nw is None:
            continue
        cap, pl = (float(x) / 100000 for x in nw)
        for label, tnw in tnw_lakh:
            if abs(tnw - cap) <= 0.05 and abs(pl) > 0.05:
                out.append(f"{label} TNW {tnw:g} is the partners' capital alone; with the "
                           f"profit & loss balance of {pl:.2f} lakh in {s.name} it is "
                           f"{cap + pl:.2f}")
    return out


def _label(text: str) -> str:
    # The words since the last line, cell or table break: "Capital Account", not the
    # table header printed before it.
    pieces = [p for p in re.split(r"\n|<br\s*/?>|\|", text) if p.strip(" -:")]
    text = pieces[-1] if pieces else ""
    text = re.sub(r"&amp;|[*#]", lambda m: "&" if m.group(0) == "&amp;" else " ", text)
    # "T o t a l" (letter-spaced headers in some PDF extractions) is "Total".
    text = re.sub(r"\b(?:\w ){2,}\w\b", lambda m: m.group(0).replace(" ", ""), text)
    text = re.sub(r"\s+", " ", text).strip(" :-–—")
    return text[-48:].strip()


def amounts(text: str) -> list[Amount]:
    out: list[Amount] = []
    prev = 0
    for m in _AMOUNT.finditer(text or ""):
        sign, digits, close = m.group(1) or "", m.group(2), m.group(3) or ""
        try:
            v = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue
        if "-" in sign or (sign == "(" and close == ")"):
            v = -v
        places = len(digits.split(".")[1]) if "." in digits else 0
        out.append(Amount(v, m.group(0).strip(), _label(text[prev:m.start()]), places))
        prev = m.end()
    return out


def _unit(x: Amount) -> Decimal:
    """Half of this, per figure summed, is the rounding a sum may carry: a statement
    printed in rupees and paise sums exactly; one printed rounded may be off by that."""
    return Decimal(1).scaleb(-x.places)


def _is_total(label: str) -> bool:
    return bool(re.search(r"\btotal\b", label, re.I))


def check(text: str, name: str = "statement") -> Arithmetic:
    """Groups (a figure equal to the sum of two or more figures right after it) and
    printed totals (equal to a run of the top-level items before them)."""
    a = amounts(text)
    res = Arithmetic(name, a)
    inside: set[int] = set()
    for i, head in enumerate(a):
        if head.value == 0 or _is_total(head.label):
            continue
        run, unit = Decimal(0), _unit(head)
        for j in range(i + 1, min(len(a), i + 1 + _MAX_LINES)):
            if _is_total(a[j].label):
                break
            run += a[j].value
            unit = min(unit, _unit(a[j]))
            # The tolerance is kept as the run grows rather than rebuilt at every line.
            if j - i >= 2 and abs(run - head.value) <= unit / 2 * (j - i + 1) + _EPS:
                res.groups.append(Group(i, list(range(i + 1, j + 1))))
                inside.update(range(i + 1, j + 1))
                break
    # A two-sided statement prints its total twice ("Total | Total"): each is matched to
    # its own side — the run nearest it that no earlier total used.
    used: set[int] = set()
    for t, tot in enumerate(a):
        if not _is_total(tot.label) or tot.value == 0:
            continue
        top = [i for i in range(t)
               if i not in inside and i not in used and not _is_total(a[i].label)]
        found: list[int] | None = None
        # The run ends among the last few items before the total and spans at most a
        # side's worth of lines: unbounded, this was cubic in the figures on the page, and
        # a 35-page OCR'd bank statement with its "Total" rows held the server's event
        # loop for minutes (Oct 2026).
        for e in range(len(top) - 1, max(len(top) - 1 - _TOTAL_ENDS, 0), -1):
            run, unit = a[top[e]].value, min(_unit(a[top[e]]), _unit(tot))
            for s in range(e - 1, max(e - 1 - _TOTAL_SPAN, -1), -1):
                run += a[top[s]].value
                unit = min(unit, _unit(a[top[s]]))
                if abs(run - tot.value) <= unit / 2 * (e - s + 2) + _EPS:
                    found = top[s:e + 1]
                    break
            if found:
                break
        used.update(found or [])
        res.totals.append(Total(t, found))
    return res


def is_statement(text: str) -> bool:
    return bool(_STATEMENT.search((text or "")[:20_000]))


def statement_notes(text: str, name: str) -> tuple[str, Arithmetic | None]:
    """(the block to show beside the statement, the arithmetic) — ("", None) for a
    document that is not a statement or in which PRISM found nothing to prove."""
    if not is_statement(text):
        return "", None
    arith = check(text, name)
    # No group, no proof of layout: a return or schedule with many "Total" rows (an ITR)
    # gets nothing rather than a list of totals PRISM could not rebuild.
    if not arith.groups:
        return "", None
    return arith.render(), arith


# ------------------------------------------------- net-worth (assets & liabilities) forms
# The SIDBI-style "Networth of Promoters / Guarantors" annexure: a form whose filled-in
# values sit after the printed labels. A real CAM read four of them as "liabilities only,
# no asset side" and called the guarantees "of indeterminate value" — the forms declared
# Rs 60–470 lakh of assets each, one with its own net-assets summary. PRISM reads the
# declared values itself and hands them over as fact.
_NW_FORM = re.compile(r"net\s*worth\s+of\s+promoters|details\s+of\s+assets", re.I)
_NW_VAL = (r"(?:[A-Za-z]{2,8}\s*-?\s*)?\d[\d.,]*\s*(?:L\b|lakhs?\b|cr\b)?"
           r"(?:\s*/\s*\d[\d.,]*\s*L\b)?")
_NW_PAIR = re.compile(r"(?:^|<br>|\n|\b[a-f]\)\s*)\s*([A-Za-z][A-Za-z /()&.,-]{2,70}?)\s*:\s*("
                      + _NW_VAL + ")", re.I)
# The summary's label and value are split by a colon as typed, or by a cell border when
# the form came through OCR as a table ("| Total Assets (Rs. Lakh) | 176.43 |"). Only the
# summary: the property table's cells ("House | 50%") are no label and value.
_NW_SUMMARY = (("movable assets", r"Value of Movable assets\s*[:|]\s*([\d.,]+\s*L?)"),
               ("immovable property", r"Value of immovable propert\w*\s*[:|]\s*([\d.,]+\s*L?)"),
               ("total assets", r"Total Assets[^:<|]*[:|]\s*([\d.,]+)"),
               ("total liabilities", r"Total liabilities[^:<|]*[:|]\s*([\d.,]+)"),
               ("net assets", r"Net Assets[^:<|]*[:|]\s*(-?[\d.,]+)"))


def networth_values(text: str) -> dict[str, object] | None:
    """What a net-worth form declares: {"person", "lines": [(label, value)], "summary":
    {key: value as printed}} — None for any other document, or a form left blank."""
    t = (text or "").replace("&amp;", "&")
    if not _NW_FORM.search(t[:20_000]):
        return None
    assets = re.search(r"DETAILS\s+OF\s+ASSETS", t, re.I)
    liab = re.search(r"DETAILS\s+OF\s+LIABILITIES", t, re.I)
    lines: list[tuple[str, str]] = []
    if assets:
        end = liab.start() if liab and liab.start() > assets.end() else assets.end() + 3000
        for m in _NW_PAIR.finditer(t[assets.end():end]):
            label = re.sub(r"^[a-f]\)\s*", "", m.group(1).strip(" :"))
            if not re.fullmatch(r"total", label, re.I):
                lines.append((label, m.group(2).strip()))
    summary = re.search(r"\bSUMMARY\b", t, re.I)
    tail = t[summary.end():] if summary else ""
    found = [(k, re.search(rx, tail, re.I)) for k, rx in _NW_SUMMARY] if tail else []
    summ = {k: hit.group(1).strip() for k, hit in found if hit}
    if not lines and not summ:
        return None
    who = re.search(r"Name\s+of\s+the\s+promoter\s*/?\s*Guarantor\s*:\s*([A-Za-z][A-Za-z .]{1,60}?)"
                    r"\s*(?:<br>|\n|\d\.|$)", t, re.I)
    return {"person": who.group(1).strip() if who else "", "lines": lines, "summary": summ}


def networth_notes(text: str, name: str) -> tuple[str, bool]:
    """(the block to show beside a net-worth form, whether it declares any asset) —
    ("", False) for any other document."""
    vals = networth_values(text)
    if vals is None:
        return "", False
    lines = vals["lines"]
    summ = vals["summary"]
    assert isinstance(lines, list) and isinstance(summ, dict)
    out = [f"===== PRISM READ: {name} — the values the form declares (Rs lakh unless "
           "marked) =====",
           "These are the guarantor's declared assets and summary. Report them as declared "
           "(self-declared, to be supported by documents), never as 'liabilities only' or "
           "'no asset side'."]
    out += [f"- {label}: {value}" for label, value in lines]
    if summ:
        out.append("- Summary as declared: " + "; ".join(f"{k} {v}" for k, v in summ.items()))
    return "\n".join(out), True


_NO_ASSETS = re.compile(r"\bno\s+(?:evidenced\s+)?asset\b|\bliabilit\w*[- ](?:side\s+)?only\b"
                        r"|\bliabilities[- ]side\s+declarations?\b|\bwithout\s+(?:any\s+|an\s+)?"
                        r"asset|\basset\s+side\s+(?:is\s+)?(?:missing|absent|blank|not)"
                        r"|\bno\s+asset\s+(?:side|schedule|annexure)", re.I)


def asset_claims(section_md: str, declared: list[str]) -> list[str]:
    """Sentences saying the guarantors declared no assets when PRISM read declared assets
    in their net-worth forms (``declared``: the forms' names)."""
    if not declared:
        return []
    body = "\n".join(ln for ln in section_md.splitlines() if not ln.lstrip().startswith("#"))
    return list(dict.fromkeys(
        f"'{s.strip()[:160]}' — but the net-worth forms declare assets: "
        + "; ".join(declared[:4])
        for s in re.split(r"(?<=[;!?])\s+|(?<=[a-z)\]][.])(?<!Rs\.)\s+|\n+|\|", body)
        if _NO_ASSETS.search(s)))


# What a misread looks like in a draft: THE STATEMENT "does not balance" (a schedule that
# "does not foot to the balance sheet" is another document's claim), a figure "printed
# twice", a group "unexplained".
_STMT = r"(?:balance\s*sheets?|statements?|accounts|liabilit\w*|books)"
_UNBALANCED = re.compile(
    rf"{_STMT}\W+(?:\w+\W+){{0,6}}?(?:does|do|did)\s+not\s+(?:balance|tie|add\s+up)\b"
    rf"|{_STMT}\W+(?:\w+\W+){{0,6}}?fails?\s+to\s+(?:balance|tie)\b"
    rf"|{_STMT}\s+(?:is|are)\s+(?:not\s+balanced|unbalanced|out\s+of\s+balance)\b"
    rf"|\bunbalanced\s+(?:\w+\s+)?{_STMT}", re.I)
_DOUBLE = re.compile(r"printed\s+twice|double[- ]?count|counted\s+twice|appears?\s+twice"
                     r"|\bduplicat\w*\b|stated\s+twice", re.I)
_UNEXPLAINED = re.compile(r"\bunexplained\b|\bunattributed\b|\bunidentified\b", re.I)
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
# The same figure in a draft is often in lakh or crore while the statement is in rupees.
_SCALES = (Decimal(1), Decimal(100000), Decimal(10000000))


def _matches(raw: str, values: list[Decimal]) -> bool:
    try:
        shown = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return False
    places = len(raw.split(".")[1]) if "." in raw else 0
    # Three significant digits at least: "8.8" lakh matches some small ledger line in
    # any statement; "110.4" names one.
    if len(re.sub(r"\D", "", raw).lstrip("0")) < 3:
        return False
    step = Decimal(1).scaleb(-places)
    for v in values:
        for s in _SCALES:
            if abs((abs(v) / s).quantize(step) - shown) <= step / 2:
                return True
    return False


def misread_claims(section_md: str, statements: list[Arithmetic]) -> list[str]:
    """Sentences a statement's own arithmetic disproves: one PRISM showed ties called
    unbalanced; a group total or its lines called a duplicate, a double count or
    unexplained."""
    if not statements:
        return []
    tied = [t for s in statements for t in s.tied]
    figs = [v for s in statements for v in s.figures()]
    heads = [v for s in statements for v in s.heads()]
    out: list[str] = []
    body = "\n".join(ln for ln in section_md.splitlines() if not ln.lstrip().startswith("#"))
    # A decimal point is not a sentence end ("973.6 lakh", "3.06x"), nor is "Rs." or "No.".
    for sentence in re.split(r"(?<=[;!?])\s+|(?<=[a-z)\]][.])(?<!Rs\.)(?<!No\.)\s+|\n+|\|",
                             body):
        nums = [m.group(0) for m in _NUM.finditer(sentence)]
        said = sentence.strip()[:160]
        if tied and _UNBALANCED.search(sentence):
            s = next(s for s in statements if s.tied)
            t = s.tied[0]
            out.append(f"'{said}' — but PRISM's arithmetic shows {s.name} ties: "
                       f"{s.amounts[t.at].raw} = the sum of its groups")
        elif _DOUBLE.search(sentence) and any(_matches(n, figs) for n in nums):
            out.append(f"'{said}' — but that figure is a group total or one of its lines "
                       "(see PRISM ARITHMETIC): one item, not a duplicate or a double count")
        elif _UNEXPLAINED.search(sentence) and any(_matches(n, heads) for n in nums):
            # Only a GROUP is explained by its lines; an unsecured loan whose lender is
            # not named is fairly called unattributed.
            out.append(f"'{said}' — but that figure is a group total: its lines are in "
                       "PRISM ARITHMETIC, so it is not unexplained")
    return list(dict.fromkeys(out))
