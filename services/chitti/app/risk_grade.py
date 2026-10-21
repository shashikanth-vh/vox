"""Company 360 risk grade on the AI host: a company's ATLAS footprint → RED / AMBER / GREEN.

PRISM's orchestrator reads the company's panorama from the Register AS the verified
caller (so RBAC holds — a role never gets a grade built from data it cannot see)
and posts it here with the 30-day news. Chitti adds what only this host has close
at hand — passages from the company's indexed documents in the local DocRAG,
retrieved with the 360 ask-box's preset questions — and runs the desk's rubric on
GLM with its own key.

The rubric is ``prompts/ATLAS_Client_Risk_Rating_Prompt.md``, Part B, verbatim —
edit that file to change how clients are graded. The model answers in JSON; the
rubric's arithmetic is then RE-APPLIED here — weights, the score, the band, the
hard-trigger override and the data-gap cap — so the colour on screen always follows
the written rules, whatever the model's own summary said. Every change the
enforcement makes is listed in ``adjustments``.

No model configured → 409, never a stub grade: a fake colour on a credit screen is
worse than none. Caching belongs to the caller (the orchestrator).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from evam_backend_core.errors import AppError, ConflictError
from evam_backend_core.logging import get_logger
from fastapi import APIRouter, Request
from openai import APIError, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_client_key
from app.config import Settings

log = get_logger("chitti.risk_grade")

PROMPT_FILE = Path(__file__).parent / "prompts" / "ATLAS_Client_Risk_Rating_Prompt.md"

# The rubric's pillars (Part B, STEP 2). The weights are enforced here, not trusted
# from the model.
PILLARS: list[tuple[str, str, int]] = [
    ("A", "Financial strength", 30),
    ("B", "Credit & banking conduct", 20),
    ("C", "Market validation", 15),
    ("D", "News & reputation", 10),
    ("E", "Documentation & transparency", 10),
    ("F", "Engagement & process health", 10),
    ("G", "Sector & structure", 5),
]
_WEIGHT = {k: w for k, _, w in PILLARS}
_NAME = {k: n for k, n, _ in PILLARS}

# The 360 ask-box's presets — the same questions the desk asks of a file.
DOC_QUESTIONS: list[tuple[str, str]] = [
    ("Key financials", "Key financials: revenue, EBITDA, PAT, net worth, total debt, "
     "finance cost, with the years they belong to"),
    ("Banking & CIBIL", "Banking and CIBIL: accounts, limits, CIBIL score, DPD, overdues, "
     "bounces, adverse remarks"),
    ("Promoters & shareholding", "Promoters, directors and shareholding pattern, "
     "including any pledge of shares"),
    ("Registrations", "Registrations: CIN, PAN, GST numbers and validity"),
    ("Compliance", "Compliance filings, GST returns, statutory dues and certificates"),
]

_OUTPUT_CONTRACT = """
=== OUTPUT FORMAT (overrides the numbered format above) ===
Answer with ONE JSON object and nothing else — no prose, no markdown fences. It
carries the same eight items as the numbered format, as data:
{
  "rating": "RED" | "AMBER" | "GREEN",
  "provisional": true | false,
  "score": <0-100 integer>,
  "confidence": "High" | "Medium" | "Low",
  "verdict": "<one line, at most 25 words>",
  "pillars": [
    {"key": "A", "score": <0-10>, "available": true | false,
     "evidence": "<the ATLAS fields and figures you used, or 'Not available'>"}
    ... one entry for each of A, B, C, D, E, F, G
  ],
  "hard_triggers": [
    {"trigger": "<the trigger as written in STEP 1>", "status": "Hit" | "Clear" | "Unknown",
     "evidence": "<why>"}
    ... one entry for each STEP 1 trigger
  ],
  "risks": ["<top risk>", "<second>", "<third>"],
  "mitigants": ["<top mitigant>", "<second>", "<third>"],
  "data_gaps": ["<Data Register item name to request>", ...],
  "move_up": "<the specific change that would lift it one band>",
  "move_down": "<the specific change that would drop it one band>",
  "action": "Proceed" | "Proceed with conditions" | "Hold" | "Decline",
  "conditions": ["<CP>", ...]
}
Set "available": false for a pillar whose inputs are "Not available" — for A
(Financial strength) and B (Credit & banking conduct) this drives the data-gap rule.
"""


def load_prompt(path: Path = PROMPT_FILE) -> str:
    """The rubric: Part B's fenced block, verbatim, plus the JSON output contract."""
    text = path.read_text(encoding="utf-8")
    part_b = text.split("## Part B", 1)[-1]
    m = re.search(r"```[a-z]*\n(.*?)```", part_b, re.DOTALL)
    if not m:
        raise ValueError(f"{path.name}: no fenced prompt block under '## Part B'")
    return m.group(1).strip() + "\n" + _OUTPUT_CONTRACT


# --------------------------------------------------------------------------- #
# Client data — the rubric's ten headings, filled from the panorama
# --------------------------------------------------------------------------- #
def _days_since(value: Any, today: date) -> int | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            d = date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return (today - d).days


def _clip(value: Any, n: int = 300) -> Any:
    if isinstance(value, str) and len(value) > n:
        return value[:n] + "…"
    return value


def _rows(rows: list[dict], keep: int = 25) -> str:
    if not rows:
        return "Not available"
    return json.dumps([{k: _clip(v) for k, v in r.items() if v not in (None, "", [])}
                       for r in rows[:keep]], ensure_ascii=False, default=str)


