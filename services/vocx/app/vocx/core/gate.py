"""
core.gate — confidence gate, approval card, and register-record builders.

Step 2 of VOX. Given an extraction (vocx_extract) whose entity_match has been
filled by the resolver (vocx_resolve), this module:

  1. gate() — decides, PER CRITICAL FIELD, whether VOX may auto-write or must
     raise an approval card. Critical fields (any low -> approval):
        * entity match   (resolver confidence / needs_approval)
        * next_meeting date (only when a follow-up was actually stated)
        * register writes (register_signals confidence; new-lead creation)
     Non-critical fields (discussion points, commitments, attendees, note prose)
     never block.

  2. build_interaction() — the ATLAS interaction record that is ALWAYS appended
     (existing match -> refType Deal/Lead; new lead -> refType Lead on the minted
     id). Byte-compatible with logInteraction() in ATLAS_EVAM_v15.html.

  3. build_new_lead() — the ATLAS lead record for the new-lead branch, with lens
     auto-derived from sector via ADAPT_SECT, exactly as Add-Lead does.

  4. build_approval_card() — the payload the mobile approval card renders.

  5. plan_writes() / MockWriter — the ordered set of side effects (Drive note,
     team copy, calendar event, interaction append, optional new lead, alias
     write-back). Step 3 swaps MockWriter for real Google/ATLAS writers; the plan
     shape does not change.
"""

from __future__ import annotations

import datetime as _dt
import random
import string
from dataclasses import dataclass, field
from typing import Any

from app.vocx.core.atlas import AtlasStore
from app.vocx.core.resolve import norm_name

# --- id + time helpers (ATLAS-faithful) --------------------------------------
_B36 = string.digits + string.ascii_uppercase


def _to_b36(n: int) -> str:
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out


def new_interaction_id(now_ms: int | None = None, rng: random.Random | None = None) -> str:
    """Replica of ATLAS newInteractionId(): INT-<Date.now b36 upper>-<rand4 b36 upper>."""
    if now_ms is None:
        now_ms = int(_dt.datetime.now().timestamp() * 1000)
    rng = rng or random
    rand4 = "".join(rng.choice(_B36) for _ in range(4))
    return f"INT-{_to_b36(now_ms)}-{rand4}"


def _date_of(ts: str) -> str:
    """YYYY-MM-DD from an ISO capture timestamp (ATLAS today())."""
    if not ts:
        return _dt.date.today().isoformat()
    return ts[:10]


def _iso_now(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now()).isoformat()


