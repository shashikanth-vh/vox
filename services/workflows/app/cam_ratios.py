"""The CAM's ratios, computed by PRISM — not by the model.

The master prompt locks one formula per ratio (A3.5) and one band per assessment
(A3.3). A model asked to "compute every ratio" does the arithmetic in its head, and
on a real draft (FarmWAtt) Section 5 then recomputed margins and ratios again on
its own: 17 table figures that were in no workpaper. So the split is:

* the dataset step EXTRACTS — it ends its workpaper with a ``locked-figures`` JSON
  block: the base figures per period, in Rs. lakh, as the documents state them;
* this module COMPUTES — every A3.5 ratio from those figures, exactly as defined,
  and the A3.3 assessments, into a table the writers must quote;
* a ratio whose inputs are not on record is named with what it needs, never
  estimated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# The base figures the dataset step must state per period (Rs. lakh), with what each
# means — this text IS the contract the model is given.
FIELDS: dict[str, str] = {
    "net_sales": "net revenue from operations",
    "cogs": "cost of goods sold / cost of materials consumed",
    "purchases": "purchases of stock-in-trade and materials (if stated)",
    "pbdit": "profit before depreciation, interest and tax (EBITDA)",
    "depreciation": "depreciation and amortisation",
    "non_cash_charges": "other non-cash charges added back (if any)",
    "interest_total": "total interest / finance cost",
    "interest_term_debt": "interest on term debt only (if separable)",
    "principal_due": "scheduled principal repayments due in the year",
    "pat": "profit after tax",
    "tnw": "tangible net worth (share capital + reserves)",
    "intangibles": "intangible assets",
    "related_party_advances": "loans/advances to related parties",
    "group_investments": "investments in group companies",
    "promoter_unsecured_loans": "unsecured loans from promoters/directors",
    "term_debt": "term borrowings (long + current maturities)",
    "wc_debt": "working-capital bank borrowings",
    "total_outside_liabilities": "total outside liabilities",
    "current_assets": "total current assets",
    "current_liabilities": "total current liabilities incl. WC bank borrowings",
    "trade_receivables": "closing trade receivables",
    "trade_payables": "closing trade payables",
    "inventory": "closing inventory",
}

FIGURES_TASK = (
    "Then END the workpaper with ONE fenced block, exactly:\n"
    "```locked-figures\n"
    '{"unit": "Rs lakh", "promoter_loans_subordinated": false, "periods": [\n'
    '  {"period": "FY24", "basis": "audited", <field>: <number or null>, ...}, ...]}\n'
    "```\n"
    "Periods OLDEST FIRST; basis is one of audited | provisional | projected. Give every "
    "field below for every period, in Rs. lakh as a plain number (no commas, no units), "
    "or null when the documents do not state it — never estimate, never derive a figure "
    "the documents do not give. Each number must be one your workpaper tables show (PRISM "
    "traces them). Units: note the unit each source prints (rupees, hundreds, thousands, "
    "lakh, crore — MCA/AOC filings are often in rupees or thousands) and convert to lakh "
    "deliberately; a year-on-year jump of 10x or more is almost always a unit slip — "
    "re-check it before you lock it. Set promoter_loans_subordinated true ONLY when a "
    "subordination/retention undertaking is on record. Fields:\n"
    + "\n".join(f"- {k}: {v}" for k, v in FIELDS.items())
    + "\nDo NOT compute ratios, margins, growth rates or day counts: PRISM computes "
    "them from this block with the prompt's locked formulas.")

_BLOCK = re.compile(r"```locked-figures\s*\n(.*?)```", re.S)


@dataclass
class Period:
    label: str
    basis: str
    v: dict[str, float | None] = field(default_factory=dict)

    def __getitem__(self, key: str) -> float | None:
        return self.v.get(key)


@dataclass
class Figures:
    periods: list[Period]
    subordinated: bool = False


def trace(fig: Figures, workpaper: str) -> list[str]:
    """Drop every figure that is not in the workpaper's own text (rounded to the
    precision written there): the block must restate the locked tables, not add to
    them. Returns what was dropped, "FY25 pbdit 180.7"."""
    from decimal import ROUND_HALF_UP, Decimal

    shown: list[Decimal] = []
    for m in re.finditer(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", workpaper):
        try:
            shown.append(Decimal(m.group(0).replace(",", "")))
        except ArithmeticError:
            continue

    def found(x: float) -> bool:
        d = Decimal(str(abs(x)))
        return any(d.quantize(Decimal(1).scaleb(-max(0, -v.as_tuple().exponent)),  # type: ignore[operator]
                              ROUND_HALF_UP) == v for v in shown)

    dropped = []
    for p in fig.periods:
        for k, x in p.v.items():
            if x is not None and x != 0 and not found(x):
                p.v[k] = None
                dropped.append(f"{p.label} {k} {x:g}")
    return dropped


def parse_figures(text: str) -> tuple[Figures | None, str, str | None]:
    """(figures, the workpaper without the block, why not) — a missing or malformed
    block is reported, and the workpaper is kept either way."""
    m = _BLOCK.search(text or "")
    if not m:
        return None, text, "the dataset step returned no locked-figures block"
    cleaned = (text[:m.start()] + text[m.end():]).rstrip() + "\n"
    try:
        raw = json.loads(m.group(1))
        periods = []
        for p in raw.get("periods") or []:
            vals: dict[str, float | None] = {}
            for k in FIELDS:
                x = p.get(k)
                vals[k] = float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) \
                    else None
            periods.append(Period(str(p.get("period") or "?"),
                                  str(p.get("basis") or "").lower(), vals))
    except (ValueError, TypeError, AttributeError) as exc:
        return None, cleaned, f"the locked-figures block could not be read ({exc})"
    if not periods:
        return None, cleaned, "the locked-figures block has no periods"
    return Figures(periods, bool(raw.get("promoter_loans_subordinated"))), cleaned, None


# ------------------------------------------------------------------------ compute
def _div(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b == 0 else a / b


def _need(p: Period, *keys: str) -> list[str]:
    return [k for k in keys if p[k] is None]


@dataclass
class Ratio:
    name: str
    unit: str                        # "x", "%", "days", "lakh"
    values: dict[str, float | None]  # period label → value
    notes: list[str] = field(default_factory=list)


def compute(fig: Figures) -> list[Ratio]:
    """Every A3.5 ratio, per period, with the formula's exact definition."""
    ps = fig.periods
    out: list[Ratio] = []

    def per(name: str, unit: str, fn: Any, needs: tuple[str, ...], note: str = "") -> Ratio:
        r = Ratio(name, unit, {})
        missing: set[str] = set()
        for p in ps:
            gap = _need(p, *needs)
            r.values[p.label] = None if gap else fn(p)
            missing.update(gap)
        if missing:
            r.notes.append("not computable where missing: " + ", ".join(sorted(missing)))
        if note:
            r.notes.append(note)
        return r

    out.append(per("PBDIT Margin", "%", lambda p: 100 * p["pbdit"] / p["net_sales"]
                   if p["net_sales"] else None, ("pbdit", "net_sales")))
    out.append(per("PAT Margin", "%", lambda p: 100 * p["pat"] / p["net_sales"]
                   if p["net_sales"] else None, ("pat", "net_sales")))
    growth = Ratio("Revenue growth YoY", "%", {})
    for prev, cur in zip([None, *ps[:-1]], ps, strict=True):
        growth.values[cur.label] = (None if prev is None else
                                    (100 * (cur["net_sales"] / prev["net_sales"] - 1)
                                     if cur["net_sales"] is not None and prev["net_sales"]
                                     else None))
    out.append(growth)
    out.append(per("Interest Coverage", "x", lambda p: _div(p["pbdit"], p["interest_total"]),
                   ("pbdit", "interest_total")))

    # DSCR — or Interest Coverage as the service metric when there is no term debt.
    dscr = Ratio("DSCR", "x", {})
    no_term = all(not (p["term_debt"] or p["interest_term_debt"] or p["principal_due"])
                  for p in ps)
    for p in ps:
        gap = _need(p, "pat", "depreciation", "interest_term_debt", "principal_due")
        dscr.values[p.label] = None if gap else _div(
            p["pat"] + p["depreciation"] + p["interest_term_debt"],    # type: ignore[operator]
            p["interest_term_debt"] + p["principal_due"])              # type: ignore[operator]
    if no_term:
        dscr.notes.append("no term debt in the system — Interest Coverage is the primary "
                          "service metric (A3.5)")
    elif any(v is None for v in dscr.values.values()):
        dscr.notes.append("not computable where missing: interest on term debt and/or "
                          "scheduled principal due")
    out.append(dscr)

    out.append(per("Cash Accruals", "lakh",
                   lambda p: p["pat"] + p["depreciation"] + (p["non_cash_charges"] or 0),
                   ("pat", "depreciation")))

    # Adjusted TNW and leverage (A3.5 [CONFIRM-2]).
    adj = Ratio("Adjusted TNW", "lakh", {})
    lev = Ratio("Total Debt / Adj. TNW", "x", {})
    assumed: set[str] = set()
    for p in ps:
        if p["tnw"] is None:
            adj.values[p.label] = lev.values[p.label] = None
            continue
        ded = 0.0
        for k in ("intangibles", "related_party_advances", "group_investments"):
            if p[k] is None:
                assumed.add(k)
            ded += p[k] or 0
        promoter = p["promoter_unsecured_loans"] or 0
        a = p["tnw"] - ded + (promoter if fig.subordinated else 0)
        adj.values[p.label] = a
        if p["term_debt"] is None and p["wc_debt"] is None:
            lev.values[p.label] = None
            continue
        debt = (p["term_debt"] or 0) + (p["wc_debt"] or 0) + (0 if fig.subordinated else promoter)
        lev.values[p.label] = _div(debt, a) if a > 0 else None
    adj.notes.append("promoter loans " + ("ADDED (subordination on record)" if fig.subordinated
                                          else "EXCLUDED (no subordination evidenced) — "
                                               "sensitivity applies"))
    if assumed:
        adj.notes.append("taken as nil, not on record: " + ", ".join(sorted(assumed)))
    if any(v is None for v in adj.values.values()):
        adj.notes.append("not computable where TNW is missing")
    out += [adj, lev]

    out.append(per("TOL/TNW", "x", lambda p: _div(p["total_outside_liabilities"], p["tnw"]),
                   ("total_outside_liabilities", "tnw")))
    out.append(per("Current Ratio", "x", lambda p: _div(p["current_assets"],
                                                        p["current_liabilities"]),
                   ("current_assets", "current_liabilities")))
    dd = per("Debtor Days", "days", lambda p: _days(p["trade_receivables"], p["net_sales"]),
             ("trade_receivables", "net_sales"),
             "closing, not average; receivables carry GST while sales do not (~15–18% "
             "of apparent stretch may be a GST artefact)")
    cd = Ratio("Creditor Days", "days", {})
    via_cogs = False
    for p in ps:
        base = p["purchases"]
        if base is None:
            base, via_cogs = p["cogs"], via_cogs or p["cogs"] is not None
        cd.values[p.label] = _days(p["trade_payables"], base)
    if via_cogs:
        cd.notes.append("purchases not stated for some periods — COGS used (A3.5)")
    inv = per("Inventory Days", "days", lambda p: _days(p["inventory"], p["cogs"]),
              ("inventory", "cogs"))
    ccc = Ratio("Cash Conversion Cycle", "days", {
        p.label: (None if None in (dd.values[p.label], inv.values[p.label], cd.values[p.label])
                  else dd.values[p.label] + inv.values[p.label]   # type: ignore[operator]
                  - cd.values[p.label])                           # type: ignore[operator]
        for p in ps})
    out += [dd, cd, inv, ccc]

    cost = Ratio("Blended Cost of Debt", "%", {})
    for prev, cur in zip([None, *ps[:-1]], ps, strict=True):
        if prev is None:
            cost.values[cur.label] = None
            continue
        debts = [_total_debt(x) for x in (prev, cur)]
        avg = None if None in debts else (debts[0] + debts[1]) / 2   # type: ignore[operator]
        cost.values[cur.label] = (None if cur["interest_total"] is None or not avg
                                  else 100 * cur["interest_total"] / avg)
    cost.notes.append("total interest / average of opening and closing total debt")
    out.append(cost)
    return out


