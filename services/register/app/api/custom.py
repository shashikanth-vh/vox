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

from fastapi import Depends, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.core.errors import NotFoundError
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
from app.repositories.documents import data_register, register_document
from app.repositories.financials import create_version
from app.repositories.interactions import create_interaction, timeline
from app.schemas import resources as s

router = api_router()

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
) -> Any:
    obj = await create_interaction(
        ctx.session, ctx.tenant_id, ctx.actor, payload.model_dump(exclude_unset=False)
    )
    response.headers["ETag"] = f'"{obj.version}"'
    return s.InteractionRead.model_validate(obj)


def _timeline_routes(path_prefix: str, subject_type: str) -> None:
    """Register GET/POST /v1/<path_prefix>/{id}/interactions for a subject type."""
    label = subject_type.lower()

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/interactions",
                response_model=list[s.InteractionRead], tags=["Interactions"],
                summary=f"Interaction timeline for a {label}", name=f"timeline_{subject_type}")
    async def _get(subject_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
                   limit: int = Query(default=100, ge=1, le=1000)) -> Any:
        rows = await timeline(ctx.session, ctx.tenant_id, subject_type, subject_id, limit=limit)
        return [s.InteractionRead.model_validate(r) for r in rows]

    @router.post(f"/v1/{path_prefix}/{{subject_id}}/interactions",
                 response_model=s.InteractionRead, status_code=201, tags=["Interactions"],
                 summary=f"Log an interaction against a {label}", name=f"log_{subject_type}")
    async def _post(subject_id: uuid.UUID, payload: s.InteractionCreate,
                    ctx: RequestContext = Depends(get_context)) -> Any:
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
        from app.core.errors import NotFoundError

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
    obj = await register_document(
        ctx.session, ctx.tenant_id, ctx.actor, payload.model_dump(exclude_unset=False)
    )
    response.headers["ETag"] = f'"{obj.version}"'
    return s.DocumentRead.model_validate(obj)


def _document_routes(path_prefix: str, subject_type: str) -> None:
    """Register GET/POST /v1/<path_prefix>/{id}/documents and the data-register rollup."""
    label = subject_type.lower()

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/documents",
                response_model=list[s.DocumentRead], tags=["Documents"],
                summary=f"Documents on file for a {label}", name=f"documents_{subject_type}")
    async def _get(subject_id: uuid.UUID, ctx: RequestContext = Depends(get_context),
                   limit: int = Query(default=200, ge=1, le=1000)) -> Any:
        rows = (
            await ctx.session.execute(
                select(Document)
                .where(Document.tenant_id == ctx.tenant_id,
                       Document.subject_type == subject_type,
                       Document.subject_id == subject_id,
                       Document.deleted_at.is_(None))
                .order_by(Document.uploaded_at.desc().nullslast(), Document.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [s.DocumentRead.model_validate(r) for r in rows]

    @router.post(f"/v1/{path_prefix}/{{subject_id}}/documents",
                 response_model=s.DocumentRead, status_code=201, tags=["Documents"],
                 summary=f"Register a document against a {label}", name=f"add_document_{subject_type}")
    async def _post(subject_id: uuid.UUID, payload: s.DocumentCreate,
                    response: Response, ctx: RequestContext = Depends(get_context)) -> Any:
        data = payload.model_dump(exclude_unset=False)
        data["subject_type"] = subject_type  # path wins over body
        data["subject_id"] = subject_id
        obj = await register_document(ctx.session, ctx.tenant_id, ctx.actor, data)
        response.headers["ETag"] = f'"{obj.version}"'
        return s.DocumentRead.model_validate(obj)

    @router.get(f"/v1/{path_prefix}/{{subject_id}}/data-register", tags=["Documents"],
                summary=f"Data Register (checklist + progress) for a {label}",
                name=f"data_register_{subject_type}")
    async def _rollup(subject_id: uuid.UUID,
                      ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
        return await data_register(ctx.session, ctx.tenant_id, subject_type, subject_id)


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
    """Streams inline-stored bytes; redirects to an http(s) ``storage_uri``; otherwise
    returns the object-storage reference for the caller to fetch (e.g. via a presigned
    URL). Metadata-only records (no bytes on record) 404."""
    doc = await _document_repo.get(ctx.session, ctx.tenant_id, doc_id)
    if doc.inline_content is not None:
        filename = doc.original_filename or doc.title
        return Response(
            content=doc.inline_content,
            media_type=doc.content_type or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    if doc.storage_uri:
        if doc.storage_uri.startswith(("http://", "https://")):
            return RedirectResponse(doc.storage_uri)
        return {"storage_backend": doc.storage_backend, "storage_uri": doc.storage_uri,
                "note": "Fetch these bytes from object storage (e.g. via a presigned URL)."}
    raise NotFoundError(f"document '{doc_id}' has no bytes on record (metadata-only).")
