"""Early-warning-signal cases — trigger → investigation → escalation → closure.

One case per trigger source (a covenant breach, a RED/AMBER intel item, a lapsed
waiver, or an RM raising a flag manually), deduped by the database. The lifecycle:

    Open ──assign──▶ UnderInvestigation ──escalate──▶ Escalated ──close──▶ Closed
      └────────────────────┴──────────────────────────────┴───────close───▶ Closed

* Every RM desk can OPEN a case on its own book (``manage_ews``, scoped) — a field RM
  spotting distress must never be blocked from raising the flag.
* ESCALATION needs reasons; auto-escalation (an investigation SLA lapsing in the case's
  Temporal run) comes through the internal service route, audited as ``system:sla``.
* CLOSURE needs a disposition + note; closing an ESCALATED case is reserved to senior
  credit authority (Credit Head / Management / Admin) — an escalation can never be
  quietly buried by the person it escalated past. Closed rows are FROZEN by trigger.

    POST /v1/ews-cases                        open (idempotent per source)
    GET  /v1/ews-cases?entity_id=…            the case register
    GET  /v1/ews-cases/{id}                   one case
    POST /v1/ews-cases/{id}/assign            → UnderInvestigation
    POST /v1/ews-cases/{id}/note              append an investigation note
    POST /v1/ews-cases/{id}/escalate          → Escalated (reasons mandatory)
    POST /v1/ews-cases/{id}/close             → Closed (disposition + note mandatory)
    POST /v1/internal/ews-cases/{id}/auto-escalate   service: SLA-lapsed escalation
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.custom import _ensure_subject_scope
from app.authz.engine import service_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.covenants import EwsCase
from app.repositories.subjects import load_subject

router = api_router()

_SENIOR = {"Credit Head", "Management", "Admin"}
_ALLOWED_SERVICES = {"svc_workflows"}
_DISPOSITIONS = "^(Resolved|Downgraded|FalseAlarm|LossMitigated|Restructured)$"


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    source: str = Field(default="manual", pattern="^(manual|intel|covenant|waiver_expiry)$")
    # The trigger's identity for dedupe (intel id / observation id); a manual case gets
    # a fresh one when omitted.
    source_ref: str | None = Field(default=None, max_length=120)
    severity: str = Field(default="Amber", pattern="^(Amber|Red)$")
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=10000)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str = Field(min_length=3, max_length=200)
    note: str | None = Field(default=None, max_length=4000)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=4000)


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: str = Field(pattern=_DISPOSITIONS)
    note: str = Field(min_length=1, max_length=4000)


class AutoEscalateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)
    workflow_id: str | None = Field(default=None, max_length=200)


def _serialize(c: EwsCase) -> dict[str, Any]:
    return {"id": str(c.id), "entity_id": str(c.entity_id),
            "deal_id": str(c.deal_id) if c.deal_id else None,
            "source": c.source, "source_ref": c.source_ref, "severity": c.severity,
            "title": c.title, "summary": c.summary, "status": c.status,
            "opened_by": c.opened_by, "assigned_to": c.assigned_to,
            "assigned_at": c.assigned_at.isoformat() if c.assigned_at else None,
            "investigation_note": c.investigation_note,
            "escalated_by": c.escalated_by,
            "escalated_at": c.escalated_at.isoformat() if c.escalated_at else None,
            "escalation_note": c.escalation_note, "disposition": c.disposition,
            "closure_note": c.closure_note, "closed_by": c.closed_by,
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "workflow_id": c.workflow_id, "version": c.version,
            "created_at": c.created_at.isoformat() if c.created_at else None}


async def _case(ctx: RequestContext, case_id: uuid.UUID) -> EwsCase:
    row = (await ctx.session.execute(
        select(EwsCase).where(
            EwsCase.tenant_id == ctx.tenant_id, EwsCase.id == case_id,
            EwsCase.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No EWS case '{case_id}'.")
    return row


def _actor_email(ctx: RequestContext) -> str:
    return (ctx.user.email if ctx.user is not None and ctx.user.email else ctx.actor)


def _is_senior(ctx: RequestContext) -> bool:
    return ctx.user is not None and bool(set(ctx.user.roles or []) & _SENIOR)


def _require_open(row: EwsCase) -> None:
    if row.status == "Closed":
        raise ConflictError("EWS case is Closed and frozen.")


def _audit(ctx: RequestContext, action: str, row: EwsCase, changes: dict) -> None:
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action=action,
        resource_type="ews_cases", resource_id=str(row.id),
        request_id=request_id_ctx.get(), changes=changes))


@router.post("/v1/ews-cases", tags=["EWS"], status_code=201,
             summary="Open an EWS case (idempotent per trigger source)")
async def open_case(payload: CaseIn,
                    ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", payload.entity_id)
    if await load_subject(ctx.session, ctx.tenant_id, "Entity", payload.entity_id) is None:
        raise NotFoundError(f"Entity '{payload.entity_id}' not found.")
    source_ref = payload.source_ref or f"manual-{uuid.uuid4().hex[:12]}"
    won = (await ctx.session.execute(
        pg_insert(EwsCase).values(
            tenant_id=ctx.tenant_id, entity_id=payload.entity_id,
            deal_id=payload.deal_id, source=payload.source, source_ref=source_ref,
            severity=payload.severity, title=payload.title, summary=payload.summary,
            status="Open", opened_by=_actor_email(ctx), created_by=ctx.actor)
        .on_conflict_do_nothing(constraint="ews_cases_source_dedupe")
        .returning(EwsCase.id))).scalar_one_or_none()
    if won is None:
        existing = (await ctx.session.execute(
            select(EwsCase).where(
                EwsCase.tenant_id == ctx.tenant_id, EwsCase.source == payload.source,
                EwsCase.source_ref == source_ref))).scalar_one()
        return _serialize(existing)     # the trigger already has its case
    row = await _case(ctx, won)
    _audit(ctx, "ews.open", row, {"source": payload.source, "source_ref": source_ref,
                                  "severity": payload.severity})
    return _serialize(row)


@router.get("/v1/ews-cases", tags=["EWS"], summary="The EWS case register")
async def list_cases(ctx: RequestContext = Depends(get_context),
                     entity_id: uuid.UUID | None = None,
                     deal_id: uuid.UUID | None = None,
                     status: str | None = Query(default=None),
                     severity: str | None = Query(default=None),
                     open_only: bool = Query(default=False),
                     limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    await _ensure_subject_scope(ctx, "manage_ews", None, None)
    conds = [EwsCase.tenant_id == ctx.tenant_id, EwsCase.deleted_at.is_(None)]
    if entity_id is not None:
        conds.append(EwsCase.entity_id == entity_id)
    if deal_id is not None:
        conds.append(EwsCase.deal_id == deal_id)
    if status:
        conds.append(EwsCase.status == status)
    if severity:
        conds.append(EwsCase.severity == severity)
    if open_only:
        conds.append(EwsCase.status != "Closed")
    rows = list((await ctx.session.execute(
        select(EwsCase).where(*conds)
        .order_by(EwsCase.created_at.desc()).limit(limit))).scalars())
    return {"items": [_serialize(r) for r in rows]}


@router.get("/v1/ews-cases/{case_id}", tags=["EWS"], summary="One EWS case")
async def get_case(case_id: uuid.UUID,
                   ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _case(ctx, case_id)
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", row.entity_id)
    return _serialize(row)


@router.post("/v1/ews-cases/{case_id}/assign", tags=["EWS"],
             summary="Assign an investigator (→ UnderInvestigation)")
async def assign_case(case_id: uuid.UUID, payload: AssignIn,
                      ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _case(ctx, case_id)
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", row.entity_id)
    _require_open(row)
    row.assigned_to = payload.assignee
    row.assigned_at = datetime.now(UTC)
    if row.status == "Open":
        row.status = "UnderInvestigation"
    if payload.note:
        row.investigation_note = _append_note(row.investigation_note,
                                              _actor_email(ctx), payload.note)
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    _audit(ctx, "ews.assign", row, {"assignee": payload.assignee})
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


@router.post("/v1/ews-cases/{case_id}/note", tags=["EWS"],
             summary="Append an investigation note")
async def add_note(case_id: uuid.UUID, payload: NoteIn,
                   ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _case(ctx, case_id)
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", row.entity_id)
    _require_open(row)
    row.investigation_note = _append_note(row.investigation_note,
                                          _actor_email(ctx), payload.note)
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


def _append_note(existing: str | None, who: str, note: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp} {who}] {note}"
    return f"{existing}\n{line}" if existing else line


@router.post("/v1/ews-cases/{case_id}/escalate", tags=["EWS"],
             summary="Escalate a case (reasons mandatory)")
async def escalate_case(case_id: uuid.UUID, payload: NoteIn,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _case(ctx, case_id)
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", row.entity_id)
    _require_open(row)
    if row.status == "Escalated":
        raise ConflictError("EWS case is already Escalated.")
    row.status = "Escalated"
    row.escalated_by = _actor_email(ctx)
    row.escalated_at = datetime.now(UTC)
    row.escalation_note = payload.note
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    _audit(ctx, "ews.escalate", row, {"by": row.escalated_by, "note": payload.note})
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


@router.post("/v1/ews-cases/{case_id}/close", tags=["EWS"],
             summary="Close a case (disposition + note mandatory)")
async def close_case(case_id: uuid.UUID, payload: CloseIn,
                     ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _case(ctx, case_id)
    await _ensure_subject_scope(ctx, "manage_ews", "Entity", row.entity_id)
    _require_open(row)
    # An escalated case was raised ABOVE the working level — only senior credit
    # authority may close it (it can never be quietly buried by the person it
    # escalated past). Non-escalated cases close by the assignee or a senior.
    email = _actor_email(ctx)
    if row.status == "Escalated":
        if not _is_senior(ctx):
            raise ForbiddenError(
                "Closing an ESCALATED case requires senior credit authority "
                f"(one of {sorted(_SENIOR)}).")
    elif not (_is_senior(ctx) or (row.assigned_to and row.assigned_to == email)):
        raise ForbiddenError(
            "Only the assigned investigator (or senior credit authority) may close "
            "this case.")
    row.status = "Closed"
    row.disposition = payload.disposition
    row.closure_note = payload.note
    row.closed_by = email
    row.closed_at = datetime.now(UTC)
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    _audit(ctx, "ews.close", row, {"disposition": payload.disposition, "by": email,
                                   "note": payload.note})
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)


@router.post("/v1/internal/ews-cases/{case_id}/auto-escalate", tags=["Internal"],
             summary="Service: escalate a case whose investigation SLA lapsed")
async def auto_escalate(case_id: uuid.UUID, payload: AutoEscalateIn,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError(
            "Auto-escalation is the workflow service principal's plumbing only.")
    row = await _case(ctx, case_id)
    if row.status == "Closed":
        return _serialize(row)          # raced a human closure — nothing to escalate
    if row.status == "Escalated":
        return _serialize(row)          # idempotent replay
    row.status = "Escalated"
    row.escalated_by = "system:sla"
    row.escalated_at = datetime.now(UTC)
    row.escalation_note = payload.reason
    if payload.workflow_id:
        row.workflow_id = payload.workflow_id
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    _audit(ctx, "ews.auto_escalate", row, {"reason": payload.reason,
                                           "workflow_id": payload.workflow_id})
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _serialize(row)
