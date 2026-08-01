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
* **stage-aware** — tranches only make sense once the line has been handed over
  ('Disbursed'); anything earlier is a sequencing bug upstream and is refused.

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
        LendingTracker.deleted_at.is_(None)))).scalar_one_or_none()
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
    line = await _line(ctx, lending_id)
    if line.stage != "Disbursed":
        raise ConflictError(
            f"Lending line is {line.stage!r}; disbursement tranches are recorded only "
            "once the line is 'Disbursed' (handed over).")
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
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="disbursement.tranche",
        resource_type="disbursement_tranches", resource_id=str(won),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "tranche_ref": payload.tranche_ref,
                 "amount": payload.amount}))
    row = (await ctx.session.execute(select(DisbursementTranche).where(
        DisbursementTranche.id == won))).scalar_one()
    return _serialize(row)


@router.get("/v1/internal/lending/{lending_id}/tranches", tags=["Internal"],
            summary="A line's disbursement tranches + reconciliation totals")
async def list_tranches(lending_id: str,
                        ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service(ctx)
    line = await _line(ctx, lending_id)
    rows = await _existing(ctx, lending_id)
    total = sum(float(r.amount) for r in rows)
    ceiling = _ceiling(line)
    return {"lending_id": lending_id, "stage": line.stage,
            "items": [_serialize(r) for r in rows],
            "total_disbursed": total, "ceiling": ceiling,
            "fully_disbursed": (ceiling is not None and total >= ceiling - 1e-9),
            "remaining": (None if ceiling is None else max(ceiling - total, 0.0))}
