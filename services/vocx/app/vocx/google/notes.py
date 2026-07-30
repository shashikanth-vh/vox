"""
google.notes — render the meeting note (.md) and rolling company summary (.md).

Note layout (per spec):
  Header (company, entity id/code, RM, date/time, attendees)
  -> Haiku discussion summary
  -> commitments / next-actions checklist
  -> scheduled follow-up
  -> collapsed raw transcript at the bottom

These renderers are pure string builders (no I/O), so they are trivially testable
and identical whether the note lands in personal or shared Drive.
"""

from __future__ import annotations

from typing import Any


def _fmt_attendees(extraction: dict[str, Any]) -> str:
    meta = extraction.get("_meta") or {}
    people = []
    if extraction.get("contact_person"):
        people.append(str(extraction["contact_person"]))
    if meta.get("rm"):
        people.append("{} (RM)".format(meta["rm"]))
    return ", ".join(people) if people else "—"


def _fmt_followup(nm: dict[str, Any]) -> str:
    if not nm or not nm.get("date"):
        return "None scheduled"
    parts = [nm["date"]]
    if nm.get("time"):
        parts.append("at " + str(nm["time"]))
    if nm.get("mode"):
        parts.append("({})".format(nm["mode"]))
    tail = ""
    conf = nm.get("confidence")
    if isinstance(conf, (int, float)):
        tail = f"  _(confidence {conf:.0%})_"
    return " ".join(parts) + tail


def render_note(
    extraction: dict[str, Any],
    summary: str | None,
    transcript: str,
    entity_code: str | None = None,
    company: str | None = None,
) -> str:
    """Full meeting note markdown for one capture."""
    em = extraction.get("entity_match") or {}
    meta = extraction.get("_meta") or {}
    nm = extraction.get("next_meeting") or {}
    rs = extraction.get("register_signals") or {}

    company = company or em.get("canonical_name") or em.get("proposed_company") or "Unknown company"
    code = entity_code or em.get("code") or ("(new lead)" if em.get("is_new_lead") else "—")
    occurred = (meta.get("capture_ts") or "")[:16].replace("T", " ")

    lines: list[str] = []
    lines.append(f"# Meeting note — {company}")
    lines.append("")
    lines.append(f"- **Entity:** {company} ({code})")
    lines.append("- **RM:** {}".format(meta.get("rm") or "—"))
    lines.append("- **Date / time:** {}".format(occurred or "—"))
    lines.append(f"- **Attendees:** {_fmt_attendees(extraction)}")
    if extraction.get("sector_hint"):
        lines.append("- **Sector:** {}".format(extraction["sector_hint"]))
    if rs.get("temp") or rs.get("business_line_hint"):
        bits = [b for b in (rs.get("temp"), rs.get("business_line_hint")) if b]
        lines.append("- **Signals:** {}".format(" · ".join(bits)))
    lines.append("")

    lines.append("## Summary")
    lines.append(summary.strip() if summary else "_No summary available._")
    lines.append("")

    points = extraction.get("discussion_points") or []
    if points:
        lines.append("## Discussion points")
        for p in points:
            lines.append(f"- {p}")
        lines.append("")

    commits = extraction.get("commitments") or []
    lines.append("## Commitments / next actions")
    if commits:
        for c in commits:
            who = c.get("who") or "?"
            what = c.get("what") or ""
            due = " _(due {})_".format(c["due"]) if c.get("due") else ""
            lines.append(f"- [ ] **{who}:** {what}{due}")
    if rs.get("next_action"):
        lines.append("- [ ] {}".format(rs["next_action"]))
    if not commits and not rs.get("next_action"):
        lines.append("- _None captured._")
    lines.append("")

    lines.append("## Follow-up")
    lines.append(_fmt_followup(nm))
    lines.append("")

    lines.append("---")
    lines.append("<details>")
    lines.append("<summary>Raw transcript</summary>")
    lines.append("")
    lines.append("```")
    lines.append((transcript or "").strip())
    lines.append("```")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def render_company_summary(
    company: str,
    entity_code: str | None,
    rolling_summary: str,
    recent: list[dict[str, Any]],
    updated_ts: str,
    last_n: int,
) -> str:
    """Rolling _company_summary.md, regenerated from the last N note summaries.

    recent: [{date, summary}] newest first.
    """
    lines: list[str] = []
    lines.append(f"# {company} — rolling summary")
    lines.append("")
    lines.append("- **Entity:** {}".format(entity_code or "—"))
    lines.append("- **Updated:** {}".format((updated_ts or "")[:16].replace("T", " ")))
    lines.append(f"- **Window:** last {last_n} captures")
    lines.append("")
    lines.append("## Overview")
    lines.append(rolling_summary.strip() if rolling_summary else "_No summary yet._")
    lines.append("")
    if recent:
        lines.append("## Recent captures")
        for r in recent[:last_n]:
            date = r.get("date") or "—"
            one = (r.get("summary") or "").strip().replace("\n", " ")
            lines.append(f"- **{date}** — {one[:200]}")
        lines.append("")
    return "\n".join(lines)


def note_filename(capture_ts: str, template: str = "{ts}_meeting.md") -> str:
    """ATLAS_VOX/<Company>/<YYYY-MM-DD_HHMM>_meeting.md — the <ts> segment."""
    ts = (capture_ts or "")[:16]              # YYYY-MM-DDTHH:MM
    ts = ts.replace("T", "_").replace(":", "")  # YYYY-MM-DD_HHMM
    return template.format(ts=ts or "unknown")
