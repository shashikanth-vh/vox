"""The Activity Log — who did what, in plain English.

The Audit tab already serves the field-level trail: action, resource type, resource id,
raw ``changes``. That is the right artefact for an investigation and the wrong one for a
desk: nobody reads ``update · syndication · 6f3c… · {"values":{"status":…}}`` and thinks
"Kotak moved to Queries Received on Shree Ganesh's mandate".

This endpoint turns the SAME immutable rows into sentences. It reads the audit trail —
no second log to keep in step, nothing new to write at the call sites — and renders each
row with the vocabulary the desk uses, resolving the company a tracker row belongs to so
the screen never shows a UUID.

Admin-only, exactly like the Audit tab: ``activity_log`` is an Admin-only view in the
matrix (evam_backend_core.rbac), and this route asks that matrix rather than deciding for
itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import select

from app.core.errors import ForbiddenError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog

router = api_router(tags=["Activity"])

# Which area of the business a row belongs to — drives the coloured pill and the filter
# chips. Keyed by the audit row's resource_type, which the generic repository sets to the
# model's TABLE name (``lending_tracker``, not the URL's ``/v1/lending``).
AREA_OF: dict[str, str] = {
    "leads": "Leads",
    "deals": "Deals", "interactions": "Deals", "notes": "Deals",
    "lending_tracker": "Lending", "lms_accounts": "Lending", "lms_bookings": "Lending",
    "syndication_tracker": "Platform Deals", "syndication_lenders": "Platform Deals",
    "asset_monetisation": "Asset Mon",
    "entities": "Clients",
    "counterparties": "FI", "people": "Team",
    "documents": "Documents", "session": "Session",
    "import": "System", "tenants": "System",
    "vox_conversations": "VOX", "governance_evidence": "Documents",
}

# How the desk NAMES each kind of row in a sentence.
NOUN_OF: dict[str, str] = {
    "leads": "lead", "deals": "deal", "entities": "company",
    "lending_tracker": "lending line",
    "syndication_tracker": "platform-deals mandate", "syndication_lenders": "lender",
    "asset_monetisation": "asset-monetisation mandate",
    "counterparties": "lender", "people": "team member",
    "interactions": "interaction", "documents": "document", "notes": "note",
    "vox_conversations": "VOX conversation", "governance_evidence": "evidence",
}

# Resource types whose rows hang off a company. Every one of these carries entity_id
# directly (deal_id is nullable on the trackers, so the deal is the wrong hop), which is
# what lets one query per type name the company on a whole page of trail.
_ENTITY_LINKED: frozenset[str] = frozenset(
    {"deals", "leads", "lending_tracker", "syndication_tracker", "asset_monetisation",
     "interactions", "vox_conversations", "documents"})


def _fields_phrase(changes: dict[str, Any] | None) -> str:
    """The part of an update worth reading: up to two before → after pairs. `values` is
    written by the generic repository for exactly this purpose."""
    values = (changes or {}).get("values") or {}
    if not isinstance(values, dict):
        return ""
    parts: list[str] = []
    shown = 0
    for field, pair in values.items():
        if shown >= 4:
            break
        # Raw identifiers read as noise ("entity id set to 5b127d72-…"); the linked
        # company is already named on the row and the raw value stays on the audit
        # record itself.
        if field == "id" or field.endswith("_id"):
            continue
        if not isinstance(pair, dict):
            continue
        shown += 1
        before, after = pair.get("from"), pair.get("to")
        pretty = field.replace("_", " ")
        if before in (None, "", []) and after not in (None, "", []):
            parts.append(f"{pretty} set to {after}")
        elif after in (None, "", []):
            parts.append(f"{pretty} cleared")
        else:
            parts.append(f"{pretty} {before} → {after}")
    readable = sum(1 for f, pr in values.items()
                   if isinstance(pr, dict) and f != "id" and not f.endswith("_id"))
    extra = readable - len(parts)
    phrase = "; ".join(parts)
    if extra > 0:
        phrase += f" (+{extra} more)"
    return phrase


def _import_phrase(changes: dict[str, Any] | None) -> str:
    """A governed ledger import, summarised the way the import dialog reports it."""
    ch = changes or {}
    counts = ch.get("counts") or {}
    bits: list[str] = []
    for key, word in (("entities", "companies"), ("entities_matched", "companies matched"),
                      ("deals", "deals"), ("deals_updated", "deals updated"),
                      ("leads", "leads"), ("leads_updated", "leads updated")):
        n = counts.get(key)
        if isinstance(n, int) and n:
            bits.append(f"{n} {word}")
    head = f"Imported the Excel ledger ({ch.get('mode') or 'merge'})"
    tail = " — " + ", ".join(bits[:4]) if bits else ""
    reason = ch.get("reason")
    why = f" · reason: {reason}" if reason else ""
    quarantined = ch.get("quarantined_count")
    skipped = f" · {quarantined} rows quarantined" if quarantined else ""
    return head + tail + why + skipped


# Named operations, in the desk's own words. Anything not listed keeps the honest
# humanised fallback below — a verb is only ever written here, never invented.
VERB_OF: dict[str, str] = {
    "vox.approve": "Approved a VOX conversation",
    "vox.erase": "Erased a VOX conversation (content removed for everyone)",
    "vox.regenerate": "Regenerated a VOX conversation's report",
    "evidence.attach": "Attached evidence",
    "evidence.revoke": "Revoked evidence",
    "evidence.break_glass": "Used break-glass past an evidence gate",
    "lms.open": "Opened a loan account",
    "lms.terms": "Set loan terms",
    "lms.disburse": "Recorded a disbursement",
    "lms.accrue": "Ran interest accrual",
    "lms.classify": "Reclassified a loan account",
    "lms.entry": "Posted a ledger entry",
    "lms.condition.add": "Added a CP/CS condition",
    "lms.condition.receive": "Marked a CP/CS condition received",
    "cpcs.prepare": "Prepared the CP/CS checklist",
    "cpcs.approve": "Approved the CP/CS checklist",
    "cpcs.reject": "Rejected the CP/CS checklist",
    "cpcs.return": "Returned the CP/CS checklist for rework",
    "sanction.terms": "Recorded sanction terms",
    "deal.close": "Closed a deal",
    "calendar.update": "Updated a calendar item",
    "calendar.complete": "Completed a calendar item",
    "calendar.cancel": "Cancelled a calendar item",
    "covenant.update": "Updated a covenant",
    "covenant.result": "Recorded a covenant test result",
    "covenant.waive": "Waived a covenant",
    "covenant.waiver_expired": "A covenant waiver expired",
    "advaya.handover.submit": "Submitted an Advaya handover",
    "advaya.handover.approve": "Approved an Advaya handover",
    "advaya.handover.reject": "Rejected an Advaya handover",
    "advaya.handover.return": "Returned an Advaya handover",
    "advaya.handoff": "Handed a deal off to Advaya",
    "ledger.export": "Exported the Excel ledger",
    "people.handover": "Handed over a book to another RM",
    "people.sync_access": "Synced team access",
    "document.validate": "Validated a document",
    "document.reject": "Rejected a document",
    "document.replace": "Replaced a document",
    "document.expire": "Marked a document expired",
    "cam.open": "Opened a CAM",
    "cam.submit": "Submitted a CAM",
    "cam.decide": "Decided a CAM",
    "cam.reset": "Reset a CAM",
    "disbursement.tranche": "Recorded a disbursement tranche",
    "disbursement.tranche.recorded": "Recorded a disbursement tranche",
}


def _named_extra(action: str, changes: dict[str, Any] | None) -> str:
    """The one detail that makes a named operation readable, pulled from the audit
    row's own payload — never invented, dropped when absent."""
    ch = changes or {}
    if action == "vox.approve":
        bits = []
        if ch.get("recorder"):
            bits.append(f"recorded by {ch['recorder']}")
        if ch.get("created_lead_id"):
            bits.append("filed a new lead")
        return ", ".join(bits)
    if action in ("vox.erase", "vox.regenerate"):
        return f"recorded by {ch['recorder']}" if ch.get("recorder") else ""
    if action in ("evidence.attach", "evidence.revoke"):
        kind = str(ch.get("evidence_kind") or "").replace("_", " ")
        core = " · ".join(str(x) for x in (kind, ch.get("reference")) if x)
        subject = ch.get("subject_type")
        return core + (f" on the {subject}" if subject and core else "")
    if action == "evidence.break_glass":
        why = ch.get("justification") or ch.get("reason")
        return f"reason: {why}" if why else ""
    if action == "deal.close":
        why = ch.get("reason")
        return str(why) if why else ""
    return ""