def facts_text(facts: dict[str, Any] | None) -> str:
    """PRISM's own reading of the fixed-format files (bank-statement analyses, bureau
    reports, the CMA grid) — put before the rubric's STEP 1 so the readings use these
    figures instead of paraphrasing 200k characters of statements."""
    if not facts or not (facts.get("flags") or facts.get("key_figures")
                         or facts.get("kinds_read") or facts.get("unread_files")):
        return ""
    out = ["=== VERIFIED FACTS (read by PRISM from the full files; use these figures as "
           "given, and cite them) ==="]
    for f in facts.get("flags") or []:
        out.append(f"- [{str(f.get('severity', '')).upper()}] {f.get('area')}: {f.get('title')}"
                   f" — {f.get('detail')} (FILE: {f.get('source')})")
    kf = facts.get("key_figures") or {}
    if kf.get("rows"):
        years = ", ".join(kf.get("years") or [])
        out.append(f"Key figures from {kf.get('source')} ({kf.get('unit')}; years {years}):")
        for k, row in kf["rows"].items():
            out.append(f"  {k}: " + ", ".join(f"{y} {v:,.2f}" for y, v in row.items()))
    kinds = facts.get("kinds_read") or {}
    if kinds:
        out.append("Kinds of document READ for this grade (they ARE on record — never call "
                   "one missing, empty or not provided; if a period or item is truly absent "
                   "from them, say exactly what they cover):")
        out += [f"  {k}: " + "; ".join(v[:6]) + (f" (+{len(v) - 6} more)" if len(v) > 6 else "")
                for k, v in kinds.items()]
    if facts.get("unread_files"):
        out.append("Files on the Data Register that were NOT read (not evidence — a trigger "
                   "cannot be Clear because one of these exists): "
                   + "; ".join(facts["unread_files"][:40]))
    return "\n".join(out)


def _records(rows: list[dict] | None, keep: int, clip: int = 1500) -> str:
    """Register records in full (the desk's own words): long fields to ``clip``."""
    if not rows:
        return "Not available"
    return json.dumps([{k: _clip(v, clip) for k, v in r.items() if v not in (None, "", [])}
                       for r in rows[:keep]], ensure_ascii=False, default=str)


