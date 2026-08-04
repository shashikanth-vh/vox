"""Follow-ups — the things the platform must keep RAISING until they are done.

Two kinds, both computed fresh from durable rows on every read (no cron, no state to
drift — Today recomputes when someone looks, which is exactly when a reminder helps):

* ``cs-followup`` — a lending line whose latest CP/CS checklist is APPROVED but still
  carries conditions that are neither Completed nor Waived: the conditions subsequent
  (and CPs deferred as CS) the analyst keeps chasing after disbursement started. The
  reminder lives until every item lands or is waived — or the line closes.
* ``covenant-due`` — an active covenant on a DISBURSED line whose current compliance
  cycle (stepped from ``first_due_on`` by ``frequency``) is due within the window or
  overdue. Covenant monitoring starts when money moves and runs to closure; recording
  the compliance (increment ⑦'s cycle) is what will retire each occurrence.

Served on the internal lane; the workflow plane folds these into /v1/workflows/pending
as REMINDER rows (no decision verbs — there is nothing to approve, only work to chase).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import select

from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models import Entity, LendingTracker
from app.models.covenants import Covenant
from app.models.cpcs import CpcsChecklist

router = api_router()

_DONE = ("Completed", "Waived")
_FREQ_MONTHS = {"monthly": 1, "quarterly": 3, "half-yearly": 6, "half yearly": 6,
                "semi-annual": 6, "annually": 12, "annual": 12, "yearly": 12}


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def current_due(first_due_on: date, frequency: str, today: date) -> date:
    """The cycle date currently owed: the first occurrence on/after today — or, when a
    past occurrence was never closed out, that past date (overdue). Without a compliance
    log yet (increment ⑦), the practical rule is: the most recent occurrence <= today is
    owed until recorded; before the first occurrence, the first one is what's coming."""
    step = _FREQ_MONTHS.get((frequency or "").strip().lower(), 3)
    if today < first_due_on:
        return first_due_on
    due = first_due_on
    while _add_months(due, step) <= today:
        due = _add_months(due, step)
    return due


@router.get("/v1/internal/follow-ups", tags=["Internal"],
            summary="Open CS chases and covenant cycles due — computed fresh")
async def list_follow_ups(
        window_days: int = Query(default=14, ge=0, le=120),
        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    today = date.today()
    items: list[dict[str, Any]] = []

    # ---- the lending lines involved, read once ---------------------------------------
    lendings = (await ctx.session.execute(select(LendingTracker).where(
        LendingTracker.tenant_id == ctx.tenant_id,
        LendingTracker.deleted_at.is_(None)))).scalars().all()
    lending_by_id = {str(row.id): row for row in lendings}
    entity_ids = {row.entity_id for row in lendings if row.entity_id}

    # ---- CS conditions still open on the LATEST, APPROVED checklist ------------------
    checklists = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id,
        CpcsChecklist.deleted_at.is_(None)))).scalars().all()
    latest: dict[str, CpcsChecklist] = {}
    for c in checklists:
        cur = latest.get(c.lending_id)
        if cur is None or (c.checklist_version or 0) > (cur.checklist_version or 0):
            latest[c.lending_id] = c
    for lending_id, c in latest.items():
        if c.status != "Approved":
            continue
        line = lending_by_id.get(lending_id)
        if line is not None and str(getattr(line, "stage", "") or "") == "Closed":
            continue
        outstanding = [str(i.get("label") or i.get("key") or "condition")
                       for i in (c.items or [])
                       if str(i.get("status") or "Pending") not in _DONE]
        if not outstanding:
            continue
        items.append({
            "kind": "cs-followup", "lending_id": lending_id,
            "checklist_id": str(c.id), "checklist_version": c.checklist_version,
            "count": len(outstanding), "outstanding": outstanding[:20],
            "prepared_by": c.prepared_by,
            "entity_id": str(line.entity_id) if line is not None and line.entity_id else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # ---- covenant cycles due on DISBURSED lines --------------------------------------
    covenants = (await ctx.session.execute(select(Covenant).where(
        Covenant.tenant_id == ctx.tenant_id,
        Covenant.is_active.is_(True),
        Covenant.deleted_at.is_(None)))).scalars().all()
    horizon = today + timedelta(days=window_days)
    for cov in covenants:
        line = lending_by_id.get(str(cov.lending_id)) if cov.lending_id else None
        # Monitoring starts when money moves; a line not yet disbursed has nothing due,
        # and a closed one nothing left. A covenant with no lending line is skipped too —
        # its cycle belongs to whatever mandate carries it.
        if line is None or str(getattr(line, "stage", "") or "") != "Disbursed":
            continue
        due = current_due(cov.first_due_on, cov.frequency, today)
        grace_until = due + timedelta(days=int(cov.grace_days or 0))
        if due > horizon:
            continue
        items.append({
            "kind": "covenant-due", "covenant_id": str(cov.id),
            "lending_id": str(cov.lending_id), "entity_id": str(cov.entity_id),
            "name": cov.name, "covenant_type": cov.covenant_type,
            "frequency": cov.frequency, "due_on": due.isoformat(),
            "overdue": grace_until < today, "severity": cov.breach_severity,
        })
        entity_ids.add(cov.entity_id)

    # ---- company names, so a reminder reads as a company and not a UUID --------------
    names: dict[str, str] = {}
    if entity_ids:
        rows = (await ctx.session.execute(select(Entity.id, Entity.legal_name).where(
            Entity.tenant_id == ctx.tenant_id,
            Entity.id.in_(entity_ids)))).all()
        names = {str(i): n for i, n in rows}
    for item in items:
        eid = item.get("entity_id")
        if eid and eid in names:
            item["company"] = names[eid]

    return {"count": len(items), "items": items}
