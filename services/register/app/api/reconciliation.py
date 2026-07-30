"""Import-reconciliation Admin endpoints.

A governed import may RETAIN a row whose terminal stage is missing mandatory data; each such row
opens a durable ``import_reconciliation_items`` record (``status='Required'``) and flags the
subject ``reconciliation_status='Required'`` so operational reads can exclude it. These endpoints
are the Admin workflow to list, assign, and resolve those items — resolution is audited and the
ORIGINAL imported values are preserved on the item.

    GET  /v1/reconciliation                 list items (optionally filtered by status)
    POST /v1/reconciliation/{id}/assign     assign an owner
    POST /v1/reconciliation/{id}/resolve    close it (Resolved/Waived), clearing the subject flag
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from evam_backend_core import policy
from fastapi import Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.reconciliation import ImportReconciliationItem
from app.repositories.subjects import SUBJECTS

router = api_router(prefix="/v1/reconciliation", tags=["Reconciliation"])


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=120)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(default="Resolved", pattern="^(Resolved|Waived)$")
    note: str = Field(min_length=1, max_length=2000)
    # Waived is a break-glass outcome — it keeps the record incomplete on purpose, so it requires
    # a ticket reference (the note is the reason). There is deliberately NO inline field-correction
    # here: the record is corrected through its normal, policy-enforcing update API (which applies
    # the update schema, the full policy engine, field/row locks, optimistic locking and history),
    # and resolution merely VERIFIES the fix — so reconciliation can never become a write bypass.
    ticket: str | None = Field(default=None, max_length=200)


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    # Decimal / other non-JSON scalars → string; leave JSON-native types untouched.
    return v if v is None or isinstance(v, str | int | float | bool | list | dict) else str(v)


def _require_authority(ctx: RequestContext) -> None:
    """Reconciliation is an Admin or Management (senior) operation — never a service."""
    if ctx.user is None or not (ctx.user.is_admin or "Management" in ctx.user.roles):
        raise ForbiddenError("Import reconciliation requires an Admin or Management identity.")


def _require_waive_authority(ctx: RequestContext) -> None:
    """A WAIVER keeps an incomplete record in the business of record, so it is reserved to the
    designated senior business authority (Management) — not any single Admin operator."""
    if ctx.user is None or "Management" not in ctx.user.roles:
        raise ForbiddenError(
            "Waiving an incomplete record is a senior business decision reserved to Management.")


def _check_if_match(row: ImportReconciliationItem, if_match: str | None) -> None:
    """Optimistic-concurrency guard: if the caller sent an If-Match version and it no longer
    matches the item, someone else changed it first — refuse so a stale close can't clobber."""
    if if_match is None:
        return
    want = if_match.strip().strip('"')
    if want.isdigit() and int(want) != row.version:
        raise ConflictError(
            f"Reconciliation item was modified (current version {row.version}); refetch and retry.")


