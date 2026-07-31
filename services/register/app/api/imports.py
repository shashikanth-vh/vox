"""Import endpoint — load the ATLAS MIS xlsx into the Register via an upload.

    POST /v1/import/atlas-xlsx?mode=replace&reason=...   (multipart file=<xlsx>)

`mode=merge` (default) upserts on top of existing data; `mode=replace` first TRUNCATEs
the tenant's business tables for a clean load. Because `replace` wipes data, it must be
asked for explicitly.

An import is a GOVERNED exception to the interactive lifecycle policy: historical data may
legitimately begin at a later stage, so the importer does not run `check_write`. That exception
is made explicit and auditable here — a mandatory recovery/override `reason` is required, the
uploaded file's SHA-256 checksum is recorded, an immutable audit event names who imported what
under which reason, and rows whose lifecycle value is unknown (or whose terminal stage is
missing mandatory data) are QUARANTINED (skipped) and surfaced in the response report rather
than silently written.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import Depends, File, Query, UploadFile

from app.core.errors import ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.seed.from_xlsx import import_workbook

router = api_router(tags=["Import"])


@router.post("/v1/import/atlas-xlsx", summary="Import the ATLAS MIS spreadsheet (governed)")
async def import_atlas_xlsx(
    ctx: RequestContext = Depends(get_context),
    file: UploadFile = File(..., description="The Evam ATLAS MIS .xlsx"),
    mode: str = Query(default="merge", pattern="^(merge|replace)$",
                      description="replace = wipe the tenant's data first; merge = upsert"),
    reason: str = Query(..., min_length=1, max_length=2000,
                        description="Why this import runs — required; it bypasses the "
                                    "interactive lifecycle policy, so the reason is audited."),
    ticket: str | None = Query(default=None, max_length=200,
                               description="Optional change/incident ticket reference."),
    retain_incomplete: bool = Query(
        default=False,
        description="Historical override: import rows whose terminal stage is missing its "
                    "mandatory data (marking them reconciliation_status=Required) instead of "
                    "quarantining them. Default false — the importer rejects the same states the "
                    "interactive API does."),
) -> dict[str, Any]:
    # RBAC guardrail: a tenant-wide import is a restore-class operation — Admin only.
    from app.authz import enforce_operation

    enforce_operation(ctx.user, "backup_restore")
    from app.authz.revalidate import revalidate_sensitive
    await revalidate_sensitive(ctx, "backup_restore")
    if not reason.strip():
        raise ValidationAppError("A non-empty reason is required to run a governed import.")
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm")):
        raise ValidationAppError("Upload an .xlsx workbook.")
    content = await file.read()
    checksum = hashlib.sha256(content).hexdigest()
    actor = ctx.user.email if ctx.user is not None else ctx.actor

    report: dict[str, Any] = {}
    counts = await import_workbook(
        ctx.session, ctx.tenant_id, content, truncate=(mode == "replace"), report=report,
        retain_incomplete=retain_incomplete, batch_id=checksum, actor=actor,
    )
    quarantined = report.get("quarantined", [])
    reconciliation = report.get("reconciliation", [])
    history_changes = report.get("history_changes", [])
    translated = report.get("translated", [])
    derived = report.get("derived", [])

    # Immutable governance evidence + LINEAGE: WHO imported WHAT (filename + checksum = batch id),
    # in WHICH mode, WHY, under which ticket — with the accepted counts, the quarantined (skipped)
    # rows, any retained-incomplete rows flagged for reconciliation, and the import-driven stage
    # history changes. So a historical override is always explainable and reconcilable.
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="mis.import",
        resource_type="import", resource_id=checksum, request_id=request_id_ctx.get(),
        changes={"mode": mode, "filename": file.filename, "checksum": checksum,
                 "import_batch_id": report.get("import_batch_id"), "reason": reason.strip(),
                 "ticket": ticket, "retain_incomplete": retain_incomplete, "counts": counts,
                 "quarantined_count": len(quarantined), "quarantined": quarantined,
                 "reconciliation_count": len(reconciliation), "reconciliation": reconciliation,
                 "translated_count": len(translated), "translated": translated,
                 "derived_count": len(derived), "derived": derived,
                 "history_change_count": len(history_changes),
                 "history_changes": history_changes}))

    return {"mode": mode, "filename": file.filename, "checksum": checksum,
            "import_batch_id": report.get("import_batch_id"), "counts": counts,
            "report": {"quarantined": quarantined, "quarantined_count": len(quarantined),
                       "reconciliation": reconciliation,
                       "reconciliation_count": len(reconciliation),
                       # What normalisation did: canonical-wording translations and the
                       # column-level derivations (e.g. a Disbursed row's proposed amount/date
                       # from Lending Amount / Stage Updated) — the Excel is never silently
                       # rewritten.
                       "translated": translated, "translated_count": len(translated),
                       "derived": derived, "derived_count": len(derived),
                       "history_changes": history_changes,
                       "history_change_count": len(history_changes)}}
