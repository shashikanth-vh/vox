"""api.chitti — the READ-ONLY machine lane for the Chitti chatbot.

Chitti runs on a separate VM and answers questions from PRISM data. It reaches
the register through the edge's machine lane (/machine/v1/internal/… →
/v1/internal/…) with its own named service key, so:

* the principal is ``svc_chitti`` — stamped as the actor, audit-visible, and
  the ONLY principal these routes accept (a leaked generic key opens nothing);
* the surface is GET-only by construction — the bot can display the book, it
  can never write a row;
* every response is tenant-scoped by RLS like any other call, capped in size,
  and shaped for display (codes, names, status lines) rather than raw rows.

Adding write abilities here is a deliberate future decision, not a patch.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import or_, select

from app.authz.engine import service_ctx
from app.core.errors import ForbiddenError, NotFoundError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models.deals import Deal, Lead
from app.models.interactions import Interaction
from app.models.registry import Entity
from app.models.trackers import (AssetMonetisation, LendingTracker,
                                 SyndicationLender, SyndicationTracker)

router = api_router()

_ALLOWED_SERVICES = {"svc_chitti"}


def _require_chitti() -> None:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError("Only the Chitti service principal may use this lane.")


def _d(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _entity_row(e: Entity) -> dict:
    return {"code": e.code, "name": e.display_name or e.legal_name,
            "legal_name": e.legal_name, "sector": e.sector,
            "sub_sector": e.sub_sector, "lens": e.lens,
            "status": e.register_status}


def _lead_row(l: Lead) -> dict:
    return {"lead_no": l.lead_no, "company": l.company, "status": l.status,
            "temperature": l.temperature, "rm": l.rm, "sector": l.sector,
            "next_action": l.next_action,
            "next_action_date": _d(l.next_action_date),
            "last_interaction_date": _d(l.last_interaction_date)}


def _deal_row(d: Deal) -> dict:
    return {"deal_no": d.deal_no, "code": d.code, "product_type": d.product_type,
            "stage": d.stage, "rm": d.rm,
            "lanes": [n for n, on in (("lending", d.is_lending),
                                      ("syndication", d.is_syndication),
                                      ("asset_monetisation", d.is_asset_mon)) if on]}


def _interaction_row(i: Interaction) -> dict:
    return {"occurred_at": _d(i.occurred_at), "type": i.interaction_type,
            "direction": i.direction, "summary": i.summary,
            "lender": i.lender_name, "by": i.performed_by,
            "contact": i.contact_name, "outcome": i.outcome,
            "next_action": i.next_action,
            "next_action_date": _d(i.next_action_date)}


@router.get("/v1/internal/chitti/search", tags=["Internal"],
            summary="Chitti: search companies, leads and deals (read-only)")
async def chitti_search(q: str = Query(min_length=2, max_length=120),
                        limit: int = Query(default=8, ge=1, le=25),
                        ctx: RequestContext = Depends(get_context)) -> dict:
    _require_chitti()
    like = f"%{q.strip()}%"
    ents = (await ctx.session.execute(
        select(Entity).where(Entity.tenant_id == ctx.tenant_id,
                             Entity.deleted_at.is_(None),
                             or_(Entity.legal_name.ilike(like),
                                 Entity.display_name.ilike(like),
                                 Entity.code.ilike(like)))
        .order_by(Entity.legal_name).limit(limit))).scalars().all()
    leads = (await ctx.session.execute(
        select(Lead).where(Lead.tenant_id == ctx.tenant_id,
                           Lead.deleted_at.is_(None),
                           or_(Lead.company.ilike(like),
                               Lead.lead_no.ilike(like)))
        .order_by(Lead.lead_no).limit(limit))).scalars().all()
    deals = (await ctx.session.execute(
        select(Deal).where(Deal.tenant_id == ctx.tenant_id,
                           Deal.deleted_at.is_(None),
                           or_(Deal.deal_no.ilike(like), Deal.code.ilike(like)))
        .order_by(Deal.deal_no).limit(limit))).scalars().all()
    return {"q": q, "companies": [_entity_row(e) for e in ents],
            "leads": [_lead_row(l) for l in leads],
            "deals": [_deal_row(d) for d in deals]}


@router.get("/v1/internal/chitti/company/{code}", tags=["Internal"],
            summary="Chitti: one company's snapshot (read-only)")
async def chitti_company(code: str,
                         interactions: int = Query(default=10, ge=0, le=50),
                         ctx: RequestContext = Depends(get_context)) -> dict:
    _require_chitti()
    ent = (await ctx.session.execute(
        select(Entity).where(Entity.tenant_id == ctx.tenant_id,
                             Entity.deleted_at.is_(None),
                             Entity.code == code))).scalar_one_or_none()
    if ent is None:
        raise NotFoundError(f"No company with code '{code}'.")

    leads = (await ctx.session.execute(
        select(Lead).where(Lead.tenant_id == ctx.tenant_id,
                           Lead.deleted_at.is_(None),
                           Lead.entity_id == ent.id)
        .order_by(Lead.lead_no))).scalars().all()
    deals = (await ctx.session.execute(
        select(Deal).where(Deal.tenant_id == ctx.tenant_id,
                           Deal.deleted_at.is_(None),
                           Deal.entity_id == ent.id)
        .order_by(Deal.deal_no))).scalars().all()

    lending = (await ctx.session.execute(
        select(LendingTracker).where(LendingTracker.tenant_id == ctx.tenant_id,
                                     LendingTracker.deleted_at.is_(None),
                                     LendingTracker.entity_id == ent.id))).scalars().all()
    synd = (await ctx.session.execute(
        select(SyndicationTracker).where(SyndicationTracker.tenant_id == ctx.tenant_id,
                                         SyndicationTracker.deleted_at.is_(None),
                                         SyndicationTracker.entity_id == ent.id))).scalars().all()
    am = (await ctx.session.execute(
        select(AssetMonetisation).where(AssetMonetisation.tenant_id == ctx.tenant_id,
                                        AssetMonetisation.deleted_at.is_(None),
                                        AssetMonetisation.entity_id == ent.id))).scalars().all()
    lenders_by_syn: dict[uuid.UUID, list[dict]] = {}
    if synd:
        rows = (await ctx.session.execute(
            select(SyndicationLender).where(
                SyndicationLender.tenant_id == ctx.tenant_id,
                SyndicationLender.deleted_at.is_(None),
                SyndicationLender.syndication_id.in_([s.id for s in synd]))
            .order_by(SyndicationLender.lender_name))).scalars().all()
        for r in rows:
            lenders_by_syn.setdefault(r.syndication_id, []).append({
                "lender": r.lender_name, "status": r.status,
                "amount_cr": float(r.amount_cr) if r.amount_cr is not None else None,
                "chased_date": _d(r.chased_date), "response_date": _d(r.response_date),
                "last_chase_note": r.last_chase_note,
                "last_reply_note": r.last_reply_note})

    recent = (await ctx.session.execute(
        select(Interaction).where(Interaction.tenant_id == ctx.tenant_id,
                                  Interaction.deleted_at.is_(None),
                                  Interaction.entity_id == ent.id)
        .order_by(Interaction.occurred_at.desc())
        .limit(interactions))).scalars().all() if interactions else []

    return {
        "company": _entity_row(ent),
        "leads": [_lead_row(l) for l in leads],
        "deals": [_deal_row(d) for d in deals],
        "lending": [{"stage": t.stage, "stage_updated_at": _d(t.stage_updated_at),
                     "remarks": t.remarks} for t in lending],
        "syndication": [{"status": s.status, "facility": s.facility,
                         "mandate_status": s.mandate_status, "remarks": s.remarks,
                         "lenders": lenders_by_syn.get(s.id, [])} for s in synd],
        "asset_monetisation": [{"status": a.status, "notes": a.notes} for a in am],
        "recent_interactions": [_interaction_row(i) for i in recent],
    }


@router.get("/v1/internal/chitti/interactions", tags=["Internal"],
            summary="Chitti: a company's recent interactions (read-only)")
async def chitti_interactions(company: str = Query(min_length=1, max_length=60),
                              limit: int = Query(default=20, ge=1, le=100),
                              ctx: RequestContext = Depends(get_context)) -> dict:
    _require_chitti()
    ent = (await ctx.session.execute(
        select(Entity).where(Entity.tenant_id == ctx.tenant_id,
                             Entity.deleted_at.is_(None),
                             Entity.code == company))).scalar_one_or_none()
    if ent is None:
        raise NotFoundError(f"No company with code '{company}'.")
    rows = (await ctx.session.execute(
        select(Interaction).where(Interaction.tenant_id == ctx.tenant_id,
                                  Interaction.deleted_at.is_(None),
                                  Interaction.entity_id == ent.id)
        .order_by(Interaction.occurred_at.desc()).limit(limit))).scalars().all()
    return {"company": ent.code, "interactions": [_interaction_row(i) for i in rows]}