def _serialize(row: ImportReconciliationItem) -> dict:
    return {
        "id": str(row.id), "version": row.version,
        "import_batch_id": row.import_batch_id, "checksum": row.checksum,
        "subject_type": row.subject_type,
        "subject_id": str(row.subject_id) if row.subject_id else None,
        "sheet": row.sheet, "company": row.company, "stage_field": row.stage_field,
        "stage_value": row.stage_value, "missing_fields": list(row.missing_fields or []),
        "original_values": dict(row.original_values or {}), "status": row.status,
        "owner": row.owner, "resolution_note": row.resolution_note,
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("", summary="List import-reconciliation items (Admin)")
async def list_items(ctx: RequestContext = Depends(get_context),
                     status: str | None = Query(default=None,
                                                 pattern="^(Required|Resolved|Waived)$"),
                     ) -> dict[str, Any]:
    _require_authority(ctx)
    conds: list[Any] = [ImportReconciliationItem.deleted_at.is_(None)]
    if status is not None:
        conds.append(ImportReconciliationItem.status == status)
    rows = (await ctx.session.execute(
        select(ImportReconciliationItem).where(*conds)
        .order_by(ImportReconciliationItem.created_at.desc()))).scalars().all()
    return {"items": [_serialize(r) for r in rows]}


async def _load(ctx: RequestContext, item_id: str,
                for_update: bool = False) -> ImportReconciliationItem:
    stmt = select(ImportReconciliationItem).where(
        ImportReconciliationItem.id == item_id,
        ImportReconciliationItem.deleted_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    row = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Reconciliation item '{item_id}' not found.")
    return row


async def _lock_subject_items(ctx: RequestContext, subject_type: str, subject_id) -> None:  # noqa: ANN001
    """SERIALIZE reconciliation on one business record: lock every item for this
    (tenant, subject_type, subject_id) FOR UPDATE, so two admins closing DIFFERENT items on the
    same subject cannot both read the other as still-open and leave the flag wrong. The second
    transaction blocks here until the first commits, then recomputes against the settled set.

    Locks are taken in a DETERMINISTIC id order and this sweep is the FIRST lock a resolution
    acquires (it also covers the item being resolved) — so two concurrent resolutions queue on the
    same lowest-id row rather than each grabbing its own item first and then deadlocking on the
    other's."""
    if subject_id is None:
        return
    await ctx.session.execute(
        select(ImportReconciliationItem.id).where(
            ImportReconciliationItem.subject_type == subject_type,
            ImportReconciliationItem.subject_id == subject_id,
            ImportReconciliationItem.deleted_at.is_(None))
        .order_by(ImportReconciliationItem.id).with_for_update())


@router.post("/{item_id}/assign", summary="Assign a reconciliation item to an owner (Admin)")
async def assign_item(item_id: str, payload: AssignIn,
                      ctx: RequestContext = Depends(get_context),
                      if_match: str | None = Header(default=None, alias="If-Match"),
                      ) -> dict[str, Any]:
    _require_authority(ctx)
    row = await _load(ctx, item_id, for_update=True)
    _check_if_match(row, if_match)
    if row.status != "Required":
        raise ValidationAppError(f"Item is already {row.status.lower()}.")
    row.owner = payload.owner.strip()
    return _serialize(row)


@router.post("/{item_id}/resolve", summary="Resolve/waive a reconciliation item (Admin, audited)")
async def resolve_item(item_id: str, payload: ResolveIn,
                       ctx: RequestContext = Depends(get_context),
                       if_match: str | None = Header(default=None, alias="If-Match"),
                       ) -> dict[str, Any]:
    _require_authority(ctx)
    # A waiver keeps an incomplete record in the business of record — reserved to Management.
    if payload.status == "Waived":
        _require_waive_authority(ctx)
    # SERIALIZE every open item for this subject FIRST (deterministic id order — this sweep also
    # locks the item being resolved), THEN read the now-locked target. Acquiring the whole set
    # before the single item is what prevents two concurrent resolutions on the same subject from
    # each grabbing their own item first and deadlocking on the other's.
    head = await _load(ctx, item_id)  # unlocked read, only to discover the subject to serialize on
    await _lock_subject_items(ctx, head.subject_type, head.subject_id)
    row = await _load(ctx, item_id, for_update=True)  # fresh, locked, settled view of the target
    _check_if_match(row, if_match)
    if row.status != "Required":
        raise ValidationAppError(f"Item is already {row.status.lower()}.")
    reason = payload.note.strip()
    if not reason:
        raise ValidationAppError("A non-empty resolution note is required.")
    missing_fields = list(row.missing_fields or [])

    model = SUBJECTS.get(row.subject_type)
    subject = None
    if model is not None and row.subject_id is not None:
        subject = (await ctx.session.execute(
            select(model).where(model.id == row.subject_id)  # type: ignore[attr-defined]
            .with_for_update())).scalar_one_or_none()

    before: dict[str, Any] = ({f: _jsonable(getattr(subject, f, None)) for f in missing_fields}
              if subject else {})

    if payload.status == "Resolved":
        # RESOLUTION MUST PROVE THE DATA IS ACTUALLY CORRECT — a note alone is not enough, and the
        # correction itself must have gone through the record's normal, policy-enforcing update
        # API (this endpoint never writes business fields). Every field this item flagged missing
        # must now be present…
        if subject is None:
            raise ValidationAppError(
                "The subject record no longer exists; resolve as 'Waived' with a ticket instead.")
        still_missing = [f for f in missing_fields if getattr(subject, f, None) in (None, "")]
        if still_missing:
            raise ValidationAppError(
                "Cannot resolve while these fields are still missing on the record: "
                f"{still_missing}. Correct the record first via its normal update API.")
        # …and the COMPLETE shared policy engine must accept the record as it now stands (re-assert
        # the current stage as a no-op change, which runs transition + mandatory-field validation).
        stage_field = policy.stage_field_of(row.subject_type)
        if stage_field is not None:
            current = {c.name: getattr(subject, c.name) for c in subject.__table__.columns}
            cur_stage = getattr(subject, stage_field, None)
            if cur_stage is not None:
                violation = policy.check_write(row.subject_type, current=current,
                                               changes={stage_field: cur_stage}, roles=None)
                if violation is not None:
                    raise ValidationAppError(f"Cannot resolve: {violation.message}")
    else:  # Waived — an explicit break-glass keeping an incomplete record, so it needs a ticket.
        if not (payload.ticket and payload.ticket.strip()):
            raise ValidationAppError(
                "A waiver keeps an incomplete record; it requires a ticket reference.")

    row.status = payload.status
    row.resolution_note = reason
    row.resolved_by = ctx.user.email  # type: ignore[union-attr]
    row.resolved_at = datetime.now(UTC)
    after: dict[str, Any] = ({f: _jsonable(getattr(subject, f, None)) for f in missing_fields}
             if subject else {})

    # Recompute the subject's operational flag from ALL its items (matched by subject_type + id —
    # ids belong to separate tables). 'Required' dominates (any open issue keeps it hidden); else a
    # 'Waived' item keeps it visibly 'Waived' (a deliberate exception, NOT fully reconciled); else
    # every issue is Resolved and the flag clears.
    if subject is not None and hasattr(subject, "reconciliation_status"):
        def _count(status: str):  # noqa: ANN202
            return select(func.count()).select_from(ImportReconciliationItem).where(
                ImportReconciliationItem.subject_type == row.subject_type,
                ImportReconciliationItem.subject_id == row.subject_id,
                ImportReconciliationItem.status == status,
                ImportReconciliationItem.id != row.id,
                ImportReconciliationItem.deleted_at.is_(None))
        remaining_required = (await ctx.session.execute(_count("Required"))).scalar_one()
        other_waived = (await ctx.session.execute(_count("Waived"))).scalar_one()
        if remaining_required > 0:
            subject.reconciliation_status = "Required"
        elif payload.status == "Waived" or other_waived > 0:
            subject.reconciliation_status = "Waived"
        else:
            subject.reconciliation_status = None

    # Immutable evidence: WHO resolved WHICH item, HOW, WHY, under which ticket — with the original
    # imported values and the BEFORE/AFTER of the corrected fields.
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.user.email, action="reconciliation.resolve",  # type: ignore[union-attr]
        resource_type="import_reconciliation_items", resource_id=item_id,
        request_id=request_id_ctx.get(),
        changes={"status": payload.status, "note": reason, "ticket": payload.ticket,
                 "by": ctx.user.email, "subject_type": row.subject_type,  # type: ignore[union-attr]
                 "subject_id": str(row.subject_id) if row.subject_id else None,
                 "missing_fields": missing_fields, "before": before, "after": after,
                 "original_values": dict(row.original_values or {}),
                 "import_batch_id": row.import_batch_id}))
    return _serialize(row)
