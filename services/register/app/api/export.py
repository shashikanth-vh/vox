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
    ("ref_values", RefValue),
]
_MODELS_BY_NAME = dict(_EXPORT_MODELS)


def _rows_query(table: Table, tenant_id: uuid.UUID, include_deleted: bool):
    stmt = select(table)
    if "tenant_id" in table.c:
        stmt = stmt.where(table.c.tenant_id == tenant_id)
    if not include_deleted and "deleted_at" in table.c:
        stmt = stmt.where(table.c.deleted_at.is_(None))
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
    return v


def _json(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _selected(tables: str | None) -> list[tuple[str, Any]]:
    if not tables:
        return _EXPORT_MODELS
    wanted = [t.strip() for t in tables.split(",") if t.strip()]
    return [(n, _MODELS_BY_NAME[n]) for n in wanted if n in _MODELS_BY_NAME]


@router.get("/v1/export/excel", summary="Export all tables to an Excel workbook")
async def export_excel(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
    tables: str | None = Query(default=None, description="Comma-separated subset, e.g. leads,deals"),
) -> StreamingResponse:
    wb = Workbook(write_only=True)  # constant-memory writer for large datasets
    for name, model in _selected(tables):
        table: Table = model.__table__
        ws = wb.create_sheet(title=name[:31])
        cols = [c.name for c in table.columns]
        ws.append(cols)  # header row
        result = await ctx.session.stream(_rows_query(table, ctx.tenant_id, include_deleted))
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


@router.get("/v1/export/json", summary="Export all tables as JSON (type-faithful backup)")
async def export_json(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
    tables: str | None = Query(default=None),
) -> ORJSONResponse:
    out: dict[str, Any] = {
        "tenant": ctx.tenant_code,
        "exported_at": datetime.now(UTC).isoformat(),
        "tables": {},
    }
    for name, model in _selected(tables):
        table: Table = model.__table__
        rows: list[dict] = []
        result = await ctx.session.stream(_rows_query(table, ctx.tenant_id, include_deleted))
        async for row in result:
            rows.append({k: _json(v) for k, v in row._mapping.items()})
        out["tables"][name] = rows
    return ORJSONResponse(out)


@router.get("/v1/export/counts", summary="Row counts per table (quick verification)")
async def export_counts(
    ctx: RequestContext = Depends(get_context),
    include_deleted: bool = Query(default=False),
) -> dict[str, int]:
    from sqlalchemy import func

    counts: dict[str, int] = {}
    for name, model in _EXPORT_MODELS:
        table: Table = model.__table__
        stmt = select(func.count()).select_from(table)
        if "tenant_id" in table.c:
            stmt = stmt.where(table.c.tenant_id == ctx.tenant_id)
        if not include_deleted and "deleted_at" in table.c:
            stmt = stmt.where(table.c.deleted_at.is_(None))
        counts[name] = (await ctx.session.execute(stmt)).scalar_one()
    return counts
