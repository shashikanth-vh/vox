"""Tranche-level disbursement callbacks.

Advaya (or ops on its behalf, until the integration is live) reports each disbursement
TRANCHE against a Lending line. The record is:

* **service-principal only** — like decisions and handoffs, this is machine plumbing, not
  a user-facing surface (``svc_workflows`` / ``svc_advaya`` present the service key);
* **idempotent** — replaying the same ``tranche_ref`` returns the original row unchanged;
  the same ref with a DIFFERENT amount is a 409 (a corrected figure is a NEW tranche with
  its own ref and a note — the record is append-only at the database);
* **bounded** — cumulative tranches may never exceed the line's proposed disbursement
  amount (falling back to the facility amount); an over-disbursement callback is refused
  loudly rather than silently absorbed;
* **boundary-aware** — tranches are accepted only after Advaya ACCEPTED the handover
  package; the FIRST tranche (money actually moving) is what advances the line to
  'Disbursed', and the line's actuals (disbursed_amount / disbursement_date) are
  written ONLY from these callbacks — PRISM never asserts them on its own authority.

    POST /v1/internal/lending/{lending_id}/tranches       record one tranche
    GET  /v1/internal/lending/{lending_id}/tranches       list + totals (reconciliation)
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
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


async def apply_tranche(ctx: RequestContext, lending_id: str, payload: TrancheIn,
                        source: str | None = None) -> dict[str, Any]:
    """The ONE tranche-recording path — shared by the machine lane (Advaya's
    callbacks) and the MANUAL attestation lane (an authorised human relaying
    Advaya's offline disbursement confirmation). Identical guards, ceilings,
    actuals and stage move; ``source`` marks the provenance."""
    line = await _line(ctx, lending_id)
    # Tranches are ADVAYA's disbursement evidence. They are recorded only after Advaya
    # ACCEPTED the handover (PRISM's workflow boundary), and it is the FIRST tranche —
    # money actually moving — that advances the line to 'Disbursed', never a PRISM
    # approval. Anything earlier is a sequencing bug upstream and is refused.
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
    ceiling = _ceiling(line)
    already = sum(float(r.amount) for r in rows)
    if ceiling is not None and already + payload.amount > ceiling + 1e-9:
        raise ValidationAppError(
            f"Cumulative disbursement {already + payload.amount} would exceed the "
            f"line's ceiling {ceiling} (proposed disbursement / facility amount); "
            "over-disbursement callbacks are refused.")
    won = (await ctx.session.execute(
        pg_insert(DisbursementTranche).values(
            tenant_id=ctx.tenant_id, lending_id=lending_id,
            deal_id=str(line.deal_id) if line.deal_id else None,
            tranche_ref=payload.tranche_ref, amount=payload.amount,
            disbursed_on=payload.disbursed_on, advaya_reference=payload.advaya_reference,
            note=payload.note, recorded_by=ctx.actor, created_by=ctx.actor)
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
    # ACTUALS come only from here — Advaya's reports: cumulative disbursed amount, the
    # first drawdown date, and (on the FIRST tranche) the stage move to 'Disbursed'.
    line.disbursed_amount = already + payload.amount
    if line.disbursement_date is None:
        line.disbursement_date = payload.disbursed_on or date.today()
    if line.stage != "Disbursed":
        history = list(line.stage_history or [])
        history.append({"from": line.stage, "to": "Disbursed",
                        "source": source or "advaya-disbursement",
                        "tranche_ref": payload.tranche_ref, "by": ctx.actor})
        line.stage = "Disbursed"
        line.stage_history = history
    line.updated_by = ctx.actor
    # The LMS follows the money: the FIRST confirmed tranche opens the loan account
    # (header from the sanction terms), each further one raises its principal, and the
    # statement gets its "Loan Disbursement" row — all from Advaya's confirmation, never
    # from a PRISM approval.
    from app.api.lms import open_or_grow_account

    await open_or_grow_account(ctx, line, payload.amount, payload.disbursed_on,
                               tranche_no=len(rows) + 1, tranche_ref=payload.tranche_ref)
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="disbursement.tranche",
        resource_type="disbursement_tranches", resource_id=str(won),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "tranche_ref": payload.tranche_ref,
                 "amount": payload.amount, "cumulative": already + payload.amount,
                 "stage": line.stage,
                 **({"source": source} if source else {})}))
    row = (await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.id == won))).scalar_one()
    return _serialize(row)


def _schedule(line: LendingTracker, rows: list[DisbursementTranche],
              lending_id: str) -> dict[str, Any]:
    """The tranche SCHEDULE as people talk about it: T1, T2, … in the order the money
    moved, with the reconciliation totals."""
    total = sum(float(r.amount) for r in rows)
    ceiling = _ceiling(line)
    return {"lending_id": lending_id, "stage": line.stage,
            "items": [{**_serialize(r), "tranche_no": f"T{i + 1}"}
                      for i, r in enumerate(rows)],
            "total_disbursed": total, "ceiling": ceiling,
            "fully_disbursed": (ceiling is not None and total >= ceiling - 1e-9),
            "remaining": (None if ceiling is None else max(ceiling - total, 0.0))}


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
    UTRs and what remains. Gated like every other company-composite read."""
    from app.api.custom import _ensure_company_read

    line = await _line(ctx, lending_id)
    await _ensure_company_read(ctx, line.entity_id)
    return _schedule(line, await _existing(ctx, lending_id), lending_id)