def build_client_data(p: dict[str, Any], news: list[dict[str, Any]],
                      evidence: list[dict[str, Any]], today: date,
                      documents: list[dict[str, Any]] | None = None,
                      facts: dict[str, Any] | None = None,
                      records: dict[str, list[dict[str, Any]]] | None = None) -> str:
    a = p.get("anchor") or {}
    s = p.get("stats") or {}
    restricted = p.get("restricted") or []
    rec = records or {}

    def lines_with_age(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows or []:
            r = dict(r)
            age = _days_since(r.get("stage_updated_at"), today)
            if age is not None:
                r["days_in_stage"] = age
            out.append(r)
        return out

    docs = p.get("documents") or []
    by_section: dict[str, int] = {}
    for d in docs:
        by_section[d.get("section") or "Unfiled"] = by_section.get(d.get("section") or "Unfiled", 0) + 1

    ev_text = "Not available (no Data Register files in the graded sections)"
    if documents:
        # The Data Register files themselves, read by DocRAG — the primary evidence.
        ev_text = "\n\n".join(
            f"--- FILE: {d.get('name')} · Data Register section: {d.get('section') or '?'}"
            f"{' · slot: ' + d['title'] if d.get('title') else ''} · read by: "
            f"{', '.join(d.get('engines') or []) or 'text'} ---\n{d.get('text', '')}"
            for d in documents)
    elif evidence:
        ev_text = "\n".join(
            f"- [{e['question']}] {e['doc']}"
            f"{(' p.' + ','.join(map(str, e['pages']))) if e.get('pages') else ''}: {e['text']}"
            for e in evidence)

    news_text = "Not available"
    if news:
        label = {"GREEN": "GOOD", "AMBER": "BAD", "RED": "UGLY", "BLUE": "POLICY"}
        news_text = "\n".join(
            f"- {'[about ' + n['about'] + '] ' if n.get('about') else ''}"
            f"{_clip(n.get('headline'), 200)} · {n.get('source') or '?'} · "
            f"{n.get('when') or '?'} · {label.get(str(n.get('severity') or '').upper(), 'UNTAGGED')}"
            for n in news[:100])

    last_touch = s.get("last_touch")
    contacts = p.get("contacts") or []
    identity = {"legal_name": a.get("name"), "cin": a.get("cin"), "sector": a.get("sector"),
                "sub_sector": a.get("sub_sector"), "state": a.get("state"),
                "website": a.get("domain"), "about": _clip(a.get("about"), 600),
                "matched_by": a.get("matched_by")}
    return "\n".join([
        "=== CLIENT DATA (from ATLAS) ===",
        f"1. Identity: {json.dumps(identity, ensure_ascii=False)}",
        f"   Prospect record: {_rows([p['prospect']]) if p.get('prospect') else 'Not available'}",
        f"2. Exposure: headline stats {json.dumps(s, default=str)}",
        f"   Deals: {_rows(p.get('deals') or [])}",
        f"   Lending lines: {_rows(lines_with_age(p.get('lending') or []))}",
        f"   Platform Deals (per-lender status): {_rows(lines_with_age(p.get('syndication') or []))}",
        f"   Asset Monetisation: {_rows(lines_with_age(p.get('asset_monetisation') or []))}",
        "3. Financials and 4. Banking & bureau, 5. Promoters & shareholding — the company's "
        "documents (cite the FILE name in evidence; take figures only from here, never "
        "invent them; a CMA/projection file is a projection, not an actual):",
        ev_text,
        f"   Financial statements recorded on the Register (current versions): "
        f"{_records(rec.get('financials'), 60, 400)}",
        f"6. Documentation: {len(docs)} file(s) on the Data Register, by section "
        f"{json.dumps(by_section)}; files: {_rows(docs, keep=40)}",
        "7. Project & technical: use the documents and deal fields above if present.",
        f"8. Engagement: last touch {last_touch or 'Not available'}"
        f" ({_days_since(last_touch, today)} days ago)" if last_touch else
        "8. Engagement: last touch Not available",
        f"   Key contacts on record: {'Y' if contacts else 'N'} {_rows(contacts)}",
        f"   Leads: {_rows(p.get('leads') or [])}",
        # The full timeline when PRISM sent it (every interaction with its notes), else
        # the panorama's summary of the latest.
        f"   Interactions & VOX, full notes (most recent first): "
        f"{_records(rec.get('interactions'), 120)}" if rec.get("interactions") else
        f"   Interactions & VOX (most recent first): {_rows(p.get('interactions') or [], keep=15)}",
        f"   Monitoring records (covenants, submissions, behaviour): "
        f"{_records(rec.get('monitoring'), 60, 400)}",
        f"   Field brief: {_clip(p.get('brief_full') or p.get('brief'), 1500) or 'Not available'}",
        f"9. News (PULSE radar, last 12 months — the firm, and its key people marked "
        f"[about <name>]): \n{news_text}",
        f"   Intelligence on the Register (the news monitor's SEBI / IBBI / NSE / adverse "
        f"findings and the desk's own signals, not dismissed; newest first): "
        f"{_records(rec.get('intelligence'), 120, 600)}",
        f"10. Today's date: {today.isoformat()}",
        facts_text(facts),
        (f"NOTE: these sections are not visible to the requesting role and are absent: "
         f"{', '.join(restricted)}. Treat them as Not available.") if restricted else "",
    ])


# --------------------------------------------------------------------------- #
# The model's answer → the rubric, enforced
# --------------------------------------------------------------------------- #
def parse_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in the reply (fences and stray prose tolerated)."""
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    if not isinstance(obj, dict):
        raise ValueError("the reply's JSON is not an object")
    return obj


# A score this close to 45 or 70 is flagged: the colour is not firm there.
BORDER_MARGIN = 3


def band_for(score: float) -> str:
    return "GREEN" if score >= 70 else "AMBER" if score >= 45 else "RED"


# Hard triggers that name a numeric threshold. A model will sometimes fire one
# of these on evidence that disproves it — seen in the field: "CIBIL consumer
# score <650 for promoters" reported as Hit with evidence quoting scores of
# 742/752/806/861, where the real finding was a delinquency flag belonging to
# pillar B. A trigger that forces RED on its own must agree with its evidence.
#
# The contract asks for each trigger "as written in STEP 1", and STEP 1's first line
# bundles the numeric clauses with non-numeric ones ("Wilful defaulter / NPA / SMA-2 /
# write-off / settlement, or CIBIL (commercial) rank 8–10 or consumer score < 650"). So a
# Hit is only set aside when EVERY numeric clause the trigger names is disproved by the
# figures it cites, at least one figure was cited, and the evidence names none of the
# non-numeric conditions — a real SMA-2 must never be cancelled by a promoter's 780.
# Names are matched with spacing ignored: the model writes "<650" as often as "< 650".
#   clause:   how the trigger names it (regex on the lowercased name)
#   figures:  pulls that clause's figures out of the evidence
#   holds:    given those figures, is the clause met?
def _bureau_scores(evidence: str) -> list[int]:
    """CIBIL consumer scores cited in the evidence: 3-digit figures in 300–900 that follow
    "CIBIL", "bureau" or "score" within the same clause. Amounts (₹/Rs/INR, lakh, crore)
    and digits inside larger numbers are not scores. 650 itself is dropped: it is the
    rule's threshold, quoted far more often than scored."""
    out: set[int] = set()
    for m in re.finditer(r"cibil|bureau|scores?", evidence, re.I):
        clause = re.split(r"[.;\n]", evidence[m.end():m.end() + 80])[0]
        # A comma or point joined to a digit is part of a larger number (1,234,000; 7.75).
        for n in re.finditer(r"(₹|\brs\.?|\binr)?\s*(?<![\d.])(?<!\d,)(\d{3})"
                             r"(?![\d.]|,\d|\s*(?:lakh|crore|cr\b))", clause, re.I):
            if not n.group(1):
                out.add(int(n.group(2)))
    return sorted(n for n in out if 300 <= n <= 900 and n != 650)


_NUMERIC_CLAUSES: list[tuple[str, re.Pattern[str], Any, Any]] = [
    # Consumer bureau score below 650 (CIBIL consumer scores run 300–900).
    ("consumer score < 650", re.compile(r"score\s*(?:<|below|under|less than)\s*650"),
     _bureau_scores, lambda ns: any(n < 650 for n in ns)),
    # Commercial rank 8–10 (CMR scale is 1–10).
    ("commercial rank 8–10", re.compile(r"(?:rank|cmr)\s*(?:of\s*)?8\s*(?:-|–|to)\s*10"),
     lambda ev: [int(n) for n in re.findall(r"\bCMR[-\s]?(\d{1,2})\b", ev, re.I)],
     lambda ns: any(8 <= n <= 10 for n in ns)),
]
# STEP 1's non-numeric bureau conditions: any one named in the evidence keeps the Hit.
_NON_NUMERIC = re.compile(r"wilful|\bnpa\b|sma[-\s]?2|write[-\s]?off|written[-\s]?off|"
                          r"settle(?:d|ment)|default", re.I)
_NEGATED = re.compile(r"\b(no|not|nil|none|without|zero|neither|nor)\b[^.;:]{0,25}$", re.I)


def _cites_condition(evidence: str) -> bool:
    """Whether the evidence NAMES a non-numeric STEP 1 condition as present — "No
    NPA/SMA-2/write-off" names three and asserts none (live, 6 Oct)."""
    return any(not _NEGATED.search(evidence[max(0, m.start() - 40):m.start()])
               for m in _NON_NUMERIC.finditer(evidence))


def check_numeric_triggers(triggers: list[dict[str, Any]]) -> list[str]:
    """Downgrade a Hit to Unknown where the evidence disproves every numeric clause.

    Returns the notes to add to ``adjustments``; mutates ``triggers`` in place.
    Evidence that cites no usable figure is left alone — absent numbers mean
    "cannot verify here", not "disproved", and the aggregate's consensus rule
    is the other guard.
    """
    notes: list[str] = []
    for t in triggers:
        if t.get("status") != "Hit":
            continue
        name = str(t.get("trigger") or "").lower()
        evidence = str(t.get("evidence") or "")
        named = [c for c in _NUMERIC_CLAUSES if c[1].search(name)]
        if not named or _cites_condition(evidence):
            continue
        cited: list[int] = []
        supported = False
        for _label, _name, figures, holds in named:
            ns = figures(evidence)
            cited += ns
            supported = supported or (bool(ns) and holds(ns))
        if not cited or supported:
            continue
        shown = ", ".join(str(n) for n in cited[:6])
        t["status"] = "Unknown"
        t["evidence"] = (f"{evidence} [NOT HONOURED: the figures cited ({shown}) do not meet "
                         f"this threshold; treated as Unknown, not a RED override]")[:400]
        notes.append(f'Hard trigger "{str(t.get("trigger"))[:60]}" was reported as Hit but its '
                     f"own evidence cites {shown}, which does not meet the threshold — not "
                     "honoured as a RED override.")
    return notes


def enforce(raw: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the rubric's arithmetic and rules to the model's pillar scores."""
    adjustments: list[str] = []
    given = {str(x.get("key", "")).upper()[:1]: x for x in raw.get("pillars") or []
             if isinstance(x, dict)}
    pillars: list[dict[str, Any]] = []
    for key, name, weight in PILLARS:
        x = given.get(key) or {}
        try:
            sc = float(x.get("score", 0))
        except (TypeError, ValueError):
            sc = 0.0
        if not 0 <= sc <= 10:
            adjustments.append(f"Pillar {key} score {sc:g} clamped to 0–10.")
            sc = min(10.0, max(0.0, sc))
        if key not in given:
            adjustments.append(f"Pillar {key} ({name}) was not scored — treated as no data.")
        available = bool(x.get("available", key in given))
        pillars.append({"key": key, "name": name, "weight": weight, "score": round(sc, 1),
                        "weighted": round(sc * weight / 10, 1), "available": available,
                        "evidence": str(x.get("evidence") or "Not available")[:600]})
    # A pillar with no data is left OUT, not scored 0: scoring missing financials as 0
    # caps every document-less client at 50 → RED, contradicting the rubric's own
    # worked example (no financials, no banking, clean news → AMBER – PROVISIONAL).
    # The data-gap rule below is what penalises the gap: never GREEN, provisional.
    counted = [p for p in pillars if p["available"]]
    for p in pillars:
        if not p["available"]:
            p["weighted"] = 0.0
    weight_counted = sum(p["weight"] for p in counted)
    score = (round(sum(p["weighted"] for p in counted) * 100 / weight_counted)
             if weight_counted else 0)
    if weight_counted < 100:
        left_out = ", ".join(p["key"] for p in pillars if not p["available"])
        adjustments.append(f"Scored over the pillars with data ({weight_counted}% of the "
                           f"weight); left out for lack of data: {left_out}.")
    try:
        model_score = round(float(raw["score"]))
    except (KeyError, TypeError, ValueError):
        model_score = None
    if model_score is not None and model_score != score:
        adjustments.append(f"Score recomputed from the pillars: {score} (the model said {model_score}).")

    triggers = [{"trigger": str(t.get("trigger") or "")[:300],
                 "status": str(t.get("status") or "Unknown").title()
                 if str(t.get("status") or "").title() in ("Hit", "Clear", "Unknown") else "Unknown",
                 "evidence": str(t.get("evidence") or "")[:400]}
                for t in raw.get("hard_triggers") or [] if isinstance(t, dict)]
    adjustments.extend(check_numeric_triggers(triggers))
    hits = [t for t in triggers if t["status"] == "Hit"]

    rating = band_for(score)
    provisional = False
    fin_missing = not pillars[0]["available"]
    bank_missing = not pillars[1]["available"]
    if hits:
        rating = "RED"
        # A RED from a trigger is still provisional when the score behind it
        # rests on a partial file: the colour is firm, the reasoning is not.
        if fin_missing or bank_missing:
            provisional = True
    elif fin_missing or bank_missing:
        # The data-gap rule is a CAP (never GREEN without A and B), not a lift: a
        # score under 45 stays RED, and is provisional for the same missing data.
        provisional = True
        if rating == "GREEN":
            missing = " and ".join(n for n, m in (("Financials", fin_missing),
                                                   ("Banking", bank_missing)) if m)
            adjustments.append(f"Capped at AMBER: {missing} not available (data-gap rule).")
            rating = "AMBER"
    model_rating = (re.findall(r"RED|AMBER|GREEN", str(raw.get("rating") or "").upper()) or [""])[0]
    if model_rating and model_rating != rating and not any(
            a.startswith("Capped") for a in adjustments):
        adjustments.append(f"Band set by the rubric: {rating} (the model said {model_rating}).")

    def strs(key: str, n: int = 8) -> list[str]:
        return [str(v)[:400] for v in (raw.get(key) or []) if str(v).strip()][:n]

    conf = str(raw.get("confidence") or "Low").title()
    action = str(raw.get("action") or "Hold")
    return {
        "rating": rating, "provisional": provisional,
        "label": f"{rating} – PROVISIONAL" if provisional else rating,
        "score": score,
        "confidence": conf if conf in ("High", "Medium", "Low") else "Low",
        "verdict": str(raw.get("verdict") or "")[:300],
        "pillars": pillars, "hard_triggers": triggers,
        "risks": strs("risks", 3), "mitigants": strs("mitigants", 3),
        "data_gaps": strs("data_gaps", 12),
        "move_up": str(raw.get("move_up") or "")[:400],
        "move_down": str(raw.get("move_down") or "")[:400],
        "action": action if action in ("Proceed", "Proceed with conditions", "Hold",
                                       "Decline") else "Hold",
        "conditions": strs("conditions", 10),
        "adjustments": adjustments,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


# What counts as evidence for the two pillars the data-gap rule is about. A sanction
# letter or a CMA projection sits in these sections but says nothing about actual
# results or account conduct, so it does not make the pillar "have data".
_EVIDENCE = {
    "A": ("Financials", ("audited", "balance sheet", "financial statement", "provisional",
                         "itr", "profit", "p&l", "annual report")),
    "B": ("Banking", ("bank statement", "current account", "cibil", "bureau", "crif",
                      "equifax", "experian", "scoreme", "score me", "repayment",
                      "overdraft", "cash credit", "bsa", "statement analysis",
                      "account statement")),
}
_NOT_EVIDENCE = ("cma", "projection", "sanction")


def evidence_file(key: str, documents: list[dict[str, Any]]) -> str | None:
    """The first file read that is real evidence for pillar A or B, or None."""
    _label, words = _EVIDENCE[key]
    for d in documents:
        text = f"{d.get('title') or ''} {d.get('name') or ''}".lower()
        if any(w in text for w in words) and not any(w in text for w in _NOT_EVIDENCE):
            return str(d.get("name") or "a file")
    return None


def structural_gaps(p: dict[str, Any]) -> dict[str, str]:
    """Pillars the panorama holds NOTHING for, so they cannot have data whatever a
    reading says. C is the rubric's lender outcomes and Asset Monetisation; a reading
    that scores it from purchase orders is scoring D's evidence twice. F is the
    engagement record. Only ever removes data — the readings decide the rest."""
    gaps: dict[str, str] = {}
    if not (p.get("syndication") or p.get("asset_monetisation")):
        gaps["C"] = "no Platform Deals lender outcomes or Asset Monetisation in ATLAS"
    if not ((p.get("stats") or {}).get("last_touch") or p.get("interactions")
            or p.get("contacts")):
        gaps["F"] = "no engagement record (last touch, contacts, interactions) in ATLAS"
    return gaps


# A hard trigger PRISM verified in a document, by the STEP 1 line it belongs to.
_TRIGGER_LINE = {"consumer score < 650": "wilful", "commercial rank 8–10": "wilful",
                 "sub-standard or loss account": "wilful",
                 "negative net worth": "negative net worth"}


def apply_verified(grade: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, Any]:
    """Code over model, both ways a document can settle a trigger:

    * a trigger PRISM verified in a file (a CMR-9, a CIBIL 590, negative net worth) is
      a Hit whatever the readings said — the rubric says any hard trigger is RED;
    * a trigger the readings marked Clear on a file that was NOT read (only its name
      was listed) is Unknown: a file name is not evidence."""
    facts = facts or {}
    triggers = grade.get("hard_triggers") or []
    notes: list[str] = []
    for v in facts.get("triggers") or []:
        word = _TRIGGER_LINE.get(str(v.get("clause")), "")
        t = next((t for t in triggers if word and word in str(t.get("trigger", "")).lower()), None)
        if t is None:
            t = {"trigger": str(v.get("clause")), "status": "Unknown", "evidence": ""}
            triggers.append(t)
        if t["status"] != "Hit":
            t["status"] = "Hit"
            t["evidence"] = f"Verified by PRISM in the file: {v.get('evidence')}"[:400]
            notes.append(f"Hard trigger \"{v.get('clause')}\" verified by PRISM in a document "
                         f"— {v.get('evidence')}"[:300])
    # The rubric's "consumer score < 650" is the CIBIL score (CreditVision). When PRISM
    # read every promoter's, and none is below 650, a Hit resting on another score
    # (Grameen, seen live on 6 Oct) or on nothing at all is not the rubric's trigger.
    cibil = [s for s in facts.get("bureau_scores") or [] if isinstance(s.get("score"), int)]
    if cibil and all(s["score"] >= 650 for s in cibil):
        shown = ", ".join(f"{s.get('person')} {s['score']}" for s in cibil[:6])
        for t in triggers:
            name = str(t.get("trigger") or "").lower()
            if (t.get("status") == "Hit"
                    and _NUMERIC_CLAUSES[0][1].search(name)
                    and not _cites_condition(str(t.get("evidence") or ""))
                    and not any(str(v.get("clause")) == "consumer score < 650"
                                for v in facts.get("triggers") or [])):
                t["status"] = "Clear"
                t["evidence"] = (f"Verified by PRISM: promoters' CIBIL scores {shown} — none "
                                 "below 650. Other bureau scores below 650 are flagged for a "
                                 "desk ruling, not this trigger.")[:400]
                notes.append(f"\"{name[:60]}\" was marked Hit, but every promoter's CIBIL score "
                             f"is 650 or above ({shown}) — not the rubric's trigger.")
    unread = [u for u in facts.get("unread_files") or [] if len(u) > 4]
    stems = {u: u.rsplit(".", 1)[0].lower() for u in unread}
    for t in triggers:
        if t.get("status") != "Clear":
            continue
        ev = str(t.get("evidence") or "").lower()
        cited = [u for u, stem in stems.items() if stem and stem in ev]
        backed = [r for r in facts.get("read_files") or []
                  if (st := r.rsplit(" / ", 1)[-1].rsplit(".", 1)[0].lower()) and st in ev]
        if cited and not backed:
            t["status"] = "Unknown"
            t["evidence"] = (f"{t.get('evidence')} [UNVERIFIED: rests on "
                             f"{', '.join(cited[:3])}, which was not read]")[:400]
            notes.append(f"\"{str(t.get('trigger'))[:60]}\" was marked Clear on "
                         f"{', '.join(cited[:3])}, a file that was not read — shown as Unknown.")
    if not notes:
        return grade
    grade["hard_triggers"] = triggers
    grade["adjustments"] = [*grade.get("adjustments", []), *notes]
    # Re-band from the score and the triggers as they now stand (the rubric's rules,
    # as enforce applies them): any Hit → RED; else the score's band, capped at AMBER
    # and provisional without Financials or Banking.
    pillars = grade.get("pillars") or []
    gap = any(p.get("key") in ("A", "B") and not p.get("available") for p in pillars)
    before = grade.get("rating")
    if any(t["status"] == "Hit" for t in triggers):
        rating = "RED"
    else:
        rating = band_for(float(grade.get("score") or 0))
        if gap and rating == "GREEN":
            rating = "AMBER"
    grade["rating"], grade["provisional"] = rating, gap
    grade["borderline"] = (rating != "RED" or not any(t["status"] == "Hit" for t in triggers)) \
        and any(abs(float(grade.get("score") or 0) - line) <= BORDER_MARGIN for line in (45, 70))
    if rating != before:
        grade["adjustments"].append(f"Band set to {rating} by the verified triggers "
                                    f"(the score is {grade.get('score')}; it was {before}).")
    grade["label"] = f"{rating} – PROVISIONAL" if gap else rating
    return grade


def aggregate(raws: list[dict[str, Any]],
              documents: list[dict[str, Any]] | None = None,
              gaps: dict[str, str] | None = None) -> dict[str, Any]:
    """Several independent readings → one grade, pillar by pillar.

    Each pillar takes the MEDIAN of the scores from the readings that found data for
    it (a "no data" 0 never drags it down), with the evidence of the reading closest
    to that median; for C–G a pillar has data when most readings say so. When the
    caller sent the Data Register files, whether A (Financials) and B (Banking) have
    data follows the KIND of file read — the same files always give the same
    PROVISIONAL answer, and the reason is named. A hard trigger is Hit when most
    readings mark it Hit. The rubric is then applied once to that consensus. The
    words (verdict, risks, gaps) come from the reading whose own total is the median.
    A pillar named in ``gaps`` (see structural_gaps) has no data in every reading, so the
    per-run scores and their spread describe the same grade as the consensus."""
    decided: list[str] = []
    for key, reason in (gaps or {}).items():
        if any(isinstance(x, dict) and str(x.get("key", "")).upper()[:1] == key
               and x.get("available", True) for r in raws for x in r.get("pillars") or []):
            decided.append(f"Pillar {key} ({_NAME[key]}) counted as without data: {reason}.")
        raws = [{**r, "pillars": [{**x, "available": False}
                                  if isinstance(x, dict)
                                  and str(x.get("key", "")).upper()[:1] == key else x
                                  for x in r.get("pillars") or []]} for r in raws]
    graded = [enforce(r) for r in raws]
    base_i = sorted(range(len(raws)), key=lambda i: graded[i]["score"])[(len(raws) - 1) // 2]
    consensus = dict(raws[base_i])
    pillars = []
    for key, name, _w in PILLARS:
        rows: list[dict[str, Any]] = []
        for r in raws:
            for x in r.get("pillars") or []:
                if isinstance(x, dict) and str(x.get("key", "")).upper()[:1] == key:
                    rows.append(x)
                    break
        if not rows:
            continue
        scored = [x for x in rows if x.get("available", True)]
        if documents is not None and key in _EVIDENCE:
            label = _EVIDENCE[key][0]
            source = evidence_file(key, documents)
            if source is None:
                available = False
                if scored:
                    decided.append(f"Pillar {key} ({name}) counted as without data: no "
                                   f"{label.lower()} evidence was read (audited or provisional "
                                   f"statements / bank statements or a bureau report).")
            elif not scored:
                available = False
                decided.append(f"Pillar {key} ({name}): {source} was read but no reading "
                               f"found usable figures in it.")
            else:
                available = True
        else:
            available = len(scored) * 2 > len(rows)
        pool = scored if (available and scored) else rows
        scores = []
        for x in pool:
            try:
                scores.append(min(10.0, max(0.0, float(x.get("score", 0)))))
            except (TypeError, ValueError):
                scores.append(0.0)
        med = _median(scores)
        closest = pool[min(range(len(pool)), key=lambda i: abs(scores[i] - med))]
        pillars.append({"key": key, "score": med, "available": available,
                        "evidence": closest.get("evidence")})
    consensus["pillars"] = pillars
    base_triggers = [dict(t) for t in (raws[base_i].get("hard_triggers") or [])
                     if isinstance(t, dict)]
    # The STEP 1 triggers come back in the rubric's order, so they line up by position.
    for idx, t in enumerate(base_triggers):
        votes = 0
        for r in raws:
            others = [x for x in (r.get("hard_triggers") or []) if isinstance(x, dict)]
            if idx < len(others) and str(others[idx].get("status", "")).title() == "Hit":
                votes += 1
        if votes * 2 > len(raws):
            t["status"] = "Hit"
        elif str(t.get("status", "")).title() == "Hit":
            t["status"] = "Unknown"         # one reading's Hit that the others didn't share
    consensus["hard_triggers"] = base_triggers
    consensus.pop("score", None)          # the consensus score is the rubric's, not a model's
    grade = enforce(consensus)
    grade["adjustments"] = [*decided, *grade["adjustments"]]

    run_scores = [g["score"] for g in graded]
    spread = max(run_scores) - min(run_scores) if run_scores else 0
    coverage = sum(p["weight"] for p in grade["pillars"] if p["available"])
    agreement = max(0, 100 - 5 * spread)
    pct = round(0.6 * coverage + 0.4 * agreement)
    grade.update(
        runs=[{"label": g["label"], "score": g["score"]} for g in graded],
        confidence_pct=pct,
        confidence="High" if pct >= 75 else "Medium" if pct >= 50 else "Low",
        confidence_detail={"data_coverage": coverage, "agreement": agreement,
                           "spread": spread, "readings": len(graded),
                           "model_said": graded[base_i]["confidence"]},
        borderline=(not any(t["status"] == "Hit" for t in grade["hard_triggers"])
                    and any(abs(grade["score"] - line) <= BORDER_MARGIN for line in (45, 70))))
    if len({g["rating"] for g in graded}) > 1:
        grade["adjustments"] = [*grade["adjustments"],
                                f"The {len(graded)} readings disagreed on the band "
                                f"({', '.join(map(str, run_scores))}); each pillar's median is used."]
    return grade


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #
class NewsIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headline: str = Field(max_length=500)
    source: str | None = Field(default=None, max_length=200)
    when: str | None = Field(default=None, max_length=60)
    severity: str | None = Field(default=None, max_length=10)
    # A key person's name when the headline is about them, not the firm.
    about: str | None = Field(default=None, max_length=200)


class DocumentIn(BaseModel):
    """One Data Register file, already read (text only — files never travel)."""
    model_config = ConfigDict(extra="ignore")
    name: str = Field(max_length=300)
    section: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=300)
    engines: list[str] = Field(default_factory=list, max_length=8)
    text: str = Field(max_length=60_000)


class RiskGradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=300)
    # The Register's panorama for the company, exactly as the caller's role sees it.
    panorama: dict[str, Any]
    news: list[NewsIn] = Field(default_factory=list, max_length=100)
    # The company's Data Register files, read by the caller's DocRAG (CAM's path).
    documents: list[DocumentIn] = Field(default_factory=list, max_length=150)
    # The Register's records beyond the panorama, read as the caller by PRISM:
    # interactions (full notes), intelligence, financials, monitoring.
    records: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # What PRISM read itself from those files: flags, key figures, verified triggers,
    # and the listed files that were not read (see apply_verified).
    facts: dict[str, Any] = Field(default_factory=dict)


class GradeFailedError(AppError):
    """The model failed, or its answer could not be read as the rubric's JSON."""

    status_code = 502
    error_type = "grade_failed"
    title = "Grading failed"


def build_risk_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/v1")
    system = load_prompt()
    prompt_version = hashlib.sha256(system.encode()).hexdigest()[:12]
    model = settings.risk_grade_model or settings.answer_model
    docrag_url = settings.docrag_url.rstrip("/")
    client: AsyncOpenAI | None = None
    if settings.llm_base_url and settings.llm_api_key and model:
        client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                             timeout=settings.risk_grade_timeout_seconds, max_retries=1)

    async def evidence(company: str, tenant: str) -> tuple[list[dict], str | None]:
        """Passages from the company's indexed files — the ask-box's scope rule
        (DocRAG names bridge uploads "<Company> — <file>")."""
        if not docrag_url:
            return [], "document AI not configured"
        headers = {"X-API-Key": settings.docrag_api_key, "X-Tenant": tenant}
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                lr = await http.get(f"{docrag_url}/v1/documents", headers=headers)
                if lr.status_code >= 300:
                    return [], f"document AI refused the listing (HTTP {lr.status_code})"
                prefix = f"{company.lower()} — "
                ids = [str(d.get("id")) for d in (lr.json() or {}).get("items", [])
                       if str(d.get("name") or d.get("filename") or "").lower()
                       .startswith(prefix)][:50]
                if not ids:
                    return [], "no indexed documents for this company"
                seen: set[str] = set()
                out: list[dict] = []
                budget = 24_000
                for label, q in DOC_QUESTIONS:
                    qr = await http.post(f"{docrag_url}/v1/query", headers=headers,
                                         json={"query": q, "mode": "extractive", "top_k": 4,
                                               "doc_ids": ids})
                    if qr.status_code >= 300:
                        continue
                    for row in (qr.json() or {}).get("results", []):
                        c = row.get("chunk") or {}
                        text = str(c.get("text") or "").strip()
                        cid = str(c.get("chunk_id") or c.get("id")
                                  or f"{c.get('doc_id')}:{text[:40]}")
                        if not text or cid in seen or budget <= 0:
                            continue
                        seen.add(cid)
                        text = text[:min(1500, budget)]
                        budget -= len(text)
                        out.append({"question": label, "doc": c.get("doc") or "document",
                                    "pages": c.get("pages") or [], "text": text})
            return out, None if out else "no matching passages"
        except httpx.HTTPError as exc:
            log.warning("risk_grade_docrag_unreachable", extra={"error": str(exc)})
            return [], f"document AI unreachable ({exc.__class__.__name__})"

    async def complete(messages: list[dict[str, str]], usage: dict[str, int]) -> str:
        assert client is not None
        try:
            # temperature 0: a regrade of unchanged data should not flip a band.
            r = await client.chat.completions.create(
                model=model, messages=cast(list[ChatCompletionMessageParam], messages),
                temperature=0,
                max_tokens=settings.llm_max_completion_tokens)
        except APIError as exc:
            raise GradeFailedError(f"the grading model failed ({exc.__class__.__name__}: "
                                   f"{str(exc)[:200]})") from exc
        if r.usage:
            usage["input_tokens"] += r.usage.prompt_tokens or 0
            usage["output_tokens"] += r.usage.completion_tokens or 0
        usage["calls"] += 1
        return (r.choices[0].message.content or "") if r.choices else ""

    @router.post("/risk-grade", tags=["Company 360"],
                 summary="Grade a company RED / AMBER / GREEN with the ATLAS client rubric")
    async def risk_grade(payload: RiskGradeIn, request: Request) -> dict[str, Any]:
        require_client_key(request, settings)
        if client is None:
            raise ConflictError("No grading model configured: set CHITTI_LLM_BASE_URL, "
                                "CHITTI_LLM_API_KEY and CHITTI_RISK_GRADE_MODEL.")
        p = payload.panorama
        tenant = request.headers.get("X-Tenant", settings.register_tenant)
        company = (p.get("anchor") or {}).get("name") or payload.company
        started = time.monotonic()
        documents = [d.model_dump() for d in payload.documents]
        # The Data Register files are the evidence; the index is a fallback for a
        # caller that sends none (e.g. a company with no client master yet).
        ev, ev_note = ([], None) if documents else await evidence(company, tenant)
        news = [n.model_dump() for n in payload.news]
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": build_client_data(
                        p, news, ev, datetime.now(UTC).date(), documents, payload.facts,
                        payload.records)}]
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

        async def one_run() -> dict[str, Any]:
            turns = list(messages)
            reply = await complete(turns, usage)
            try:
                raw = parse_json_object(reply)
            except ValueError:
                turns += [{"role": "assistant", "content": reply},
                          {"role": "user", "content": "Return ONLY the JSON object described "
                           "in the output format — no other text."}]
                reply = await complete(turns, usage)
                try:
                    raw = parse_json_object(reply)
                except ValueError as exc:
                    raise GradeFailedError(
                        f"the model did not return the rubric's JSON ({exc})") from exc
            return raw

        # Independent readings, combined pillar by pillar (see aggregate). A failed
        # reading is tolerated while one succeeds.
        outcomes = await asyncio.gather(*(one_run() for _ in range(settings.risk_grade_runs)),
                                        return_exceptions=True)
        raws = [g for g in outcomes if isinstance(g, dict)]
        if not raws:
            first = next(e for e in outcomes if isinstance(e, BaseException))
            if isinstance(first, AppError):
                raise first
            raise GradeFailedError(f"grading failed ({first.__class__.__name__})") from first
        grade = apply_verified(
            aggregate(raws, documents if documents else None, structural_gaps(p)),
            payload.facts)
        seconds = round(time.monotonic() - started, 1)
        log.info("risk_grade", extra={"company": company, "rating": grade["label"],
                                      "score": grade["score"], "model": model,
                                      "seconds": seconds, "evidence_passages": len(ev),
                                      "documents": len(documents),
                                      "news": len(news), **usage})
        s = p.get("stats") or {}
        return {
            **grade, "company": company,
            "engine": f"chitti:{model}", "prompt": PROMPT_FILE.name,
            "prompt_version": prompt_version,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "inputs": {
                "register_sections_hidden": p.get("restricted") or [],
                "lending_lines": len(p.get("lending") or []),
                "platform_deals": len(p.get("syndication") or []),
                "asset_monetisation": len(p.get("asset_monetisation") or []),
                "interactions": len(p.get("interactions") or []),
                "register_documents": s.get("documents", len(p.get("documents") or [])),
                "documents_read": len(documents),
                "document_chars": sum(len(d["text"]) for d in documents),
                "document_passages": len(ev),
                "documents_cited": sorted({d["name"] for d in documents} | {e["doc"] for e in ev}),
                "document_note": ev_note,
                "news_items": len(news),
                "records": {k: len(v) for k, v in payload.records.items()},
            },
            "usage": {**usage, "seconds": seconds},
        }

    return router
