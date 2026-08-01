"""
core.extract — turn a meeting transcript into the VOX extraction JSON.

Claude Haiku (claude-haiku-4-5-20251001) reads the raw transcript and emits the
extraction contract below. The resolver (vocx_resolve) fills entity_match
afterwards; Haiku only proposes company_mentioned + aliases_heard.

Extraction JSON contract:
  {company_mentioned, company_aliases_heard[], sector_hint, contact_person,
   next_meeting{date,time,mode,confidence},
   discussion_points[],
   commitments[{who,what,due}],
   register_signals{temp, next_action, business_line_hint(lend/syn/am),
                    lender_updates[{lender,state}], confidence},
   entity_match{...},                 # resolver fills this
   _meta{capture_ts, rm, transcript_ref}}

Design rules baked into the system prompt:
  * JSON only, no markdown fences.
  * Emit null, never a guess. Unknown -> null.
  * Per-field confidence where the contract asks for it (next_meeting,
    register_signals) — 0..1.
  * Resolve relative dates ("next Tuesday", "in a fortnight") against capture_ts.

If the Anthropic SDK or ANTHROPIC_API_KEY is unavailable, extract() raises unless
offline=True is passed, in which case a deterministic heuristic stub runs so the
rest of the pipeline (resolver, gate, interaction builder) stays testable.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any

SYSTEM_PROMPT = """You are VOX, the field-intelligence engine for Evam Finance's ATLAS relationship register. \
A business development / relationship manager (RM) has just finished a client meeting and dictated a free-form voice note \
(often in English, Hindi, or Hinglish). Your job is to convert the transcript into a single structured JSON call report.