def _days(a: float | None, b: float | None) -> float | None:
    q = _div(a, b)
    return None if q is None else q * 365


def _total_debt(p: Period) -> float | None:
    if p["term_debt"] is None and p["wc_debt"] is None:
        return None
    return (p["term_debt"] or 0) + (p["wc_debt"] or 0)


def cagr(fig: Figures) -> tuple[float | None, str]:
    """Revenue CAGR over the audited periods: (latest/earliest)^(1/n) − 1."""
    aud = [p for p in fig.periods if p.basis == "audited" and p["net_sales"]]
    if len(aud) < 2:
        return None, "fewer than two audited years with sales"
    n = len(aud) - 1
    return (100 * ((aud[-1]["net_sales"] / aud[0]["net_sales"]) ** (1 / n) - 1),  # type: ignore[operator]
            f"{aud[0].label}–{aud[-1].label}, {n} year(s)")


def assessments(fig: Figures, ratios: list[Ratio]) -> list[tuple[str, str]]:
    """A3.3 bands for the latest historical (non-projected) period."""
    hist = [p for p in fig.periods if p.basis != "projected"] or fig.periods
    label = hist[-1].label
    by = {r.name: r.values.get(label) for r in ratios}
    out: list[tuple[str, str]] = []
    dscr, icr = by.get("DSCR"), by.get("Interest Coverage")
    if dscr is not None:
        out.append((f"DSCR {label}: {dscr:.2f}x", _band(dscr, [
            (2.0, "Comfortable; significant headroom"),
            (1.5, "Adequate; standard for mid-market MSME lending"),
            (1.2, "Tight; needs close monitoring; stress test mandatory")],
            "Stressed; flag as credit concern")))
    elif icr is not None:
        out.append((f"Interest Coverage {label}: {icr:.2f}x (DSCR not computable)",
                    "assess as the service metric"))
    cr = by.get("Current Ratio")
    if cr is not None:
        out.append((f"Current Ratio {label}: {cr:.2f}", _band(cr, [
            (1.5, "Healthy"), (1.2, "Adequate for asset-light; tight for inventory-heavy"),
            (1.0, "Tight; flag WC stress")],
            "Negative working capital; explain context")))
    lev = by.get("Total Debt / Adj. TNW")
    if lev is not None:
        band = ("Conservative" if lev < 1.5 else "Acceptable for growth-stage MSME"
                if lev <= 3.0 else "Elevated; needs strong cash flow support" if lev <= 5.0
                else "Highly leveraged; flag as material concern")
        out.append((f"Total Debt / Adj. TNW {label}: {lev:.2f}x", band))
    return out


