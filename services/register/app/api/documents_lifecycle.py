"""Document lifecycle — validation, rejection, replacement and expiry (increment 7).

The catalog (``documents``) records what is ON FILE; this module gives each record a
verifiable lifecycle:

    On File / Pending ──▶ Verified     a DIFFERENT checker validated it (maker≠checker)
                     └──▶ Rejected     with a MANDATORY reason
    any live status  ───▶ Superseded   replaced by a successor row (chain kept)
    On File / Pending / Verified ─▶ Expired   the validity window (expires_on) lapsed

Rules, mirroring the platform's maker–checker conventions:

* **Maker ≠ checker** — the uploader can neither verify nor reject their own document.
* **Rejection needs reasons** — a rejection without a note is refused; the note lands on
  the record (``status_note``) and in the audit log.
* **Replacement keeps the chain** — the new document is a NEW row (same subject + slot);
  the old row becomes ``Superseded`` with ``superseded_by`` → the successor. Nothing is
  ever edited in place or deleted.
* **Expiry is observed, not asserted** — the internal sweep (service-principal only,
  driven by the workflows service's ``DocumentExpiryMonitorWorkflow``) marks lapsed
  documents ``Expired`` and reports both the newly expired and the soon-to-expire set,
  idempotently, so the caller can notify the owners.

Lifecycle statuses can ONLY be reached here: the generic create/update payloads refuse
them (see ``_direct_document_status`` in the schemas).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.api.custom import _ensure_subject_scope
from app.authz.engine import service_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.documents import Document
from app.repositories.documents import register_document
from app.schemas import resources as s

router = api_router()

# Statuses a checker can act on / that can still expire.
_CHECKABLE = {"On File", "Pending"}
_EXPIRABLE = {"On File", "Pending", "Verified"}
_REPLACEABLE = {"On File", "Pending", "Verified", "Rejected", "Expired", "Waived"}

_SWEEP_SERVICES = {"svc_workflows"}


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=4000)
    # The checker fixes the validity window at verification time (an insurance policy's
    # end date, a sanction letter's validity…). Optional — not every document expires.
    expires_on: date | None = None


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=4000)   # reasons are MANDATORY


class SweepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Documents expiring within this window are reported (not yet marked) so owners can
    # be warned before the lapse.
    warn_days: int = Field(default=7, ge=0, le=365)
    limit: int = Field(default=500, ge=1, le=2000)


def _checker(ctx: RequestContext) -> str:
    if ctx.user is None or not ctx.user.email:
        raise ForbiddenError(
            "Document validation/rejection is a human checker action — a verified user "
            "identity is required.")
    return ctx.user.email


async def _document(ctx: RequestContext, doc_id: uuid.UUID) -> Document:
    row = (await ctx.session.execute(
        select(Document).where(
            Document.tenant_id == ctx.tenant_id, Document.id == doc_id,
            Document.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No document '{doc_id}'.")
    return row


def _read(row: Document) -> Any:
    return s.DocumentRead.model_validate(row)


async def document_pre_delete(ctx: RequestContext, doc_id: uuid.UUID) -> None:
    """Removal guard: a VERIFIED document is standing evidence — the desk that put
    it on file cannot quietly take it back. Reject it first (with the reason the
    record deserves), or an Admin removes it. Un-verified files (On File / Pending /
    Rejected / Expired) are normal desk housekeeping under upload_remove_documents."""
    row = await _document(ctx, doc_id)
    roles = set(ctx.user.roles) if ctx.user is not None else set()
    if row.status == "Verified" and "Admin" not in roles:
        from app.core.errors import ValidationAppError
        raise ValidationAppError(
            "This document is Verified — evidence stays put. Reject it first "
            "(with a note saying why), or ask an Admin to remove it.")


@router.post("/v1/documents/{doc_id}/validate", response_model=s.DocumentRead,
             tags=["Documents"], summary="Verify a document (checker ≠ uploader)")
async def validate_document(doc_id: uuid.UUID, payload: ValidateIn,
                            ctx: RequestContext = Depends(get_context)) -> Any:
    checker = _checker(ctx)
    row = await _document(ctx, doc_id)
    await _ensure_subject_scope(ctx, "upload_remove_documents",
                                row.subject_type, row.subject_id)
    if row.uploaded_by and row.uploaded_by == checker:
        raise ValidationAppError(
            "A document must be verified by a DIFFERENT checker than its uploader "
            "(maker–checker).")
    if row.status not in _CHECKABLE:
        raise ConflictError(
            f"Document is {row.status!r}; only {sorted(_CHECKABLE)} can be verified.")
    row.status = "Verified"
    row.verified_by = checker
    row.verified_at = datetime.now(UTC)
    if payload.note:
        row.status_note = payload.note
    if payload.expires_on is not None:
        row.expires_on = payload.expires_on
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="document.validate",
        resource_type="documents", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"by": checker, "note": payload.note,
                 "expires_on": payload.expires_on.isoformat() if payload.expires_on else None}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _read(row)


@router.post("/v1/documents/{doc_id}/reject", response_model=s.DocumentRead,
             tags=["Documents"],
             summary="Reject a document (checker ≠ uploader, reasons mandatory)")
async def reject_document(doc_id: uuid.UUID, payload: RejectIn,
                          ctx: RequestContext = Depends(get_context)) -> Any:
    checker = _checker(ctx)
    row = await _document(ctx, doc_id)
    await _ensure_subject_scope(ctx, "upload_remove_documents",
                                row.subject_type, row.subject_id)
    if row.uploaded_by and row.uploaded_by == checker:
        raise ValidationAppError(
            "A document must be rejected by a DIFFERENT checker than its uploader "
            "(maker–checker).")
    if row.status not in _CHECKABLE:
        raise ConflictError(
            f"Document is {row.status!r}; only {sorted(_CHECKABLE)} can be rejected.")
    row.status = "Rejected"
    row.status_note = payload.note
    row.updated_by = ctx.actor
    row.version = (row.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="document.reject",
        resource_type="documents", resource_id=str(row.id),
        request_id=request_id_ctx.get(), changes={"by": checker, "note": payload.note}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _read(row)


@router.post("/v1/documents/{doc_id}/replace", response_model=s.DocumentRead,
             status_code=201, tags=["Documents"],
             summary="Replace a document (old → Superseded, chained to the successor)")
async def replace_document(doc_id: uuid.UUID, payload: s.DocumentCreate,
                           ctx: RequestContext = Depends(get_context)) -> Any:
    old = await _document(ctx, doc_id)
    await _ensure_subject_scope(ctx, "upload_remove_documents",
                                old.subject_type, old.subject_id)
    if old.status == "Superseded":
        raise ConflictError(
            "Document is already Superseded; replace its successor instead "
            f"(superseded_by={old.superseded_by}).")
    if old.status not in _REPLACEABLE:
        raise ConflictError(f"Document is {old.status!r} and cannot be replaced.")
    data = payload.model_dump(exclude_unset=False)
    # The replacement inherits the slot identity — it answers the SAME checklist slot.
    data["subject_type"] = old.subject_type
    data["subject_id"] = old.subject_id
    data["section"] = data.get("section") or old.section
    data["slot_key"] = data.get("slot_key") or old.slot_key
    data["doc_type"] = data.get("doc_type") or old.doc_type
    data["is_required"] = old.is_required
    new = await register_document(ctx.session, ctx.tenant_id, ctx.actor, data)
    old.status = "Superseded"
    old.superseded_by = new.id
    if payload.notes:
        old.status_note = payload.notes
    old.updated_by = ctx.actor
    old.version = (old.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="document.replace",
        resource_type="documents", resource_id=str(old.id),
        request_id=request_id_ctx.get(),
        changes={"superseded_by": str(new.id), "slot_key": old.slot_key}))
    await ctx.session.flush()
    await ctx.session.refresh(new)
    return _read(new)


@router.post("/v1/internal/documents/expiry-sweep", tags=["Internal"],
             summary="Mark lapsed documents Expired; report the expiring set (idempotent)")
async def expiry_sweep(payload: SweepIn,
                       ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    if service_ctx.get() not in _SWEEP_SERVICES:
        raise ForbiddenError(
            "The document expiry sweep runs under the workflow service principal only.")
    today = datetime.now(UTC).date()
    # 1. Mark lapsed documents Expired (idempotent: an already-Expired row no longer
    #    matches the status filter, so a re-run returns it in neither list). The status
    #    set is a module constant inlined as literals — no user input in the SQL text.
    expirable = ", ".join(f"'{v}'" for v in sorted(_EXPIRABLE))
    expired_rows = (await ctx.session.execute(text(
        "UPDATE documents SET status = 'Expired', updated_by = :actor "  # noqa: S608
        "WHERE tenant_id = :tid AND deleted_at IS NULL AND expires_on < :today "
        f"AND status IN ({expirable}) "
        "RETURNING id, title, slot_key, subject_type, subject_id, entity_id, "
        "expires_on, uploaded_by"),
        {"actor": ctx.actor, "tid": str(ctx.tenant_id), "today": today})).mappings().all()
    for r in expired_rows:
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=ctx.actor, action="document.expire",
            resource_type="documents", resource_id=str(r["id"]),
            request_id=request_id_ctx.get(),
            changes={"expires_on": r["expires_on"].isoformat(), "title": r["title"]}))
    # 2. Report (don't touch) the soon-to-expire set so owners can be warned.
    expiring_rows = (await ctx.session.execute(text(
        "SELECT id, title, slot_key, subject_type, subject_id, entity_id, "  # noqa: S608
        "expires_on, uploaded_by FROM documents "
        "WHERE tenant_id = :tid AND deleted_at IS NULL "
        "AND expires_on >= :today AND expires_on <= :horizon "
        f"AND status IN ({expirable}) "
        "ORDER BY expires_on LIMIT :lim"),
        {"tid": str(ctx.tenant_id), "today": today,
         "horizon": today + timedelta(days=payload.warn_days),
         "lim": payload.limit})).mappings().all()

    def _dump(r: Any) -> dict[str, Any]:
        return {"id": str(r["id"]), "title": r["title"], "slot_key": r["slot_key"],
                "subject_type": r["subject_type"], "subject_id": str(r["subject_id"]),
                "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
                "expires_on": r["expires_on"].isoformat(),
                "uploaded_by": r["uploaded_by"]}

    return {"swept_on": today.isoformat(),
            "expired": [_dump(r) for r in expired_rows],
            "expiring": [_dump(r) for r in expiring_rows]}
