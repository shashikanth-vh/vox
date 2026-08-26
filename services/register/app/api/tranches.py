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
  until the LMS MANAGEMENT approves it — then the same settlement runs, attributed and
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
from app.core.config import get_settings
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
            "conditions_open": row.conditions_open or [],
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


async def _open_conditions(ctx: RequestContext, lending_id: str) -> list[dict[str, Any]]:
    """The line's outstanding CP/CS conditions — the same disclosure the disbursement
    request makes to the partner, snapshotted onto each recorded tranche so the booking
    permanently says what was open when it was made. Once the account opened, the LMS's
    own conditions register is the live source; before that, the latest checklist."""
    from app.models.cpcs import CpcsChecklist
    from app.models.lms import LoanAccountCondition

    handed = (await ctx.session.execute(select(LoanAccountCondition).where(
        LoanAccountCondition.tenant_id == ctx.tenant_id,
        LoanAccountCondition.lending_id == lending_id,
        LoanAccountCondition.deleted_at.is_(None)))).scalars().all()
    if handed:
        return [{"key": c.key, "label": c.label, "condition_type": c.condition_type,
                 "status": c.status,
                 **({"expiry_date": c.expiry_date.isoformat()} if c.expiry_date else {})}
                for c in handed if c.status not in ("Completed", "Waived")][:50]

    rows = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id,
        CpcsChecklist.lending_id == lending_id,
        CpcsChecklist.deleted_at.is_(None)))).scalars().all()
    latest = None
    for c in rows:
        if latest is None or (c.checklist_version or 0) > (latest.checklist_version or 0):
            latest = c
    if latest is None or latest.status != "Approved":
        return []
    open_items = [i for i in (latest.items or [])
                  if str(i.get("status") or "Pending") not in ("Completed", "Waived")]
    return [{"key": str(i.get("key") or ""),
             "label": str(i.get("label") or i.get("key") or "condition"),
             "condition_type": str(i.get("condition_type") or "CS"),
             "status": str(i.get("status") or "Pending"),
             **({"expiry_date": str(i["expiry_date"])} if i.get("expiry_date") else {})}
            for i in open_items[:50]]


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


async def cs_milestone_error(ctx: RequestContext, lending_id: str) -> str | None:
    """Why this line may NOT take the 'CP/CS Completed' label right now, or None.

    The label claims BOTH halves are done — and the cp_cs_completion evidence alone
    cannot vouch for that (it is minted at CP approval, while the CS chase is usually
    still live). Entry to the milestone is therefore earned from CHECKLIST TRUTH:
    the latest approved checklist must carry conditions subsequent and every one of
    them must be settled. Shared by the direct stage edit, the change-request
    approval, and the settlement's own automatic move."""
    if await _cs_all_settled(ctx, lending_id):
        return None
    return ("'CP/CS Completed' says every condition — precedent AND subsequent — is "
            "settled, and the approved checklist does not show that yet. Record the "
            "remaining CS receipts on the checklist (the stage moves itself when the "
            "last one closes); a facility whose checklist has no conditions "
            "subsequent ends at 'Disbursed'.")


async def _cs_all_settled(ctx: RequestContext, lending_id: str) -> bool:
    """Whether the latest APPROVED CP/CS checklist HAS conditions subsequent and every
    one of them is settled. No approved checklist, or a checklist with no CS at all →
    False: there was no CS chase to have finished, so the settlement ends the line at
    'Disbursed' and 'CP/CS Completed' has nothing to add."""
    from app.models.cpcs import CpcsChecklist
    chk = (await ctx.session.execute(
        select(CpcsChecklist).where(
            CpcsChecklist.tenant_id == ctx.tenant_id,
            CpcsChecklist.lending_id == str(lending_id),
            CpcsChecklist.status == "Approved",
            CpcsChecklist.deleted_at.is_(None))
        .order_by(CpcsChecklist.checklist_version.desc()).limit(1))).scalar_one_or_none()
    if chk is None:
        return False
    cs = [i for i in (chk.items or []) if str(i.get("condition_type")) == "CS"]
    return bool(cs) and all(
        str(i.get("status") or "Pending") in ("Completed", "Waived") for i in cs)


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
    if line.stage not in ("Disbursed", "CP/CS Completed"):
        history = list(line.stage_history or [])
        history.append({"from": line.stage, "to": "Disbursed",
                        "source": source,
                        "tranche_ref": row.tranche_ref, "by": ctx.actor})
        line.stage = "Disbursed"
        # 'CP/CS COMPLETED' NEEDS BOTH HALVES: whichever finishes second triggers it.
        # When the CS chase settled first (recorded on the checklist, stage held at
        # 'Ready for Disbursement'), this settlement IS the second half — the line
        # completes NOW, and BOTH audit events stand: the Disbursed event above and
        # this closing move, each with its own provenance.
        if await _cs_all_settled(ctx, str(line.id)):
            history.append({"from": "Disbursed", "to": "CP/CS Completed",
                            "source": f"{source}:cs-already-complete",
                            "tranche_ref": row.tranche_ref, "by": ctx.actor})
            line.stage = "CP/CS Completed"
        line.stage_history = history
    line.updated_by = ctx.actor
    # The LMS follows the money: the FIRST booked tranche opens the loan account
    # (header from the sanction terms), each further one raises its principal, and the
    # statement gets its "Loan Disbursement" row.
    #
    # WITH LMS DEFERRED this does not run. Opening the account is also what HANDS THE
    # CP/CS CHECKLIST OVER to the servicing desk, and there is no servicing desk — a
    # checklist handed to nobody is a chase that stops. So the book ends at 'Disbursed'
    # and the conditions stay with the origination desk, which keeps chasing them.
    if not get_settings().lms_enabled:
        return
    from app.api.lms import open_or_grow_account

    await open_or_grow_account(ctx, line, float(row.amount), row.disbursed_on,
                               tranche_no=len(booked_before) + 1,
                               tranche_ref=row.tranche_ref)