# Beyond these a figure is far more likely a unit or period slip than a business fact.
_IMPLAUSIBLE: dict[str, tuple[float, float]] = {
    "Revenue growth YoY": (-80, 300), "PBDIT Margin": (-60, 60), "PAT Margin": (-60, 60),
    "Total Debt / Adj. TNW": (0, 15), "TOL/TNW": (0, 15), "Current Ratio": (0.2, 8),
    "Debtor Days": (0, 365), "Creditor Days": (0, 365), "Inventory Days": (0, 365),
    "Blended Cost of Debt": (3, 30), "Interest Coverage": (-20, 50), "DSCR": (-10, 20),
}


def plausibility(fig: Figures, ratios: list[Ratio]) -> list[str]:
    """Values outside what a real MSME shows — named, so the analyst checks the unit
    and period of the inputs before any of it is read as a finding."""
    out = []
    for r in ratios:
        lo, hi = _IMPLAUSIBLE.get(r.name, (float("-inf"), float("inf")))
        for label, v in r.values.items():
            if v is not None and not lo <= v <= hi:
                out.append(f"{r.name} {label} = {_fmt(v, r.unit)}")
    g, _span = cagr(fig)
    if g is not None and g > 150:
        out.append(f"Revenue CAGR = {_fmt(g, '%')}")
    return out + consistency(fig)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 0.5