Return ONLY the JSON object. No prose, no markdown fences, no ``` — the first character of your reply must be { and the last must be }.

Rules:
- Emit null (or [] for lists) for anything not clearly stated. NEVER guess a company, date, name, or number. A wrong value is far worse than null.
- Where a field carries a "confidence", set it to your calibrated certainty from 0.0 to 1.0 (1.0 = explicit and unambiguous, ~0.5 = inferred, <0.3 = weak).
- Resolve every relative date ("next Tuesday", "end of the month", "in a fortnight", "after Diwali") to an absolute YYYY-MM-DD using the capture timestamp provided in the user message. If you cannot resolve it to a real date, set the date to null and confidence low.
- company_mentioned: the single most likely company/counterparty the meeting was about. company_aliases_heard: every distinct spoken form/short name/misspelling you heard for that company. Do not include unrelated companies.
- sector_hint: one of the ATLAS sectors if evident, else null (Solar variants, BESS / Energy Storage, EV Mobility, Biofuels / Biogas / CBG, Industrial Decarbonisation, Industrial Water, Water Treatment / WASH, Climate Data & IoT, Industrial Efficiency, Agri / Drone, Solar EPC, Other).
- next_meeting: the SINGLE follow-up MEETING to schedule — a real Google Calendar event is created from this, so capture it carefully. If the RM says anything like "meet again at 4 pm next Wednesday", "call them Friday 11am", "review on the 29th" — put the DATE, the TIME, and the mode HERE (NOT as a next_step). next_meeting.time must be 24-hour "HH:MM" (e.g. "4 pm" -> "16:00", "11am" -> "11:00") or null if no time was said. mode: in-person, video, phone, site, whatsapp, email, or null. Only use next_steps for non-meeting action items (prepare X, send Y, review Z).
- commitments: concrete promises. who = "us"/"them"/a named person; what = the action; due = YYYY-MM-DD or null.
- register_signals.temp: Hot/Warm/Cold if the RM signals deal temperature, else null. business_line_hint: lend, syn, am, or null. lender_updates: only for syndication chatter — [{lender, state}].
- discussion_points: short bullet strings capturing what was discussed.

ALL output fields — summaries, bullets, next steps, attendees' roles, every report field — must be in ENGLISH regardless of the language(s) spoken; translate faithfully, keep proper nouns as-is.

Also produce the "report" object — a polished field-intel call report:
- report.title: the client / counterparty name as a clean report title (usually company_mentioned).
- report.summary: a 2-4 sentence third-person narrative summary of the meeting ("The BDM met with ... to discuss ...").
- report.transcript_english: the FULL transcript translated inline into fluent English, word-for-word in meaning. If the note is already English, lightly clean it. Never invent content.
- report.key_intel: 3-6 concise factual bullet strings — the substantive intel (asks, amounts, projects, decisions). Each bullet must be SELF-CONTAINED: name the amount with its unit, the concrete date (YYYY-MM-DD when derivable from the capture date), and who owns/said it — "₹50 Cr term loan under discussion, financials due 2026-07-31 (Ravi)" not "they will share financials".
- report.nuances: 0-4 soft-signal bullets — tone, hesitations, internal politics, off-hand remarks worth remembering.
- report.next_steps: action items as [{owner, action, date}] — owner = the team/person responsible (e.g. "Credit", "RM", a name), action = what to do, date = YYYY-MM-DD or null.
- report.attendees: people present as [{name, role, company}] — role/company null if unknown.
- Additional deal fields (null if not stated): report.sector (same as sector_hint), report.project_type, report.project_size (e.g. "10000 MW"), report.location (sites/geographies), report.loan_product (e.g. "Project Finance", "Term Loan"), report.ticket_size (loan amount, keep the spoken units e.g. "₹100 Cr"), report.collateral, report.equity_raised, report.turnover.
- report.pipeline_stage: one of Prospecting, Proposal, Negotiation, Sanctioned, Disbursed, Closed, or null.
- report.opportunity_score: integer 1-5 (5 = strongest opportunity) based on intent, ticket size, and stage, or null.
- report.business_line: which Evam desk this belongs to — "lending" (a direct loan / term loan / project finance to the client), "syndication" (arranging debt from OTHER lenders / a consortium), or "asset_mgmt" (asset monetisation / divestment / fund) — or null.
- report.lender: for syndication only — the specific lender/bank being chased or that responded, else null.
- report.direction: for syndication only — "outbound" (we reached out / chased a lender) or "inbound" (a lender responded), else null.
- report.deal_temp: Hot / Warm / Cold if the RM signals deal temperature, else null.

Output exactly this shape (fill values or null):
{"company_mentioned": null, "company_aliases_heard": [], "sector_hint": null, "contact_person": null,
 "next_meeting": {"date": null, "time": null, "mode": null, "confidence": 0.0},
 "discussion_points": [],
 "commitments": [{"who": null, "what": null, "due": null}],
 "register_signals": {"temp": null, "next_action": null, "business_line_hint": null, "lender_updates": [], "confidence": 0.0},
 "report": {"title": null, "summary": null, "transcript_english": null, "key_intel": [], "nuances": [],
   "next_steps": [{"owner": null, "action": null, "date": null}],
   "attendees": [{"name": null, "role": null, "company": null}],
   "sector": null, "project_type": null, "project_size": null, "location": null,
   "loan_product": null, "ticket_size": null, "collateral": null, "equity_raised": null, "turnover": null,
   "pipeline_stage": null, "opportunity_score": null,
   "business_line": null, "lender": null, "direction": null, "deal_temp": null}}
"""


# --- contract skeleton --------------------------------------------------------
def empty_report() -> dict[str, Any]:
    return {
        "title": None, "summary": None, "transcript_english": None,
        "key_intel": [], "nuances": [], "next_steps": [], "attendees": [],
        "sector": None, "project_type": None, "project_size": None, "location": None,
        "loan_product": None, "ticket_size": None, "collateral": None,
        "equity_raised": None, "turnover": None,
        "pipeline_stage": None, "opportunity_score": None,
        "business_line": None, "lender": None, "direction": None, "deal_temp": None,
        "extra": {},
    }


def empty_extraction() -> dict[str, Any]:
    return {
        "company_mentioned": None,
        "company_aliases_heard": [],
        "sector_hint": None,
        "contact_person": None,
        "next_meeting": {"date": None, "time": None, "mode": None, "confidence": 0.0},
        "discussion_points": [],
        "commitments": [],
        "register_signals": {
            "temp": None, "next_action": None, "business_line_hint": None,
            "lender_updates": [], "confidence": 0.0,
        },
        "report": empty_report(),
        "entity_match": None,
        "_meta": {"capture_ts": None, "rm": None, "transcript_ref": None},
    }


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge a model/stub result onto the skeleton so every field is present."""
    out = empty_extraction()
    for k in ("company_mentioned", "sector_hint", "contact_person"):
        if raw.get(k) is not None:
            out[k] = raw[k]
    if isinstance(raw.get("company_aliases_heard"), list):
        out["company_aliases_heard"] = [a for a in raw["company_aliases_heard"] if a]
    if isinstance(raw.get("discussion_points"), list):
        out["discussion_points"] = [d for d in raw["discussion_points"] if d]
    if isinstance(raw.get("commitments"), list):
        out["commitments"] = [c for c in raw["commitments"] if isinstance(c, dict) and (c.get("what"))]
    nm = raw.get("next_meeting") or {}
    if isinstance(nm, dict):
        out["next_meeting"].update({k: nm.get(k) for k in ("date", "time", "mode") if k in nm})
        if isinstance(nm.get("confidence"), (int, float)):
            out["next_meeting"]["confidence"] = float(nm["confidence"])
    rs = raw.get("register_signals") or {}
    if isinstance(rs, dict):
        for k in ("temp", "next_action", "business_line_hint"):
            if k in rs:
                out["register_signals"][k] = rs.get(k)
        if isinstance(rs.get("lender_updates"), list):
            out["register_signals"]["lender_updates"] = rs["lender_updates"]
        if isinstance(rs.get("confidence"), (int, float)):
            out["register_signals"]["confidence"] = float(rs["confidence"])
    out["report"] = _coerce_report(raw.get("report"), out)
    return out


def _coerce_report(rep: Any, ext: dict[str, Any]) -> dict[str, Any]:
    """Normalise the field-intel report block, filling sensible fallbacks so the
    UI always has something to render even from a thin extraction."""
    out = empty_report()
    rep = rep if isinstance(rep, dict) else {}
    _str_fields = ("title", "summary", "transcript_english", "sector", "project_type",
                   "project_size", "location", "loan_product", "ticket_size",
                   "collateral", "equity_raised", "turnover", "pipeline_stage",
                   "business_line", "lender", "direction", "deal_temp")
    for k in _str_fields:
        v = rep.get(k)
        if v not in (None, ""):
            out[k] = v
    for k in ("key_intel", "nuances"):
        if isinstance(rep.get(k), list):
            out[k] = [str(x).strip() for x in rep[k] if str(x).strip()]
    if isinstance(rep.get("next_steps"), list):
        out["next_steps"] = [
            {"owner": s.get("owner"), "action": s.get("action"), "date": s.get("date")}
            for s in rep["next_steps"] if isinstance(s, dict) and (s.get("action") or s.get("owner"))
        ]
    if isinstance(rep.get("attendees"), list):
        out["attendees"] = [
            {"name": a.get("name"), "role": a.get("role"), "company": a.get("company")}
            for a in rep["attendees"] if isinstance(a, dict) and a.get("name")
        ]
    sc = rep.get("opportunity_score")
    if isinstance(sc, (int, float)):
        out["opportunity_score"] = max(1, min(5, int(round(sc))))
    # Fallbacks so a thin/offline extraction still populates the report shell.
    out["title"] = out["title"] or ext.get("company_mentioned")
    out["sector"] = out["sector"] or ext.get("sector_hint")
    if not out["key_intel"]:
        out["key_intel"] = list(ext.get("discussion_points") or [])
    if not out["summary"]:
        pts = ext.get("discussion_points") or []
        out["summary"] = pts[0] if pts else None
    if not out["attendees"] and ext.get("contact_person"):
        out["attendees"] = [{"name": ext["contact_person"], "role": None,
                             "company": ext.get("company_mentioned")}]
    # business line / temperature fall back to the register signals the gate reads
    rs = ext.get("register_signals") or {}
    if not out["business_line"]:
        out["business_line"] = {"lend": "lending", "syn": "syndication",
                                "am": "asset_mgmt"}.get(rs.get("business_line_hint"))
    if not out["deal_temp"] and rs.get("temp"):
        out["deal_temp"] = rs.get("temp")
    if not out["lender"]:
        lu = rs.get("lender_updates") or []
        if lu and isinstance(lu[0], dict):
            out["lender"] = lu[0].get("lender")
    if out["direction"] not in ("inbound", "outbound"):
        out["direction"] = None
    if isinstance(rep.get("extra"), dict):
        out["extra"] = rep["extra"]
    return out


def build_system_prompt(config: dict[str, Any] | None = None) -> str:
    """SYSTEM_PROMPT + the Evam domain glossary + few-shot worked examples, both from
    config.json — editable without touching code. The glossary teaches the model what
    Evam's process words mean; the examples teach it the depth/shape of a good report."""
    config = config or {}
    intel = config.get("intelligence", {}) or {}
    parts = [SYSTEM_PROMPT]
    glossary = ([g for g in (config.get("glossary") or []) if g]
                if intel.get("glossary", True) else [])
    if glossary:
        parts.append("\n\nEVAM DOMAIN GLOSSARY (authoritative — use these meanings):\n- "
                     + "\n- ".join(glossary))
    examples = ((config.get("few_shot_examples") or [])
                if intel.get("few_shot", True) else [])
    if examples:
        blocks = []
        for i, ex in enumerate(examples, 1):
            if not (ex.get("transcript") and ex.get("extraction")):
                continue
            blocks.append(
                f"EXAMPLE {i} ({ex.get('label', '')}):\nTranscript:\n\"\"\"\n{ex['transcript']}\n\"\"\"\n"
                f"Ideal extraction (dates like <...resolved> mean: resolve against capture_ts):\n"
                + json.dumps(ex["extraction"], ensure_ascii=False))
        if blocks:
            parts.append("\n\nWORKED EXAMPLES — match this depth and structure:\n\n"
                         + "\n\n".join(blocks))
    return "".join(parts)


# The extraction contract as a JSON SCHEMA — used to FORCE structured output via
# tool-use (the API guarantees a schema-valid object; no fence/truncation parsing).
# Kept permissive (additionalProperties) so prompt evolution never hard-fails.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_mentioned": {"type": ["string", "null"]},
        "company_aliases_heard": {"type": "array", "items": {"type": "string"}},
        "sector_hint": {"type": ["string", "null"]},
        "contact_person": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "next_meeting": {"type": "object", "properties": {
            "date": {"type": ["string", "null"]}, "time": {"type": ["string", "null"]},
            "mode": {"type": ["string", "null"]}, "confidence": {"type": ["number", "null"]}},
            "additionalProperties": True},
        "discussion_points": {"type": "array", "items": {"type": "string"}},
        "commitments": {"type": "array", "items": {"type": "object", "properties": {
            "who": {"type": ["string", "null"]}, "what": {"type": ["string", "null"]},
            "due": {"type": ["string", "null"]}}, "additionalProperties": True}},
        "register_signals": {"type": "object", "additionalProperties": True},
        "report": {"type": "object", "additionalProperties": True},
    },
    "required": ["company_mentioned", "report"],
    "additionalProperties": True,
}


class _StructuredOffError(Exception):
    """Internal: structured output disabled by flag — take the text path."""


# --- live extraction (Haiku) --------------------------------------------------
def extract(
    transcript: str,
    capture_ts: str,
    rm: str,
    transcript_ref: str | None = None,
    config: dict[str, Any] | None = None,
    offline: bool = False,
    client: Any = None,
) -> dict[str, Any]:
    """Extract the VOX contract from a transcript.

    capture_ts: ISO timestamp of capture (relative dates resolve against it).
    offline=True forces the heuristic stub (no network, deterministic).
    """
    config = config or {}
    model = config.get("model", "claude-haiku-4-5-20251001")
    key_env = config.get("anthropic_api_key_env", "ANTHROPIC_API_KEY")

    use_stub = offline or (client is None and not os.environ.get(key_env))
    if use_stub:
        result = _stub_extract(transcript, capture_ts)
    else:
        result = _haiku_extract(transcript, capture_ts, model, key_env, client, config)

    ext = _coerce(result)
    _backfill_next_meeting(ext, transcript, capture_ts)
    ext["_meta"] = {"capture_ts": capture_ts, "rm": rm, "transcript_ref": transcript_ref}
    return ext


# Phrases that mark the transcript as SCHEDULING a follow-up meeting (vs. a mere
# commitment like "they will share financials on Friday"). Deliberately meeting-
# specific so the deterministic backfill never invents a meeting from a deadline.
_FOLLOWUP_HINTS = (
    "follow up", "follow-up", "followup", "next meeting", "meet again",
    "meet on", "meet next", "meeting on", "meeting next", "meeting at",
    "schedule a meeting", "schedule meeting", "catch up", "call with",
    "call them", "call him", "call her", "review meeting", "site visit",
    "visit on", "visit next",
)


def _backfill_next_meeting(ext: dict[str, Any], transcript: str, capture_ts: str) -> None:
    """Deterministic safety net for the follow-up meeting.

    The model is told to resolve relative dates against capture_ts, but when it
    can't (or returns null), a plainly stated follow-up ("schedule a follow-up
    next Monday 11am") must still land in next_meeting — a calendar event and the
    review card depend on it. Only fires when the transcript carries an explicit
    scheduling phrase AND a resolvable date; fills time/mode from the transcript
    whenever a date is present but they are missing."""
    low = transcript.lower()
    nm = ext["next_meeting"]
    if not nm.get("date") and any(k in low for k in _FOLLOWUP_HINTS):
        date, conf = _stub_next_date(low, capture_ts)
        if date:
            nm["date"] = date
            nm["confidence"] = max(float(nm.get("confidence") or 0.0), conf)
    if nm.get("date"):
        if not nm.get("time"):
            nm["time"] = _stub_time(low)
        if not nm.get("mode"):
            nm["mode"] = next((v for k, v in _MODE_WORDS.items() if k in low), None)


def _haiku_extract(transcript, capture_ts, model, key_env, client,
                   config: dict[str, Any] | None = None) -> dict[str, Any]:
    if client is None:
        import anthropic  # imported lazily so offline use needs no SDK
        client = anthropic.Anthropic(api_key=os.environ[key_env])
    system = build_system_prompt(config)
    # Name the weekday explicitly — resolving "next Monday" requires knowing what
    # day capture_ts falls on, and weekday arithmetic from a bare ISO date is
    # exactly the kind of thing a model gets silently wrong.
    base = _parse_ts(capture_ts)
    ts_line = f"{capture_ts} (a {base.strftime('%A')})" if base else capture_ts
    user_msg = (
        f"capture_ts (resolve all relative dates against this): {ts_line}\n\n"
        f"Transcript:\n\"\"\"\n{transcript.strip()}\n\"\"\""
    )
    # STRUCTURED OUTPUT: force a tool call whose input must match EXTRACTION_SCHEMA —
    # the API validates, so fences/truncation/type drift can't corrupt the extraction.
    # Flag-off or any SDK/feature failure → the original text+lenient-parse path.
    structured = ((config or {}).get("intelligence", {}) or {}).get("structured_output", True)
    try:
        if not structured:
            raise _StructuredOffError
        resp = client.messages.create(
            model=model, max_tokens=3000, system=system,
            tools=[{"name": "file_extraction",
                    "description": "File the structured call-report extraction.",
                    "input_schema": EXTRACTION_SCHEMA}],
            tool_choice={"type": "tool", "name": "file_extraction"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and isinstance(
                    getattr(block, "input", None), dict):
                return block.input
    except _StructuredOffError:
        pass
    except Exception:  # noqa: BLE001 — degrade to the text path, never fail a capture
        pass
    resp = client.messages.create(
        model=model, max_tokens=3000, system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    return _parse_json_lenient(text)


def _parse_json_lenient(text: str) -> dict[str, Any]:
    """Parse JSON even if the model wrapped it in fences or added stray text."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# --- offline heuristic stub ---------------------------------------------------
# Deterministic, dependency-free. Not meant to rival Haiku — just enough signal
# to exercise the resolver + gate + writers end to end without a key.
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MODE_WORDS = {
    "in person": "in-person", "in-person": "in-person", "office": "in-person",
    "video": "video", "zoom": "video", "google meet": "video", "meet": "video",
    "call": "phone", "phone": "phone", "site": "site", "plant": "site",
    "whatsapp": "whatsapp", "email": "email",
}
_TEMP_WORDS = {"hot": "Hot", "warm": "Warm", "cold": "Cold"}


def _stub_extract(transcript: str, capture_ts: str) -> dict[str, Any]:
    t = transcript.strip()
    low = t.lower()
    out: dict[str, Any] = {}

    # company: "met <X>", "meeting with <X>", "with the <X> team", "<X> folks"
    company, aliases = _stub_company(t)
    out["company_mentioned"] = company
    out["company_aliases_heard"] = aliases

    # contact person: "spoke with <Name>", "met <Name> from"
    out["contact_person"] = _stub_contact(t)

    # next meeting
    date, conf = _stub_next_date(low, capture_ts)
    mode = next((v for k, v in _MODE_WORDS.items() if k in low), None)
    out["next_meeting"] = {"date": date, "time": _stub_time(low), "mode": mode,
                           "confidence": conf}

    # discussion points: sentences, trimmed
    pts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if len(s.strip()) > 12]
    out["discussion_points"] = pts[:6]

    # register signals
    temp = next((v for k, v in _TEMP_WORDS.items() if re.search(r"\b" + k + r"\b", low)), None)
    bl = None
    if any(w in low for w in ("syndicat", "lender", "consortium")):
        bl = "syn"
    elif any(w in low for w in ("term loan", "lending", "debt", "sanction")):
        bl = "lend"
    elif any(w in low for w in ("asset mon", "monetis", "monetiz", "divest")):
        bl = "am"
    out["register_signals"] = {
        "temp": temp, "next_action": pts[-1] if pts else None,
        "business_line_hint": bl, "lender_updates": [],
        "confidence": 0.5 if (temp or bl) else 0.3,
    }
    out["sector_hint"] = _stub_sector(low)
    return out


def _stub_company(t: str) -> tuple:
    # Common terminators that mark the end of a spoken company name.
    # Trigger words are case-insensitive (sentence-initial "Met" as well as mid-
    # sentence "met"); the company capture stays case-sensitive so it anchors on
    # a Capitalised name.
    _end = r"(?: team| folks| people| about| on | to | who| which| keen| regarding|,|\.|$)"
    patterns = [
        r"(?i:\b(?:called|named))\s+([A-Z][\w&.\- ]+?)" + _end,
        r"(?i:\b(?:company|outfit|firm|startup|player|shop))\s*,?\s+([A-Z][\w&.\- ]+?)" + _end,
        r"(?i:\bmeeting with)\s+(?i:the\s+)?([A-Z][\w&.\- ]+?)" + _end,
        r"(?i:\bmet)\s+(?i:with\s+)?(?i:the\s+)?([A-Z][\w&.\- ]+?)" + _end,
        r"(?i:\bwith)\s+(?i:the\s+)?([A-Z][\w&.\- ]+?)\s+(?:team|folks|people)",
        r"(?i:\bat)\s+([A-Z][\w&.\- ]+?)(?:'s)?\s+(?:office|plant|site)",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            name = m.group(1).strip(" .,")
            name = re.sub(r"\s+", " ", name)
            if 2 <= len(name) <= 60:
                return name, [name]
    return None, []


def _stub_contact(t: str) -> str | None:
    m = re.search(r"(?:spoke (?:with|to)|met|talked to) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", t)
    return m.group(1) if m else None


def _stub_time(low: str) -> str | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?!\w)", low)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3).startswith("p"):
            h += 12
        return "{:02d}:{}".format(h, m.group(2) or "00")
    m = re.search(r"\b(\d{1,2})\s*o'?\s?clock\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:               # business context: "4 o'clock" means 16:00
            h += 12
        return f"{h % 24:02d}:00"
    m = re.search(r"\bat ([01]?\d|2[0-3]):([0-5]\d)\b", low)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _stub_sector(low: str) -> str | None:
    table = [
        ("rooftop", "Solar - Rooftop"), ("solar", "Solar - General"),
        ("bess", "BESS / Energy Storage"), ("battery", "BESS / Energy Storage"),
        ("ev ", "EV Mobility"), ("biogas", "Biofuels / Biogas / CBG"),
        ("cbg", "Biofuels / Biogas / CBG"), ("water", "Industrial Water"),
        ("drone", "Agri / Drone"),
    ]
    for kw, sec in table:
        if kw in low:
            return sec
    return None


def _stub_next_date(low: str, capture_ts: str) -> tuple:
    base = _parse_ts(capture_ts)
    if base is None:
        return None, 0.0
    m = re.search(r"\b(?:next|this|coming) (mon|tues|wednes|thurs|fri|satur|sun)day\b", low)
    if not m:
        m = re.search(r"\bon (mon|tues|wednes|thurs|fri|satur|sun)day\b", low)
    if m:
        target = {"mon": 0, "tues": 1, "wednes": 2, "thurs": 3, "fri": 4,
                  "satur": 5, "sun": 6}[m.group(1)]
        delta = (target - base.weekday() + 7) % 7 or 7
        return (base + _dt.timedelta(days=delta)).date().isoformat(), 0.9
    m = re.search(r"\bin (?:a|one) week\b|\bnext week\b", low)
    if m:
        return (base + _dt.timedelta(days=7)).date().isoformat(), 0.7
    m = re.search(r"\bin (\d{1,2}) days?\b", low)
    if m:
        return (base + _dt.timedelta(days=int(m.group(1)))).date().isoformat(), 0.8
    # "day after tomorrow" must be tested BEFORE bare "tomorrow" — the shorter
    # pattern matches inside the longer phrase and would resolve a day early.
    m = re.search(r"\bday after tomorrow\b", low)
    if m:
        return (base + _dt.timedelta(days=2)).date().isoformat(), 0.9
    m = re.search(r"\btomorrow\b", low)
    if m:
        return (base + _dt.timedelta(days=1)).date().isoformat(), 0.9
    m = re.search(r"\bin (?:a|two) (?:fortnight|weeks)\b", low)
    if m:
        days = 14 if ("fortnight" in m.group(0) or "two" in m.group(0)) else 7
        return (base + _dt.timedelta(days=days)).date().isoformat(), 0.6
    m = re.search(r"\bon the (\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m:
        day = int(m.group(1))
        year, month = base.year, base.month
        if day <= base.day:           # already past this month -> next month
            month += 1
            if month > 12:
                month, year = 1, year + 1
        try:
            return _dt.date(year, month, day).isoformat(), 0.8
        except ValueError:            # e.g. "on the 31st" in a 30-day month
            return None, 0.0
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        return m.group(1), 0.95
    return None, 0.0


def _parse_ts(ts: str):
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(ts[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
