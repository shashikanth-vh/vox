"""Follow-ups — the things the platform must keep RAISING until they are done.

Two kinds, both computed fresh from durable rows on every read (no cron, no state to
drift — Today recomputes when someone looks, which is exactly when a reminder helps):

* ``cs-followup`` — a lending line whose latest CP/CS checklist is APPROVED but still
  carries conditions that are neither Completed nor Waived: the conditions subsequent
  (and CPs deferred as CS) the analyst keeps chasing after disbursement started. The
  reminder lives until every item lands or is waived — or the line closes.
* ``cpcs-approval`` — a CP/CS checklist filed and sitting at 'Completed', waiting for a
  checker. The only approval in the lending flow with no durable run behind it and so no
  clock of its own: it never expires (timing out a checklist would discard prepared work
  and walk the line backwards), so it is CHASED instead — shown from the moment it is
  filed and marked escalated once it has waited 72 hours, the same point the parked runs
  escalate at.
* ``covenant-due`` — an active covenant on a DISBURSED line whose current compliance
  cycle (stepped from ``first_due_on`` by ``frequency``) is due within the window or
  overdue. Covenant monitoring starts when money moves and runs to closure; recording
  the compliance (increment ⑦'s cycle) is what will retire each occurrence.

Served on the internal lane; the workflow plane folds these into /v1/workflows/pending
as REMINDER rows (no decision verbs — there is nothing to approve, only work to chase).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import select

from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models import Entity, LendingTracker
from app.models.covenants import Covenant
from app.models.cpcs import CpcsChecklist
from app.models.prism import MonitoringReporting

router = api_router()

_DONE = ("Completed", "Waived")
# A filed CP/CS checklist turns urgent at the same 72 hours the parked durable runs
# escalate at, so one waiting desk reads the same as another.
_ESCALATE_AFTER_HOURS = 72
_FREQ_MONTHS = {"monthly": 1, "quarterly": 3, "half-yearly": 6, "half yearly": 6,
                "semi-annual": 6, "semiannual": 6, "annually": 12, "annual": 12,
                "yearly": 12}


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
        scope_email: str | None = Query(default=None),
        serviced_only: bool = Query(default=False),
        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    today = date.today()
    items: list[dict[str, Any]] = []

    # OWN-BOOK scoping (``scope_email``): an IC's reminders are their book, not the
    # tenant's — an item stays when they PREPARED it or the line names them as its
    # RM/analyst (the people roster maps the email to those names). Heads and the
    # servicing desk call without it and keep the whole book.
    scope_names: set[str] = set()
    if scope_email:
        from app.models.registry import Person
        person = (await ctx.session.execute(select(Person).where(
            Person.tenant_id == ctx.tenant_id,
            Person.email == scope_email,
            Person.deleted_at.is_(None)))).scalars().first()
        if person is not None:
            scope_names = {n for n in (person.name, person.full_name) if n}

    def _in_scope(line: Any, prepared_by: str | None) -> bool:
        if not scope_email:
            return True
        if prepared_by and prepared_by == scope_email:
            return True
        if line is not None and scope_names:
            return (getattr(line, "rm", None) in scope_names
                    or getattr(line, "analyst", None) in scope_names)
        return False

    # ---- the lending lines involved, read once ---------------------------------------
    lendings = (await ctx.session.execute(select(LendingTracker).where(
        LendingTracker.tenant_id == ctx.tenant_id,
        LendingTracker.deleted_at.is_(None)))).scalars().all()
    lending_by_id = {str(row.id): row for row in lendings}
    entity_ids = {row.entity_id for row in lendings if row.entity_id}

    # ---- CS conditions still open ----------------------------------------------------
    # Two sources, one owner each. A line whose loan account has opened HANDED ITS
    # CONDITIONS OVER: the LMS's own register (loan_account_conditions) is the live
    # chase and the checklist is a frozen decision record — so the reminder reads the
    # LMS rows. Every other line still originates: the latest APPROVED checklist rules.
    from app.models.lms import LoanAccountCondition

    cond_rows = (await ctx.session.execute(select(LoanAccountCondition).where(
        LoanAccountCondition.tenant_id == ctx.tenant_id,
        LoanAccountCondition.deleted_at.is_(None)))).scalars().all()
    handed: dict[str, list[LoanAccountCondition]] = {}
    for cr in cond_rows:
        handed.setdefault(cr.lending_id, []).append(cr)

    checklists = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id,
        CpcsChecklist.deleted_at.is_(None)))).scalars().all()
    latest: dict[str, CpcsChecklist] = {}
    for c in checklists:
        cur = latest.get(c.lending_id)
        if cur is None or (c.checklist_version or 0) > (cur.checklist_version or 0):
            latest[c.lending_id] = c
    for lending_id, c in latest.items():
        # Pre-handover chases are LOS work: a purely-servicing caller
        # (``serviced_only``) starts where the handover did.
        if c.status != "Approved" or lending_id in handed or serviced_only:
            continue
        line = lending_by_id.get(lending_id)
        if line is not None and str(getattr(line, "stage", "") or "") == "Closed":
            continue
        outstanding = [str(i.get("label") or i.get("key") or "condition")
                       for i in (c.items or [])
                       if str(i.get("status") or "Pending") not in _DONE]
        if not outstanding or not _in_scope(line, c.prepared_by):
            continue
        items.append({
            "kind": "cs-followup", "lending_id": lending_id,
            "checklist_id": str(c.id), "checklist_version": c.checklist_version,
            "count": len(outstanding), "outstanding": outstanding[:20],
            "prepared_by": c.prepared_by,
            "entity_id": str(line.entity_id) if line is not None and line.entity_id else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    for lending_id, conds in handed.items():
        line = lending_by_id.get(lending_id)
        if line is not None and str(getattr(line, "stage", "") or "") == "Closed":
            continue
        open_conds = [cr for cr in conds if cr.status not in _DONE]
        if not open_conds or not _in_scope(line, None):
            continue
        first = min((cr.created_at for cr in open_conds if cr.created_at), default=None)
        items.append({
            "kind": "cs-followup", "lending_id": lending_id, "source": "lms",
            "count": len(open_conds),
            "outstanding": [cr.label for cr in open_conds][:20],
            "prepared_by": None,
            "entity_id": str(line.entity_id) if line is not None and line.entity_id else None,
            "created_at": first.isoformat() if first else None,
        })

    # ---- a CP/CS checklist waiting on its CHECKER ------------------------------------
    # The one approval in the lending flow that does NOT park a durable run: the
    # checklist workflow files it, tells the checkers, and returns. Approval is a REST
    # call whenever someone gets to it, so a checklist sitting at 'Completed' waits
    # indefinitely — and an unapproved checklist blocks disbursement SILENTLY, because
    # nothing else in the flow is waiting on a clock that would notice.
    #
    # It must not expire: timing out a CP checklist would throw away prepared work and
    # walk a line backwards for no reason, and unlike a committee decision there is no
    # external deadline it is answering to. So it gets chased instead. The feed is
    # recomputed on every read, so this shows CONTINUOUSLY from the moment it is filed
    # — a stronger nudge than a daily ping — and it turns urgent at the same 72 hours
    # the parked runs escalate at.
    for lending_id, c in latest.items():
        if c.status != "Completed":          # Approved / Returned / Rejected: not waiting
            continue
        line = lending_by_id.get(lending_id)
        if line is not None and str(getattr(line, "stage", "") or "") in ("Closed", "Rejected"):
            continue
        if not _in_scope(line, c.prepared_by):
            continue
        waited_h = None
        if c.created_at is not None:
            waited_h = max(0.0, (datetime.now(UTC) - c.created_at).total_seconds() / 3600)
        items.append({
            "kind": "cpcs-approval", "lending_id": lending_id,
            "checklist_id": str(c.id), "checklist_version": c.checklist_version,
            "prepared_by": c.prepared_by,
            "waiting_hours": round(waited_h, 1) if waited_h is not None else None,
            "escalated": bool(waited_h is not None and waited_h >= _ESCALATE_AFTER_HOURS),
            "entity_id": str(line.entity_id) if line is not None and line.entity_id else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # ---- covenant cycles due on DISBURSED lines --------------------------------------
    # The DOCUMENT chase, mostly: reporting covenants owe the borrower's financials
    # every cycle (monthly for the usual reporting pack) until the loan closes. The
    # sweep mints one OBSERVATION row per period; each open observation is a reminder
    # the analyst retires by RECORDING the received document's result — which is what
    # advances the nag to the next month instead of letting it ring forever.
    covenants = (await ctx.session.execute(select(Covenant).where(
        Covenant.tenant_id == ctx.tenant_id,
        Covenant.is_active.is_(True),
        Covenant.deleted_at.is_(None)))).scalars().all()
    horizon = today + timedelta(days=window_days)

    obs_rows = (await ctx.session.execute(select(MonitoringReporting).where(
        MonitoringReporting.tenant_id == ctx.tenant_id,
        MonitoringReporting.record_type == "Covenant",
        MonitoringReporting.status.in_(("Pending", "Overdue")),
        MonitoringReporting.deleted_at.is_(None)))).scalars().all()
    obs_by_covenant: dict[str, list[MonitoringReporting]] = {}
    for m in obs_rows:
        cov_id = str((m.details or {}).get("covenant_id") or "")
        if cov_id:
            obs_by_covenant.setdefault(cov_id, []).append(m)

    for cov in covenants:
        line = lending_by_id.get(str(cov.lending_id)) if cov.lending_id else None
        # Monitoring starts when money moves; a line not yet disbursed has nothing due,
        # and a closed one nothing left. A covenant with no lending line is skipped too —
        # its cycle belongs to whatever mandate carries it.
        if line is None or str(getattr(line, "stage", "") or "") != "Disbursed":
            continue
        if not _in_scope(line, None):
            continue
        base = {"kind": "covenant-due", "covenant_id": str(cov.id),
                "lending_id": str(cov.lending_id), "entity_id": str(cov.entity_id),
                "name": cov.name, "covenant_type": cov.covenant_type,
                "metric": cov.metric, "frequency": cov.frequency,
                "severity": cov.breach_severity}
        open_obs = sorted(obs_by_covenant.get(str(cov.id)) or [],
                          key=lambda m: (m.due_date or today))
        if open_obs:
            # One reminder per OPEN period — recording the result is what closes it.
            for m in open_obs:
                if m.due_date is not None and m.due_date > horizon:
                    continue
                grace_until = ((m.due_date or today)
                               + timedelta(days=int(cov.grace_days or 0)))
                items.append({**base, "monitoring_id": str(m.id), "period": m.period,
                              "due_on": (m.due_date or today).isoformat(),
                              "overdue": m.status == "Overdue" or grace_until < today})
        else:
            # A deferred schedule (no first due yet — it stamps at first disbursement)
            # simply does not remind.
            if cov.first_due_on is None:
                continue
            # No observation minted yet (sweep not run) — fall back to the computed
            # current cycle so the reminder never depends on a cron having fired.
            due = current_due(cov.first_due_on, cov.frequency, today)
            if due > horizon:
                continue
            grace_until = due + timedelta(days=int(cov.grace_days or 0))
            items.append({**base, "due_on": due.isoformat(),
                          "overdue": grace_until < today})
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