def _sentence(action: str, resource_type: str | None, changes: dict[str, Any] | None,
              company: str | None, detail: str = "") -> str:
    label = (changes or {}).get("label") or ""
    noun = NOUN_OF.get(resource_type or "", (resource_type or "row").rstrip("s"))
    named = f"{noun} {label}".strip() if label else noun
    on_company = f" on {company}" if company and company != label else ""

    if action in ("signin", "signout"):
        return "Signed in to ATLAS" if action == "signin" else "Signed out"
    if action == "mis.import":
        return _import_phrase(changes)
    if action == "create":
        return f"Added a new {named}{on_company}" + (f" — {detail}" if detail else "")
    if action == "delete":
        return f"Deleted {named}{on_company}" + (f" — it held: {detail}" if detail else "")
    if action == "restore":
        return f"Restored {named}{on_company}" + (f" — {detail}" if detail else "")
    if action == "update":
        phrase = _fields_phrase(changes)
        return (f"Updated {named}{on_company}"
                + (f" — {phrase}" if phrase else (f" — {detail}" if detail else "")))
    # NAMED operations (vox.approve, lms.disburse, …): the verb map speaks the desk's
    # language; anything unmapped keeps its own humanised name — never invented.
    verb = VERB_OF.get(action)
    if verb:
        extra = _named_extra(action, changes)
        return (verb + on_company + (f" — {extra}" if extra else "")
                + (f" — {detail}" if detail else ""))
    pretty = action.replace(".", " ").replace("_", " ")
    return f"{pretty[:1].upper()}{pretty[1:]}" + (f" — {named}" if label else "") + on_company


