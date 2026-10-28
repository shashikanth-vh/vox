"""Findings — the credit picture, with its colours set by the credit team's rubric.

WHY. A weaker model asked to "weigh the evidence" weighs it the way its last paragraph
leaned: the reviewed CAM rated six of nine risks High on a CMR-3 file at 0 DPD and had no
GREEN anywhere. Whether a coverage of 2.6x is comfortable, or a CMR-3 is a strength, is
not the model's call — the CAM prompt's own A3 tables already say. So the v2 harness sets
each signal in code from ``cam_rubric.json`` (the bands of A3, owned by the credit team),
on the figures PRISM computed and the ledger read, and hands the writers a list of
findings: what the evidence shows, its signal, the section that owns it, what offsets it
and what would resolve it. The writer explains a finding; it never re-grades it. An
analyst can override a signal for the next draft, with a reason — named in the CAM.

Pure functions: no model, no network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.cam_facts import Ledger
from app.cam_facts import _latest as _latest_debts
from app.cam_ratios import Figures, compute

_DEFAULT = Path(__file__).with_name("cam_rubric.json")
SIGNALS = ("GREEN", "AMBER", "RED")


@dataclass
class Finding:
    id: str
    topic: str
    home: str                  # a pattern of section headings that own it
    signal: str
    evidence: str
    resolve: str = ""
    offsets: list[str] = field(default_factory=list)
    override: str = ""         # the analyst's reason, when they set the signal


def load_rubric(path: str = "") -> dict[str, Any]:
    """The rubric at ``path`` (the deployment's edited copy), else the packaged one."""
    p = Path(path) if path else _DEFAULT
    with open(p, encoding="utf-8") as fh:
        rubric: dict[str, Any] = json.load(fh)
    return rubric


def _latest(fig: Figures | None) -> tuple[str, dict[str, float | None]]:
    """(the latest historical period, its computed ratios)."""
    if fig is None or not fig.periods:
        return "", {}
    hist = [p for p in fig.periods if p.basis != "projected"] or fig.periods
    label = hist[-1].label
    return label, {r.name: r.values.get(label) for r in compute(fig)}


def bank_limits(ledger: Ledger | None) -> dict[str, float]:
    """Each bank's working-capital limit on record (CC/OD or sanctioned limit), lakh."""
    out: dict[str, float] = {}
    for f in (ledger.facts if ledger else []):
        if f.key.startswith("limit.") and "drawing power" not in f.key \
                and isinstance(f.value, float):
            bank = f.key.split(".", 2)[-1]
            out[bank] = max(out.get(bank, 0.0), f.value)
    return out


def assess(ledger: Ledger | None, fig: Figures | None, facility_lakh: float | None,
           params: dict[str, Any] | None = None,
           facility_rate: float | None = None) -> dict[str, Any]:
    """The sizing a committee asks for, from figures PRISM already holds — so a draft
    neither sizes the facility as if no bank funded the same current assets, nor calls
    DSCR "not computable" while the debt profile states every instalment (Pulze, Oct
    2026). On the latest historical period:

    * working-capital gap (second method of lending): ``(1 − margin) × current assets −
      other current liabilities − the existing bank limits``;
    * current-asset cover: stock and debtors after haircuts, over the existing limits
      plus this facility;
    * DSCR, estimated when the dataset has no scheduled principal: cash accruals plus
      interest over a year of the debt profile's instalments plus interest on working
      capital (all interest when term interest is not separable — conservative), plus
      a year's interest on this facility at the term sheet's rate — leaving it out
      showed Pulze 1.59x where 1.34x was the honest figure;
    * the same working-capital gap on the first projected year, beside the actual one:
      a limit is sized on the year it will fund, and the actual alone read "no gap".

    The margin and haircuts are the credit team's, in the rubric's "params"."""
    prm = {"wc_margin": 0.25, "stock_haircut": 0.5, "debtor_haircut": 0.25, **(params or {})}
    hist = [p for p in (fig.periods if fig else []) if p.basis != "projected"]
    if not hist:
        return {}
    p = hist[-1]
    limits = bank_limits(ledger)
    out: dict[str, Any] = {"period": p.label, "limits": limits, "facility": facility_lakh,
                           **prm}
    ca, cl, wc = p["current_assets"], p["current_liabilities"], p["wc_debt"]
    if ca is not None and cl is not None:
        other = cl - (wc or 0)
        mpbf = (1 - prm["wc_margin"]) * ca - other
        out.update(ca=ca, other_cl=other, mpbf=mpbf, gap=mpbf - sum(limits.values()))
    proj = next((q for q in (fig.periods if fig else []) if q.basis == "projected"
                 and q.label not in {h.label for h in hist}), None)
    if proj is not None and proj["current_assets"] is not None \
            and proj["current_liabilities"] is not None:
        pother = proj["current_liabilities"] - (proj["wc_debt"] or 0)
        pmpbf = (1 - prm["wc_margin"]) * proj["current_assets"] - pother
        out.update(proj_period=proj.label, proj_ca=proj["current_assets"], proj_other_cl=pother,
                   proj_mpbf=pmpbf, proj_gap=pmpbf - sum(limits.values()))
    stock, debtors = p["inventory"], p["trade_receivables"]
    owed = sum(limits.values()) + (facility_lakh or 0)
    if stock is not None and debtors is not None and owed > 0:
        value = stock * (1 - prm["stock_haircut"]) + debtors * (1 - prm["debtor_haircut"])
        out.update(stock=stock, debtors=debtors, cover_value=value, cover_owed=owed,
                   cover=value / owed)
    got = _latest_debts(ledger.debts) if ledger else None
    emis = [r.emi for r in (got[1] if got else []) if r.emi]
    pat, dep, it = p["pat"], p["depreciation"], p["interest_total"]
    if emis and pat is not None and dep is not None and it is not None:
        itd = p["interest_term_debt"]
        wc_int = max(it - itd, 0.0) if itd is not None else it
        ours = (facility_lakh or 0) * (facility_rate or 0) / 100
        service = 12 * sum(emis) + wc_int + ours
        if service > 0:
            out.update(dscr=(pat + dep + it) / service, emi_month=sum(emis),
                       debt_profile=got[0] if got else "", dscr_conservative=itd is None,
                       facility_interest=ours, facility_rate=facility_rate)
    return out


def limit_table(a: dict[str, Any]) -> str:
    """The sizing above as a table, every step shown."""
    if "gap" not in a and "cover" not in a:
        return ""
    rows = ["| Step | Rs lakh |", "| --- | --- |"]
    if "gap" in a:
        rows += [f"| Current assets ({a['period']}) | {a['ca']:,.1f} |",
                 f"| Less: {a['wc_margin']:.0%} borrower's margin | "
                 f"{a['wc_margin'] * a['ca']:,.1f} |",
                 f"| Less: other current liabilities (excl. bank WC) | {a['other_cl']:,.1f} |",
                 f"| Permissible bank finance | {a['mpbf']:,.1f} |"]
        rows += [f"| Less: existing limit — {b.title()} | {v:,.1f} |"
                 for b, v in a["limits"].items()]
        rows.append(f"| **Working-capital gap ({a['period']}, actual)** | **{a['gap']:,.1f}** |")
        if "proj_gap" in a:
            rows += [f"| Current assets ({a['proj_period']}, projected) | {a['proj_ca']:,.1f} |",
                     f"| Less: margin and other current liabilities | "
                     f"{a['proj_ca'] - a['proj_mpbf']:,.1f} |",
                     f"| Less: existing bank limits | {sum(a['limits'].values()):,.1f} |",
                     f"| **Working-capital gap ({a['proj_period']}, projected)** | "
                     f"**{a['proj_gap']:,.1f}** |"]
        if a.get("facility"):
            rows.append(f"| Facility proposed | {a['facility']:,.1f} |")
    if "cover" in a:
        rows += [f"| Stock {a['stock']:,.1f} at {a['stock_haircut']:.0%} haircut + debtors "
                 f"{a['debtors']:,.1f} at {a['debtor_haircut']:.0%} | {a['cover_value']:,.1f} |",
                 f"| Working-capital lending (existing limits + this facility) | "
                 f"{a['cover_owed']:,.1f} |",
                 f"| **Current-asset cover** | **{a['cover']:.2f}x** |"]
    return "\n".join(rows)


def metrics(ledger: Ledger | None, fig: Figures | None, *, statements_tie: bool | None,
            facility_lakh: float | None, params: dict[str, Any] | None = None,
            facility_rate: float | None = None,
            ) -> tuple[str, dict[str, float | None], dict[str, Any]]:
    """(period, {metric: value}, extra format values) — everything the rubric can grade."""
    period, ratios = _latest(fig)
    m: dict[str, float | None] = dict(ratios)
    extra: dict[str, Any] = {}
    facts = {f.key: f for f in (ledger.facts if ledger else [])}
    cmr = facts.get("bureau.cmr")
    if cmr is not None and (hit := re.search(r"\d+", str(cmr.value))):
        m["cmr"] = float(hit.group(0))
    if statements_tie is not None:
        m["statements_tie"] = 1.0 if statements_tie else 0.0
    gs = ledger.guarantors if ledger else []
    nets = [g.net_assets for g in gs if g.net_assets is not None]
    after = [g.net_after_bureau for g in gs if g.net_after_bureau is not None]
    if nets and facility_lakh:
        m["guarantor_cover"] = sum(after) / facility_lakh
        extra["total"] = sum(after)
        if any(g.bureau is not None for g in gs) and sum(after) < sum(nets) - 0.5:
            extra["guarantor_note"] = (
                f" — after the partners' own bureau balances (Rs. {sum(nets):,.1f} lakh as "
                "declared); some of those balances may be the firm's loans they co-signed")
        off = [f"{g.person}: {c}" for g in gs for c in g.checks]
        if off:
            m["guarantor_form_off"] = 1.0
            extra["guarantor_form_off_says"] = (" — and a form does not add up (" + "; ".join(off)
                                                + ")")
    if ledger is not None:
        conflicts = ledger.conflicts()
        m["conflicts"] = float(len(conflicts))
        extra["detail"] = "; ".join(k.split(".", 1)[-1] for k, _f in conflicts[:4]) or "none"
    # What a GREEN must not hide (a rule names these under "green_needs"): a year not yet
    # audited, a CC drawn past its limit.
    basis = next((p.basis for p in (fig.periods if fig else []) if p.label == period), "")
    if period and basis and basis != "audited":
        m["unaudited"] = 1.0
        extra["unaudited_says"] = f" — on {basis} figures, not yet audited"
    over = [f for f in facts.values() if f.key.startswith("bank.overdrawn days.")
            and isinstance(f.value, float) and f.value > 0]
    if over:
        worst = max(over, key=lambda f: float(f.value))
        latest = facts.get(worst.key.replace("overdrawn days.", "overdrawn days latest month."))
        m["cc_overdrawn"] = float(worst.value)
        extra["cc_overdrawn_says"] = (
            f" — but the CC/OD was drawn over its limit on {float(worst.value):.0f} day(s) in "
            f"the statement window" + (f", {float(latest.value):.0f} of them in the latest month"
                                       if latest is not None and latest.value else "")
            + f" ({worst.source})")
    # Leverage on what is owed now: a bureau total above the statements' debt replaces it.
    owed = facts.get("bureau.total outstanding")
    adj, lev = m.get("Adjusted TNW"), m.get("Total Debt / Adj. TNW")
    if owed is not None and isinstance(owed.value, float) and adj and adj > 0 and lev is not None:
        then = lev * adj
        if owed.value > then * 1.1:
            m["Total Debt / Adj. TNW"] = owed.value / adj
            extra["leverage_note"] = (f" — on the Rs. {owed.value:,.1f} lakh the company bureau "
                                      f"report shows outstanding now (Rs. {then:,.1f} lakh in "
                                      f"the {period} statements)")
    a = assess(ledger, fig, facility_lakh, params, facility_rate)
    if "gap" in a and facility_lakh:
        m["wc_gap_cover"] = a["gap"] / facility_lakh
        extra.update(gap=a["gap"], limits=sum(a["limits"].values()), facility=facility_lakh)
        if "proj_gap" in a:
            extra["gap_note"] = (f"; on the {a['proj_period']} projection the gap is "
                                 f"Rs. {a['proj_gap']:,.1f} lakh — the limit lies between the two, "
                                 "sized on what the actual year supports")
    if "cover" in a:
        m["ca_cover"] = a["cover"]
        extra.update(cover_value=a["cover_value"], cover_owed=a["cover_owed"])
    if "dscr" in a and m.get("DSCR") is None:
        m["DSCR"] = a["dscr"]
        extra["dscr_note"] = (f" — estimated: Rs. {a['emi_month']:,.2f} lakh a month of "
                              f"instalments on the {a['debt_profile']} plus interest on working "
                              "capital" + (" (all interest counted — conservative)"
                                           if a["dscr_conservative"] else "")
                              + (f" plus Rs. {a['facility_interest']:,.1f} lakh a year on this "
                                 f"facility at {a['facility_rate']:g}%"
                                 if a.get("facility_interest") else
                                 " — interest on this facility not included (no rate on "
                                 "record)"))
    return period, m, extra


def _band(rule: dict[str, Any], v: float) -> dict[str, Any] | None:
    for b in rule.get("bands") or []:
        if "max" in b and v <= float(b["max"]):
            return b
        if "min" in b and v >= float(b["min"]):
            return b
    return None


def evaluate(rubric: dict[str, Any], ledger: Ledger | None, fig: Figures | None, *,
             statements_tie: bool | None = None, facility_lakh: float | None = None,
             overrides: dict[str, dict[str, str]] | None = None,
             facility_rate: float | None = None) -> list[Finding]:
    """Every rubric rule whose metric PRISM has, graded; a rule without its metric is left
    out (the writer covers it from the documents), never guessed."""
    period, m, extra = metrics(ledger, fig, statements_tie=statements_tie,
                               facility_lakh=facility_lakh, params=rubric.get("params"),
                               facility_rate=facility_rate)
    out: list[Finding] = []
    for rule in rubric.get("rules") or []:
        v = m.get(rule["metric"])
        if v is None or (rule.get("only_without") and m.get(rule["only_without"]) is not None):
            continue
        band = _band(rule, v)
        if band is None:
            continue
        try:
            says = str(band["says"]).format(v=v, a=abs(v), p=period, **extra)
        except (KeyError, ValueError, IndexError):
            says = str(band["says"])
        signal = band["signal"]
        if rule.get("append") and extra.get(rule["append"]):
            says += extra[rule["append"]]
        for need in rule.get("green_needs") or []:
            if signal == "GREEN" and m.get(need):
                signal, says = "AMBER", says + extra.get(f"{need}_says", f" — {need}")
        out.append(Finding(rule["id"], rule["topic"], rule.get("home", ""), signal,
                           says, rule.get("resolve", ""), list(rule.get("offset_by") or [])))
    for f in out:
        o = (overrides or {}).get(f.id)
        if o and str(o.get("signal", "")).upper() in SIGNALS:
            f.signal, f.override = str(o["signal"]).upper(), str(o.get("note") or "set by the analyst")
    return out


def block(findings: list[Finding]) -> str:
    """What every writer is given: the findings, their signals and their homes."""
    if not findings:
        return ""
    by = {f.id: f for f in findings}
    out = ["===== PRISM FINDINGS (signals set by the credit team's rubric — explain each in "
           "four beats in the section that owns it; never change a signal) ====="]
    for f in findings:
        offs = [by[o].evidence for o in f.offsets if o in by and by[o].signal == "GREEN"]
        out.append(f"- [{f.id}] {f.topic} — {f.signal}: {f.evidence}."
                   + (f" Offsetting: {'; '.join(offs)}." if offs and f.signal != "GREEN" else "")
                   + (f" To resolve: {f.resolve}." if f.resolve and f.signal != "GREEN" else "")
                   + (f" (Signal set by the analyst: {f.override}.)" if f.override else "")
                   + f" Owned by: {f.home.replace('|', ' / ').lower()}.")
    return "\n".join(out)


def picture_table(findings: list[Finding]) -> str:
    """The credit picture at a glance, rendered by PRISM: strengths first, then what to
    watch, then what to resolve."""
    if not findings:
        return ""
    order = {"GREEN": 0, "AMBER": 1, "RED": 2}
    rows = ["| Factor | Signal | What the evidence shows |", "| --- | --- | --- |"]
    for f in sorted(findings, key=lambda f: order.get(f.signal, 3)):
        note = f" (analyst: {f.override})" if f.override else ""
        rows.append(f"| {f.topic} | {f.signal} | {f.evidence}{note} |")
    return "\n".join(rows)


def homes_of(findings: list[Finding], heading: str) -> list[Finding]:
    """The findings a section owns."""
    return [f for f in findings if f.home and re.search(f.home, heading, re.I)]
