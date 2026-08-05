"""The LMS — loan accounts and their statement ledger (lending increment ④).

The account OPENS ITSELF on the first Advaya-confirmed disbursement tranche (see
``app.api.tranches.apply_tranche``): number, borrower, facility, rate/tenor/EMI copied
from the sanction terms — nothing re-typed. Later tranches raise the principal, each
with its own "Loan Disbursement (Tn)" ledger row.

From there the servicing team keeps the statement the way their Excel does:

    GET   /v1/lending/{id}/loan-account                       the account + full ledger
    GET   /v1/lending/{id}/loan-account/interest-preview      the CALCULATOR: what
          ?upto=YYYY-MM-DD                                    interest accrues to a date
    POST  /v1/lending/{id}/loan-account/accrue                write that interest row
    POST  /v1/lending/{id}/loan-account/entries               EMI receipt / charge / adj
    PATCH /v1/lending/{id}/loan-account                       classification & closure

Interest is COMPUTED — balance × rate% × days ÷ day-count — never hand-keyed, and the
preview shows the inputs so the figure is checkable before it is written.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.core.errors import ConflictError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.lms import LoanAccount, LoanAccountCondition, LoanLedgerEntry
from app.models.registry import Entity
from app.models.sanction import SanctionTerms
from app.models.trackers import LendingTracker

router = api_router()

_ENTRY_TYPES = {"Disbursement", "Interest", "EMI", "Receipt", "Charge", "Adjustment"}
_STATUSES = {"Standard", "SMA", "Sub-Standard", "Doubtful", "Loss", "Closed"}


def _round2(x: float) -> float:
    return float(f"{x:.2f}")


def interest_amount(balance: float, rate_pct: float, days: int, day_count: str) -> float:
    """The one interest formula: balance × rate% × days ÷ day-count."""
    base = 360 if str(day_count).strip() == "360" else 365
    return _round2(balance * (rate_pct / 100.0) * (days / base))


def _acct_out(row: LoanAccount) -> dict[str, Any]:
    return {
        "id": str(row.id), "lending_id": row.lending_id, "account_no": row.account_no,
        "borrower": row.borrower, "facility_type": row.facility_type,
        "disbursed_on": row.disbursed_on.isoformat() if row.disbursed_on else None,
        "amount": float(row.amount) if row.amount is not None else None,
        "rate_kind": row.rate_kind,
        "rate_pct": float(row.rate_pct) if row.rate_pct is not None else None,
        "tenor_months": row.tenor_months,
        "emi_amount": float(row.emi_amount) if row.emi_amount is not None else None,
        "repayment_start": row.repayment_start.isoformat() if row.repayment_start else None,
        "day_count": row.day_count, "status": row.status,
        "overdue_position": row.overdue_position,
        "provisioning_amount": (float(row.provisioning_amount)
                                if row.provisioning_amount is not None else None),
        "closed_on": row.closed_on.isoformat() if row.closed_on else None,
        "note": row.note,
    }


def _entry_out(e: LoanLedgerEntry) -> dict[str, Any]:
    return {"entry_no": e.entry_no, "entry_date": e.entry_date.isoformat(),
            "particulars": e.particulars, "entry_type": e.entry_type,
            "debit": float(e.debit) if e.debit is not None else None,
            "credit": float(e.credit) if e.credit is not None else None,
            "balance": float(e.balance)}


async def _entries(ctx: RequestContext, account_id: uuid.UUID) -> list[LoanLedgerEntry]:
    return list((await ctx.session.execute(select(LoanLedgerEntry).where(
        LoanLedgerEntry.tenant_id == ctx.tenant_id,
        LoanLedgerEntry.account_id == account_id,
        LoanLedgerEntry.deleted_at.is_(None))
        .order_by(LoanLedgerEntry.entry_no))).scalars())


async def _account(ctx: RequestContext, lending_id: str,
                   lock: bool = False) -> LoanAccount:
    q = select(LoanAccount).where(
        LoanAccount.tenant_id == ctx.tenant_id,
        LoanAccount.lending_id == lending_id,
        LoanAccount.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    row = (await ctx.session.execute(q)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            "No loan account on this line yet — one opens automatically on the first "
            "confirmed disbursement tranche.")
    return row


async def _line_scoped(ctx: RequestContext, lending_id: str) -> LendingTracker:
    from app.api.custom import _ensure_company_read

    try:
        lid = uuid.UUID(lending_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("lending_id must be a valid id.") from None
    line = (await ctx.session.execute(select(LendingTracker).where(
        LendingTracker.tenant_id == ctx.tenant_id, LendingTracker.id == lid,
        LendingTracker.deleted_at.is_(None)))).scalar_one_or_none()
    if line is None:
        raise NotFoundError(f"No Lending line {lending_id!r}.")
    await _ensure_company_read(ctx, line.entity_id)
    return line


def _maker(ctx: RequestContext) -> None:
    """Routine ledger events — the LMS OPERATOR's verb (bridge grants keep the credit
    desk working until a servicing team exists)."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "record_ledger_entry")


