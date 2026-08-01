"""Calendar events — create / update (reschedule) / cancel / complete.

First-class meeting records (Release-1 increment 7). The lifecycle is deliberately small
and terminal-frozen (DB trigger, migration 0017):

    Scheduled ──▶ Completed   (it happened — who closed it, what came of it)
             └──▶ Cancelled   (it won't — who called it off and why)

* A reschedule UPDATES the Scheduled row (audited, optimistic version) — the event's
  identity is the commitment, not the time slot.
* Writes are gated like interaction logging (the ``log_interaction`` operation, scoped
  through the central evaluator when the event binds to a subject); the organizer (or an
  Admin/Management identity) owns the lifecycle actions.
* The workflow plane (``svc_workflows``) creates VOX follow-up events with
  ``source="VOX"`` and the run id in ``workflow_id`` — idempotently (the activity checks
  for the run's event before creating).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.calendar import CalendarEvent
from app.repositories.subjects import SUBJECTS, derive_links, load_subject

router = api_router()


class CalendarEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    starts_at: datetime
    ends_at: datetime | None = None
    description: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=300)
    subject_type: str | None = Field(default=None, max_length=30)
    subject_id: str | None = Field(default=None, max_length=64)
    # Humans default to themselves; a service caller (no user identity) MUST name the
    # organizer explicitly.
    organizer: str | None = Field(default=None, max_length=200)
    attendees: list[str] | None = None
    source: str = Field(default="manual", pattern="^(manual|VOX|workflow)$")
    workflow_id: str | None = Field(default=None, max_length=200)
    external_ref: str | None = Field(default=None, max_length=200)


class CalendarEventUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    description: str | None = Field(default=None, max_length=10000)
    location: str | None = Field(default=None, max_length=300)
    attendees: list[str] | None = None
    external_ref: str | None = Field(default=None, max_length=200)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=4000)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=4000)


def _serialize(row: CalendarEvent) -> dict[str, Any]:
    return {
        "id": str(row.id), "title": row.title, "description": row.description,
        "location": row.location,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "subject_type": row.subject_type, "subject_id": row.subject_id,
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "organizer": row.organizer, "attendees": list(row.attendees or []),
        "status": row.status, "source": row.source, "workflow_id": row.workflow_id,
        "external_ref": row.external_ref,
        "completed_by": row.completed_by,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completion_note": row.completion_note,
        "cancelled_by": row.cancelled_by,
        "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None,
        "cancel_note": row.cancel_note,
        "version": row.version,
    }


async def _authorize_write(ctx: RequestContext, subject_type: str | None,
                           subject_id: str | None) -> None:
    """Calendar writes are interaction-shaped: the ``log_interaction`` operation, scoped
    through the central evaluator when the event binds to a subject."""
    from app.api.custom import _ensure_subject_scope
    sid: uuid.UUID | None = None
    if subject_type is not None:
        if subject_type not in SUBJECTS:
            raise ValidationAppError(
                f"Unknown subject_type '{subject_type}'. One of: {', '.join(SUBJECTS)}.")
        try:
            sid = uuid.UUID(str(subject_id))
        except (ValueError, TypeError):
            raise ValidationAppError(
                "subject_id must be a valid id when subject_type is set.") from None
    await _ensure_subject_scope(ctx, "log_interaction", subject_type, sid)


def _actor_email(ctx: RequestContext) -> str | None:
    return ctx.user.email if (ctx.user is not None and ctx.user.email) else None


def _may_manage(ctx: RequestContext, row: CalendarEvent) -> bool:
    """The organizer owns the lifecycle; Admin/Management may act for them; the workflow
    plane may manage the events it created (matched by workflow_id, not trust)."""
    email = _actor_email(ctx)
    if email and ctx.user is not None:
        if email == row.organizer or ctx.user.is_admin:
            return True
        return bool({"Management"} & set(ctx.user.roles or []))
    # Service caller: only over rows the workflow plane itself created.
    return row.source in ("VOX", "workflow")


async def _event(ctx: RequestContext, event_id: uuid.UUID) -> CalendarEvent:
    row = (await ctx.session.execute(
        select(CalendarEvent).where(
            CalendarEvent.tenant_id == ctx.tenant_id,
            CalendarEvent.id == event_id,
            CalendarEvent.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No calendar event '{event_id}'.")
    return row


def _require_scheduled(row: CalendarEvent, action: str) -> None:
    if row.status != "Scheduled":
        raise ConflictError(
            f"Calendar event is {row.status!r} and frozen; cannot {action}.")


@router.post("/v1/calendar-events", tags=["Calendar"], status_code=201,
             summary="Schedule a meeting / follow-up")
async def create_event(payload: CalendarEventIn, response: Response,
                       ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    await _authorize_write(ctx, payload.subject_type, payload.subject_id)
    organizer = payload.organizer or _actor_email(ctx)
    if not organizer:
        raise ValidationAppError(
            "organizer is required (a service caller must name the human organizer).")
    if payload.ends_at is not None and payload.ends_at < payload.starts_at:
        raise ValidationAppError("ends_at must not be before starts_at.")
    entity_id = None
    if payload.subject_type and payload.subject_id:
        subj = await load_subject(ctx.session, ctx.tenant_id, payload.subject_type,
                                  uuid.UUID(payload.subject_id))
        if subj is None:
            raise NotFoundError(f"{payload.subject_type} '{payload.subject_id}' not found.")
        entity_id = derive_links(payload.subject_type, subj)[0]
    row = CalendarEvent(
        tenant_id=ctx.tenant_id, title=payload.title, description=payload.description,
        location=payload.location, starts_at=payload.starts_at, ends_at=payload.ends_at,
        subject_type=payload.subject_type, subject_id=payload.subject_id,
        entity_id=entity_id, organizer=organizer, attendees=payload.attendees,
        source=payload.source, workflow_id=payload.workflow_id,
        external_ref=payload.external_ref, created_by=ctx.actor)
    ctx.session.add(row)
    await ctx.session.flush()
    await ctx.session.refresh(row)
    response.headers["ETag"] = f'"{row.version}"'
    return _serialize(row)


@router.get("/v1/calendar-events", tags=["Calendar"],
            summary="List calendar events (mine by default)")
async def list_events(ctx: RequestContext = Depends(get_context),
                      organizer: str | None = Query(default=None),
                      subject_type: str | None = Query(default=None),
                      subject_id: str | None = Query(default=None),
                      status: str | None = Query(default=None),
                      workflow_id: str | None = Query(default=None),
                      from_ts: datetime | None = Query(default=None),
                      to_ts: datetime | None = Query(default=None),
                      limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    email = _actor_email(ctx)
    is_org_wide = (ctx.user is None
                   or ctx.user.is_admin
                   or bool({"Management"} & set(ctx.user.roles or [])))
    who = organizer
    if not is_org_wide:
        # Ordinary users see their own calendar (organizer or attendee) only.
        if organizer and organizer != email:
            raise ForbiddenError("Only Admin/Management may read another user's calendar.")
        who = email
    conds = [CalendarEvent.tenant_id == ctx.tenant_id, CalendarEvent.deleted_at.is_(None)]
    if who:
        conds.append(CalendarEvent.organizer == who)
    if subject_type:
        conds.append(CalendarEvent.subject_type == subject_type)
    if subject_id:
        conds.append(CalendarEvent.subject_id == str(subject_id))
    if status:
        conds.append(CalendarEvent.status == status)
    if workflow_id:
        conds.append(CalendarEvent.workflow_id == workflow_id)
    if from_ts:
        conds.append(CalendarEvent.starts_at >= from_ts)
    if to_ts:
        conds.append(CalendarEvent.starts_at <= to_ts)
    rows = list((await ctx.session.execute(
        select(CalendarEvent).where(*conds)
        .order_by(CalendarEvent.starts_at).limit(limit))).scalars())
    return {"items": [_serialize(r) for r in rows]}


@router.get("/v1/calendar-events/{event_id}", tags=["Calendar"],
            summary="One calendar event")
async def get_event(event_id: uuid.UUID,
                    ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    return _serialize(await _event(ctx, event_id))


@router.patch("/v1/calendar-events/{event_id}", tags=["Calendar"],
              summary="Update / reschedule a Scheduled event")
async def update_event(event_id: uuid.UUID, payload: CalendarEventUpdate,
                       ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _event(ctx, event_id)
    await _authorize_write(ctx, row.subject_type, row.subject_id)
    if not _may_manage(ctx, row):
        raise ForbiddenError("Only the organizer (or Admin/Management) may change this event.")
    _require_scheduled(row, "update")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise ValidationAppError("Nothing to update.")
    before = {k: getattr(row, k) for k in changes}
    for k, v in changes.items():
        setattr(row, k, v)
    if row.ends_at is not None and row.ends_at < row.starts_at:
        raise ValidationAppError("ends_at must not be before starts_at.")
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="calendar.update",
        resource_type="calendar_events", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"before": {k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in before.items()},
                 "after": {k: (v.isoformat() if isinstance(v, datetime) else v)
                           for k, v in changes.items()}}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


@router.post("/v1/calendar-events/{event_id}/cancel", tags=["Calendar"],
             summary="Cancel a Scheduled event (note mandatory)")
async def cancel_event(event_id: uuid.UUID, payload: NoteIn,
                       ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _event(ctx, event_id)
    await _authorize_write(ctx, row.subject_type, row.subject_id)
    if not _may_manage(ctx, row):
        raise ForbiddenError("Only the organizer (or Admin/Management) may cancel this event.")
    _require_scheduled(row, "cancel")
    row.status = "Cancelled"
    row.cancelled_by = _actor_email(ctx) or ctx.actor
    row.cancelled_at = datetime.now(UTC)
    row.cancel_note = payload.note
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="calendar.cancel",
        resource_type="calendar_events", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"by": row.cancelled_by, "note": payload.note}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


@router.post("/v1/calendar-events/{event_id}/complete", tags=["Calendar"],
             summary="Mark a Scheduled event completed")
async def complete_event(event_id: uuid.UUID, payload: CompleteIn,
                         ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _event(ctx, event_id)
    await _authorize_write(ctx, row.subject_type, row.subject_id)
    email = _actor_email(ctx)
    # Attendees may close a meeting they sat in; otherwise the organizer rule applies.
    if not (_may_manage(ctx, row) or (email and email in (row.attendees or []))):
        raise ForbiddenError(
            "Only the organizer, an attendee, or Admin/Management may complete this event.")
    _require_scheduled(row, "complete")
    row.status = "Completed"
    row.completed_by = email or ctx.actor
    row.completed_at = datetime.now(UTC)
    row.completion_note = payload.note
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="calendar.complete",
        resource_type="calendar_events", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"by": row.completed_by, "note": payload.note}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)
