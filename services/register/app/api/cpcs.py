"""The authoritative CP/CS checklist — maker-checker completion that MINTS ``cp_cs_completion``.

Before this, ``cp_cs_completion`` evidence was caller-attached: whoever could attach evidence could
assert CP/CS was done. Now a maker PREPARES/completes a checklist and a DIFFERENT checker APPROVES
it; only then can ``cp_cs_completion`` be filed (``evidence.py`` verifies it against the approved
checklist). This router owns that lifecycle.

    POST /v1/internal/cpcs-checklists              prepare / complete a checklist (maker)
    POST /v1/internal/cpcs-checklists/{id}/approve approve it (checker; must differ from the maker)
    GET  /v1/internal/cpcs-checklists/{id}         read it
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.cpcs import CpcsChecklist

router = api_router()

# Only senior credit authority may WAIVE a condition or DEFER a CP as a CS obligation.
_WAIVER_AUTHORITY = {"Credit Head", "Management", "Admin"}


class ChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=300)
    # CP = condition precedent (must be met before disbursement); CS = condition subsequent (an
    # obligation after). A CP may be 'Deferred as CS' with an expiry, converting it to an obligation.
    condition_type: str = Field(pattern="^(CP|CS)$")
    required: bool = True
    status: str = Field(default="Pending", pattern="^(Pending|Completed|Waived|Deferred as CS)$")
    # Governance for a Waived / Deferred item — reason is mandatory; expiry is mandatory for a
    # deferral (and any time-bound waiver); supporting evidence is an optional reference.
    reason: str | None = Field(default=None, max_length=1000)
    expiry_date: date | None = None
    evidence_ref: str | None = Field(default=None, max_length=300)
    note: str | None = None


class ChecklistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(min_length=1, max_length=64)
    deal_id: str | None = Field(default=None, max_length=64)
    checklist_version: int = Field(default=1, ge=1)
    items: list[ChecklistItem] = Field(min_length=1)
    # 'Draft' keeps it editable; 'Completed' asserts the maker has finished it (ready for approval).
    status: str = Field(default="Completed", pattern="^(Draft|Completed)$")
    note: str | None = None


def _actor_id(ctx: RequestContext) -> str | None:
    return str(ctx.user.id) if ctx.user else None


def _validate_items(ctx: RequestContext, payload: ChecklistIn) -> None:
    """Per-item governance: distinguish CP/CS, require ≥1 item, and enforce waiver/CS-deferment
    controls (authority, reason, expiry). Raises on the first violation."""
    waived_or_deferred = [i for i in payload.items if i.status in ("Waived", "Deferred as CS")]
    # Waiver / deferral authority — only senior credit authority may do it (humans; a delegated
    # service acts under the authority the orchestrator already verified).
    if waived_or_deferred and ctx.user is not None and not (
            set(ctx.user.roles or []) & _WAIVER_AUTHORITY):
        raise ForbiddenError(
            "Only Credit Head / Management / Admin may waive or defer a CP/CS condition.")
    for i in payload.items:
        if i.status == "Waived" and not (i.reason and i.reason.strip()):
            raise ValidationAppError(
                f"Waiving condition {i.key!r} requires a reason.")
        if i.status == "Deferred as CS":
            if i.condition_type != "CP":
                raise ValidationAppError(
                    f"Only a CP may be 'Deferred as CS' (condition {i.key!r} is {i.condition_type}).")
            if not (i.reason and i.reason.strip()):
                raise ValidationAppError(
                    f"Deferring condition {i.key!r} as a CS requires a reason.")
            if i.expiry_date is None:
                raise ValidationAppError(
                    f"Deferring condition {i.key!r} as a CS requires an expiry_date.")
    # A 'Completed' checklist may not leave a REQUIRED CP outstanding (Pending or unmet).
    if payload.status == "Completed":
        outstanding = [i.key for i in payload.items
                       if i.required and i.condition_type == "CP" and i.status == "Pending"]
        if outstanding:
            raise ValidationAppError(
                f"Cannot complete the CP/CS checklist — required CP items still pending: "
                f"{outstanding}.")


def _serialize(row: CpcsChecklist) -> dict[str, Any]:
    return {
        "id": str(row.id), "lending_id": row.lending_id, "deal_id": row.deal_id,
        "checklist_version": row.checklist_version, "status": row.status, "items": row.items or [],
        "prepared_by": row.prepared_by, "prepared_by_id": row.prepared_by_id,
        "approved_by": row.approved_by, "approved_by_id": row.approved_by_id,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/v1/internal/cpcs-checklists", tags=["Internal"],
            summary="List CP/CS checklists (the checker's queue: ?status=Prepared)")
async def list_checklists(ctx: RequestContext = Depends(get_context),
                          lending_id: str | None = Query(default=None),
                          status: str | None = Query(default=None),
                          limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    """How a CHECKER discovers work: ``?status=Prepared`` is everything awaiting a
    check, newest first — regardless of whether the maker prepared it through the
    orchestrator lane or this register surface. ``lending_id`` narrows to one line
    (all its versions)."""
    conds = [CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.deleted_at.is_(None)]
    if lending_id:
        conds.append(CpcsChecklist.lending_id == lending_id)
    if status:
        conds.append(CpcsChecklist.status == status)
    rows = (await ctx.session.execute(
        select(CpcsChecklist).where(*conds)
        .order_by(CpcsChecklist.created_at.desc()).limit(limit))).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/v1/internal/cpcs-checklists", tags=["Internal"], status_code=201,
             summary="Prepare / complete the CP/CS checklist (maker)")
async def create_checklist(payload: ChecklistIn,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "prepare_cpcs_checklist")
    _validate_items(ctx, payload)
    items = [i.model_dump(mode="json") for i in payload.items]
    won = (await ctx.session.execute(
        pg_insert(CpcsChecklist).values(
            tenant_id=ctx.tenant_id, lending_id=payload.lending_id, deal_id=payload.deal_id,
            checklist_version=payload.checklist_version, items=items, status=payload.status,
            prepared_by=ctx.actor, prepared_by_id=_actor_id(ctx), note=payload.note,
            created_by=ctx.actor)
        .on_conflict_do_nothing(constraint="cp_cs_checklists_tenant_lending_version")
        .returning(CpcsChecklist.id))).scalar_one_or_none()
    if won is None:
        raise ConflictError(
            f"A CP/CS checklist v{payload.checklist_version} already exists for Lending "
            f"{payload.lending_id!r}; open a new version.")
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cpcs.prepare",
        resource_type="cp_cs_checklists", resource_id=str(won),
        request_id=request_id_ctx.get(),
        changes={"lending_id": payload.lending_id, "checklist_version": payload.checklist_version,
                 "status": payload.status}))
    row = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.id == won))).scalar_one()
    return _serialize(row)


@router.post("/v1/internal/cpcs-checklists/{checklist_id}/approve", tags=["Internal"],
             summary="Approve the CP/CS checklist (checker; must differ from the maker)")
async def approve_checklist(checklist_id: str,
                            ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_cpcs_checklist")
    try:
        cid = uuid.UUID(checklist_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("checklist_id must be a valid id.") from None
    row = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.id == cid,
        CpcsChecklist.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No CP/CS checklist {checklist_id!r}.")
    if row.status != "Completed":
        raise ConflictError(
            f"The CP/CS checklist is {row.status!r}; only a 'Completed' checklist can be approved.")
    checker_id = _actor_id(ctx)
    # Maker-checker: the approver must be a DIFFERENT person than the preparer.
    if checker_id is not None and checker_id == row.prepared_by_id:
        raise ValidationAppError(
            "The CP/CS checklist must be approved by a DIFFERENT checker than its preparer.")
    if checker_id is None and ctx.actor == row.prepared_by:
        raise ValidationAppError(
            "The CP/CS checklist must be approved by a DIFFERENT checker than its preparer.")
    row.status = "Approved"
    row.approved_by = ctx.actor
    row.approved_by_id = checker_id
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cpcs.approve",
        resource_type="cp_cs_checklists", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": row.lending_id, "checklist_version": row.checklist_version,
                 "status": "Approved"}))
    return _serialize(row)


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A return without a reason is useless to the maker — the note is mandatory.
    note: str = Field(min_length=1, max_length=2000)


@router.post("/v1/internal/cpcs-checklists/{checklist_id}/return", tags=["Internal"],
             summary="Return the CP/CS checklist to its maker (checker; with reasons)")
async def return_checklist(checklist_id: str, payload: ReturnIn,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """RETURN-TO-MAKER: the checker sends a Completed checklist back for amendment instead
    of approving or rejecting it. The row becomes 'Returned' (immutable reasons in the
    audit trail + note); the maker AMENDS by submitting the NEXT checklist_version — the
    returned version stays on the record untouched, so every iteration of the checklist is
    permanently reviewable."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_cpcs_checklist")   # checker authority, like approve
    try:
        cid = uuid.UUID(checklist_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("checklist_id must be a valid id.") from None
    row = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.id == cid,
        CpcsChecklist.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No CP/CS checklist {checklist_id!r}.")
    if row.status != "Completed":
        raise ConflictError(
            f"The CP/CS checklist is {row.status!r}; only a 'Completed' checklist can be "
            "returned to its maker.")
    checker_id = _actor_id(ctx)
    if (checker_id is not None and checker_id == row.prepared_by_id) or (
            checker_id is None and ctx.actor == row.prepared_by):
        raise ValidationAppError(
            "The CP/CS checklist must be returned by a DIFFERENT checker than its preparer.")
    row.status = "Returned"
    row.note = payload.note
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cpcs.return",
        resource_type="cp_cs_checklists", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": row.lending_id, "checklist_version": row.checklist_version,
                 "status": "Returned", "note": payload.note}))
    return _serialize(row)


@router.get("/v1/internal/cpcs-checklists/{checklist_id}", tags=["Internal"],
            summary="Read a CP/CS checklist")
async def get_checklist(checklist_id: str,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    try:
        cid = uuid.UUID(checklist_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("checklist_id must be a valid id.") from None
    row = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.id == cid,
        CpcsChecklist.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No CP/CS checklist {checklist_id!r}.")
    return _serialize(row)