async def _companies_for(session, tenant_id: uuid.UUID,
                         rows: list[AuditLog]) -> dict[str, tuple[str, str]]:
    """resource_id → (company name, group code) for the rows on THIS page.

    One query per resource type over the ids actually present — bounded by the page size,
    never a lookup per row.
    """
    from app.models.deals import Deal, Lead
    from app.models.documents import Document
    from app.models.interactions import Interaction
    from app.models.registry import Entity
    from app.models.trackers import AssetMonetisation, LendingTracker, SyndicationTracker
    from app.models.vox import VoxConversation

    by_type: dict[str, set[str]] = {}
    for r in rows:
        if (r.resource_type in _ENTITY_LINKED or r.resource_type == "entities") and r.resource_id:
            by_type.setdefault(r.resource_type or "", set()).add(r.resource_id)

    def _uuids(ids: set[str]) -> list[uuid.UUID]:
        out = []
        for i in ids:
            try:
                out.append(uuid.UUID(i))
            except (ValueError, AttributeError, TypeError):
                continue
        return out

    out: dict[str, tuple[str, str]] = {}
    ent_name: dict[uuid.UUID, tuple[str, str]] = {}

    # Entities first — every other lookup lands here in the end.
    all_entity_ids: set[uuid.UUID] = set()
    model_for = {"deals": Deal, "leads": Lead, "lending_tracker": LendingTracker,
                 "syndication_tracker": SyndicationTracker,
                 "asset_monetisation": AssetMonetisation,
                 "interactions": Interaction, "vox_conversations": VoxConversation,
                 "documents": Document}
    row_entity: dict[str, uuid.UUID] = {}

    for rtype, ids in by_type.items():
        if rtype == "entities":
            all_entity_ids.update(_uuids(ids))
            continue
        model = model_for.get(rtype)
        if model is None:
            continue
        found = (await session.execute(
            select(model.id, model.entity_id).where(
                model.tenant_id == tenant_id, model.id.in_(_uuids(ids)))
        )).all()
        for rid, eid in found:
            if eid:
                row_entity[str(rid)] = eid
                all_entity_ids.add(eid)

    if all_entity_ids:
        # display_name is the desk's short name and is often blank on an imported book —
        # the legal name is what the ledger carried. Same fallback the grids use, so the
        # trail never shows a bare group code where every other screen shows a company.
        for eid, display, legal, code in (await session.execute(
            select(Entity.id, Entity.display_name, Entity.legal_name, Entity.code).where(
                Entity.tenant_id == tenant_id, Entity.id.in_(all_entity_ids))
        )).all():
            ent_name[eid] = (display or legal or "", code or "")

    for rid, eid in row_entity.items():
        if eid in ent_name:
            out[rid] = ent_name[eid]
    for eid, pair in ent_name.items():
        out.setdefault(str(eid), pair)
    await _second_hop(session, tenant_id, rows, out)
    return out


