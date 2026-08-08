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


async def _book_summary(session, tenant_id) -> dict[str, Any]:
    """What the tenant's book holds RIGHT NOW — the desk-language summary shown after an
    import (counts, ₹ totals and stage spreads), computed in the same transaction so it
    reflects exactly the state the import produced."""
    from sqlalchemy import Integer, cast, func, select

    from app.models.deals import Deal, Lead
    from app.models.registry import Counterparty, Entity
    from app.models.trackers import (
        AssetMonetisation, LendingTracker, SyndicationLender, SyndicationTracker,
    )

    def _f(v) -> float:
        return round(float(v or 0), 2)

    async def one(stmt):
        return (await session.execute(stmt)).one()

    async def dist(stmt) -> dict[str, int]:
        return {str(k or "—"): int(n) for k, n in (await session.execute(stmt)).all()}

    live = lambda m: (m.tenant_id == tenant_id, m.deleted_at.is_(None))  # noqa: E731

    ents = await one(select(func.count()).where(*live(Entity)))
    leads = await dist(select(Lead.status, func.count()).where(*live(Lead)).group_by(Lead.status))
    deals = await one(select(
        func.count(),
        func.sum(cast(Deal.is_lending, Integer)),
        func.sum(cast(Deal.is_syndication, Integer)),
        func.sum(cast(Deal.is_asset_mon, Integer)),
    ).where(*live(Deal)))
    lend = await one(select(func.count(), func.sum(LendingTracker.amount_cr))
                     .where(*live(LendingTracker)))
    lend_stages = await dist(select(LendingTracker.stage, func.count())
                             .where(*live(LendingTracker)).group_by(LendingTracker.stage))
    syn = await one(select(func.count(), func.sum(SyndicationTracker.amount_cr))
                    .where(*live(SyndicationTracker),
                           (SyndicationTracker.line.is_(None))
                           | (SyndicationTracker.line != "Partnership")))
    part = await one(select(func.count()).where(*live(SyndicationTracker),
                                                SyndicationTracker.line == "Partnership"))
    lenders = await one(select(func.count(), func.sum(SyndicationLender.amount_cr))
                        .where(*live(SyndicationLender)))
    lender_states = await dist(select(SyndicationLender.status, func.count())
                               .where(*live(SyndicationLender))
                               .group_by(SyndicationLender.status))
    am = await one(select(func.count(), func.sum(AssetMonetisation.indicative_value_cr),
                          func.sum(AssetMonetisation.size_mw))
                   .where(*live(AssetMonetisation)))
    cps = await one(select(
        func.count(),
        func.sum(cast(Counterparty.is_active, Integer)),
    ).where(*live(Counterparty)))
    mandates = await one(select(func.count(SyndicationTracker.mandate_status))
                         .where(*live(SyndicationTracker)))

    return {
        "entities": int(ents[0]),
        "leads": {"total": sum(leads.values()), "by_status": leads},
        "deals": {"total": int(deals[0]), "lending": int(deals[1] or 0),
                  "syndication": int(deals[2] or 0), "asset_mon": int(deals[3] or 0)},
        "lending": {"lines": int(lend[0]), "amount_cr": _f(lend[1]),
                    "by_stage": lend_stages},
        "syndication": {"trackers": int(syn[0]), "ask_cr": _f(syn[1]),
                        "partnership_trackers": int(part[0]),
                        "lenders": int(lenders[0]), "allocation_cr": _f(lenders[1]),
                        "lenders_by_status": lender_states,
                        "mandate_statuses": int(mandates[0])},
        "asset_monetisation": {"mandates": int(am[0]), "indicative_cr": _f(am[1]),
                               "size_mw": _f(am[2])},
        "counterparties": {"total": int(cps[0]), "active": int(cps[1] or 0)},
    }


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

    # The desk-language summary of what the book NOW holds (₹ totals, stage spreads) —
    # same transaction, so it is exactly the state this import produced.
    await ctx.session.flush()
    book = await _book_summary(ctx.session, ctx.tenant_id)

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
            "book": book,
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
