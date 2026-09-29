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


def build_client_data(p: dict[str, Any], news: list[dict[str, Any]],
                      evidence: list[dict[str, Any]], today: date,
                      documents: list[dict[str, Any]] | None = None) -> str:
    a = p.get("anchor") or {}
    s = p.get("stats") or {}
    restricted = p.get("restricted") or []

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
            for n in news[:40])

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
        f"6. Documentation: {len(docs)} file(s) on the Data Register, by section "
        f"{json.dumps(by_section)}; files: {_rows(docs, keep=40)}",
        "7. Project & technical: use the documents and deal fields above if present.",
        f"8. Engagement: last touch {last_touch or 'Not available'}"
        f" ({_days_since(last_touch, today)} days ago)" if last_touch else
        "8. Engagement: last touch Not available",
        f"   Key contacts on record: {'Y' if contacts else 'N'} {_rows(contacts)}",
        f"   Leads: {_rows(p.get('leads') or [])}",
        f"   Interactions & VOX (most recent first): {_rows(p.get('interactions') or [], keep=15)}",
        f"   Field brief: {_clip(p.get('brief_full') or p.get('brief'), 1500) or 'Not available'}",
        f"9. News (PULSE radar, last 90 days — the firm, and its key people marked "
        f"[about <name>]): \n{news_text}",
        f"10. Today's date: {today.isoformat()}",
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
    hits = [t for t in triggers if t["status"] == "Hit"]

    rating = band_for(score)
    provisional = False
    fin_missing = not pillars[0]["available"]
    bank_missing = not pillars[1]["available"]
    if hits:
        rating = "RED"
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
                      "overdraft", "cash credit")),
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


def aggregate(raws: list[dict[str, Any]],
              documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Several independent readings → one grade, pillar by pillar.

    Each pillar takes the MEDIAN of the scores from the readings that found data for
    it (a "no data" 0 never drags it down), with the evidence of the reading closest
    to that median; for C–G a pillar has data when most readings say so. When the
    caller sent the Data Register files, whether A (Financials) and B (Banking) have
    data follows the KIND of file read — the same files always give the same
    PROVISIONAL answer, and the reason is named. A hard trigger is Hit when most
    readings mark it Hit. The rubric is then applied once to that consensus. The
    words (verdict, risks, gaps) come from the reading whose own total is the median."""
    graded = [enforce(r) for r in raws]
    base_i = sorted(range(len(raws)), key=lambda i: graded[i]["score"])[(len(raws) - 1) // 2]
    consensus = dict(raws[base_i])
    pillars = []
    decided: list[str] = []
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
    news: list[NewsIn] = Field(default_factory=list, max_length=60)
    # The company's Data Register files, read by the caller's DocRAG (CAM's path).
    documents: list[DocumentIn] = Field(default_factory=list, max_length=40)


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
                        p, news, ev, datetime.now(UTC).date(), documents)}]
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
        grade = aggregate(raws, documents if documents else None)
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
            },
            "usage": {**usage, "seconds": seconds},
        }

    return router
