"""The facts a risk grade must not leave to a model — read by PRISM from each file's
FULL text, before any budget clips it, and shown as the report's red flags.

A model reading 200k characters of bank statements and bureau reports paraphrases:
it missed a cheque bounce, read a promoter's 742 as "below 650", cited the capital
account as net worth. These are fixed-format documents — the desk's bank-statement
analysis (BSA), TransUnion CIBIL consumer and commercial reports, the ScoreMe legal
report, the CMA workbook — so their key fields are read here by label, the way an
analyst would look them up. A parser that does not recognise a file returns nothing:
no fact is ever guessed, and the coverage panel says which evidence was read.

Everything here is a pure function of (file name, text) so it is tested on real
documents without a stack.
"""

from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_DATE = re.compile(r"(20\d\d-\d\d-\d\d|\d\d[-/]\d\d[-/]20\d\d)")


def _cells(line: str) -> list[str]:
    """A table row's cells, empty ones kept (positions matter in the CMA grid)."""
    s = line.strip()
    if "|" not in s:
        return [s]
    return [c.strip() for c in s.strip("|").split("|")]


def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = _NUM.search(s.replace("₹", "").replace("Rs.", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


_STRICT = re.compile(r"^[₹\s]*(-?[\d,]+(?:\.\d+)?)\s*%?$")


def _val(cell: str | None) -> float | None:
    """A cell that IS a number (₹ and commas allowed) — not '#DIV/0!', not 'REJECT:408812'."""
    m = _STRICT.match((cell or "").replace("Rs.", "").strip())
    return float(m.group(1).replace(",", "")) if m else None


def _after(text: str, label: str) -> list[str]:
    """The non-empty cells after the first cell that IS ``label`` (case-insensitive),
    on the first line that has one."""
    want = label.lower()
    for line in text.splitlines():
        if want not in line.lower():
            continue
        cells = _cells(line)
        for i, c in enumerate(cells):
            if c.lower().startswith(want):
                rest = [x for x in cells[i + 1:] if x and x != "-"]
                if rest:
                    return rest
    return []


def _first_num(text: str, label: str) -> float | None:
    for c in _after(text, label):
        if (v := _num(c)) is not None:
            return v
    return None


def lakh(v: float | None) -> str:
    """Rupees → '₹2.83 Cr' / '₹3.64 L' — how the desk reads amounts."""
    if v is None:
        return "—"
    a = abs(v)
    return f"₹{a / 1e7:.2f} Cr" if a >= 1e7 else f"₹{a / 1e5:.2f} L"


# --------------------------------------------------------------------------- masking
_AADHAAR = re.compile(r"(?<![\d.])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\d.])")
_PAN = re.compile(r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b")


def mask_ids(text: str) -> str:
    """Aadhaar- and PAN-shaped numbers masked before any text leaves PRISM: a grade
    needs to know KYC exists and is legible, never the number itself. (12-digit
    references in statements are masked too — harmless, they are not evidence.)"""
    return _PAN.sub("[PAN]", _AADHAAR.sub("[ID-12]", text))


# --------------------------------------------------------------------------- BSA
def _bank_of(name: str, text: str) -> str:
    head = re.split(r"\s*[-_]\s*", name.strip(), maxsplit=1)[0]
    if 2 <= len(head) <= 12 and head.isalpha():
        return head.upper()
    got = _after(text, "Bank Name")
    return got[0] if got else name


def bsa_facts(name: str, text: str) -> dict[str, Any] | None:
    """A bank-statement analysis: limit, overdrawing, utilisation, returned cheques."""
    t = text
    if not (("bsa" in name.lower() or "analysed by" in t.lower())
            and re.search(r"cheque return|overview-month wise|over-drawn", t, re.I)):
        return None
    limit = _first_num(t, "CC/OD Limit")
    days = _first_num(t, "No. of days limit over-drawn")
    peak = _first_num(t, "Peak overdrawing amount")
    peak_date = next(iter(_after(t, "Peak overdrawing date")), None)
    util = [v for c in _after(t, "Average OD & CC Limit Utilization") if (v := _val(c)) is not None]
    # Returns are read only from the analysis's RETURN sheets (Cheque Return, Online
    # Returns, ECS/NACH/Cash Return) — the transaction listing repeats the same event
    # with a charge, and a cheque number is not an amount.
    bounces: list[dict[str, Any]] = []
    seen: set[tuple[str, float | None]] = set()
    in_returns = False
    for line in t.splitlines():
        if line.lstrip().startswith("#"):
            in_returns = "return" in line.lower()
            continue
        dated = _DATE.search(line)
        if not in_returns or dated is None or not re.search(r"insufficien", line, re.I):
            continue
        amounts = [v for c in _cells(line) if (v := _val(c)) is not None and v >= 100]
        key = (dated.group(1)[:10], amounts[0] if amounts else None)
        if key not in seen:
            seen.add(key)
            bounces.append({"date": key[0], "amount": key[1]})
    return {"kind": "bsa", "file": name, "bank": _bank_of(name, t),
            "limit": limit or None, "overdrawn_days": int(days) if days is not None else None,
            "peak": abs(peak) if peak is not None else None, "peak_date": peak_date,
            "utilisation_avg": util[0] if util else None,
            "utilisation_latest": util[-1] if len(util) > 1 else None,
            "insufficient_funds": bounces}


# --------------------------------------------------------------------------- bureau
_DPD = re.compile(r"(\d+)\s*-\s*\d+\s*DPD|(\d+)\+\s*DPD|\b(SUB|DBT|LSS|SMA)\b", re.I)


def cir_facts(name: str, text: str) -> dict[str, Any] | None:
    """A TransUnion CIBIL consumer report: the CreditVision score, any other score on
    it, the worst delinquency and how recent it is, overdue accounts."""
    t = text
    if "creditvision" not in t.lower() or "consumer" not in t.lower():
        return None
    who = re.search(r"CONSUMER NAME\s*:\s*([A-Z][A-Z .]+?)\s*(?:\||D\.O\.B|$)", t)
    words = who.group(1).split() if who else []
    half = len(words) // 2
    if half and words[:half] == words[half:]:          # the report prints the name twice
        words = words[:half]
    person = " ".join(words).title() if words else re.sub(
        r".*Cons_CIR_|\.\w+$", "", name).replace("_", " ")
    m = (re.search(r"CREDITVISION®?\s*Score\s*:\s*(\d{3})", t, re.I)
         or re.search(r"CREDITVISION[^|\n]*\|\s*300\s*900\s*(\d{3})", t, re.I))
    score = int(m.group(1)) if m else None
    if score is None:
        return None
    others = []
    for line in t.splitlines():
        if "creditvision" in line.lower() or "ranges from" not in line.lower():
            continue
        s = re.search(r"\|\s*300\s*900\s*(\d{3})\s*\|", line)
        label = _cells(line)[0].split("Ranges")[0].strip()
        if s and label:
            others.append({"name": label, "score": int(s.group(1))})
    worst = next(iter(_after(t, "Maximum delinquency across all trades")[1:2]
                      or _after(t, "Maximum delinquency across all trades")), "")
    if worst.startswith("("):            # the attribute code column came first
        rest = _after(t, "Maximum delinquency across all trades")
        worst = next((c for c in rest if not c.startswith("(")), "")
    since = next((c for c in _after(t, "Months since most recent delinquency (30+ DPD)")
                  if not c.startswith("(")), "")
    months = int(n) if (n := (re.match(r"\s*(\d+)", since) or [None, None])[1]) else None
    od = re.search(r"Overdue\s*\|\s*:\s*\|\s*(\d+)\s*\|", t)
    od_amt = re.search(r"Overdue\s*\|\s*:\s*\|\s*₹\s*([\d,]+)", t)
    return {"kind": "cir", "file": name, "person": person, "score": score,
            "other_scores": others, "worst_delinquency": worst or None,
            "months_since_30dpd": None if months == 999 else months,
            "overdue_accounts": int(od.group(1)) if od else None,
            "overdue_amount": float(od_amt.group(1).replace(",", "")) if od_amt else None}


def commercial_facts(name: str, text: str) -> dict[str, Any] | None:
    """A TransUnion CIBIL commercial report: the entity's CMR rank (1 best – 10 worst)."""
    if "commercial rank" not in text.lower() and "cmr-" not in text.lower():
        return None
    m = re.search(r"\bCMR[-\s]?(\d{1,2})\b", text)
    return {"kind": "commercial", "file": name,
            "cmr": int(m.group(1)) if m else None} if m else None


def legal_facts(name: str, text: str) -> dict[str, Any] | None:
    """A ScoreMe-style litigation report: cases against the firm, criminal cases."""
    if "total cases in latest 5 years" not in text.lower():
        return None
    return {"kind": "legal", "file": name,
            "cases_5y": _first_num(text, "Total cases in latest 5 years"),
            "respondent_5y": _first_num(text, "Total cases in latest 5 years as a Respondent"),
            "criminal": _first_num(text, "Total criminal cases in all years")}


# --------------------------------------------------------------------------- CMA
_CMA_ROWS = {"revenue": "Revenue", "ebitda": "EBITDA", "pat": "PAT", "tnw": "TNW",
             "interest": "Interest exp", "current_ratio": "Current ratio",
             "tol_tnw": "TOL/TNW", "td_ebitda": "TD/EBITDA", "dscr": "DSCR",
             "growth": "Growth", "ebitda_pct": "EBITDA%"}


def cma_facts(name: str, text: str) -> dict[str, Any] | None:
    """The CMA workbook's grid: every labelled row by year (₹ lakh), actuals and the
    provisional year only — projections are not facts."""
    years: list[str] = []
    for line in text.splitlines():
        cells = _cells(line)
        if cells and re.match(r"year\s*/\s*inr", cells[0], re.I):
            for c in cells[1:]:              # the first block only: stop at its end
                if not re.match(r"20\d\d", c):
                    break
                years.append(c)
            break
    if not years:
        return None
    found: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        cells = _cells(line)
        for j, c in enumerate(cells):
            key = next((k for k, lab in _CMA_ROWS.items() if c.strip().lower() == lab.lower()), None)
            if key is None or key in found:
                continue
            row: dict[str, float] = {}
            for k, y in enumerate(years):
                if "(p)" in y.lower():
                    continue
                v = _val(cells[j + 1 + k]) if j + 1 + k < len(cells) else None
                if v is not None and v != 0:
                    row[y] = v
            if row:
                found[key] = row
    if not found:
        return None
    return {"kind": "cma", "file": name, "unit": "₹ lakh", "years": [
        y for y in years if "(p)" not in y.lower()], "rows": found}


PARSERS = (bsa_facts, cir_facts, commercial_facts, legal_facts, cma_facts)


def facts_of(name: str, text: str) -> list[dict[str, Any]]:
    out = []
    for parse in PARSERS:
        try:
            if (f := parse(name, text)) is not None:
                out.append(f)
        except (ValueError, IndexError, AttributeError):
            continue                       # an odd layout is "not recognised", never a crash
    return out


# --------------------------------------------------------------------------- flags
def _flag(sev: str, area: str, title: str, detail: str, source: str,
          trigger: str | None = None) -> dict[str, Any]:
    f = {"severity": sev, "area": area, "title": title, "detail": detail, "source": source}
    if trigger:
        f["trigger"] = trigger
    return f


def _latest(row: dict[str, float] | None) -> tuple[str, float] | None:
    if not row:
        return None
    y = list(row)[-1]
    return y, row[y]


def flags_from(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Red (material), amber (watch) and info flags, each with its source file.
    ``trigger`` marks a fact that meets a rubric hard trigger outright."""
    out: list[dict[str, Any]] = []
    for f in facts:
        src = f["file"]
        if f["kind"] == "bsa":
            lim, peak, days = f.get("limit"), f.get("peak"), f.get("overdrawn_days")
            if days:
                over = f" — peak {lakh(peak)} vs {lakh(lim)} limit" if peak and lim else ""
                when = f" on {f['peak_date']}" if f.get("peak_date") else ""
                sev = "red" if days >= 10 or (peak and lim and peak > 1.05 * lim) else "amber"
                out.append(_flag(sev, "Banking", f"{f['bank']} CC/OD overdrawn beyond its limit",
                                 f"{days} day(s) over the limit{over}{when}.", src))
            util = f.get("utilisation_latest") or f.get("utilisation_avg")
            if util and util >= 0.9 and not days:
                out.append(_flag("amber", "Banking", f"{f['bank']} limit near fully used",
                                 f"Average utilisation {util:.0%}.", src))
            b = f.get("insufficient_funds") or []
            if b:
                amt = ", ".join(f"{lakh(x['amount'])} on {x['date']}" for x in b[:4])
                out.append(_flag("red" if len(b) >= 3 else "amber", "Banking",
                                 f"{len(b)} payment(s) returned for insufficient funds "
                                 f"({f['bank']})", amt + ".", src))
        elif f["kind"] == "cir":
            who, s = f["person"], f.get("score")
            if s is not None and s < 650:
                out.append(_flag("red", "Bureau", f"Promoter {who}: CIBIL score {s} (< 650)",
                                 "Meets the rubric's hard trigger.", src,
                                 trigger="consumer score < 650"))
            for o in f.get("other_scores") or []:
                if o["score"] < 650:
                    out.append(_flag("amber", "Bureau",
                                     f"Promoter {who}: {o['name']} {o['score']} (< 650)",
                                     f"CIBIL CreditVision is {s}. Whether this score counts as "
                                     "the rubric's consumer score is a desk ruling.", src))
            w, m = str(f.get("worst_delinquency") or ""), f.get("months_since_30dpd")
            d = _DPD.search(w)
            days = int(d.group(1) or d.group(2)) if d and (d.group(1) or d.group(2)) else None
            npa = bool(d and d.group(3) and d.group(3).upper() in ("SUB", "DBT", "LSS"))
            if npa or (days is not None and days >= 30):
                recent = m is not None and m <= 12
                sev = "red" if npa or (days and days >= 60 and recent) else "amber"
                od = (f"; {f['overdue_accounts']} account(s) overdue"
                      + (f" ({lakh(f['overdue_amount'])})" if f.get("overdue_amount") else "")
                      if f.get("overdue_accounts") else "")
                out.append(_flag(sev, "Bureau", f"Promoter {who}: delinquency up to {w}",
                                 (f"Most recent 30+ DPD {m} month(s) ago" if m is not None
                                  else "Recency not shown") + od + ".", src,
                                 trigger="sub-standard or loss account" if npa else None))
        elif f["kind"] == "commercial" and f.get("cmr") is not None:
            r = f["cmr"]
            if r >= 8:
                out.append(_flag("red", "Bureau", f"Entity CIBIL rank CMR-{r}",
                                 "CMR 8–10 meets the rubric's hard trigger.", src,
                                 trigger="commercial rank 8–10"))
            elif r >= 6:
                out.append(_flag("amber", "Bureau", f"Entity CIBIL rank CMR-{r}",
                                 "Medium-high risk band.", src))
        elif f["kind"] == "legal":
            if (f.get("criminal") or 0) > 0:
                out.append(_flag("red", "Legal", f"{int(f['criminal'])} criminal case(s) on record",
                                 "Litigation report.", src))
            if (f.get("respondent_5y") or 0) > 0:
                out.append(_flag("amber", "Legal",
                                 f"{int(f['respondent_5y'])} case(s) against the firm (5 years)",
                                 "Litigation report.", src))
        elif f["kind"] == "cma":
            rows = f["rows"]
            if (t := _latest(rows.get("tnw"))) and t[1] < 0:
                out.append(_flag("red", "Financials", f"Negative net worth ({t[0]})",
                                 f"TNW {t[1]:,.2f} lakh.", src, trigger="negative net worth"))
            if (t := _latest(rows.get("tol_tnw"))) and t[1] > 3:
                out.append(_flag("red" if t[1] > 4 else "amber", "Financials",
                                 f"High leverage: TOL/TNW {t[1]:.2f}× ({t[0]})",
                                 "Above 3× outside liabilities to net worth.", src))
            if (t := _latest(rows.get("dscr"))) and t[1] < 1.5:
                out.append(_flag("red" if t[1] < 1.2 else "amber", "Financials",
                                 f"DSCR {t[1]:.2f}× ({t[0]})", "Below the rubric's 1.5× comfort.", src))
            if (t := _latest(rows.get("current_ratio"))) and t[1] < 1.25:
                out.append(_flag("red" if t[1] < 1 else "amber", "Financials",
                                 f"Current ratio {t[1]:.2f}× ({t[0]})", "Thin working-capital cover.", src))
            if (t := _latest(rows.get("ebitda_pct"))) and 0 <= t[1] < 0.06:
                out.append(_flag("amber", "Financials", f"Thin EBITDA margin {t[1]:.1%} ({t[0]})",
                                 "Little buffer against cost or price moves.", src))
            for y, g in (rows.get("growth") or {}).items():
                if g < 0:
                    out.append(_flag("amber", "Financials", f"Revenue fell {abs(g):.1f}% in {y}",
                                     "A decline year in the track record.", src))
    order = {"red": 0, "amber": 1, "info": 2}
    return sorted(out, key=lambda x: order[x["severity"]])


def key_figures(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The CMA's rows the report tabulates (₹ lakh, ratios as ×)."""
    cma = next((f for f in facts if f["kind"] == "cma"), None)
    if not cma:
        return None
    keep = ("revenue", "growth", "ebitda", "ebitda_pct", "pat", "tnw", "current_ratio",
            "tol_tnw", "td_ebitda", "dscr")
    return {"source": cma["file"], "unit": cma["unit"], "years": cma["years"],
            "rows": {k: cma["rows"][k] for k in keep if k in cma["rows"]}}


# --------------------------------------------------------------------------- coverage
AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Financials", ("audited", "balance sheet", "bsheet", "p&l", "pandl", "profit", "provisional",
                    "itr", "financial statement", "cma", "income tax")),
    ("Banking", ("bsa", "bank statement", "statement", "current account", "cc ", "boi", "bob",
                 "kotak", "hdfc", "icici", "sbi", "axis")),
    ("Bureau", ("cibil", "cir", "crif", "experian", "equifax", "bureau", "w-6", "commercial report")),
    ("Legal", ("legal", "litigation")),
    ("GST & statutory", ("gst", "gstr", "epf", "tds", "statutory")),
    ("KYC & constitution", ("kyc", "pan", "aadhar", "aadhaar", "deed", "partnership",
                            "incorporation", "moa", "aoa", "registration", "udyam", "msme")),
    ("Existing debt", ("sanction", "debt profile", "loan", "outstanding", "soa")),
)
_SECTION_AREA = {"Banking & Debt": "Banking", "Financials": "Financials",
                 "Compliance & Bureau": "Bureau", "KYC & Constitutional": "KYC & constitution"}


def area_of(name: str, title: str | None, section: str | None) -> str:
    text = f" {name} {title or ''} ".lower()
    for area, words in AREAS:
        if any(w in text for w in words):
            return area
    return _SECTION_AREA.get(section or "", "Other")


def coverage(fates: list[dict[str, Any]], facts: list[dict[str, Any]],
             news_items: int, news_note: str | None) -> list[dict[str, Any]]:
    """Per evidence area: read / not read (with why) / no file — the panel that
    tells a reader how much of the file the grade actually stands on."""
    rows: dict[str, dict[str, Any]] = {a: {"area": a, "read": [], "unread": []}
                                       for a, _ in AREAS}
    for f in fates:
        a = area_of(f.get("name", ""), f.get("title"), f.get("section"))
        if a not in rows:
            continue
        if f.get("used"):
            rows[a]["read"].append(f["name"])
        else:
            rows[a]["unread"].append({"name": f["name"], "reason": f.get("reason", "")})
    parsed = {f["kind"] for f in facts}
    for r in rows.values():
        r["status"] = ("read" if r["read"] and not r["unread"] else
                       "partly read" if r["read"] else
                       "not read" if r["unread"] else "no file")
    if "bsa" in parsed:
        rows["Banking"]["checked_by_prism"] = True
    if parsed & {"cir", "commercial"}:
        rows["Bureau"]["checked_by_prism"] = True
    if "legal" in parsed:
        rows["Legal"]["checked_by_prism"] = True
    if "cma" in parsed:
        rows["Financials"]["checked_by_prism"] = True
    out = list(rows.values())
    out.append({"area": "News (PULSE)", "status": "read" if news_items else "none found",
                "read": [], "unread": [], "note": news_note or f"{news_items} headline(s)"})
    return out
