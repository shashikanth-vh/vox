"""Tranche-level disbursement callbacks + the BOOKING gate (LMS increment ⑥).

Advaya (or ops on its behalf, until the integration is live) reports each disbursement
TRANCHE against a Lending line. The record is:

* **idempotent** — replaying the same ``tranche_ref`` returns the original row unchanged;
  the same ref with a DIFFERENT amount is a 409 (a corrected figure is a NEW tranche with
  its own ref and a note — the tranche facts are append-only at the database);
* **bounded** — cumulative tranches (pending + booked) may never exceed the line's
  proposed disbursement amount (falling back to the facility amount); an
  over-disbursement is refused loudly rather than silently absorbed;
* **boundary-aware** — tranches are accepted only after Advaya ACCEPTED the handover
  package; it is the FIRST *booked* tranche (money actually moving) that advances the
  line to 'Disbursed' and opens the loan account — the line's actuals are written ONLY
  from booked tranches.

Two lanes, one maker/checker seam:

* **machine lane** (service keys — the real Advaya integration): the partner's system
  speaking IS the confirmation, so the tranche books directly — actuals, stage move,
  loan account, covenant stamping, all in the same transaction (unchanged behaviour).
* **human lane** (manual attestation in LOS, or the LMS recorder for later phases): a
  person relaying an offline confirmation records a PENDING BOOKING. Nothing moves
  until the LMS AUTHORIZER approves it — then the same settlement runs, attributed and
  four-eyed (the recorder can never approve their own booking). A rejection settles the
  row 'Rejected' with the reason; the corrected figure is a fresh recording.

    POST /v1/internal/lending/{lending_id}/tranches        machine lane: record + book
    GET  /v1/internal/lending/{lending_id}/tranches        list + totals (reconciliation)
    POST /v1/lending/{lending_id}/tranches                 human lane: record → Pending
    GET  /v1/lending/{lending_id}/tranches                 the schedule, as the UI shows it
    GET  /v1/bookings/pending                              the authorizer's queue
    POST /v1/lending/{lending_id}/tranches/{tranche_id}/book   approve | reject
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.authz.engine import service_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.advaya import DisbursementTranche
from app.models.trackers import LendingTracker

router = api_router()


class TrancheIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tranche_ref: str = Field(min_length=1, max_length=200)
    amount: float = Field(gt=0)
    disbursed_on: date | None = None
    advaya_reference: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


_ALLOWED_SERVICES = {"svc_workflows", "svc_advaya"}


def _require_service(ctx: RequestContext) -> None:  # noqa: ARG001 - signature parity
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError(
            "Disbursement tranches are recorded by the workflow/Advaya service principal "
            "only.")


def _serialize(row: DisbursementTranche) -> dict[str, Any]:
    return {"id": str(row.id), "lending_id": row.lending_id, "deal_id": row.deal_id,
            "tranche_ref": row.tranche_ref,
            "amount": float(row.amount) if row.amount is not None else None,
            "disbursed_on": row.disbursed_on.isoformat() if row.disbursed_on else None,
            "advaya_reference": row.advaya_reference, "note": row.note,
            "recorded_by": row.recorded_by,
            "booking_status": row.booking_status,
            "booked_by": row.booked_by,
            "booked_at": row.booked_at.isoformat() if row.booked_at else None,
            "booking_note": row.booking_note,
            "created_at": row.created_at.isoformat() if row.created_at else None}


async def _line(ctx: RequestContext, lending_id: str) -> LendingTracker:
    try:
        lid = uuid.UUID(lending_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("lending_id must be a valid id.") from None
    line = (await ctx.session.execute(select(LendingTracker).where(
        LendingTracker.tenant_id == ctx.tenant_id, LendingTracker.id == lid,
        LendingTracker.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if line is None:
        raise NotFoundError(f"No Lending line {lending_id!r}.")
    return line


async def _existing(ctx: RequestContext, lending_id: str) -> list[DisbursementTranche]:
    return list((await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.tenant_id == ctx.tenant_id,
        DisbursementTranche.lending_id == lending_id,
        DisbursementTranche.deleted_at.is_(None))
        .order_by(DisbursementTranche.created_at, DisbursementTranche.id))).scalars())


def _ceiling(line: LendingTracker) -> float | None:
    if line.proposed_disbursement_amount is not None:
        return float(line.proposed_disbursement_amount)
    if line.amount_cr is not None:
        return float(line.amount_cr)
    return None


@router.post("/v1/internal/lending/{lending_id}/tranches", tags=["Internal"],
             status_code=201, summary="Record one disbursement tranche (idempotent)")
async def record_tranche(lending_id: str, payload: TrancheIn,
                         ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service(ctx)
    return await apply_tranche(ctx, lending_id, payload)


async def _settle_booked(ctx: RequestContext, line: LendingTracker,
                         row: DisbursementTranche, source: str) -> None:
    """The money is REAL — the one settlement block, shared by the machine lane (books
    on arrival) and the authorizer's approval: actuals from booked tranches only, the
    first booked tranche moves the stage, and the loan account opens/grows."""
    rows = await _existing(ctx, str(line.id))
    booked_before = [r for r in rows
                     if r.booking_status == "Booked" and r.id != row.id]
    already = sum(float(r.amount) for r in booked_before)
    line.disbursed_amount = already + float(row.amount)
    if line.disbursement_date is None:
        line.disbursement_date = row.disbursed_on or date.today()
    if line.stage != "Disbursed":
        history = list(line.stage_history or [])
        history.append({"from": line.stage, "to": "Disbursed",
                        "source": source,
                        "tranche_ref": row.tranche_ref, "by": ctx.actor})
        line.stage = "Disbursed"
        line.stage_history = history
    line.updated_by = ctx.actor
    # The LMS follows the money: the FIRST booked tranche opens the loan account
    # (header from the sanction terms), each further one raises its principal, and the
    # statement gets its "Loan Disbursement" row.
    from app.api.lms import open_or_grow_account

    await open_or_grow_account(ctx, line, float(row.amount), row.disbursed_on,
                               tranche_no=len(booked_before) + 1,
                               tranche_ref=row.tranche_ref)


async def apply_tranche(ctx: RequestContext, lending_id: str, payload: TrancheIn,
                        source: str | None = None,
                        require_booking: bool = False) -> dict[str, Any]:
    """The ONE tranche-recording path — shared by the machine lane (Advaya's callbacks,
    ``require_booking=False``: books directly) and the human lanes (manual attestation /
    the LMS recorder, ``require_booking=True``: lands Pending for the LMS Authorizer).
    Identical guards and ceilings; ``source`` marks the provenance."""
    line = await _line(ctx, lending_id)
    # Tranches are ADVAYA's disbursement evidence. They are recorded only after Advaya
    # ACCEPTED the handover (PRISM's workflow boundary), and it is the FIRST BOOKED
    # tranche — money actually moving — that advances the line to 'Disbursed', never a
    # PRISM approval. Anything earlier is a sequencing bug upstream and is refused.
    from app.models.advaya import AdvayaHandoverPackage
    pkg = (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lending_id,
        AdvayaHandoverPackage.deleted_at.is_(None)))).scalar_one_or_none()
    accepted = pkg is not None and pkg.status in ("Accepted", "HandedOver")
    if not accepted:
        raise ConflictError(
            f"Handover package is {pkg.status if pkg else 'absent'!r}; disbursement "
            "tranches are recorded only after Advaya ACCEPTED the handover.")
    if line.stage not in ("Ready for Disbursement", "Disbursed"):
        raise ConflictError(
            f"Lending line is {line.stage!r}; disbursement tranches apply only to a "
            "line at 'Ready for Disbursement' (first tranche) or 'Disbursed'.")
    rows = await _existing(ctx, lending_id)
    prior = next((r for r in rows if r.tranche_ref == payload.tranche_ref), None)
    if prior is not None:
        if float(prior.amount) == payload.amount:
            return _serialize(prior)                      # idempotent replay
        raise ConflictError(
            f"Tranche {payload.tranche_ref!r} is already recorded with amount "
            f"{float(prior.amount)}; a corrected figure needs a NEW tranche_ref (the "
            "record is append-only).")
    # The ceiling counts everything still standing — booked money AND pending bookings —
    # so two makers cannot queue more than the line allows. A rejection frees its slice.
    ceiling = _ceiling(line)
    standing = [r for r in rows if r.booking_status != "Rejected"]
    committed = sum(float(r.amount) for r in standing)
    if ceiling is not None and committed + payload.amount > ceiling + 1e-9:
        raise ValidationAppError(
            f"Cumulative disbursement {committed + payload.amount} would exceed the "
            f"line's ceiling {ceiling} (proposed disbursement / facility amount); "
            "over-disbursement is refused.")
    booking_status = "Pending" if require_booking else "Booked"
    won = (await ctx.session.execute(
        pg_insert(DisbursementTranche).values(
            tenant_id=ctx.tenant_id, lending_id=lending_id,
            deal_id=str(line.deal_id) if line.deal_id else None,
            tranche_ref=payload.tranche_ref, amount=payload.amount,
            disbursed_on=payload.disbursed_on, advaya_reference=payload.advaya_reference,
            note=payload.note, recorded_by=ctx.actor, created_by=ctx.actor,
            booking_status=booking_status,
            **({} if require_booking else
               {"booked_by": ctx.actor, "booked_at": func.now()}))
        .on_conflict_do_nothing(constraint="disbursement_tranches_tenant_ref")
        .returning(DisbursementTranche.id))).scalar_one_or_none()
    if won is None:
        # A concurrent identical callback won the insert race — replay semantics apply.
        row = next(r for r in await _existing(ctx, lending_id)
                   if r.tranche_ref == payload.tranche_ref)
        if float(row.amount) == payload.amount:
            return _serialize(row)
        raise ConflictError(
            f"Tranche {payload.tranche_ref!r} was recorded concurrently with a different "
            "amount.")
    row = (await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.id == won))).scalar_one()
    if require_booking:
        # A PENDING booking moves nothing: no actuals, no stage, no account — the LMS
        # Authorizer's approval is what makes the money real on the book.
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=ctx.actor, action="disbursement.tranche.recorded",
            resource_type="disbursement_tranches", resource_id=str(won),
            request_id=request_id_ctx.get(),
            changes={"lending_id": lending_id, "tranche_ref": payload.tranche_ref,
                     "amount": payload.amount, "booking_status": "Pending",
                     **({"source": source} if source else {})}))
        return _serialize(row)
    await _settle_booked(ctx, line, row, source or "advaya-disbursement")
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="disbursement.tranche",
        resource_type="disbursement_tranches", resource_id=str(won),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "tranche_ref": payload.tranche_ref,
                 "amount": payload.amount,
                 "cumulative": float(line.disbursed_amount or 0.0),
                 "stage": line.stage,
                 **({"source": source} if source else {})}))
    return _serialize(row)


def _schedule(line: LendingTracker, rows: list[DisbursementTranche],
              lending_id: str) -> dict[str, Any]:
    """The tranche SCHEDULE as people talk about it: T1, T2, … in the order the money
    moved (rejected recordings keep their row but no number), with the reconciliation
    totals — booked money is the book; pending bookings are shown, and counted only
    against the remaining headroom."""
    booked = sum(float(r.amount) for r in rows if r.booking_status == "Booked")
    pending = sum(float(r.amount) for r in rows if r.booking_status == "Pending")
    ceiling = _ceiling(line)
    items, n = [], 0
    for r in rows:
        if r.booking_status == "Rejected":
            items.append({**_serialize(r), "tranche_no": None})
        else:
            n += 1
            items.append({**_serialize(r), "tranche_no": f"T{n}"})
    return {"lending_id": lending_id, "stage": line.stage, "items": items,
            "total_disbursed": booked, "total_pending": pending, "ceiling": ceiling,
            "fully_disbursed": (ceiling is not None and booked >= ceiling - 1e-9),
            "remaining": (None if ceiling is None
                          else max(ceiling - booked - pending, 0.0))}


@router.get("/v1/internal/lending/{lending_id}/tranches", tags=["Internal"],
            summary="A line's disbursement tranches + reconciliation totals")
async def list_tranches(lending_id: str,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service(ctx)
    line = await _line(ctx, lending_id)
    return _schedule(line, await _existing(ctx, lending_id), lending_id)


@router.get("/v1/lending/{lending_id}/tranches", tags=["Lending"],
            summary="The tranche schedule (T1, T2, …) as the drawer shows it")
async def list_tranches_user(lending_id: str,
                             ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """The USER-facing read of the same schedule — the internal route stays service-only
    (it is the write lane's mirror), while the drawer needs to show T1/T2, amounts,
    UTRs, booking states and what remains. Gated like every other company-composite read."""
    from app.api.custom import _ensure_company_read

    line = await _line(ctx, lending_id)
    await _ensure_company_read(ctx, line.entity_id)
    return _schedule(line, await _existing(ctx, lending_id), lending_id)


# ------------------------------------------------------------------------------------ #
# The human lane: record → Pending; the LMS Authorizer settles.
# ------------------------------------------------------------------------------------ #
@router.post("/v1/lending/{lending_id}/tranches", tags=["Lending"], status_code=201,
             summary="Record a disbursement tranche for LMS booking approval (human lane)")
async def record_tranche_user(lending_id: uuid.UUID, payload: TrancheIn,
                              ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """The MAKER's recorder — the LMS Operator (or the credit desk on bridge grants)
    records a later phase (T2, T3, …) directly in LMS · Servicing. It lands as a
    PENDING BOOKING, exactly like the LOS manual attestation's first tranche."""
    if ctx.user is None:
        raise ForbiddenError(
            "This is the human recording lane — a machine integration books through "
            "the service lane (/v1/internal/…).")
    from app.api.custom import _ensure_subject_scope

    await _ensure_subject_scope(ctx, "record_ledger_entry", "Lending", lending_id)
    return await apply_tranche(ctx, str(lending_id), payload,
                               source="lms-recorded", require_booking=True)


class BookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=2000)