def consistency(fig: Figures) -> list[str]:
    """Identities a correct extraction cannot break. Each is a mis-read line, not a
    business fact: Pulze's first draft locked current liabilities equal to total outside
    liabilities in every year (with term debt on the books) and one year's term debt
    equal to its working-capital debt — and every leverage ratio inherited both."""
    out = []
    for p in fig.periods:
        tol, cl = p["total_outside_liabilities"], p["current_liabilities"]
        td, wc = p["term_debt"], p["wc_debt"]
        if tol is not None and cl is not None:
            if cl > tol + 0.5:
                out.append(f"Consistency {p.label}: current liabilities {cl:g} exceed total "
                           f"outside liabilities {tol:g}")
            elif _close(tol, cl) and ((td or 0) > 0 or (p["promoter_unsecured_loans"] or 0) > 0):
                out.append(f"Consistency {p.label}: total outside liabilities equal current "
                           f"liabilities ({tol:g}) although term debt or unsecured loans are "
                           "on the books — TOL is probably the current-liabilities total")
        if td and wc and _close(td, wc):
            out.append(f"Consistency {p.label}: term debt and working-capital debt are the "
                       f"same figure ({td:g}) — one was probably copied into the other")
        if wc is not None and cl is not None and wc > cl + 0.5:
            out.append(f"Consistency {p.label}: working-capital debt {wc:g} exceeds current "
                       f"liabilities {cl:g}, which include it")
        it, itd = p["interest_total"], p["interest_term_debt"]
        if it is not None and itd is not None and itd > it + 0.5:
            out.append(f"Consistency {p.label}: interest on term debt {itd:g} exceeds total "
                       f"interest {it:g}")
        pat, pbdit = p["pat"], p["pbdit"]
        if pat is not None and pbdit is not None and pat > pbdit + 0.5:
            out.append(f"Consistency {p.label}: PAT {pat:g} exceeds PBDIT {pbdit:g}")
        ca = p["current_assets"]
        parts = [x for x in (p["trade_receivables"], p["inventory"]) if x is not None]
        if ca is not None and parts and sum(parts) > ca + 0.5:
            out.append(f"Consistency {p.label}: receivables + inventory {sum(parts):g} exceed "
                       f"current assets {ca:g}")
    return out