async def _second_hop(session, tenant_id: uuid.UUID, rows: list[AuditLog],
                      out: dict[str, tuple[str, str]]) -> None:
    """Rows further from the company than one hop still name the firm:

    * a syndication lender reaches it through its mandate;
    * a VOX conversation not pinned to an entity reaches it through its lead (or the
      lead its approval created, or its filed interaction) — and when even the lead
      knows only a typed company NAME, that name is shown rather than nothing;
    * governance evidence reaches it through the subject it attaches to.
    """
    from app.models.deals import Deal, Lead
    from app.models.interactions import Interaction
    from app.models.registry import Entity
    from app.models.trackers import (AssetMonetisation, LendingTracker,
                                     SyndicationLender, SyndicationTracker)
    from app.models.vox import VoxConversation

    def _uuids(ids: set[str]) -> list[uuid.UUID]:
        got = []
        for i in ids:
            try:
                got.append(uuid.UUID(i))
            except (ValueError, AttributeError, TypeError):
                continue
        return got

    pending: dict[str, uuid.UUID] = {}          # resource_id -> entity_id
    text_name: dict[str, str] = {}              # resource_id -> typed company name

    # -- syndication lenders: lender -> mandate -> entity ----------------------
    lender_ids = {r.resource_id for r in rows
                  if r.resource_type == "syndication_lenders" and r.resource_id
                  and r.resource_id not in out}
    if lender_ids:
        for rid, eid in (await session.execute(
            select(SyndicationLender.id, SyndicationTracker.entity_id)
            .join(SyndicationTracker,
                  SyndicationTracker.id == SyndicationLender.syndication_id)
            .where(SyndicationLender.tenant_id == tenant_id,
                   SyndicationLender.id.in_(_uuids(lender_ids)))
        )).all():
            if eid:
                pending[str(rid)] = eid

    # -- VOX conversations: lead, filed interaction, or the lead approve created
    vox_ids = {r.resource_id for r in rows
               if r.resource_type == "vox_conversations" and r.resource_id
               and r.resource_id not in out}
    if vox_ids:
        for rid, eid, lead_co in (await session.execute(
            select(VoxConversation.id, Lead.entity_id, Lead.company)
            .join(Lead, Lead.id == VoxConversation.lead_id)
            .where(VoxConversation.tenant_id == tenant_id,
                   VoxConversation.entity_id.is_(None),
                   VoxConversation.id.in_(_uuids(vox_ids)))
        )).all():
            if eid:
                pending[str(rid)] = eid
            elif lead_co:
                text_name[str(rid)] = lead_co
        still = vox_ids - set(pending) - set(text_name)
        if still:
            for rid, eid in (await session.execute(
                select(VoxConversation.id, Interaction.entity_id)
                .join(Interaction, Interaction.id == VoxConversation.interaction_id)
                .where(VoxConversation.tenant_id == tenant_id,
                       VoxConversation.id.in_(_uuids(still)))
            )).all():
                if eid:
                    pending[str(rid)] = eid
        # The approval's own audit row names the lead it created.
        still = vox_ids - set(pending) - set(text_name)
        made_lead = {r.resource_id: str((r.changes or {}).get("created_lead_id") or "")
                     for r in rows if r.resource_id in still and r.changes}
        made_ids = _uuids({v for v in made_lead.values() if v})
        if made_ids:
            lead_info = {str(lid): (eid, co) for lid, eid, co in (await session.execute(
                select(Lead.id, Lead.entity_id, Lead.company).where(
                    Lead.tenant_id == tenant_id, Lead.id.in_(made_ids))
            )).all()}
            for rid, lid in made_lead.items():
                eid, co = lead_info.get(lid, (None, None))
                if eid:
                    pending[rid] = eid
                elif co:
                    text_name[rid] = co

    # -- governance evidence: through the subject it attaches to ---------------
    ev_rows = [r for r in rows
               if r.resource_type == "governance_evidence" and r.resource_id
               and r.resource_id not in out and r.changes]
    subject_model = {"Lead": Lead, "Deal": Deal, "Entity": Entity,
                     "Lending": LendingTracker, "Syndication": SyndicationTracker,
                     "AssetMonetisation": AssetMonetisation}
    by_subject: dict[str, dict[str, str]] = {}   # subject_type -> {subject_id: rid}
    for r in ev_rows:
        stype = str((r.changes or {}).get("subject_type") or "")
        sid = str((r.changes or {}).get("subject_id") or "")
        if stype in subject_model and sid:
            by_subject.setdefault(stype, {})[sid] = r.resource_id or ""
    for stype, sid_map in by_subject.items():
        model = subject_model[stype]
        if model is Entity:
            for rid_key, rid in sid_map.items():
                try:
                    pending[rid] = uuid.UUID(rid_key)
                except (ValueError, TypeError):
                    continue
            continue
        cols = [model.id, model.entity_id]
        has_company_text = stype == "Lead"
        if has_company_text:
            cols.append(Lead.company)
        for got in (await session.execute(
            select(*cols).where(model.tenant_id == tenant_id,
                                model.id.in_(_uuids(set(sid_map))))
        )).all():
            sid, eid = got[0], got[1]
            rid = sid_map.get(str(sid), "")
            if not rid:
                continue
            if eid:
                pending[rid] = eid
            elif has_company_text and got[2]:
                text_name[rid] = got[2]

    if pending:
        names = {eid: (display or legal or "", code or "")
                 for eid, display, legal, code in (await session.execute(
                     select(Entity.id, Entity.display_name, Entity.legal_name,
                            Entity.code).where(Entity.tenant_id == tenant_id,
                                               Entity.id.in_(set(pending.values())))
                 )).all()}
        for rid, eid in pending.items():
            if eid in names:
                out[rid] = names[eid]
    for rid, name in text_name.items():
        out.setdefault(rid, (name, ""))


