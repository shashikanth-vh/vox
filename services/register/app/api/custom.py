"""Bespoke endpoints that go beyond generic CRUD.

* Financials version-aware create + version history
* Syndication → nested lenders convenience routes
* Reference vocabularies (dropdown source of truth)
* Audit-log reader
* Entity dossier — the entity-centric 360° view stitching a company's whole footprint
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app import authz
from app import storage as storage_mod
from app.core.config import get_settings
from app.core.errors import ForbiddenError, NotFoundError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models import (
    Deal,
    Document,
    DocumentChecklistItem,
    Entity,
    ExternalIntelligence,
    Financial,
    Interaction,
)
from app.models.system import RefValue, TenantSettings
from app.models.trackers import (
    AssetMonetisation,
    LendingTracker,
    SyndicationLender,
    SyndicationTracker,
)
from app.repositories.crud import CRUDRepository
from app.repositories.documents import (
    data_register,
    documents_for_subject,
    register_document,
    store_and_register,
)
from app.repositories.financials import create_version
from app.repositories.interactions import create_interaction, timeline
from app.repositories.subjects import load_subject
from app.schemas import resources as s

router = api_router()


# --------------------------------------------------------------------------- #
# RBAC helpers — every custom endpoint funnels through these two, which in turn
# funnel through the CENTRAL scope evaluator (app.authz.scope). One definition
# of "in my scope" across list, GET, documents, timelines, dossier and audit.
# --------------------------------------------------------------------------- #
async def _ensure_subject_scope(ctx: RequestContext, operation: str,
                                subject_type: str | None,
                                subject_id: uuid.UUID | None) -> None:
    """Gate a WRITE that references a polymorphic subject (document upload,
    interaction log): the operation must be granted, and a SCOPED grant must cover
    the referenced line/company through the central evaluator."""
    if ctx.user is None:
        # Machine caller: authorize against its SERVICE PRINCIPAL's allowlist (a named
        # service key), or legacy compat for a generic key. enforce_operation raises 403
        # when the service is not permitted this operation — no more blanket ingestion pass.
        authz.enforce_operation(None, operation)
        return
    granted = authz.enforce_operation(ctx.user, operation)
    if granted is not authz.Access.SCOPED:
        return
    if subject_type is None or subject_id is None:
        return  # subject validated (and 404ed) by the caller
    from app.authz import scope as scope_mod

    user_scope = await scope_mod.build_scope(ctx, ctx.user)
    subj = await load_subject(ctx.session, ctx.tenant_id, subject_type, subject_id)
    if subj is None:
        return  # the caller raises the proper 404
    if subject_type == "Entity":
        if await scope_mod.entity_in_scope(ctx, user_scope, subj.id):
            return
    elif subject_type == "Counterparty":
        return  # FI records carry no company linkage; the operation grant governs
    else:
        if await scope_mod.row_in_scope(ctx, user_scope, subject_type, subj):
            return
        entity_id = getattr(subj, "entity_id", None)
        if entity_id is not None and await scope_mod.entity_in_scope(
            ctx, user_scope, entity_id
        ):
            return
    raise ForbiddenError(
        f"Scoped access: this {subject_type}'s company is not in your scope.")


async def _ensure_company_read(ctx: RequestContext, entity_id: uuid.UUID | None) -> None:
    """Gate a company-wide READ (dossier, documents, timeline, data register) on the
    clients view: NONE → 403; SCOPED → the company must be connected to the user."""
    if ctx.user is None or entity_id is None:
        return
    from app.authz import scope as scope_mod
    from app.authz.engine import view_access
    from app.authz.matrix import Access

    granted = view_access(ctx.user, "clients")
    if granted is Access.NONE:
        raise ForbiddenError(
            f"Role(s) {sorted(ctx.user.roles)} have no access to company records.")
    if granted is not Access.SCOPED:
        return
    user_scope = await scope_mod.build_scope(ctx, ctx.user)
    if not await scope_mod.entity_in_scope(ctx, user_scope, entity_id):
        raise ForbiddenError("Scoped access: this company is not in your scope.")

_financial_repo = CRUDRepository(Financial)
_synlender_repo = CRUDRepository(
    SyndicationLender, filterable=["status", "syndication_id", "counterparty_id"]
)
_intel_repo = CRUDRepository(ExternalIntelligence)
_document_repo = CRUDRepository(Document)

# Built-in defaults for per-tenant settings (ATLAS alert thresholds, in days). A tenant's
# stored settings are merged over these on read, so a fresh tenant already has sane values.
DEFAULT_SETTINGS: dict[str, Any] = {
    "thresholds": {
        "staleLead": 21, "lendAmber": 7, "lendRed": 14, "showcase": 10,
        "lenderSilent": 14, "ballUs": 7, "undisb": 30, "pendAge": 10,
    },
}


def _merge_settings(stored: dict | None) -> dict:
    """Two-level merge of stored settings over DEFAULT_SETTINGS."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Financials: versioned create + history