def _band(v: float, steps: list[tuple[float, str]], below: str) -> str:
    for floor, text in steps:
        if v >= floor:
            return text
    return below


# ------------------------------------------------------------------------- render
def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "not computable"
    if unit == "x":
        return f"{v:.2f}x"
    if unit == "%":
        return f"{v:.1f}%"
    return f"{v:,.1f}"                           # lakh and days: one decimal (A3.8)


def ratios_markdown(fig: Figures) -> str:
    """The table every section quotes. Figures and ratios, period by period."""
    ratios = compute(fig)
    labels = [p.label + (f" ({p.basis})" if p.basis else "") for p in fig.periods]
    head = "| Ratio | " + " | ".join(labels) + " | Notes |"
    rule = "| --- | " + " | ".join("---" for _ in labels) + " | --- |"
    rows = [f"| {r.name} | " + " | ".join(_fmt(r.values[p.label], r.unit) for p in fig.periods)
            + f" | {'; '.join(r.notes) or '—'} |" for r in ratios]
    lines = ["## Computed ratios (A3.5 — computed by PRISM from the locked figures)", "",
             "Quote these exactly. Rs. lakh, one decimal; ratios two decimals.", "",
             head, rule, *rows]
    g, span = cagr(fig)
    lines += ["", f"**Revenue CAGR (audited):** {_fmt(g, '%')} — {span}."]
    hist = [p for p in fig.periods if p.basis != "projected"] or fig.periods
    latest = hist[-1]
    if latest["net_sales"] is not None:
        lines.append(f"**Debtor-stretch stress (A3.6, +30 days, {latest.label}):** incremental "
                     f"working capital Rs. {30 / 365 * latest['net_sales']:,.1f} lakh.")
    flags = plausibility(fig, ratios)
    if flags:
        lines += ["", "**⚠ Implausible values — check the source's unit (rupees / thousands "
                  "/ lakh / crore) and period before relying on them; state them as data "
                  "caveats, not findings:**", ""]
        lines += [f"- {f}" for f in flags]
    marks = assessments(fig, ratios)
    if marks:
        lines += ["", "**A3.3 assessments (latest historical period):**", ""]
        lines += [f"- {what}: {band}" for what, band in marks]
    return "\n".join(lines)
