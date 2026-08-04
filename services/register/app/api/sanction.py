"""Sanction terms + the CAM workbench lifecycle (lending increments 1–2).

**Sanction terms** are the fan-out point of the whole lending life: the analyst enters
the committee-approved terms ONCE, and the save seeds the CP/CS checklist (the existing
maker-checker artefact) and the covenant register in the same transaction. Everything
downstream — CP approval, the CS chase, covenant cycles, the LMS account — is born from
this row rather than re-typed.

**CAM reports** follow the CpcsChecklist lifecycle verbatim: the analyst's workbench
(the workflows service) drafts versions; the committee decides. Draft → Submitted →
Approved | Returned | Rejected, with the preparer barred from deciding and committee
authority required to decide. The formal stage gate stays where it is — ``Sanctioned``
still requires the ``credit_committee_approval`` evidence verified against a
WorkflowDecision — this record is the working copy that review runs on, and the audit
trail of how the CAM came to say what it says.

    POST /v1/internal/sanction-terms                       enter terms; seeds CP/CS + covenants
    GET  /v1/internal/sanction-terms?lending_id=…          read them
    POST /v1/internal/cam-reports                          open a CAM version (Draft)
    POST /v1/internal/cam-reports/{id}/turns               append a workbench turn + new draft
    POST /v1/internal/cam-reports/{id}/submit              maker: send to committee
    POST /v1/internal/cam-reports/{id}/decide              committee: Approve/Return/Reject
    GET  /v1/internal/cam-reports?lending_id=…             the version history
    GET  /v1/internal/cam-reports/{id}                     one version, with its transcript
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ConflictError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.covenants import Covenant
from app.models.cpcs import CpcsChecklist
from app.models.sanction import CamReport, CamTurn, SanctionTerms

router = api_router()

# Deciding a CAM is committee authority — the same seniority that waives a CP.
_COMMITTEE_AUTHORITY = {"Credit Head", "Management", "Admin"}


def _actor_id(ctx: RequestContext) -> str | None:
    return str(ctx.user.id) if ctx.user is not None and ctx.user.id else None


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValidationAppError(f"{field} must be a valid id.") from None


async def _lending_or_404(ctx: RequestContext, lending_id: str):  # noqa: ANN202
    from app.repositories.subjects import load_subject

    row = await load_subject(ctx.session, ctx.tenant_id, "Lending", _uuid(lending_id, "lending_id"))
    if row is None:
        raise NotFoundError(f"Lending line '{lending_id}' not found.")
    return row


# --------------------------------------------------------------------------- #
# Sanction terms — entered once, seeding everything downstream
# --------------------------------------------------------------------------- #
class SeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=300)
    required: bool = True
    note: str | None = None


class SeedCovenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    covenant_type: str = Field(default="Financial",
                               pattern="^(Financial|Reporting|Security|Other)$")
    description: str | None = None
    metric: str | None = Field(default=None, max_length=60)
    operator: str | None = Field(default=None, pattern="^(>=|<=|>|<|=)$")
    threshold: float | None = None
    frequency: str = Field(default="Quarterly")
    first_due_on: date
    grace_days: int = Field(default=0, ge=0, le=365)
    breach_severity: str = Field(default="Amber", pattern="^(Amber|Red)$")


class SanctionTermsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(min_length=1, max_length=64)
    deal_id: str | None = Field(default=None, max_length=64)
    amount_cr: float | None = Field(default=None, gt=0)
    rate_kind: str = Field(default="Fixed", pattern="^(Fixed|Floating)$")
    rate_pct: float | None = Field(default=None, ge=0, le=100)
    spread_pct: float | None = Field(default=None, ge=0, le=100)
    tenor_months: int | None = Field(default=None, ge=1, le=600)
    emi_amount: float | None = Field(default=None, gt=0)
    repayment_start: date | None = None
    day_count: str = Field(default="365", pattern="^(365|360)$")
    penal_rate_pct: float | None = Field(default=None, ge=0, le=100)
    moratorium_months: int = Field(default=0, ge=0, le=120)
    schedule_kind: str = Field(default="EMI", pattern="^(EMI|Bullet|Custom)$")
    cp_items: list[SeedItem] = Field(default_factory=list)
    cs_items: list[SeedItem] = Field(default_factory=list)
    covenants: list[SeedCovenant] = Field(default_factory=list)
    note: str | None = None


def _terms_out(row: SanctionTerms) -> dict[str, Any]:
    return {
        "id": str(row.id), "lending_id": row.lending_id, "deal_id": row.deal_id,
        "amount_cr": float(row.amount_cr) if row.amount_cr is not None else None,
        "rate_kind": row.rate_kind,
        "rate_pct": float(row.rate_pct) if row.rate_pct is not None else None,
        "spread_pct": float(row.spread_pct) if row.spread_pct is not None else None,
        "tenor_months": row.tenor_months,
        "emi_amount": float(row.emi_amount) if row.emi_amount is not None else None,
        "repayment_start": row.repayment_start.isoformat() if row.repayment_start else None,
        "day_count": row.day_count,
        "penal_rate_pct": float(row.penal_rate_pct) if row.penal_rate_pct is not None else None,
        "moratorium_months": row.moratorium_months, "schedule_kind": row.schedule_kind,
        "cp_items": row.cp_items or [], "cs_items": row.cs_items or [],
        "covenants": row.covenants or [],
        "seeded_checklist_id": row.seeded_checklist_id,
        "seeded_covenant_ids": row.seeded_covenant_ids or [],
        "note": row.note, "created_by": row.created_by,
    }


@router.post("/v1/internal/sanction-terms", tags=["Internal"], status_code=201,
             summary="Enter the sanctioned terms (seeds the CP/CS checklist + covenants)")
async def create_sanction_terms(payload: SanctionTermsIn,
                                ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    # The analyst's preparation authority — the same operation that prepares the CP/CS
    # checklist, because entering terms IS preparing those artefacts.
    enforce_operation(ctx.user, "prepare_cpcs_checklist")
    lending = await _lending_or_404(ctx, payload.lending_id)
    entity_id = getattr(lending, "entity_id", None)

    existing = (await ctx.session.execute(select(SanctionTerms).where(
        SanctionTerms.tenant_id == ctx.tenant_id,
        SanctionTerms.lending_id == payload.lending_id,
        SanctionTerms.deleted_at.is_(None)))).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            f"Sanction terms already exist for Lending {payload.lending_id!r} "
            f"(id {existing.id}). Terms are entered once; corrections go through "
            "an amendment, not a second entry.")

    # ---- seed the CP/CS checklist (one artefact, CP + CS items distinguished) -------
    seeded_checklist_id: str | None = None
    if payload.cp_items or payload.cs_items:
        next_version = ((await ctx.session.execute(
            select(func.coalesce(func.max(CpcsChecklist.checklist_version), 0)).where(
                CpcsChecklist.tenant_id == ctx.tenant_id,
                CpcsChecklist.lending_id == payload.lending_id))).scalar_one() or 0) + 1
        items = (
            [{"key": i.key, "label": i.label or i.key, "condition_type": "CP",
              "required": i.required, "status": "Pending", "reason": None,
              "expiry_date": None, "evidence_ref": None, "note": i.note}
             for i in payload.cp_items]
            + [{"key": i.key, "label": i.label or i.key, "condition_type": "CS",
                "required": i.required, "status": "Pending", "reason": None,
                "expiry_date": None, "evidence_ref": None, "note": i.note}
               for i in payload.cs_items])
        seeded_checklist_id = str((await ctx.session.execute(
            pg_insert(CpcsChecklist).values(
                tenant_id=ctx.tenant_id, lending_id=payload.lending_id,
                deal_id=payload.deal_id, checklist_version=next_version, items=items,
                status="Draft", prepared_by=ctx.actor, prepared_by_id=_actor_id(ctx),
                note="Seeded from sanction terms.", created_by=ctx.actor)
            .returning(CpcsChecklist.id))).scalar_one())

    # ---- seed the covenant register --------------------------------------------------
    covenant_ids: list[str] = []
    if payload.covenants:
        if entity_id is None:
            raise ValidationAppError(
                "This lending line has no entity_id — covenants attach to the company, "
                "so link the line to a company first.")
        for c in payload.covenants:
            if c.covenant_type == "Financial" and not (
                    c.metric and c.operator and c.threshold is not None):
                raise ValidationAppError(
                    f"Covenant {c.name!r}: a Financial covenant needs metric + operator "
                    "+ threshold (e.g. dscr >= 1.20).")
            row = Covenant(tenant_id=ctx.tenant_id, entity_id=entity_id,
                           deal_id=_uuid(payload.deal_id, "deal_id") if payload.deal_id else None,
                           lending_id=_uuid(payload.lending_id, "lending_id"),
                           created_by=ctx.actor, **c.model_dump())
            ctx.session.add(row)
            await ctx.session.flush()
            covenant_ids.append(str(row.id))

    terms = SanctionTerms(
        tenant_id=ctx.tenant_id, lending_id=payload.lending_id, deal_id=payload.deal_id,
        amount_cr=payload.amount_cr, rate_kind=payload.rate_kind,
        rate_pct=payload.rate_pct, spread_pct=payload.spread_pct,
        tenor_months=payload.tenor_months, emi_amount=payload.emi_amount,
        repayment_start=payload.repayment_start, day_count=payload.day_count,
        penal_rate_pct=payload.penal_rate_pct,
        moratorium_months=payload.moratorium_months, schedule_kind=payload.schedule_kind,
        cp_items=[i.model_dump(mode="json") for i in payload.cp_items],
        cs_items=[i.model_dump(mode="json") for i in payload.cs_items],
        covenants=[c.model_dump(mode="json") for c in payload.covenants],
        seeded_checklist_id=seeded_checklist_id, seeded_covenant_ids=covenant_ids,
        note=payload.note, created_by=ctx.actor)
    ctx.session.add(terms)
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="sanction.terms",
        resource_type="sanction_terms", resource_id=str(terms.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": payload.lending_id,
                 "seeded_checklist_id": seeded_checklist_id,
                 "seeded_covenants": len(covenant_ids)}))
    await ctx.session.refresh(terms)
    return _terms_out(terms)


@router.get("/v1/internal/sanction-terms", tags=["Internal"],
            summary="The sanctioned terms for a lending line")
async def get_sanction_terms(lending_id: str = Query(min_length=1, max_length=64),
                             ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = (await ctx.session.execute(select(SanctionTerms).where(
        SanctionTerms.tenant_id == ctx.tenant_id,
        SanctionTerms.lending_id == lending_id,
        SanctionTerms.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No sanction terms recorded for Lending {lending_id!r}.")
    return _terms_out(row)


# --------------------------------------------------------------------------- #
# CAM reports — the workbench's record, the committee's working copy
# --------------------------------------------------------------------------- #
class CamOpenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(min_length=1, max_length=64)
    deal_id: str | None = Field(default=None, max_length=64)
    engine: str | None = Field(default=None, max_length=120)
    source_doc_ids: list[str] = Field(default_factory=list, max_length=50)
    prompt_doc_id: str | None = Field(default=None, max_length=64)
    note: str | None = None


class CamTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=200_000)
    # The assistant's turn usually carries the NEW full draft; storing it on the report
    # keeps "current draft" a one-row read.
    draft_md: str | None = Field(default=None, max_length=500_000)


class CamDecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(Approved|Returned|Rejected)$")
    # A return or rejection without reasons is useless to the analyst.
    note: str | None = Field(default=None, max_length=4000)


class CamSubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str | None = Field(default=None, max_length=64)


def _cam_out(row: CamReport, turns: list[CamTurn] | None = None) -> dict[str, Any]:
    out = {
        "id": str(row.id), "lending_id": row.lending_id, "deal_id": row.deal_id,
        "report_version": row.report_version, "status": row.status,
        "engine": row.engine, "source_doc_ids": row.source_doc_ids or [],
        "prompt_doc_id": row.prompt_doc_id, "draft_md": row.draft_md,
        "document_id": row.document_id,
        "prepared_by": row.prepared_by, "decided_by": row.decided_by,
        "decision_note": row.decision_note, "note": row.note,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if turns is not None:
        out["turns"] = [{"turn_no": t.turn_no, "role": t.role, "content": t.content}
                        for t in turns]
    return out


async def _cam_or_404(ctx: RequestContext, report_id: str) -> CamReport:
    row = (await ctx.session.execute(select(CamReport).where(
        CamReport.tenant_id == ctx.tenant_id,
        CamReport.id == _uuid(report_id, "report_id"),
        CamReport.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No CAM report {report_id!r}.")
    return row


@router.post("/v1/internal/cam-reports", tags=["Internal"], status_code=201,
             summary="Open a CAM version (Draft — the workbench writes into it)")
async def open_cam_report(payload: CamOpenIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "prepare_cpcs_checklist")
    await _lending_or_404(ctx, payload.lending_id)
    open_version = (await ctx.session.execute(select(CamReport).where(
        CamReport.tenant_id == ctx.tenant_id,
        CamReport.lending_id == payload.lending_id,
        CamReport.status.in_(("Draft", "Submitted")),
        CamReport.deleted_at.is_(None)))).scalars().first()
    if open_version is not None:
        raise ConflictError(
            f"CAM v{open_version.report_version} for this line is still "
            f"{open_version.status!r} — finish or withdraw it before opening another.")
    next_version = ((await ctx.session.execute(
        select(func.coalesce(func.max(CamReport.report_version), 0)).where(
            CamReport.tenant_id == ctx.tenant_id,
            CamReport.lending_id == payload.lending_id))).scalar_one() or 0) + 1
    row = CamReport(
        tenant_id=ctx.tenant_id, lending_id=payload.lending_id, deal_id=payload.deal_id,
        report_version=next_version, status="Draft", engine=payload.engine,
        source_doc_ids=payload.source_doc_ids, prompt_doc_id=payload.prompt_doc_id,
        prepared_by=ctx.actor, prepared_by_id=_actor_id(ctx), note=payload.note,
        created_by=ctx.actor)
    ctx.session.add(row)
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cam.open",
        resource_type="cam_reports", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": payload.lending_id, "report_version": next_version,
                 "engine": payload.engine}))
    await ctx.session.refresh(row)
    return _cam_out(row)


@router.post("/v1/internal/cam-reports/{report_id}/turns", tags=["Internal"],
             status_code=201, summary="Append a workbench turn (and the new draft)")
async def append_cam_turn(report_id: str, payload: CamTurnIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "prepare_cpcs_checklist")
    row = await _cam_or_404(ctx, report_id)
    if row.status not in ("Draft", "Returned"):
        raise ConflictError(
            f"CAM v{row.report_version} is {row.status!r}; the workbench writes only "
            "into a Draft (or a Returned version being amended).")
    # A Returned version being reworked goes back to Draft — the amendment IS a redraft.
    if row.status == "Returned":
        row.status = "Draft"
    next_turn = ((await ctx.session.execute(
        select(func.coalesce(func.max(CamTurn.turn_no), 0)).where(
            CamTurn.tenant_id == ctx.tenant_id,
            CamTurn.report_id == row.id))).scalar_one() or 0) + 1
    ctx.session.add(CamTurn(tenant_id=ctx.tenant_id, report_id=row.id,
                            turn_no=next_turn, role=payload.role,
                            content=payload.content, created_by=ctx.actor))
    if payload.draft_md is not None:
        row.draft_md = payload.draft_md
    row.updated_by = ctx.actor
    await ctx.session.flush()
    return {"ok": True, "turn_no": next_turn, "report_version": row.report_version,
            "status": row.status}


@router.post("/v1/internal/cam-reports/{report_id}/submit", tags=["Internal"],
             summary="Submit the CAM to the committee (maker)")
async def submit_cam_report(report_id: str, payload: CamSubmitIn,
                            ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "prepare_cpcs_checklist")
    row = await _cam_or_404(ctx, report_id)
    if row.status != "Draft":
        raise ConflictError(f"CAM v{row.report_version} is {row.status!r}; only a Draft "
                            "can be submitted.")
    if not (row.draft_md or "").strip() and not payload.document_id:
        raise ValidationAppError(
            "This CAM has no content — generate a draft (or attach the finalised "
            "document) before submitting it to the committee.")
    row.status = "Submitted"
    if payload.document_id:
        row.document_id = payload.document_id
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cam.submit",
        resource_type="cam_reports", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": row.lending_id, "report_version": row.report_version,
                 "document_id": row.document_id}))
    return _cam_out(row)


@router.post("/v1/internal/cam-reports/{report_id}/decide", tags=["Internal"],
             summary="Committee decision on the CAM: Approve / Return / Reject")
async def decide_cam_report(report_id: str, payload: CamDecideIn,
                            ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_cpcs_checklist")
    if ctx.user is not None and not (set(ctx.user.roles) & _COMMITTEE_AUTHORITY):
        raise ValidationAppError(
            f"Deciding a CAM needs committee authority "
            f"({', '.join(sorted(_COMMITTEE_AUTHORITY))}).")
    row = await _cam_or_404(ctx, report_id)
    if row.status != "Submitted":
        raise ConflictError(f"CAM v{row.report_version} is {row.status!r}; only a "
                            "Submitted CAM can be decided.")
    decider_id = _actor_id(ctx)
    # Maker-checker: the analyst who prepared the CAM may not decide it.
    if (decider_id is not None and decider_id == row.prepared_by_id) or (
            decider_id is None and ctx.actor == row.prepared_by):
        raise ValidationAppError(
            "The CAM must be decided by a DIFFERENT person than its preparer.")
    if payload.decision in ("Returned", "Rejected") and not (payload.note or "").strip():
        raise ValidationAppError(
            f"A {payload.decision} decision must say why — the note reaches the analyst.")
    row.status = payload.decision
    row.decided_by = ctx.actor
    row.decided_by_id = decider_id
    row.decision_note = payload.note
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="cam.decide",
        resource_type="cam_reports", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": row.lending_id, "report_version": row.report_version,
                 "decision": payload.decision}))
    return _cam_out(row)


@router.get("/v1/internal/cam-reports", tags=["Internal"],
            summary="CAM version history for a lending line")
async def list_cam_reports(lending_id: str = Query(min_length=1, max_length=64),
                           ctx: RequestContext = Depends(get_context)) -> list[dict[str, Any]]:
    rows = (await ctx.session.execute(select(CamReport).where(
        CamReport.tenant_id == ctx.tenant_id,
        CamReport.lending_id == lending_id,
        CamReport.deleted_at.is_(None)).order_by(CamReport.report_version))).scalars().all()
    return [_cam_out(r) for r in rows]


@router.get("/v1/internal/cam-reports/{report_id}", tags=["Internal"],
            summary="One CAM version, with its workbench transcript")
async def get_cam_report(report_id: str,
                         ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = (await ctx.session.execute(select(CamReport).where(
        CamReport.tenant_id == ctx.tenant_id,
        CamReport.id == _uuid(report_id, "report_id"),
        CamReport.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No CAM report {report_id!r}.")
    turns = (await ctx.session.execute(select(CamTurn).where(
        CamTurn.tenant_id == ctx.tenant_id,
        CamTurn.report_id == row.id).order_by(CamTurn.turn_no))).scalars().all()
    return _cam_out(row, turns=list(turns))
