"""Import endpoint — load the ATLAS MIS xlsx into the Register via an upload.

    POST /v1/import/atlas-xlsx?mode=replace   (multipart file=<xlsx>)

`mode=merge` (default) upserts on top of existing data; `mode=replace` first TRUNCATEs
the tenant's business tables for a clean load. Because `replace` wipes data, it must be
asked for explicitly.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, File, Query, UploadFile

from app.core.errors import ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.seed.from_xlsx import import_workbook

router = api_router(tags=["Import"])


@router.post("/v1/import/atlas-xlsx", summary="Import the ATLAS MIS spreadsheet")
async def import_atlas_xlsx(
    ctx: RequestContext = Depends(get_context),
    file: UploadFile = File(..., description="The Evam ATLAS MIS .xlsx"),
    mode: str = Query(default="merge", pattern="^(merge|replace)$",
                      description="replace = wipe the tenant's data first; merge = upsert"),
) -> dict[str, Any]:
    # RBAC guardrail: a tenant-wide import is a restore-class operation — Admin only.
    from app.authz import enforce_operation

    enforce_operation(ctx.user, "backup_restore")
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm")):
        raise ValidationAppError("Upload an .xlsx workbook.")
    content = await file.read()
    counts = await import_workbook(
        ctx.session, ctx.tenant_id, content, truncate=(mode == "replace")
    )
    return {"mode": mode, "filename": file.filename, "counts": counts}