@router.post("/v1/lending/{lending_id}/tranches/{tranche_id}/book", tags=["Lending"],
             summary="Settle a pending tranche booking (LMS Authorizer: approve | reject)")
async def book_tranche(lending_id: uuid.UUID, tranche_id: uuid.UUID, payload: BookIn,
                       ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """The CHECKER's verb. Approval makes the money real — actuals, the stage move and
    the loan account (with covenant stamping) all land in this one transaction.
    Rejection needs the reason and returns the recording to its maker; the corrected
    figure is a fresh recording with its own ref. Four-eyes is enforced: the recorder
    can never settle their own booking."""
    if ctx.user is None:
        raise ForbiddenError(
            "Booking approval is a human decision — it is attributed to the person.")
    from app.api.custom import _ensure_subject_scope

    await _ensure_subject_scope(ctx, "authorize_loan_account", "Lending", lending_id)
    line = await _line(ctx, str(lending_id))
    row = (await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.tenant_id == ctx.tenant_id,
        DisbursementTranche.id == tranche_id,
        DisbursementTranche.lending_id == str(lending_id),
        DisbursementTranche.deleted_at.is_(None))
        .with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No tranche {tranche_id} on this line.")
    if row.booking_status != "Pending":
        raise ConflictError(
            f"Tranche {row.tranche_ref!r} is already {row.booking_status} — a settled "
            "booking is frozen; a corrected figure is a fresh recording.")
    if row.recorded_by and row.recorded_by == ctx.actor:
        raise ValidationAppError(
            "Four-eyes: the person who recorded this tranche cannot settle its "
            "booking — a different LMS Authorizer must decide.")
    if payload.action == "reject":
        if not (payload.note or "").strip():
            raise ValidationAppError(
                "A rejection needs the reason — the maker corrects from your words.")
        row.booking_status = "Rejected"
    else:
        row.booking_status = "Booked"
    row.booked_by = ctx.actor
    row.booked_at = func.now()
    row.booking_note = (payload.note or "").strip() or None
    row.updated_by = ctx.actor
    await ctx.session.flush()
    if payload.action == "approve":
        await _settle_booked(ctx, line, row, source="lms-booking-approval")
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor,
        action=f"disbursement.tranche.{payload.action}",
        resource_type="disbursement_tranches", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": str(lending_id), "tranche_ref": row.tranche_ref,
                 "amount": float(row.amount), "booking_status": row.booking_status,
                 "recorded_by": row.recorded_by,
                 **({"note": payload.note} if payload.note else {})}))
    await ctx.session.refresh(row)
    return _serialize(row)