async def _lender_bits(session, tenant_id: uuid.UUID,
                       rows: list[AuditLog]) -> dict[str, str]:
    """resource_id → the lender's name (and its slice, when set) — "Added a new
    lender" says WHICH lender, on whose mandate."""
    from app.models.trackers import SyndicationLender

    ids = {r.resource_id for r in rows
           if r.resource_type == "syndication_lenders" and r.resource_id}
    if not ids:
        return {}
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except (ValueError, AttributeError, TypeError):
            continue
    out: dict[str, str] = {}
    for rid, name, status, amount in (await session.execute(
        select(SyndicationLender.id, SyndicationLender.lender_name,
               SyndicationLender.status, SyndicationLender.amount_cr).where(
            SyndicationLender.tenant_id == tenant_id, SyndicationLender.id.in_(uuids))
    )).all():
        bits = name or ""
        if amount:
            bits += f" · {amount} cr"
        if status:
            bits += f" · {status}"
        out[str(rid)] = bits
    return out


async def _vox_bits(session, tenant_id: uuid.UUID,
                    rows: list[AuditLog]) -> dict[str, str]:
    """resource_id → what the VOX conversation SAID: the report's meeting summary
    (or its first key discussion point) and the take's length. An erased conversation
    has no content left by design, so its rows honestly carry nothing."""
    from app.models.vox import VoxConversation

    ids = {r.resource_id for r in rows
           if r.resource_type == "vox_conversations" and r.resource_id}
    if not ids:
        return {}
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(i))
        except (ValueError, AttributeError, TypeError):
            continue
    out: dict[str, str] = {}
    for rid, report, seconds in (await session.execute(
        select(VoxConversation.id, VoxConversation.structured_report,
               VoxConversation.duration_seconds).where(
            VoxConversation.tenant_id == tenant_id, VoxConversation.id.in_(uuids))
    )).all():
        common = (report or {}).get("common") or {}

        def _cv(key: str) -> Any:
            cell = common.get(key)
            return cell.get("value") if isinstance(cell, dict) else None

        text = str(_cv("meeting_summary") or "").strip()
        if not text:
            points = [x for x in (_cv("key_discussion_points") or [])
                      if isinstance(x, str) and x.strip()]
            text = points[0].strip() if points else ""
        bits = f'"{text[:160]}{"…" if len(text) > 160 else ""}"' if text else ""
        if seconds:
            mins = f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"
            bits = f"{bits} ({mins})" if bits else f"({mins})"
        if bits:
            out[str(rid)] = bits
    return out