# --------------------------------------------------------------------------- #
@router.post("/v1/financials", response_model=s.FinancialRead, status_code=201,
             tags=["Financials"], summary="Create a new financial version")
async def create_financial_version(
    payload: s.FinancialCreate,
    response: Response,
    ctx: RequestContext = Depends(get_context),
) -> Any:
    # Financials are FI data: writing one requires the FI write operation (edit_fi_record),
    # not the far-looser add_company_note — otherwise a Deal Analyst / AM RM who may only
    # *note* a company could publish financial statements. A SCOPED writer must also be
    # connected to the company.
    await _ensure_subject_scope(ctx, "edit_fi_record", "Entity", payload.entity_id)
    obj = await create_version(ctx.session, ctx.tenant_id, ctx.actor,
                               payload.model_dump(exclude_unset=False))
    response.headers["ETag"] = f'"{obj.version}"'
    return s.FinancialRead.model_validate(obj)


@router.get("/v1/financials/history", response_model=list[s.FinancialRead],
            tags=["Financials"], summary="Full version history for a statement")
async def financial_history(
    entity_id: uuid.UUID,
    statement_type: str,
    ctx: RequestContext = Depends(get_context),
    period_end: str | None = Query(default=None),
) -> Any:
    await _ensure_company_read(ctx, entity_id)
    conds = [
        Financial.tenant_id == ctx.tenant_id,
        Financial.entity_id == entity_id,
        Financial.statement_type == statement_type,
        Financial.deleted_at.is_(None),
    ]
    if period_end:
        conds.append(Financial.period_end == period_end)
    rows = (
        await ctx.session.execute(
            select(Financial).where(*conds).order_by(
                Financial.period_end.desc(), Financial.version_no.desc()
            )
        )
    ).scalars().all()
    return [s.FinancialRead.model_validate(r) for r in rows]


# --------------------------------------------------------------------------- #
# Syndication → nested lenders
# --------------------------------------------------------------------------- #
@router.get("/v1/syndication/{syndication_id}/lenders",
            response_model=list[s.SyndicationLenderRead], tags=["Syndication Lenders"],
            summary="List lenders on a syndication")
async def list_syndication_lenders(
    syndication_id: uuid.UUID,
    ctx: RequestContext = Depends(get_context),
) -> Any:
    syn = await load_subject(ctx.session, ctx.tenant_id, "Syndication", syndication_id)
    if syn is not None:
        await _ensure_company_read(ctx, syn.entity_id)
    rows = (
        await ctx.session.execute(
            select(SyndicationLender)
            .where(
                SyndicationLender.tenant_id == ctx.tenant_id,
                SyndicationLender.syndication_id == syndication_id,
                SyndicationLender.deleted_at.is_(None),
            )
            .order_by(SyndicationLender.created_at.asc())
        )
    ).scalars().all()
    return [s.SyndicationLenderRead.model_validate(r) for r in rows]


@router.post("/v1/syndication/{syndication_id}/lenders",
             response_model=s.SyndicationLenderRead, status_code=201,
             tags=["Syndication Lenders"], summary="Add a lender to a syndication")
async def add_syndication_lender(
    syndication_id: uuid.UUID,
    payload: s.SyndicationLenderCreate,
    ctx: RequestContext = Depends(get_context),
) -> Any:
    # A SCOPED Syn RM may only touch mandates on THEIR lines/companies.
    await _ensure_subject_scope(ctx, "add_lender_to_mandate", "Syndication",
                                syndication_id)
    data = payload.model_dump(exclude_unset=False)
    data["syndication_id"] = syndication_id
    obj = await _synlender_repo.create(ctx.session, ctx.tenant_id, ctx.actor, data)
    return s.SyndicationLenderRead.model_validate(obj)


