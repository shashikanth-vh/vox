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
}

# How the desk NAMES each kind of row in a sentence.
NOUN_OF: dict[str, str] = {
    "leads": "lead", "deals": "deal", "entities": "company",
    "lending_tracker": "lending line",
    "syndication_tracker": "platform-deals mandate", "syndication_lenders": "lender",
    "asset_monetisation": "asset-monetisation mandate",
    "counterparties": "lender", "people": "team member",
    "interactions": "interaction", "documents": "document", "notes": "note",
}

# Resource types whose rows hang off a company. Every one of these carries entity_id
# directly (deal_id is nullable on the trackers, so the deal is the wrong hop), which is
# what lets one query per type name the company on a whole page of trail.
_ENTITY_LINKED: frozenset[str] = frozenset(
    {"deals", "leads", "lending_tracker", "syndication_tracker", "asset_monetisation"})


def _fields_phrase(changes: dict[str, Any] | None) -> str:
    """The part of an update worth reading: up to two before → after pairs. `values` is
    written by the generic repository for exactly this purpose."""
    values = (changes or {}).get("values") or {}
    if not isinstance(values, dict):
        return ""
    parts: list[str] = []
    for field, pair in list(values.items())[:2]:
        if not isinstance(pair, dict):
            continue
        before, after = pair.get("from"), pair.get("to")
        pretty = field.replace("_", " ")
        if before in (None, "", []) and after not in (None, "", []):
            parts.append(f"{pretty} set to {after}")
        elif after in (None, "", []):
            parts.append(f"{pretty} cleared")
        else:
            parts.append(f"{pretty} {before} → {after}")
    extra = len(values) - len(parts)
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


def _sentence(action: str, resource_type: str | None, changes: dict[str, Any] | None,
              company: str | None) -> str:
    label = (changes or {}).get("label") or ""
    noun = NOUN_OF.get(resource_type or "", (resource_type or "row").rstrip("s"))
    named = f"{noun} {label}".strip() if label else noun
    on_company = f" on {company}" if company and company != label else ""

    if action in ("signin", "signout"):
        return "Signed in to ATLAS" if action == "signin" else "Signed out"
    if action == "mis.import":
        return _import_phrase(changes)
    if action == "create":
        return f"Added a new {named}{on_company}"
    if action == "delete":
        return f"Deleted {named}{on_company}"
    if action == "restore":
        return f"Restored {named}{on_company}"
    if action == "update":
        phrase = _fields_phrase(changes)
        return f"Updated {named}{on_company}" + (f" — {phrase}" if phrase else "")
    # Anything else is a NAMED operation (evidence.break_glass, lms.disburse, …). Its own
    # name is the most honest thing to show — humanised, never invented.
    pretty = action.replace(".", " ").replace("_", " ")
    return f"{pretty[:1].upper()}{pretty[1:]}" + (f" — {named}" if label else "") + on_company


async def _companies_for(session, tenant_id: uuid.UUID,
                         rows: list[AuditLog]) -> dict[str, tuple[str, str]]:
    """resource_id → (company name, group code) for the rows on THIS page.

    One query per resource type over the ids actually present — bounded by the page size,
    never a lookup per row.
    """
    from app.models.deals import Deal, Lead
    from app.models.registry import Entity
    from app.models.trackers import AssetMonetisation, LendingTracker, SyndicationTracker

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
                 "asset_monetisation": AssetMonetisation}
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
    return out


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

    items = []
    for r in rows:
        company, code = companies.get(r.resource_id or "", ("", ""))
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
            "summary": _sentence(r.action, r.resource_type, r.changes, company),
        })
    return {"items": items, "total": len(items)}