async def _interaction_bits(session, tenant_id: uuid.UUID,
                            rows: list[AuditLog]) -> dict[str, str]:
    """resource_id → the phrase that makes "Added a new interaction" mean something:
    the interaction's type and a short slice of what was said. One bounded query for
    the page; soft-deleted rows still answer, so old trail lines stay readable."""
    from app.models.interactions import Interaction

    ids = [r.resource_id for r in rows
           if r.resource_type == "interactions" and r.resource_id]
    if not ids:
        return {}
    uuids = []
    for i in set(ids):
        try:
            uuids.append(uuid.UUID(i))
        except (ValueError, AttributeError, TypeError):
            continue
    out: dict[str, str] = {}
    for rid, itype, summary, notes, lender in (await session.execute(
        select(Interaction.id, Interaction.interaction_type, Interaction.summary,
               Interaction.notes, Interaction.lender_name).where(
            Interaction.tenant_id == tenant_id, Interaction.id.in_(uuids))
    )).all():
        text = (summary or notes or "").strip().replace("\n", " ")
        snippet = f'"{text[:120]}{"…" if len(text) > 120 else ""}"' if text else ""
        bits = " — ".join(x for x in (itype, snippet) if x)
        if lender:
            bits = f"{bits} (with {lender})" if bits else f"with {lender}"
        out[str(rid)] = bits
    return out


def _row_detail(r: AuditLog, interaction_bits: dict[str, str],
                lender_bits: dict[str, str], vox_bits: dict[str, str]) -> str:
    if r.resource_type == "interactions":
        return interaction_bits.get(r.resource_id or "", "")
    if r.resource_type == "syndication_lenders":
        return lender_bits.get(r.resource_id or "", "")
    if r.resource_type == "vox_conversations":
        return vox_bits.get(r.resource_id or "", "")
    return ""


@router.get("/v1/activity", summary="The Activity Log — who did what, in plain English")
async def read_activity(
    ctx: RequestContext = Depends(get_context),
    limit: int = Query(default=200, ge=1, le=500),
    since: datetime | None = Query(default=None),
    action: str | None = Query(default=None),
) -> dict[str, Any]:
    # Same gate as the Audit tab, read from the matrix rather than decided here.
    if ctx.user is not None:
        from app.authz.engine import view_access
        from app.authz.matrix import Access

        if view_access(ctx.user, "activity_log") is Access.NONE:
            raise ForbiddenError("The activity log is Admin-only.")
    elif ctx.session is not None:
        from app.core.config import get_settings

        if get_settings().enforce_rbac:
            raise ForbiddenError("The activity log requires a user context.")

    conds = [AuditLog.tenant_id == ctx.tenant_id]
    if since:
        conds.append(AuditLog.at >= since)
    if action:
        conds.append(AuditLog.action == action)
    rows = (await ctx.session.execute(
        select(AuditLog).where(*conds).order_by(AuditLog.at.desc()).limit(limit)
    )).scalars().all()

    companies = await _companies_for(ctx.session, ctx.tenant_id, list(rows))
    interaction_bits = await _interaction_bits(ctx.session, ctx.tenant_id, list(rows))
    lender_bits = await _lender_bits(ctx.session, ctx.tenant_id, list(rows))
    vox_bits = await _vox_bits(ctx.session, ctx.tenant_id, list(rows))

    items = []
    for r in rows:
        company, code = companies.get(r.resource_id or "", ("", ""))
        detail = _row_detail(r, interaction_bits, lender_bits, vox_bits)
        # (create, delete, restore and bare updates all render it — see _sentence)
        items.append({
            "id": r.id,
            "at": r.at.isoformat(),
            "actor": r.actor or "",
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "area": AREA_OF.get(r.resource_type or "", "Other"),
            "company": company,
            "code": code or ((r.changes or {}).get("label") or ""),
            "summary": _sentence(r.action, r.resource_type, r.changes, company, detail),
        })
    return {"items": items, "total": len(items)}