async def apply_tranche(ctx: RequestContext, lending_id: str, payload: TrancheIn,
                        source: str | None = None,
                        require_booking: bool = False) -> dict[str, Any]:
    """The ONE tranche-recording path — shared by the machine lane (Advaya's callbacks,
    ``require_booking=False``: books directly) and the human lanes (manual attestation /
    the LMS recorder, ``require_booking=True``: lands Pending for the LMS Management).
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
    if line.stage not in ("Ready for Disbursement", "Disbursed", "CP/CS Completed"):
        raise ConflictError(
            f"Lending line is {line.stage!r}; disbursement tranches apply only to a "
            "line at 'Ready for Disbursement' (first tranche), 'Disbursed', or "
            "'CP/CS Completed' (later tranches after the conditions closed).")
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
    # The disclosure travels WITH the recording: whatever CP/CS conditions are open
    # right now is stamped on the tranche, so the booking decision's context survives
    # even after the conditions later complete (the live chase stays on the checklist).
    snapshot = await _open_conditions(ctx, lending_id)
    won = (await ctx.session.execute(
        pg_insert(DisbursementTranche).values(
            tenant_id=ctx.tenant_id, lending_id=lending_id,
            deal_id=str(line.deal_id) if line.deal_id else None,
            tranche_ref=payload.tranche_ref, amount=payload.amount,
            disbursed_on=payload.disbursed_on, advaya_reference=payload.advaya_reference,
            note=payload.note, recorded_by=ctx.actor, created_by=ctx.actor,
            booking_status=booking_status, conditions_open=snapshot,
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
                     "conditions_open": len(snapshot),
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
# The human lane: record → Pending; the LMS Management settles.
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
                               source="lms-recorded",
                               require_booking=get_settings().lms_enabled)


class BookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=2000)


@router.post("/v1/lending/{lending_id}/tranches/{tranche_id}/book", tags=["Lending"],
             summary="Settle a pending tranche booking (LMS Management: approve | reject)")
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
            "booking — a different LMS Management must decide.")
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
                 "conditions_open_at_recording": len(row.conditions_open or []),
                 **({"note": payload.note} if payload.note else {})}))
    # The RECORDER learns of the outcome without watching the screen: the decision
    # writes their inbox row in the same transaction.
    from app.api.notify import notify_maker
    outcome = "booked" if row.booking_status == "Booked" else "rejected"
    await notify_maker(
        ctx, recipient=row.recorded_by, event=f"booking.{outcome}",
        severity="info" if outcome == "booked" else "warning",
        title=f"Tranche {row.tranche_ref or ''} ₹ {float(row.amount)} Cr {outcome}",
        body=(f"{'Booked' if outcome == 'booked' else 'Rejected'} by {ctx.actor}."
              + (f" Note: {row.booking_note}" if row.booking_note else "")),
        subject_type="Lending", subject_id=str(lending_id),
        dedupe_key=f"ntf:booking:{row.id}:{outcome}")
    await ctx.session.refresh(row)
    return _serialize(row)


@router.get("/v1/bookings/pending", tags=["Lending"],
            summary="The LMS Management's queue: every tranche awaiting booking approval")
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
    names: dict[str, str] = {}
    if rows:
        ids = [uuid.UUID(r.lending_id) for r in rows]
        for ln in (await ctx.session.execute(select(LendingTracker).where(
                LendingTracker.tenant_id == ctx.tenant_id,
                LendingTracker.id.in_(ids)))).scalars():
            lines[str(ln.id)] = ln
        from app.models.registry import Entity
        ent_ids = {ln.entity_id for ln in lines.values() if ln.entity_id}
        if ent_ids:
            for e in (await ctx.session.execute(select(Entity).where(
                    Entity.tenant_id == ctx.tenant_id,
                    Entity.id.in_(ent_ids)))).scalars():
                names[str(e.id)] = e.legal_name or e.display_name or ""
    items = []
    for r in rows:
        ln = lines.get(r.lending_id)
        eid = str(ln.entity_id) if ln and ln.entity_id else None
        items.append({**_serialize(r),
                      "stage": ln.stage if ln else None,
                      "entity_id": eid,
                      "borrower": names.get(eid or "", None)})
    return {"items": items, "count": len(items)}