@router.get("/v1/bookings/pending", tags=["Lending"],
            summary="The LMS Authorizer's queue: every tranche awaiting booking approval")
async def pending_bookings(ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """Whole-book by design (servicing sees the book, like a bank's LMS): every PENDING
    tranche recording across the tenant, oldest first, with enough of the line to act
    on. Readable by the servicing pair and the senior credit bridge."""
    from app.authz.engine import enforce_operation

    try:
        enforce_operation(ctx.user, "authorize_loan_account")
    except ForbiddenError:
        enforce_operation(ctx.user, "record_ledger_entry")
    rows = list((await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.tenant_id == ctx.tenant_id,
        DisbursementTranche.booking_status == "Pending",
        DisbursementTranche.deleted_at.is_(None))
        .order_by(DisbursementTranche.created_at))).scalars())
    lines: dict[str, LendingTracker] = {}
    if rows:
        ids = [uuid.UUID(r.lending_id) for r in rows]
        for ln in (await ctx.session.execute(select(LendingTracker).where(
                LendingTracker.tenant_id == ctx.tenant_id,
                LendingTracker.id.in_(ids)))).scalars():
            lines[str(ln.id)] = ln
    items = []
    for r in rows:
        ln = lines.get(r.lending_id)
        items.append({**_serialize(r),
                      "stage": ln.stage if ln else None,
                      "entity_id": str(ln.entity_id) if ln and ln.entity_id else None})
    return {"items": items, "count": len(items)}