# --- confidence gate ----------------------------------------------------------
@dataclass
class FieldStatus:
    field: str
    critical: bool
    ok: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def gate(extraction: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Per-critical-field confidence gate. Returns a decision dict."""
    th = config.get("thresholds", {})
    date_min = th.get("VOX_DATE_CONF_MIN", 0.70)
    reg_min = th.get("VOX_REGISTER_CONF_MIN", 0.70)

    em = extraction.get("entity_match") or {}
    nm = extraction.get("next_meeting") or {}
    rs = extraction.get("register_signals") or {}

    statuses: list[FieldStatus] = []

    # --- entity match (critical) --------------------------------------------
    entity_ok = not em.get("needs_approval", True)
    statuses.append(FieldStatus(
        field="entity_match", critical=True, ok=entity_ok,
        reason=em.get("reason", "unresolved"),
        detail={
            "code": em.get("code"),
            "canonical_name": em.get("canonical_name"),
            "match_score": em.get("match_score"),
            "is_new_lead": em.get("is_new_lead"),
            "alternatives": em.get("alternatives", []),
        },
    ))

    # --- next_meeting date (critical, only if a follow-up was stated) --------
    has_date = bool(nm.get("date"))
    date_conf = float(nm.get("confidence") or 0.0)
    if has_date:
        date_ok = date_conf >= date_min
        statuses.append(FieldStatus(
            field="next_meeting_date", critical=True, ok=date_ok,
            reason="ok" if date_ok else "low_date_confidence",
            detail={"date": nm.get("date"), "time": nm.get("time"),
                    "mode": nm.get("mode"), "confidence": date_conf,
                    "threshold": date_min},
        ))
    else:
        # No follow-up mentioned -> nothing to schedule; not a blocker.
        statuses.append(FieldStatus(
            field="next_meeting_date", critical=True, ok=True,
            reason="no_followup_stated",
            detail={"date": None, "confidence": date_conf},
        ))

    # --- register write (critical) ------------------------------------------
    # The interaction log always fires; what gets gated here is the STRUCTURED
    # register mutation: temp/business-line updates and (esp.) new-lead creation.
    reg_conf = float(rs.get("confidence") or 0.0)
    is_new_lead = bool(em.get("is_new_lead"))
    if is_new_lead:
        # New lead creation rides the entity-match confidence: if the resolver
        # is confident this is genuinely new, allow it; else approval.
        reg_ok = entity_ok
        reg_reason = "new_lead_" + ("confident" if entity_ok else "needs_approval")
    else:
        reg_ok = reg_conf >= reg_min if _has_register_signal(rs) else True
        reg_reason = "ok" if reg_ok else "low_register_confidence"
    statuses.append(FieldStatus(
        field="register_write", critical=True, ok=reg_ok, reason=reg_reason,
        detail={"confidence": reg_conf, "threshold": reg_min,
                "is_new_lead": is_new_lead,
                "temp": rs.get("temp"),
                "business_line_hint": rs.get("business_line_hint")},
    ))

    blocking = [s for s in statuses if s.critical and not s.ok]
    needs_approval = len(blocking) > 0
    return {
        "auto_write": not needs_approval,
        "needs_approval": needs_approval,
        "blocking_fields": [_fs_dict(s) for s in blocking],
        "field_status": {s.field: _fs_dict(s) for s in statuses},
    }


def _has_register_signal(rs: dict[str, Any]) -> bool:
    return bool(rs.get("temp") or rs.get("business_line_hint") or rs.get("lender_updates"))


def _fs_dict(s: FieldStatus) -> dict[str, Any]:
    return {"field": s.field, "critical": s.critical, "ok": s.ok,
            "reason": s.reason, "detail": s.detail}


# --- record builders ----------------------------------------------------------
def _derive_lens(sector: str, config: dict[str, Any]) -> str:
    if not sector:
        return ""
    adapt = config.get("defaults", {}).get("adapt_sectors", [])
    return "Adaptation" if sector in adapt else "Mitigation"


def _meeting_type(extraction: dict[str, Any], config: dict[str, Any]) -> str:
    """Interaction type for the meeting that JUST happened.

    The contract carries the NEXT meeting's mode, not this one's, so we fall back
    to the configured default unless an explicit occurred-mode is supplied.
    """
    d = config.get("defaults", {})
    occurred = (extraction.get("_meta") or {}).get("occurred_mode")
    if occurred:
        return d.get("interaction_type_by_mode", {}).get(occurred, d.get("fallback_interaction_type"))
    return d.get("fallback_interaction_type", "In-Person Meeting")


def build_interaction(
    extraction: dict[str, Any],
    config: dict[str, Any],
    summary: str | None = None,
    ref_override: dict[str, str] | None = None,
    now: _dt.datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Build the ALWAYS-appended ATLAS interaction record.

    ref_override lets the caller pin {refId, refType} after minting a new lead id.
    """
    em = extraction.get("entity_match") or {}
    meta = extraction.get("_meta") or {}
    nm = extraction.get("next_meeting") or {}
    rs = extraction.get("register_signals") or {}
    rep = extraction.get("report") or {}
    capture_ts = meta.get("capture_ts")

    if ref_override:
        ref_id, ref_type = ref_override["refId"], ref_override["refType"]
    else:
        ref_id, ref_type = em.get("code"), em.get("ref_type") or "Deal"

    # Route to the right ATLAS register. Leads stay Leads. For an existing client/
    # deal, a syndication conversation logs under refType Syndication (with a lender
    # and direction, which ATLAS uses to stamp the lender's chased/responded dates);
    # lending / asset-mgmt conversations log as Deal.
    direction = None
    lender = None
    bl = (rep.get("business_line") or "").lower()
    if ref_type == "Deal" and bl == "syndication":
        ref_type = "Syndication"
        direction = rep.get("direction") if rep.get("direction") in ("inbound", "outbound") else "outbound"
        lender = rep.get("lender") or None

    notes = _report_notes(extraction, summary, config)
    next_action = _first_next_step(rep) or rs.get("next_action")
    if not next_action:
        commits = extraction.get("commitments") or []
        next_action = (commits[0].get("what") if commits else None)
    # follow-up date: the meeting to schedule, else the earliest dated next step
    next_date = nm.get("date") or _first_next_step_date(rep)

    person = _attendee_line(rep) or extraction.get("contact_person") or meta.get("rm") or ""

    return {
        "interactionId": new_interaction_id(
            now_ms=(int(now.timestamp() * 1000) if now else None), rng=rng),
        "refId": ref_id,
        "refType": ref_type,
        "occurredAt": _date_of(capture_ts),
        "loggedAt": _iso_now(now),
        "person": person,
        "interactionType": _meeting_type(extraction, config),
        "direction": direction,
        "lenderName": lender,
        "notes": notes,
        "nextAction": next_action or None,
        "nextActionDate": next_date,
    }


def _first_next_step(rep: dict[str, Any]) -> str | None:
    for s in rep.get("next_steps") or []:
        act = (s.get("action") or "").strip()
        if act:
            owner = (s.get("owner") or "").strip()
            return f"{owner}: {act}" if owner else act
    return None


def _first_next_step_date(rep: dict[str, Any]) -> str | None:
    for s in rep.get("next_steps") or []:
        if s.get("date"):
            return s["date"]
    return None


def _attendee_line(rep: dict[str, Any]) -> str:
    names = []
    for a in rep.get("attendees") or []:
        n = (a.get("name") or "").strip()
        if not n:
            continue
        role = (a.get("role") or "").strip()
        names.append(f"{n} ({role})" if role else n)
    return ", ".join(names)


def _report_notes(extraction: dict[str, Any], summary: str | None,
                  config: dict[str, Any] | None = None) -> str:
    """Compose a rich, scannable interaction note from the field-intel report.
    ATLAS renders interaction bodies with white-space:pre-wrap, so the line breaks
    and section headers below show through cleanly in the timeline."""
    rep = extraction.get("report") or {}
    lines: list[str] = []

    # tag line: desk · lender/direction · temperature · pipeline
    desk = {"lending": "Lending", "syndication": "Syndication",
            "asset_mgmt": "Asset Monetisation"}.get((rep.get("business_line") or "").lower())
    tags = []
    if desk:
        t = desk
        if desk == "Syndication" and rep.get("lender"):
            d = rep.get("direction")
            t += " · {}{}".format(rep["lender"],
                                  " (chased)" if d == "outbound" else " (responded)" if d == "inbound" else "")
        tags.append(t)
    if rep.get("deal_temp"):
        tags.append(str(rep["deal_temp"]))
    if rep.get("pipeline_stage"):
        tags.append(str(rep["pipeline_stage"]))
    if tags:
        lines.append("[ " + " · ".join(tags) + " ]")

    head = rep.get("summary") or summary or " · ".join(extraction.get("discussion_points") or [])
    if head:
        lines.append(head.strip())

    ki = [k for k in (rep.get("key_intel") or []) if k]
    if ki:
        lines.append("")
        lines.append("KEY INTEL")
        lines.extend("• " + k for k in ki)

    facts = []
    for label, key in (("Loan product", "loan_product"), ("Ticket", "ticket_size"),
                       ("Collateral", "collateral"), ("Project", "project_type"),
                       ("Size", "project_size"), ("Location", "location")):
        v = rep.get(key)
        if v:
            facts.append(f"{label}: {v}")
    if facts:
        lines.append("")
        lines.append("DEAL  " + "  ·  ".join(facts))

    # template extras (labels come from config.report_templates)
    extra = rep.get("extra") or {}
    if extra:
        labels = {}
        for t in (config or {}).get("report_templates", []):
            for fdef in t.get("fields", []):
                if fdef.get("key"):
                    labels[fdef["key"]] = fdef.get("label", fdef["key"])
        for fdef in rep.get("_custom") or []:   # RM-added one-off fields
            if fdef.get("key"):
                labels[fdef["key"]] = fdef.get("label", fdef["key"])
        ex_rows = [(labels.get(k, k), v) for k, v in extra.items() if v not in (None, "", [])]
        if ex_rows:
            lines.append("")
            lines.append("DETAILS  " + "  ·  ".join(f"{lbl}: {v}" for lbl, v in ex_rows))

    ns = [s for s in (rep.get("next_steps") or []) if s.get("action")]
    if ns:
        lines.append("")
        lines.append("NEXT STEPS")
        for s in ns:
            owner = (s.get("owner") or "").strip()
            date = (s.get("date") or "").strip()
            tail = (" — due " + date) if date else ""
            lines.append("• " + (("[" + owner + "] ") if owner else "") + s["action"] + tail)

    nu = [n for n in (rep.get("nuances") or []) if n]
    if nu:
        lines.append("")
        lines.append("NUANCES")
        lines.extend("• " + n for n in nu)

    att = _attendee_line(rep)
    if att:
        lines.append("")
        lines.append("Present: " + att)

    rm_name = (extraction.get("_meta") or {}).get("rm")
    lines.append("")
    lines.append(f"— captured by {rm_name} via VOM" if rm_name else "— captured via VOM")
    text = "\n".join(lines).strip()
    return text or (summary or "")


def build_new_lead(
    extraction: dict[str, Any],
    store: AtlasStore,
    config: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any]:
    """Build the ATLAS lead record for the new-lead branch (LD-V## id)."""
    em = extraction.get("entity_match") or {}
    meta = extraction.get("_meta") or {}
    nm = extraction.get("next_meeting") or {}
    rs = extraction.get("register_signals") or {}
    d = config.get("defaults", {})
    ids = config.get("ids", {})

    sector = extraction.get("sector_hint") or ""
    company = em.get("proposed_company") or em.get("canonical_name") or ""
    capture_date = _date_of(meta.get("capture_ts"))
    next_text = ""
    if nm.get("date"):
        next_text = "Follow-up {}{}".format(nm["date"], " ({})".format(nm["mode"]) if nm.get("mode") else "")

    return {
        "id": store.next_vox_lead_id(ids.get("new_lead_prefix", "LD-V"), ids.get("new_lead_pad", 2)),
        "company": company,
        "sector": sector,
        "lens": _derive_lens(sector, config),
        "source": d.get("new_lead_source", "BDRM"),
        "sourceDetail": d.get("new_lead_source_detail", "VOM voice capture"),
        "rm": meta.get("rm") or "",
        "status": d.get("new_lead_status", "Active"),
        "temp": rs.get("temp") or d.get("new_lead_temp", "Warm"),
        "contact": extraction.get("contact_person") or "",
        "phone": "",
        "last": capture_date,
        "next": next_text,
        "nextDate": nm.get("date"),
        "conv": "",
        "createdAt": capture_date,
        "notes": summary or (rs.get("next_action") or ""),
    }


def alias_writeback(extraction: dict[str, Any]) -> dict[str, Any] | None:
    """If a confirmed match's spoken form differs from the canonical name, emit an
    alias write-back op so the register matches it faster next time.

    ATLAS clients have no alias field, so this targets a VOX-side alias map keyed
    by entity code (persisted by the store layer in step 3)."""
    em = extraction.get("entity_match") or {}
    if em.get("needs_approval") or em.get("is_new_lead") or not em.get("code"):
        return None
    canonical = em.get("canonical_name") or ""
    new_aliases = []
    for form in em.get("spoken_forms") or []:
        if norm_name(form) and norm_name(form) != norm_name(canonical):
            new_aliases.append(form)
    if not new_aliases:
        return None
    return {"op": "alias_writeback", "code": em["code"], "aliases": sorted(set(new_aliases))}


# --- approval card ------------------------------------------------------------
def build_approval_card(
    extraction: dict[str, Any],
    decision: dict[str, Any],
    config: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any]:
    """The payload the mobile approval card renders when auto-write is withheld."""
    em = extraction.get("entity_match") or {}
    meta = extraction.get("_meta") or {}
    nm = extraction.get("next_meeting") or {}
    rs = extraction.get("register_signals") or {}
    return {
        "needs_approval": decision["needs_approval"],
        "captured_at": meta.get("capture_ts"),
        "rm": meta.get("rm"),
        "summary": summary,
        "entity": {
            "resolved": {
                "code": em.get("code"),
                "canonical_name": em.get("canonical_name"),
                "match_score": em.get("match_score"),
                "is_new_lead": em.get("is_new_lead"),
                "reason": em.get("reason"),
            },
            "candidate_picker": em.get("alternatives", []),
            "allow_new_lead": True,
            "proposed_company": em.get("proposed_company"),
        },
        "next_meeting": {
            "date": nm.get("date"), "time": nm.get("time"),
            "mode": nm.get("mode"), "confidence": nm.get("confidence"),
        },
        "discussion_points": extraction.get("discussion_points", []),
        "commitments": extraction.get("commitments", []),
        "register_preview": {
            "temp": rs.get("temp"),
            "business_line_hint": rs.get("business_line_hint"),
            "next_action": rs.get("next_action"),
            "lender_updates": rs.get("lender_updates", []),
        },
        "blocking_fields": decision["blocking_fields"],
        "actions": _card_actions(em),
    }


def override_entity(extraction: dict[str, Any], store: AtlasStore,
                    code: str | None = None, new_lead: bool = False,
                    company: str | None = None) -> dict[str, Any]:
    """Apply the RM's approval-card choice to an extraction's entity_match.

    Used by the mobile /commit flow: the RM either picks an existing entity
    (`code`), or chooses "none → new lead" (`new_lead=True`). Either way the match
    becomes user-confirmed (needs_approval=False).
    """
    em = extraction.get("entity_match") or {}
    if new_lead:
        proposed = company or em.get("proposed_company") or em.get("canonical_name") \
            or extraction.get("company_mentioned") or ""
        extraction["entity_match"] = {
            "code": None, "canonical_name": proposed, "kind": "new_lead",
            "ref_type": "Lead", "rm": "", "match_score": 1.0, "match_type": "user_new_lead",
            "own_client_boost": False, "alternatives": [], "is_new_lead": True,
            "needs_approval": False, "proposed_company": proposed,
            "spoken_forms": em.get("spoken_forms", []), "reason": "user_confirmed_new_lead",
        }
        return extraction

    if code:
        name, kind, ref_type, rm = code, "unknown", "Deal", ""
        if code in store.clients:
            name = store.clients[code].get("name") or code
            kind, ref_type, rm = "client", "Deal", store.rm_for_client(code)
        else:
            for l in store.leads:
                if l.get("id") == code:
                    name, kind, ref_type = l.get("company") or code, "lead", "Lead"
                    rm = (l.get("rm") or "").strip()
                    break
        extraction["entity_match"] = {
            "code": code, "canonical_name": name, "kind": kind, "ref_type": ref_type,
            "rm": rm, "match_score": 1.0, "match_type": "user_confirmed",
            "own_client_boost": False, "alternatives": [], "is_new_lead": False,
            "needs_approval": False, "spoken_forms": em.get("spoken_forms", []),
            "reason": "user_confirmed_match",
        }
    return extraction


def apply_edits(extraction: dict[str, Any], edits: dict[str, Any] | None) -> dict[str, Any]:
    """Apply RM edits from the approval card (next-meeting date/time/mode, temp).
    Edited fields are treated as confirmed → confidence 1.0 so they no longer block."""
    if not edits:
        return extraction
    nm = extraction.setdefault("next_meeting", {})
    for k in ("date", "time", "mode"):
        if k in edits:
            nm[k] = edits[k]
    if any(k in edits for k in ("date", "time", "mode")):
        nm["confidence"] = 1.0
    rs = extraction.setdefault("register_signals", {})
    if "temp" in edits:
        rs["temp"] = edits["temp"]
        rs["confidence"] = max(float(rs.get("confidence") or 0.0), 1.0)
    if "next_action" in edits:
        rs["next_action"] = edits["next_action"]
    return extraction


def _card_actions(em: dict[str, Any]) -> list[str]:
    actions = ["approve", "edit"]
    if em.get("alternatives"):
        actions.append("pick_candidate")
    actions.append("new_lead" if not em.get("is_new_lead") else "confirm_new_lead")
    actions.append("discard")
    return actions


# --- write planning + mock writer --------------------------------------------
def plan_writes(
    extraction: dict[str, Any],
    store: AtlasStore,
    config: dict[str, Any],
    summary: str | None = None,
    now: _dt.datetime | None = None,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Ordered side effects for a captured meeting. Step 3 executes these for real."""
    em = extraction.get("entity_match") or {}
    meta = extraction.get("_meta") or {}
    nm = extraction.get("next_meeting") or {}
    drive = config.get("drive", {})
    gcfg = config.get("google", {})
    # Drive and Calendar are independently toggleable. Register/interaction writes
    # always happen; Drive (note doc + team folder + rolling summary) is optional
    # because the log already carries the structured record; Calendar puts a real
    # event on the RM's own Google Calendar.
    drive_enabled = gcfg.get("drive_enabled", False)
    calendar_enabled = gcfg.get("calendar_enabled", True)
    company = em.get("canonical_name") or em.get("proposed_company") or "Unknown"
    ops: list[dict[str, Any]] = []

    # new lead first so the interaction can reference its minted id
    ref_override = None
    if em.get("is_new_lead"):
        lead = build_new_lead(extraction, store, config, summary)
        ops.append({"op": "atlas_create_lead", "target": "shared_register", "record": lead})
        ref_override = {"refId": lead["id"], "refType": "Lead"}

    interaction = build_interaction(extraction, config, summary, ref_override, now, rng)
    ops.append({"op": "atlas_append_interaction", "target": "shared_register", "record": interaction})

    # personal + shared Drive note (only when Drive is enabled)
    if drive_enabled:
        ops.append({"op": "drive_write_note", "target": "personal", "root": drive.get("personal_root"),
                    "company": company, "filename_template": drive.get("note_filename")})
        ops.append({"op": "drive_write_note", "target": "team_shared", "root": drive.get("team_root"),
                    "company": company, "filename_template": drive.get("note_filename"),
                    "on_failure": "flag_interaction_and_queue_retry"})
        ops.append({"op": "drive_write_company_summary", "target": "both",
                    "company": company, "filename": drive.get("company_summary_filename")})

    # calendar events — scheduling every follow-up is mandatory. One event for the
    # next meeting; plus an all-day reminder for each dated next-step action item.
    rep = extraction.get("report") or {}
    if calendar_enabled:
        desc = _calendar_description(extraction, summary)
        if nm.get("date"):
            ops.append({"op": "calendar_create_event", "target": "personal",
                        "kind": "meeting", "company": company, "date": nm.get("date"),
                        "time": nm.get("time"), "mode": nm.get("mode"),
                        "title": f"Follow-up: {company}",
                        "description": desc, "next_action": _first_next_step(rep)})
        for s in (rep.get("next_steps") or []):
            if s.get("date") and s.get("action"):
                owner = (s.get("owner") or "").strip()
                ops.append({"op": "calendar_create_event", "target": "personal",
                            "kind": "next_step", "company": company, "date": s["date"],
                            "time": None, "mode": None,
                            "title": "{}: {}".format(company, s["action"]),
                            "description": (("Owner: " + owner + "\n") if owner else "") +
                                           "Action item from the " + company + " meeting.\n\n— Logged via VOM (ATLAS field intel)"})

    ab = alias_writeback(extraction)
    if ab:
        ops.append({"op": ab["op"], "target": "shared_register",
                    "code": ab["code"], "aliases": ab["aliases"]})
    return ops


def _calendar_description(extraction: dict[str, Any], summary: str | None) -> str:
    """Body of the Google Calendar follow-up event: what the meeting is about and
    what to prepare, drawn from the field-intel report."""
    rep = extraction.get("report") or {}
    lines: list[str] = []
    head = rep.get("summary") or summary
    if head:
        lines.append(head.strip())
    facts = []
    for label, key in (("Loan product", "loan_product"), ("Ticket size", "ticket_size"),
                       ("Pipeline", "pipeline_stage")):
        v = rep.get(key)
        if v:
            facts.append(f"{label}: {v}")
    if facts:
        lines.append("")
        lines.append(" | ".join(facts))
    ns = [s for s in (rep.get("next_steps") or []) if s.get("action")]
    if ns:
        lines.append("")
        lines.append("Prep / next steps:")
        for s in ns:
            owner = (s.get("owner") or "").strip()
            lines.append("• " + (("[" + owner + "] ") if owner else "") + s["action"])
    lines.append("")
    lines.append("— Logged via VOM (ATLAS field intel)")
    return "\n".join(lines).strip()


class MockWriter:
    """Records write ops without performing them — for step-1/2 testing and as the
    interface step 3's real Google/ATLAS writers implement."""

    def __init__(self):
        self.performed: list[dict[str, Any]] = []

    def execute(self, ops: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
        results = []
        for op in ops:
            rec = {"op": op["op"], "target": op.get("target"), "status": "mocked", "input": op}
            self.performed.append(rec)
            results.append(rec)
        return {"ok": True, "count": len(results), "results": results}