def _authorizer(ctx: RequestContext) -> None:
    """The hard-to-reverse verbs — classification, provisioning, closure — are the
    LMS MANAGEMENT's (plus senior credit): four-eyes over the account's state."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "authorize_loan_account")


async def _append(ctx: RequestContext, acct: LoanAccount, *, entry_date: date,
                  particulars: str, entry_type: str, debit: float | None = None,
                  credit: float | None = None) -> LoanLedgerEntry:
    rows = await _entries(ctx, acct.id)
    last_balance = float(rows[-1].balance) if rows else 0.0
    balance = _round2(last_balance + (debit or 0.0) - (credit or 0.0))
    entry = LoanLedgerEntry(
        tenant_id=ctx.tenant_id, account_id=acct.id, entry_no=len(rows) + 1,
        entry_date=entry_date, particulars=particulars, entry_type=entry_type,
        debit=debit, credit=credit, balance=balance, created_by=ctx.actor)
    ctx.session.add(entry)
    await ctx.session.flush()
    return entry


async def _handover_conditions(ctx: RequestContext, lending_id: str,
                               acct: LoanAccount) -> int:
    """The LOS→LMS handover of the CONDITIONS: copy the line's final checklist —
    completed and uncompleted items alike — into the account's own register. From
    here the servicing side owns the chase; the checklist freezes into the decision
    record (its cs-progress lane refuses transferred lines)."""
    from app.models.cpcs import CpcsChecklist

    rows = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id,
        CpcsChecklist.lending_id == lending_id,
        CpcsChecklist.deleted_at.is_(None)))).scalars().all()
    latest = None
    for c in rows:
        if latest is None or (c.checklist_version or 0) > (latest.checklist_version or 0):
            latest = c
    if latest is None or latest.status != "Approved":
        return 0
    n = 0
    for i in (latest.items or []):
        key = str(i.get("key") or "").strip()
        if not key:
            continue
        expiry = None
        if i.get("expiry_date"):
            try:
                expiry = date.fromisoformat(str(i["expiry_date"])[:10])
            except ValueError:
                expiry = None
        ctx.session.add(LoanAccountCondition(
            tenant_id=ctx.tenant_id, lending_id=lending_id, account_id=acct.id,
            key=key[:80], label=str(i.get("label") or key)[:1000],
            condition_type=str(i.get("condition_type") or "CS")[:8],
            required=bool(i.get("required", True)),
            status=str(i.get("status") or "Pending")[:20],
            reason=i.get("reason"), expiry_date=expiry,
            evidence_ref=i.get("evidence_ref"),
            source_version=latest.checklist_version, created_by=ctx.actor))
        n += 1
    return n


# ------------------------------------------------------------------------------------ #
# The auto-open hook — called from apply_tranche (the Advaya confirmation lane).
# ------------------------------------------------------------------------------------ #
async def open_or_grow_account(ctx: RequestContext, line: LendingTracker,
                               amount: float, disbursed_on: date | None,
                               tranche_no: int, tranche_ref: str) -> LoanAccount:
    """First confirmed tranche → OPEN the account (header from the sanction terms, the
    borrower from the entity, per-tenant account number) with its disbursement row.
    Later tranches → grow the principal with a "Loan Disbursement (Tn)" row."""
    lending_id = str(line.id)
    when = disbursed_on or date.today()
    acct = (await ctx.session.execute(select(LoanAccount).where(
        LoanAccount.tenant_id == ctx.tenant_id,
        LoanAccount.lending_id == lending_id,
        LoanAccount.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if acct is None:
        terms = (await ctx.session.execute(select(SanctionTerms).where(
            SanctionTerms.tenant_id == ctx.tenant_id,
            SanctionTerms.lending_id == lending_id,
            SanctionTerms.deleted_at.is_(None)))).scalar_one_or_none()
        ent = None
        if line.entity_id:
            ent = (await ctx.session.execute(select(Entity).where(
                Entity.tenant_id == ctx.tenant_id,
                Entity.id == line.entity_id))).scalar_one_or_none()
        next_no = ((await ctx.session.execute(
            select(func.coalesce(func.max(LoanAccount.account_no), 0)).where(
                LoanAccount.tenant_id == ctx.tenant_id))).scalar_one() or 0) + 1
        acct = LoanAccount(
            tenant_id=ctx.tenant_id, lending_id=lending_id,
            deal_id=str(line.deal_id) if line.deal_id else None,
            entity_id=str(line.entity_id) if line.entity_id else None,
            account_no=next_no,
            borrower=(ent.legal_name or ent.display_name) if ent else None,
            facility_type=(getattr(line, "product_type", None)
                           or getattr(line, "facility_type", None) or "Term Loan"),
            disbursed_on=when, amount=amount,
            rate_kind=(terms.rate_kind if terms else "Fixed"),
            rate_pct=(terms.rate_pct if terms else None),
            tenor_months=(terms.tenor_months if terms else None),
            emi_amount=(terms.emi_amount if terms else None),
            repayment_start=(terms.repayment_start if terms else None),
            day_count=(terms.day_count if terms else "365"),
            created_by=ctx.actor)
        ctx.session.add(acct)
        await ctx.session.flush()
        await _append(ctx, acct, entry_date=when, particulars="Loan Disbursement",
                      entry_type="Disbursement", debit=amount)
        # DEFERRED covenants start with the money: any covenant on this line seeded
        # without a first due date gets one — one cycle after this first disbursement —
        # and the monthly chase begins from there.
        from app.api.followups import _add_months
        from app.models.covenants import Covenant

        freq_step = {"Monthly": 1, "Quarterly": 3, "SemiAnnual": 6, "Annual": 12}
        deferred = list((await ctx.session.execute(select(Covenant).where(
            Covenant.tenant_id == ctx.tenant_id,
            Covenant.lending_id == line.id,
            Covenant.first_due_on.is_(None),
            Covenant.deleted_at.is_(None)))).scalars())
        for cov in deferred:
            cov.first_due_on = _add_months(when, freq_step.get(cov.frequency, 1))
            cov.updated_by = ctx.actor
        # The conditions HAND OVER with the account: the full checklist becomes the
        # servicing side's own register, and LMS owns the chase from here.
        handed = await _handover_conditions(ctx, lending_id, acct)
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.open",
            resource_type="loan_accounts", resource_id=str(acct.id),
            request_id=request_id_ctx.get(),
            changes={"lending_id": lending_id, "account_no": acct.account_no,
                     "amount": amount, "tranche_ref": tranche_ref,
                     "conditions_handed_over": handed}))
        return acct
    acct.amount = _round2(float(acct.amount or 0.0) + amount)
    acct.updated_by = ctx.actor
    await _append(ctx, acct, entry_date=when,
                  particulars=f"Loan Disbursement (T{tranche_no})",
                  entry_type="Disbursement", debit=amount)
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.disburse",
        resource_type="loan_accounts", resource_id=str(acct.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "amount": amount,
                 "tranche_ref": tranche_ref, "cumulative": float(acct.amount)}))
    return acct


# ------------------------------------------------------------------------------------ #
# The statement + servicing verbs
# ------------------------------------------------------------------------------------ #
@router.get("/v1/lending/{lending_id}/loan-account", tags=["Lending"],
            summary="The loan account + its full statement ledger")
async def get_loan_account(lending_id: str,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id)
    return {"account": _acct_out(acct),
            "entries": [_entry_out(e) for e in await _entries(ctx, acct.id)]}


def _accrual_window(acct: LoanAccount, rows: list[LoanLedgerEntry],
                    upto: date) -> tuple[date, int, float]:
    """(from-date, days, base-balance): interest runs from the LAST interest row (or the
    disbursement) on the CURRENT outstanding balance."""
    last_interest = next((r for r in reversed(rows) if r.entry_type == "Interest"), None)
    start = (last_interest.entry_date if last_interest is not None
             else (acct.disbursed_on or (rows[0].entry_date if rows else upto)))
    days = (upto - start).days
    balance = float(rows[-1].balance) if rows else 0.0
    return start, days, balance


@router.get("/v1/lending/{lending_id}/loan-account/interest-preview", tags=["Lending"],
            summary="CALCULATE the interest to a date (nothing is written)")
async def interest_preview(lending_id: str, upto: date,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id)
    if acct.rate_pct is None:
        raise ValidationAppError(
            "This account has no interest rate — enter the sanction terms first.")
    rows = await _entries(ctx, acct.id)
    start, days, balance = _accrual_window(acct, rows, upto)
    if days <= 0:
        raise ValidationAppError(
            f"Interest is already accrued to {start.isoformat()}; pick a later date.")
    amount = interest_amount(balance, float(acct.rate_pct), days, acct.day_count)
    return {"from": start.isoformat(), "upto": upto.isoformat(), "days": days,
            "balance": balance, "rate_pct": float(acct.rate_pct),
            "day_count": acct.day_count, "interest": amount,
            "formula": f"{balance:.2f} × {float(acct.rate_pct)}% × {days}/{acct.day_count}"}


class AccrueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upto: date


@router.post("/v1/lending/{lending_id}/loan-account/accrue", tags=["Lending"],
             status_code=201, summary="Write the computed interest row to the ledger")
async def accrue_interest(lending_id: str, payload: AccrueIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _maker(ctx)
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id, lock=True)
    if acct.status == "Closed" or acct.closed_on is not None:
        raise ConflictError("This loan account is closed.")
    if acct.rate_pct is None:
        raise ValidationAppError(
            "This account has no interest rate — enter the sanction terms first.")
    rows = await _entries(ctx, acct.id)
    start, days, balance = _accrual_window(acct, rows, payload.upto)
    if days <= 0:
        raise ValidationAppError(
            f"Interest is already accrued to {start.isoformat()}; pick a later date.")
    amount = interest_amount(balance, float(acct.rate_pct), days, acct.day_count)
    entry = await _append(
        ctx, acct, entry_date=payload.upto,
        particulars=f"Interest from {start.strftime('%d-%m-%Y')} "
                    f"to {payload.upto.strftime('%d-%m-%Y')}",
        entry_type="Interest", debit=amount)
    acct.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.accrue",
        resource_type="loan_accounts", resource_id=str(acct.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "from": start.isoformat(),
                 "upto": payload.upto.isoformat(), "days": days,
                 "balance": balance, "interest": amount}))
    return _entry_out(entry)


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_date: date
    # EMI and Receipt are credits (money in); Charge is a debit; an Adjustment says
    # which side it sits on.
    kind: str = Field(pattern="^(EMI|Receipt|Charge|Adjustment)$")
    amount: float = Field(gt=0)
    side: str | None = Field(default=None, pattern="^(debit|credit)$")
    particulars: str | None = Field(default=None, max_length=300)


@router.post("/v1/lending/{lending_id}/loan-account/entries", tags=["Lending"],
             status_code=201, summary="Record a ledger entry (EMI receipt, charge, …)")
async def add_entry(lending_id: str, payload: EntryIn,
                    ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _maker(ctx)
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id, lock=True)
    if acct.status == "Closed" or acct.closed_on is not None:
        raise ConflictError("This loan account is closed.")
    side = payload.side or ("debit" if payload.kind == "Charge" else "credit")
    label = payload.particulars or {
        "EMI": "EMI Paid", "Receipt": "Payment received",
        "Charge": "Charges", "Adjustment": "Adjustment"}[payload.kind]
    entry = await _append(
        ctx, acct, entry_date=payload.entry_date, particulars=label,
        entry_type=payload.kind,
        debit=payload.amount if side == "debit" else None,
        credit=payload.amount if side == "credit" else None)
    acct.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.entry",
        resource_type="loan_accounts", resource_id=str(acct.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "kind": payload.kind, "side": side,
                 "amount": payload.amount, "balance": float(entry.balance)}))
    return _entry_out(entry)


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    overdue_position: str | None = Field(default=None, max_length=120)
    provisioning_amount: float | None = Field(default=None, ge=0)
    closed_on: date | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.patch("/v1/lending/{lending_id}/loan-account", tags=["Lending"],
              summary="Update the servicing classification (status, overdue, provisioning)")
async def patch_account(lending_id: str, payload: AccountPatch,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _authorizer(ctx)
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id, lock=True)
    if payload.status is not None:
        if payload.status not in _STATUSES:
            raise ValidationAppError(
                f"status must be one of: {', '.join(sorted(_STATUSES))}.")
        acct.status = payload.status
    if payload.overdue_position is not None:
        acct.overdue_position = payload.overdue_position
    if payload.provisioning_amount is not None:
        acct.provisioning_amount = payload.provisioning_amount
    if payload.closed_on is not None:
        acct.closed_on = payload.closed_on
        acct.status = "Closed"
    if payload.note is not None:
        acct.note = payload.note
    acct.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.classify",
        resource_type="loan_accounts", resource_id=str(acct.id),
        request_id=request_id_ctx.get(),
        changes={k: (v.isoformat() if isinstance(v, date) else v)
                 for k, v in payload.model_dump(exclude_unset=True).items()}))
    return _acct_out(acct)


# ------------------------------------------------------------------------------------ #
# The account's OWN conditions register (handed over from the checklist at opening).
# ------------------------------------------------------------------------------------ #
def _cond_out(c: LoanAccountCondition) -> dict[str, Any]:
    return {"key": c.key, "label": c.label, "condition_type": c.condition_type,
            "required": c.required, "status": c.status, "reason": c.reason,
            "expiry_date": c.expiry_date.isoformat() if c.expiry_date else None,
            "evidence_ref": c.evidence_ref, "source_version": c.source_version,
            "completed_on": c.completed_on.isoformat() if c.completed_on else None,
            "completed_by": c.completed_by, "note": c.note}


async def _conditions(ctx: RequestContext,
                      lending_id: str) -> list[LoanAccountCondition]:
    return list((await ctx.session.execute(select(LoanAccountCondition).where(
        LoanAccountCondition.tenant_id == ctx.tenant_id,
        LoanAccountCondition.lending_id == lending_id,
        LoanAccountCondition.deleted_at.is_(None))
        .order_by(LoanAccountCondition.created_at,
                  LoanAccountCondition.key))).scalars())


@router.get("/v1/lending/{lending_id}/loan-account/conditions", tags=["Lending"],
            summary="The account's conditions register (handed over at opening)")
async def list_account_conditions(lending_id: str,
                                  ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    await _line_scoped(ctx, lending_id)
    await _account(ctx, lending_id)          # 404 until the account exists
    rows = await _conditions(ctx, lending_id)
    open_n = sum(1 for c in rows if c.status not in ("Completed", "Waived"))
    return {"items": [_cond_out(c) for c in rows], "open": open_n}


class ConditionReceiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ref: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/v1/lending/{lending_id}/loan-account/conditions/{key}/receive",
             tags=["Lending"],
             summary="Record a condition's document as RECEIVED (LMS Operator)")
async def receive_condition(lending_id: str, key: str, payload: ConditionReceiveIn,
                            ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """The servicing maker retires an obligation on the LMS's OWN record — no credit
    desk round-trip, no touch on the frozen LOS checklist."""
    _maker(ctx)
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id)
    if acct.status == "Closed" or acct.closed_on is not None:
        raise ConflictError("This loan account is closed.")
    row = (await ctx.session.execute(select(LoanAccountCondition).where(
        LoanAccountCondition.tenant_id == ctx.tenant_id,
        LoanAccountCondition.lending_id == lending_id,
        LoanAccountCondition.key == key,
        LoanAccountCondition.deleted_at.is_(None))
        .with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No condition {key!r} on this account.")
    if row.status in ("Completed", "Waived"):
        raise ConflictError(f"Condition {key!r} is already {row.status}.")
    row.status = "Completed"
    row.completed_on = date.today()
    row.completed_by = ctx.actor
    if payload.evidence_ref is not None:
        row.evidence_ref = payload.evidence_ref
    if payload.note is not None:
        row.note = payload.note
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.condition.receive",
        resource_type="loan_account_conditions", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "key": key,
                 **({"evidence_ref": payload.evidence_ref} if payload.evidence_ref else {})}))
    return _cond_out(row)


class ConditionAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=1000)
    expiry_date: date | None = None
    required: bool = True
    note: str | None = Field(default=None, max_length=2000)


@router.post("/v1/lending/{lending_id}/loan-account/conditions", tags=["Lending"],
             status_code=201,
             summary="Add a post-disbursement obligation discovered later (LMS Operator)")
async def add_condition(lending_id: str, payload: ConditionAddIn,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _maker(ctx)
    await _line_scoped(ctx, lending_id)
    acct = await _account(ctx, lending_id)
    if acct.status == "Closed" or acct.closed_on is not None:
        raise ConflictError("This loan account is closed.")
    existing = [c for c in await _conditions(ctx, lending_id) if c.key == payload.key]
    if existing:
        raise ConflictError(f"Condition {payload.key!r} already exists on this account.")
    row = LoanAccountCondition(
        tenant_id=ctx.tenant_id, lending_id=lending_id, account_id=acct.id,
        key=payload.key, label=payload.label, condition_type="CS",
        required=payload.required, status="Pending",
        expiry_date=payload.expiry_date, note=payload.note, created_by=ctx.actor)
    ctx.session.add(row)
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="lms.condition.add",
        resource_type="loan_account_conditions", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "key": payload.key, "label": payload.label}))
    return _cond_out(row)
