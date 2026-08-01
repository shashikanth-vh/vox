"""Deal closure with open-item validation (Release-1 increment 8).

Closing a deal is the moment its record becomes history — so it is refused while the
record still owes answers:

* **Open EWS cases** bound to the deal — every early warning must reach a disposition.
* **Un-excused covenant breaches / overdue observations** on the deal — a breach must be
  resolved or carry a LIVE (unexpired) waiver; an overdue period must be submitted.
* **Product lines mid-pipeline** — every lending / syndication / asset-monetisation line
  must have reached a terminal stage; a facility halfway to disbursement cannot be
  closed out from under its own pipeline.

The close itself then runs through the SAME lifecycle policy as any stage change (the
Deal funnel's transition graph — Closed Won / Closed Lost are terminal), with the
closure note and the validation snapshot on the audit record.

    GET  /v1/deals/{id}/open-items     the validation report (what still blocks closure)
    POST /v1/deals/{id}/close          close (outcome won|lost, note mandatory)

Facility-level servicing closure (post-disbursement repayment/release) is outside the
Release-1 product scope — 'Disbursed' is the lending pipeline's terminal stage.
"""

from __future__ import annotations

import uuid
from typing import Any

from evam_backend_core import policy as core_policy
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.custom import _ensure_company_read
from app.authz import engine as authz
from app.core.errors import NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.covenants import EwsCase
from app.models.deals import Deal
from app.models.prism import MonitoringReporting
from app.models.trackers import AssetMonetisation, LendingTracker, SyndicationTracker

router = api_router()

# The terminal stages a product line must have reached before its deal can close.
_LINE_TERMINALS: dict[str, set[str]] = {
    "Lending": {"Disbursed", "Rejected"},
    "Syndication": {"Disbursed", "Withdrawn", "Rejected", "Dropped"},
    "AssetMonetisation": {"Closed", "Dropped"},
}

_OUTCOME_STAGE = {"won": "Closed Won", "lost": "Closed Lost"}


class CloseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str = Field(pattern="^(won|lost)$")
    note: str = Field(min_length=1, max_length=4000)


async def _deal(ctx: RequestContext, deal_id: uuid.UUID) -> Deal:
    row = (await ctx.session.execute(
        select(Deal).where(Deal.tenant_id == ctx.tenant_id, Deal.id == deal_id,
                           Deal.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No deal '{deal_id}'.")
    return row


async def _open_items(ctx: RequestContext, deal: Deal) -> dict[str, Any]:
    """Everything that still blocks this deal's closure, by category."""
    did = deal.id
    ews = list((await ctx.session.execute(
        select(EwsCase).where(
            EwsCase.tenant_id == ctx.tenant_id, EwsCase.deal_id == did,
            EwsCase.status != "Closed", EwsCase.deleted_at.is_(None)))).scalars())
    # A covenant observation blocks when it is BREACHED without a live waiver, or
    # OVERDUE (nothing submitted past due + grace). 'Waived' rows only pass while the
    # waiver is Granted (the sweep flips a lapsed waiver back to Breached).
    covenant_rows = list((await ctx.session.execute(
        select(MonitoringReporting).where(
            MonitoringReporting.tenant_id == ctx.tenant_id,
            MonitoringReporting.deal_id == did,
            MonitoringReporting.record_type == "Covenant",
            MonitoringReporting.deleted_at.is_(None),
            MonitoringReporting.status.in_(["Breached", "Overdue"])))).scalars())
    lines: list[dict[str, Any]] = []
    for label, model, field in (("Lending", LendingTracker, "stage"),
                                ("Syndication", SyndicationTracker, "status"),
                                ("AssetMonetisation", AssetMonetisation, "status")):
        rows = list((await ctx.session.execute(
            select(model).where(model.tenant_id == ctx.tenant_id,
                                model.deal_id == did,
                                model.deleted_at.is_(None)))).scalars())
        for r in rows:
            value = getattr(r, field)
            if value not in _LINE_TERMINALS[label]:
                lines.append({"line_type": label, "id": str(r.id), field: value,
                              "terminal_stages": sorted(_LINE_TERMINALS[label])})
    return {
        "deal_id": str(did), "stage": deal.stage,
        "ews_cases": [{"id": str(c.id), "title": c.title, "status": c.status,
                       "severity": c.severity} for c in ews],
        "covenants": [{"id": str(m.id), "covenant_name": m.covenant_name,
                       "status": m.status, "period": m.period,
                       "waiver_status": m.waiver_status} for m in covenant_rows],
        "lines": lines,
        "blocked": bool(ews or covenant_rows or lines),
    }


@router.get("/v1/deals/{deal_id}/open-items", tags=["Deals"],
            summary="What still blocks this deal's closure")
async def open_items(deal_id: uuid.UUID,
                     ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    deal = await _deal(ctx, deal_id)
    await _ensure_company_read(ctx, deal.entity_id)
    return await _open_items(ctx, deal)


@router.post("/v1/deals/{deal_id}/close", tags=["Deals"],
             summary="Close a deal (open-item validated; note mandatory)")
async def close_deal(deal_id: uuid.UUID, payload: CloseIn,
                     ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    # Closure is a stage-change authority action (the same authority that approves
    # stage changes generally), never an RM convenience.
    authz.enforce_operation(ctx.user, "approve_stage_change")
    deal = await _deal(ctx, deal_id)
    target = _OUTCOME_STAGE[payload.outcome]

    # 1. OPEN-ITEM VALIDATION — refuse with the full list, so the caller sees exactly
    #    what to resolve (close the cases, submit/waive the covenants, land the lines).
    report = await _open_items(ctx, deal)
    if report["blocked"]:
        raise ValidationAppError(
            f"Deal cannot close: {len(report['ews_cases'])} open EWS case(s), "
            f"{len(report['covenants'])} unresolved covenant observation(s), "
            f"{len(report['lines'])} product line(s) mid-pipeline. "
            "Resolve them (GET /open-items lists each) and retry.",
        )

    # 2. The close is a NORMAL lifecycle transition — the Deal funnel's policy decides
    #    whether the current stage may reach the terminal (never this endpoint).
    verdict = core_policy.check_write(
        "Deal", current={"stage": deal.stage}, changes={"stage": target},
        roles=sorted(ctx.user.roles) if ctx.user else None)
    if verdict is not None:
        raise ValidationAppError(verdict.message)

    deal.stage = target
    deal.updated_by = ctx.actor
    deal.version = (deal.version or 1) + 1
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="deal.close",
        resource_type="deals", resource_id=str(deal.id),
        request_id=request_id_ctx.get(),
        changes={"outcome": payload.outcome, "stage": target, "note": payload.note,
                 "open_items_checked": {"ews": 0, "covenants": 0, "lines": 0}}))
    await ctx.session.flush()
    await ctx.session.refresh(deal)
    return {"id": str(deal.id), "stage": deal.stage, "outcome": payload.outcome,
            "closed_by": ctx.user.email if ctx.user else ctx.actor,
            "note": payload.note, "version": deal.version}