# --------------------------------------------------------------------------- #
# Interactions — the user-interaction timeline
# --------------------------------------------------------------------------- #
@router.post("/v1/interactions", response_model=s.InteractionRead, status_code=201,
             tags=["Interactions"], summary="Log an interaction (timeline entry)")
async def log_interaction(
    payload: s.InteractionCreate,
    response: Response,
    ctx: RequestContext = Depends(get_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    await _ensure_subject_scope(ctx, "log_interaction",
                                payload.subject_type, payload.subject_id)
    # Idempotent like the generic creates: a retried capture (VocX flaky uplink, SDK
    # retry) with the same key replays the original interaction instead of duplicating.
    if idempotency_key:
        from app.models.system import IdempotencyKey

        existing = (
            await ctx.session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == ctx.tenant_id,
                    IdempotencyKey.key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            response.status_code = existing.status_code
            response.headers["Idempotency-Replay"] = "true"
            return existing.response_body

    obj = await create_interaction(
        ctx.session, ctx.tenant_id, ctx.actor, payload.model_dump(exclude_unset=False)
    )
    result = s.InteractionRead.model_validate(obj)
    if idempotency_key:
        from datetime import timedelta

        from app.core.config import get_settings
        from app.models.system import IdempotencyKey

        await ctx.session.flush()
        ctx.session.add(IdempotencyKey(
            tenant_id=ctx.tenant_id, key=idempotency_key, request_hash="interaction",
            method="POST", path="/v1/interactions", status_code=201,
            response_body=result.model_dump(mode="json"),
            expires_at=datetime.now(UTC) + timedelta(
                hours=get_settings().idempotency_ttl_hours),
        ))
    response.headers["ETag"] = f'"{obj.version}"'
    return result


def _timeline_routes(path_prefix: str, subject_type: str) -> None:
    """Register GET/POST /v1/<path_prefix>/{id}/interactions for a subject type."""
    label = subject_type.lower()

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/interactions",
                response_model=list[s.InteractionRead], tags=["Interactions"],
                summary=f"Interaction timeline for a {label}", name=f"timeline_{subject_type}")
    async def _get(subject_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
                   limit: int = Query(default=100, ge=1, le=1000)) -> Any:
        subj = await load_subject(ctx.session, ctx.tenant_id, subject_type, subject_id)
        entity_id = subj.id if subject_type == "Entity" else getattr(subj, "entity_id", None)
        await _ensure_company_read(ctx, entity_id)
        rows = await timeline(ctx.session, ctx.tenant_id, subject_type, subject_id, limit=limit)
        return [s.InteractionRead.model_validate(r) for r in rows]

    @router.post(f"/v1/{path_prefix}/{{subject_id}}/interactions",
                 response_model=s.InteractionRead, status_code=201, tags=["Interactions"],
                 summary=f"Log an interaction against a {label}", name=f"log_{subject_type}")
    async def _post(subject_id: uuid.UUID, payload: s.InteractionCreate,
                    ctx: RequestContext = Depends(get_context)) -> Any:
        await _ensure_subject_scope(ctx, "log_interaction", subject_type, subject_id)
        data = payload.model_dump(exclude_unset=False)
        data["subject_type"] = subject_type  # path wins over body
        data["subject_id"] = subject_id
        obj = await create_interaction(ctx.session, ctx.tenant_id, ctx.actor, data)
        return s.InteractionRead.model_validate(obj)


# Nested timeline routes for every subject type (matches ATLAS refType).
_timeline_routes("leads", "Lead")
_timeline_routes("deals", "Deal")
_timeline_routes("entities", "Entity")
_timeline_routes("counterparties", "Counterparty")
_timeline_routes("lending", "Lending")
_timeline_routes("syndication", "Syndication")
_timeline_routes("asset-monetisation", "AssetMonetisation")


# --------------------------------------------------------------------------- #
# Reference vocabularies
# --------------------------------------------------------------------------- #
@router.get("/v1/ref", tags=["Reference"], summary="All reference vocabularies")
async def list_ref(ctx: RequestContext = Depends(get_context)) -> dict[str, list[dict]]:
    rows = (
        await ctx.session.execute(
            select(RefValue).where(RefValue.is_active.is_(True)).order_by(
                RefValue.category, RefValue.sort_order, RefValue.value
            )
        )
    ).scalars().all()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.category, []).append({"value": r.value, "label": r.label or r.value})
    return out


@router.get("/v1/ref/{category}", tags=["Reference"], summary="One reference vocabulary")
async def list_ref_category(
    category: str, ctx: RequestContext = Depends(get_context)
) -> list[dict]:
    rows = (
        await ctx.session.execute(
            select(RefValue)
            .where(RefValue.category == category, RefValue.is_active.is_(True))
            .order_by(RefValue.sort_order, RefValue.value)
        )
    ).scalars().all()
    return [{"value": r.value, "label": r.label or r.value} for r in rows]


# --------------------------------------------------------------------------- #
# Audit log (read-only)
# --------------------------------------------------------------------------- #
@router.get("/v1/audit", tags=["Audit"], summary="Read the audit trail")
async def read_audit(
    ctx: RequestContext = Depends(get_context),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    # RBAC guardrail: the audit view is Admin-only (immutable even in the live matrix).
    if ctx.user is not None:
        from app.authz.engine import view_access
        from app.authz.matrix import Access

        if view_access(ctx.user, "audit") is Access.NONE:
            raise ForbiddenError("The audit trail is Admin-only.")
    elif get_settings().enforce_rbac:
        raise ForbiddenError("The audit trail requires a user context (X-User-Email).")
    conds = [AuditLog.tenant_id == ctx.tenant_id]
    if resource_type:
        conds.append(AuditLog.resource_type == resource_type)
    if resource_id:
        conds.append(AuditLog.resource_id == resource_id)
    rows = (
        await ctx.session.execute(
            select(AuditLog).where(*conds).order_by(AuditLog.at.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": r.id, "at": r.at.isoformat(), "actor": r.actor, "action": r.action,
            "resource_type": r.resource_type, "resource_id": r.resource_id,
            "request_id": r.request_id, "changes": r.changes,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Entity dossier — the entity-centric 360° view
# --------------------------------------------------------------------------- #
@router.get("/v1/entities/{entity_id}/dossier", tags=["Entities"],
            summary="Everything the Register knows about one entity")
async def entity_dossier(
    entity_id: uuid.UUID,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    """Stitches a company's whole footprint onto one response — the payoff of the
    entity-centric (not deal-centric) model: how many deals, across which businesses,
    with what current financials, latest interactions and open intelligence."""
    await _ensure_company_read(ctx, entity_id)
    tid = ctx.tenant_id

    async def _count(model) -> int:  # noqa: ANN001
        return (
            await ctx.session.execute(
                select(func.count()).select_from(model).where(
                    model.tenant_id == tid, model.entity_id == entity_id,
                    model.deleted_at.is_(None),
                )
            )
        ).scalar_one()

    entity = (
        await ctx.session.execute(
            select(Entity).where(Entity.id == entity_id, Entity.tenant_id == tid,
                                 Entity.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if entity is None:
        raise NotFoundError(f"entity '{entity_id}' not found.")

    deals = (
        await ctx.session.execute(
            select(Deal).where(Deal.tenant_id == tid, Deal.entity_id == entity_id,
                               Deal.deleted_at.is_(None))
        )
    ).scalars().all()

    latest_interactions = (
        await ctx.session.execute(
            select(Interaction)
            .where(Interaction.tenant_id == tid, Interaction.entity_id == entity_id,
                   Interaction.deleted_at.is_(None))
            .order_by(Interaction.occurred_at.desc().nullslast())
            .limit(5)
        )
    ).scalars().all()

    open_intel = (
        await ctx.session.execute(
            select(ExternalIntelligence)
            .where(ExternalIntelligence.tenant_id == tid,
                   ExternalIntelligence.entity_id == entity_id,
                   ExternalIntelligence.signal.in_(["RED", "AMBER"]),
                   ExternalIntelligence.is_dismissed.is_(False),
                   ExternalIntelligence.deleted_at.is_(None))
            .order_by(ExternalIntelligence.observed_at.desc().nullslast())
            .limit(10)
        )
    ).scalars().all()

    return {
        "entity": s.EntityRead.model_validate(entity).model_dump(mode="json"),
        "counts": {
            "deals": len(deals),
            "lending": await _count(LendingTracker),
            "syndication": await _count(SyndicationTracker),
            "asset_monetisation": await _count(AssetMonetisation),
            "financials": await _count(Financial),
            "interactions": await _count(Interaction),
        },
        "deals": [s.DealRead.model_validate(d).model_dump(mode="json") for d in deals],
        "latest_interactions": [
            s.InteractionRead.model_validate(t).model_dump(mode="json") for t in latest_interactions
        ],
        "open_intelligence": [
            s.ExternalIntelRead.model_validate(i).model_dump(mode="json") for i in open_intel
        ],
    }


# --------------------------------------------------------------------------- #
# External intelligence — shared triage (acknowledge / dismiss)
# --------------------------------------------------------------------------- #
@router.post("/v1/external-intelligence/{intel_id}/acknowledge",
             response_model=s.ExternalIntelRead, tags=["External Intelligence"],
             summary="Acknowledge an intelligence signal")
async def acknowledge_intel(
    intel_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
) -> Any:
    row = await _intel_repo.get(ctx.session, ctx.tenant_id, intel_id)
    # Acknowledging is a WRITE, not a read: require the intel write op (edit_intel) so a
    # read-only clients viewer (Credit Head / Deal Analyst) cannot mutate the signal.
    await _ensure_subject_scope(ctx, "edit_intel", "Entity", row.entity_id)
    obj = await _intel_repo.update(
        ctx.session, ctx.tenant_id, intel_id, ctx.actor,
        {"acknowledged_by": ctx.actor, "acknowledged_at": datetime.now(UTC), "is_dismissed": False},
    )
    return s.ExternalIntelRead.model_validate(obj)


@router.post("/v1/external-intelligence/{intel_id}/dismiss",
             response_model=s.ExternalIntelRead, tags=["External Intelligence"],
             summary="Dismiss an intelligence signal")
async def dismiss_intel(
    intel_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
) -> Any:
    row = await _intel_repo.get(ctx.session, ctx.tenant_id, intel_id)
    # Dismissing hides a signal — a WRITE gated on edit_intel + company scope.
    await _ensure_subject_scope(ctx, "edit_intel", "Entity", row.entity_id)
    obj = await _intel_repo.update(
        ctx.session, ctx.tenant_id, intel_id, ctx.actor,
        {"is_dismissed": True, "acknowledged_by": ctx.actor, "acknowledged_at": datetime.now(UTC)},
    )
    return s.ExternalIntelRead.model_validate(obj)


# --------------------------------------------------------------------------- #
# Per-tenant settings (alert thresholds etc.)
# --------------------------------------------------------------------------- #
@router.get("/v1/settings", response_model=s.SettingsRead, tags=["Settings"],
            summary="Read this tenant's settings (defaults merged in)")
async def get_tenant_settings(ctx: RequestContext = Depends(get_context)) -> Any:
    row = (
        await ctx.session.execute(
            select(TenantSettings).where(TenantSettings.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    return s.SettingsRead(settings=_merge_settings(row.settings if row else None))


@router.put("/v1/settings", response_model=s.SettingsRead, tags=["Settings"],
            summary="Replace this tenant's settings")
async def put_tenant_settings(
    payload: s.SettingsUpdate, ctx: RequestContext = Depends(get_context),
) -> Any:
    # Tenant-wide business config is a leadership control, not a data operation.
    if ctx.user is not None and not (ctx.user.roles & {"Admin", "Management"}):
        raise ForbiddenError("Tenant settings may only be changed by Admin/Management.")
    if ctx.user is None and get_settings().enforce_rbac:
        raise ForbiddenError("Tenant settings require a user context (X-User-Email).")
    row = (
        await ctx.session.execute(
            select(TenantSettings).where(TenantSettings.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantSettings(tenant_id=ctx.tenant_id, settings=payload.settings,
                             created_by=ctx.actor, updated_by=ctx.actor)
        ctx.session.add(row)
    else:
        row.settings = payload.settings
        row.updated_by = ctx.actor
    await ctx.session.flush()
    return s.SettingsRead(settings=_merge_settings(row.settings))


# --------------------------------------------------------------------------- #
# Lender engagement matrix — derived from syndication_lenders (never stored)
# --------------------------------------------------------------------------- #
@router.get("/v1/entities/{entity_id}/lender-matrix", tags=["Entities"],
            summary="Lender engagement grid for one entity (derived)")
async def entity_lender_matrix(
    entity_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    """Rolls up every lender's posture across this entity's syndications, grouped by
    lender. Derived live from ``syndication_lenders`` so it never drifts from the source
    (the ATLAS mock even carried matrix-vs-text conflicts — the reason not to store it)."""
    await _ensure_company_read(ctx, entity_id)
    rows = (
        await ctx.session.execute(
            select(SyndicationLender, SyndicationTracker.tracker_no)
            .join(SyndicationTracker, SyndicationLender.syndication_id == SyndicationTracker.id)
            .where(
                SyndicationLender.tenant_id == ctx.tenant_id,
                SyndicationTracker.entity_id == entity_id,
                SyndicationLender.deleted_at.is_(None),
                SyndicationTracker.deleted_at.is_(None),
            )
        )
    ).all()

    def _recency(ln: SyndicationLender):
        return (ln.chased_date or ln.response_date or ln.since or ln.updated_at.date(),)

    matrix: dict[str, dict[str, Any]] = {}
    for ln, tracker_no in rows:
        entry = {
            "syndication_id": str(ln.syndication_id), "tracker_no": tracker_no,
            "status": ln.status, "since": ln.since.isoformat() if ln.since else None,
            "response_date": ln.response_date.isoformat() if ln.response_date else None,
            "chased_date": ln.chased_date.isoformat() if ln.chased_date else None,
            "is_existing": ln.is_existing,
        }
        slot = matrix.setdefault(ln.lender_name, {"lender_name": ln.lender_name, "entries": []})
        slot["entries"].append(entry)
        # Latest posture = the most recent entry across this lender's syndications.
        if "_recency" not in slot or _recency(ln) >= slot["_recency"]:
            slot["_recency"] = _recency(ln)
            slot["latest"] = entry
    for slot in matrix.values():
        slot.pop("_recency", None)

    if not matrix:
        # Distinguish "entity has no lenders" from "no such entity".
        exists = (
            await ctx.session.execute(
                select(Entity.id).where(Entity.id == entity_id, Entity.tenant_id == ctx.tenant_id,
                                        Entity.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if exists is None:
            raise NotFoundError(f"entity '{entity_id}' not found.")
    return {"entity_id": str(entity_id), "lenders": list(matrix.values())}


# --------------------------------------------------------------------------- #
# Documents — ATLAS "Data Register" (catalog + checklist)
# --------------------------------------------------------------------------- #
@router.post("/v1/documents", response_model=s.DocumentRead, status_code=201,
             tags=["Documents"], summary="Register a document (subject-aware)")
async def create_document(
    payload: s.DocumentCreate,
    response: Response,
    ctx: RequestContext = Depends(get_context),
) -> Any:
    await _ensure_subject_scope(ctx, "upload_remove_documents",
                                payload.subject_type, payload.subject_id)
    obj = await register_document(
        ctx.session, ctx.tenant_id, ctx.actor, payload.model_dump(exclude_unset=False)
    )
    response.headers["ETag"] = f'"{obj.version}"'
    return s.DocumentRead.model_validate(obj)


@router.post("/v1/documents/upload", response_model=s.DocumentRead, status_code=201,
             tags=["Documents"],
             summary="Upload a document file (bytes → object storage / inline)")
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    subject_type: str = Form(...),
    subject_id: uuid.UUID = Form(...),
    slot_key: str | None = Form(default=None),
    section: str | None = Form(default=None),
    title: str | None = Form(default=None),
    doc_type: str | None = Form(default=None),
    is_required: bool = Form(default=False),
    status: str = Form(default="On File"),
    notes: str | None = Form(default=None),
    ctx: RequestContext = Depends(get_context),
) -> Any:
    await _ensure_subject_scope(ctx, "upload_remove_documents", subject_type, subject_id)
    obj = await store_and_register(
        ctx.session, ctx.tenant_id, ctx.actor,
        subject_type=subject_type, subject_id=subject_id, data=await file.read(),
        filename=file.filename, content_type=file.content_type, slot_key=slot_key,
        section=section, title=title, doc_type=doc_type, is_required=is_required,
        status=status, notes=notes,
    )
    response.headers["ETag"] = f'"{obj.version}"'
    return s.DocumentRead.model_validate(obj)


def _document_routes(path_prefix: str, subject_type: str) -> None:
    """Register GET/POST /v1/<path_prefix>/{id}/documents and the data-register rollup."""
    label = subject_type.lower()

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/documents",
                response_model=list[s.DocumentRead], tags=["Documents"],
                summary=f"Documents on file for a {label} (company-wide by default)",
                name=f"documents_{subject_type}")
    async def _get(subject_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
                   limit: int = Query(default=200, ge=1, le=1000),
                   scope: str = Query(default="auto",
                                      description="'auto'/'entity' = all of the company's "
                                                  "documents; 'subject' = only this record's")
                   ) -> Any:
        subj = await load_subject(ctx.session, ctx.tenant_id, subject_type, subject_id)
        if subj is not None:
            eid = subj.id if subject_type == "Entity" else getattr(subj, "entity_id", None)
            await _ensure_company_read(ctx, eid)
        rows = await documents_for_subject(
            ctx.session, ctx.tenant_id, subject_type, subject_id, scope=scope, limit=limit
        )
        return [s.DocumentRead.model_validate(r) for r in rows]

    @router.post(f"/v1/{path_prefix}/{{subject_id}}/documents",
                 response_model=s.DocumentRead, status_code=201, tags=["Documents"],
                 summary=f"Register a document against a {label}", name=f"add_document_{subject_type}")
    async def _post(subject_id: uuid.UUID, payload: s.DocumentCreate,
                    response: Response, ctx: RequestContext = Depends(get_context)) -> Any:
        await _ensure_subject_scope(ctx, "upload_remove_documents", subject_type, subject_id)
        data = payload.model_dump(exclude_unset=False)
        data["subject_type"] = subject_type  # path wins over body
        data["subject_id"] = subject_id
        obj = await register_document(ctx.session, ctx.tenant_id, ctx.actor, data)
        response.headers["ETag"] = f'"{obj.version}"'
        return s.DocumentRead.model_validate(obj)

    @router.post(f"/v1/{path_prefix}/{{subject_id}}/documents/upload",
                 response_model=s.DocumentRead, status_code=201, tags=["Documents"],
                 summary=f"Upload a document file for a {label}",
                 name=f"upload_document_{subject_type}")
    async def _upload(subject_id: uuid.UUID, response: Response,
                      file: UploadFile = File(...),
                      slot_key: str | None = Form(default=None),
                      section: str | None = Form(default=None),
                      title: str | None = Form(default=None),
                      doc_type: str | None = Form(default=None),
                      is_required: bool = Form(default=False),
                      status: str = Form(default="On File"),
                      notes: str | None = Form(default=None),
                      ctx: RequestContext = Depends(get_context)) -> Any:
        await _ensure_subject_scope(ctx, "upload_remove_documents", subject_type, subject_id)
        obj = await store_and_register(
            ctx.session, ctx.tenant_id, ctx.actor,
            subject_type=subject_type, subject_id=subject_id, data=await file.read(),
            filename=file.filename, content_type=file.content_type, slot_key=slot_key,
            section=section, title=title, doc_type=doc_type, is_required=is_required,
            status=status, notes=notes,
        )
        response.headers["ETag"] = f'"{obj.version}"'
        return s.DocumentRead.model_validate(obj)

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/data-register", tags=["Documents"],
                summary=f"Data Register (checklist + progress) for a {label}",
                name=f"data_register_{subject_type}")
    async def _rollup(subject_id: uuid.UUID,
                      ctx: RequestContext = Depends(get_context),
                      scope: str = Query(default="auto",
                                         description="'auto'/'entity' = the company's whole "
                                                     "document set; 'subject' = only this record")
                      ) -> dict[str, Any]:
        subj = await load_subject(ctx.session, ctx.tenant_id, subject_type, subject_id)
        if subj is not None:
            eid = subj.id if subject_type == "Entity" else getattr(subj, "entity_id", None)
            await _ensure_company_read(ctx, eid)
        return await data_register(ctx.session, ctx.tenant_id, subject_type, subject_id,
                                   scope=scope)


# Nested document + data-register routes for every subject type (matches ATLAS refType).
_document_routes("leads", "Lead")
_document_routes("deals", "Deal")
_document_routes("entities", "Entity")
_document_routes("counterparties", "Counterparty")
_document_routes("lending", "Lending")
_document_routes("syndication", "Syndication")
_document_routes("asset-monetisation", "AssetMonetisation")


@router.get("/v1/document-checklist/template", tags=["Documents"],
            summary="The Data Register checklist template, grouped by section")
async def document_checklist_template(
    ctx: RequestContext = Depends(get_context),
    applies_to: str = Query(default="*", description="Subject type, or '*' for all"),
) -> dict[str, Any]:
    """The configurable checklist as the ATLAS modal renders it — sections, each with its
    slots — before any documents are attached. Includes items scoped to ``applies_to`` and
    the universal ('*') items."""
    conds = [
        DocumentChecklistItem.tenant_id == ctx.tenant_id,
        DocumentChecklistItem.is_active.is_(True),
        DocumentChecklistItem.deleted_at.is_(None),
    ]
    if applies_to != "*":
        conds.append(or_(DocumentChecklistItem.applies_to == "*",
                         DocumentChecklistItem.applies_to == applies_to))
    rows = (
        await ctx.session.execute(
            select(DocumentChecklistItem).where(*conds).order_by(
                DocumentChecklistItem.section_order, DocumentChecklistItem.section,
                DocumentChecklistItem.sort_order, DocumentChecklistItem.label,
            )
        )
    ).scalars().all()

    sections: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    required_total = 0
    for it in rows:
        sec = index.get(it.section)
        if sec is None:
            sec = {"section": it.section, "section_order": it.section_order,
                   "required_total": 0, "items": []}
            index[it.section] = sec
            sections.append(sec)
        if it.is_required:
            required_total += 1
            sec["required_total"] += 1
        sec["items"].append({
            "slot_key": it.slot_key, "label": it.label, "is_required": it.is_required,
            "hint": it.hint,
        })
    return {"applies_to": applies_to, "required_total": required_total, "sections": sections}


@router.get("/v1/documents/{doc_id}/content", tags=["Documents"],
            summary="Fetch a document's bytes (inline) or its storage reference")
async def download_document(
    doc_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
) -> Any:
    """Streams inline-stored bytes; for object-storage-backed documents, redirects to a
    freshly-signed URL (or streams through the API if configured); redirects to an http(s)
    ``storage_uri``; otherwise returns the reference. Metadata-only records 404."""
    doc = await _document_repo.get(ctx.session, ctx.tenant_id, doc_id)
    # Scope: the bytes are as sensitive as the company file they belong to.
    await _ensure_company_read(ctx, doc.entity_id)
    filename = doc.original_filename or doc.title
    if doc.inline_content is not None:
        return Response(
            content=doc.inline_content,
            media_type=doc.content_type or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    if doc.storage_uri:
        parsed = storage_mod.parse_s3_uri(doc.storage_uri)
        store = storage_mod.get_storage()
        if parsed and store is not None:
            _bucket, key = parsed
            if get_settings().s3_stream_through_api:
                blob = await store.get(key)
                return Response(
                    content=blob,
                    media_type=doc.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'},
                )
            return RedirectResponse(await store.presigned_get_url(key, filename=filename))
        if doc.storage_uri.startswith(("http://", "https://")):
            return RedirectResponse(doc.storage_uri)
        return {"storage_backend": doc.storage_backend, "storage_uri": doc.storage_uri,
                "note": "Fetch these bytes from object storage (e.g. via a presigned URL)."}
    raise NotFoundError(f"document '{doc_id}' has no bytes on record (metadata-only).")
