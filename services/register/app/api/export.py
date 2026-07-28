"""Full-database export — Excel (one sheet per table) and JSON.

Two uses:
* **Verify the seed / compare** — dump every table to an .xlsx workbook and eyeball it
  against the source data.
* **Backup** — a point-in-time snapshot of the tenant's data. JSON preserves types exactly
  (best for re-import); Excel is human-friendly.

Everything is tenant-scoped (via ``X-Tenant``). Soft-deleted rows are excluded unless
``include_deleted=true``. Streaming reads keep memory bounded even at lakhs of rows.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Depends, Query
from fastapi.responses import ORJSONResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy import Table, select

from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models import (
    AssetMonetisation,
    ContractAsset,
    Counterparty,
    Deal,
    Document,
    DocumentChecklistItem,
    Entity,
    ExternalIntelligence,
    Financial,
    Interaction,
    Lead,
    LendingTracker,
    MonitoringReporting,
    Person,
    SyndicationLender,
    SyndicationTracker,
)
from app.models.system import RefValue

router = api_router(tags=["Export"])

# (sheet/section name → SQLAlchemy model). Order mirrors the entity-centric model.
_EXPORT_MODELS: list[tuple[str, Any]] = [
    ("entities", Entity),
    ("people", Person),
    ("counterparties", Counterparty),
    ("leads", Lead),
    ("deals", Deal),
    ("lending_tracker", LendingTracker),
    ("syndication_tracker", SyndicationTracker),
    ("syndication_lenders", SyndicationLender),
    ("asset_monetisation", AssetMonetisation),
    ("financials", Financial),
    ("contracts_assets", ContractAsset),
    ("interactions", Interaction),
    ("external_intelligence", ExternalIntelligence),
    ("monitoring_reporting", MonitoringReporting),
    ("documents", Document),
    ("document_checklist", DocumentChecklistItem),
    ("ref_values", RefValue),
]
_MODELS_BY_NAME = dict(_EXPORT_MODELS)


def _rows_query(table: Table, tenant_id: uuid.UUID, include_deleted: bool,
                scope_condition: Any = None):
    stmt = select(table)
    if "tenant_id" in table.c:
        stmt = stmt.where(table.c.tenant_id == tenant_id)
    if not include_deleted and "deleted_at" in table.c:
        stmt = stmt.where(table.c.deleted_at.is_(None))
    if scope_condition is not None:
        stmt = stmt.where(scope_condition)
    # Stable ordering for reproducible diffs.
    order_col = "created_at" if "created_at" in table.c else "id"
    return stmt.order_by(table.c[order_col])


def _xl(v: Any) -> Any:
    """Coerce a DB value to something openpyxl can write (and that diffs cleanly)."""
    if v is None:
        return None
    if isinstance(v, dict | list):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()  # tz-aware datetimes aren't allowed as native Excel values
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return f"<{len(bytes(v))} bytes>"  # never dump raw document bytes into a sheet
    return v


def _json(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return f"<{len(bytes(v))} bytes>"
    return v


def _selected(tables: str | None) -> list[tuple[str, Any]]:
    if not tables:
        return _EXPORT_MODELS
    wanted = [t.strip() for t in tables.split(",") if t.strip()]
    return [(n, _MODELS_BY_NAME[n]) for n in wanted if n in _MODELS_BY_NAME]


# name → the line subject_type it exports (for row-scope conditions).
_LINE_SUBJECT: dict[str, str] = {
    "leads": "Lead", "deals": "Deal", "lending_tracker": "Lending",
    "syndication_tracker": "Syndication", "asset_monetisation": "AssetMonetisation",
}
# Entity-carrying tables scoped by company (created_by ∪ entity_id in scope).
_COMPANY_TABLES = {
    "financials", "contracts_assets", "interactions", "external_intelligence",
    "monitoring_reporting", "documents",
}
# Directory / reference tables any authenticated user may already read → exported whole.
_DIRECTORY_TABLES = {"people", "counterparties", "document_checklist", "ref_values"}


async def _export_scope(ctx: RequestContext):  # noqa: ANN202
    from app.authz import scope as scope_mod

    assert ctx.user is not None  # only called for a restricted (identified) caller
    return await scope_mod.build_scope(ctx, ctx.user)


def _scope_condition(name: str, table: Table, scope: Any):  # noqa: ANN001, ANN202
    """Row-scope filter for a RESTRICTED (non-admin) export, mirroring the list endpoints.
    Returns a SQLAlchemy clause over the core ``table``, or None to include the whole
    table (directory/reference). Uses the entity_ids set (which already includes a
    vertical-Head's owned companies) so a scoped export is exactly what the user can see —
    never the whole tenant."""
    from sqlalchemy import or_
    from sqlalchemy import select as _select

    from app.models import SyndicationTracker

    empty = {uuid.UUID(int=0)}
    c = table.c
    own = scope.own_emails or {""}
    if name in _DIRECTORY_TABLES:
        return None
    if name == "entities":
        clauses = [c.created_by.in_(own)]
        if scope.entity_ids:
            clauses.append(c.id.in_(scope.entity_ids))
        return or_(*clauses)
    if name in _LINE_SUBJECT:
        subject = _LINE_SUBJECT[name]
        clauses = [c.id.in_(scope.assigned_ids(subject) or empty), c.created_by.in_(own)]
        if scope.entity_ids:
            clauses.append(c.entity_id.in_(scope.entity_ids))
        return or_(*clauses)
    if name in _COMPANY_TABLES:
        return or_(c.created_by.in_(own), c.entity_id.in_(scope.entity_ids or empty))
    if name == "syndication_lenders":
        # A lender row is visible only through its parent syndication tracker's scope.
        parent = _select(SyndicationTracker.__table__.c.id).where(
            SyndicationTracker.__table__.c.entity_id.in_(scope.entity_ids or empty))
        return or_(c.syndication_id.in_(parent), c.created_by.in_(own))
    # Unknown table → be conservative: own book only.
    return c.created_by.in_(own) if "created_by" in c else None


def _is_unrestricted(ctx: RequestContext) -> bool:
    """Admin/Management (or a vetted machine caller in compatibility mode) export the whole
    tenant. Everyone else is row-scoped."""
    if ctx.user is None:
        return True
    return bool(ctx.user.roles & {"Admin", "Management"})


async def _guard_export(ctx: RequestContext, include_deleted: bool) -> tuple[bool, Any]:
    """Common export gate. Everyone needs ``export_csv``. A FULL/deleted backup — the
    whole tenant, or soft-deleted rows — additionally needs ``backup_restore`` (Admin), so
    a scoped user can never pull the entire book. Returns (restricted, scope)."""
    from app.authz import enforce_operation

    enforce_operation(ctx.user, "export_csv")
    unrestricted = _is_unrestricted(ctx)
    if include_deleted and not unrestricted:
        enforce_operation(ctx.user, "backup_restore")  # include_deleted ⇒ Admin backup
    scope = None if unrestricted else await _export_scope(ctx)
    return (not unrestricted), scope


@router.get("/v1/export/excel", summary="Export tables to an Excel workbook (row-scoped)")
async def export_excel(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
    tables: str | None = Query(default=None, description="Comma-separated subset, e.g. leads,deals"),
) -> StreamingResponse:
    restricted, scope = await _guard_export(ctx, include_deleted)
    wb = Workbook(write_only=True)  # constant-memory writer for large datasets
    for name, model in _selected(tables):
        table: Table = model.__table__
        cond = _scope_condition(name, table, scope) if restricted else None
        ws = wb.create_sheet(title=name[:31])
        cols = [c.name for c in table.columns]
        ws.append(cols)  # header row
        result = await ctx.session.stream(
            _rows_query(table, ctx.tenant_id, include_deleted, cond))
        async for row in result:
            ws.append([_xl(v) for v in row])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    fname = f"register_export_{ctx.tenant_code}_{stamp}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/v1/export/json", summary="Export tables as JSON (row-scoped, type-faithful)")
async def export_json(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
    tables: str | None = Query(default=None),
) -> ORJSONResponse:
    restricted, scope = await _guard_export(ctx, include_deleted)
    out: dict[str, Any] = {
        "tenant": ctx.tenant_code,
        "exported_at": datetime.now(UTC).isoformat(),
        "scoped": restricted,
        "tables": {},
    }
    for name, model in _selected(tables):
        table: Table = model.__table__
        cond = _scope_condition(name, table, scope) if restricted else None
        rows: list[dict] = []
        result = await ctx.session.stream(
            _rows_query(table, ctx.tenant_id, include_deleted, cond))
        async for row in result:
            rows.append({k: _json(v) for k, v in row._mapping.items()})
        out["tables"][name] = rows
    return ORJSONResponse(out)


@router.get("/v1/export/counts", summary="Row counts per table (row-scoped)")
async def export_counts(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
) -> dict[str, int]:
    from sqlalchemy import func

    restricted, scope = await _guard_export(ctx, include_deleted)
    counts: dict[str, int] = {}
    for name, model in _EXPORT_MODELS:
        table: Table = model.__table__
        cond = _scope_condition(name, table, scope) if restricted else None
        stmt = select(func.count()).select_from(table)
        if "tenant_id" in table.c:
            stmt = stmt.where(table.c.tenant_id == ctx.tenant_id)
        if not include_deleted and "deleted_at" in table.c:
            stmt = stmt.where(table.c.deleted_at.is_(None))
        if cond is not None:
            stmt = stmt.where(cond)
        counts[name] = (await ctx.session.execute(stmt)).scalar_one()
    return counts
