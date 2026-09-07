"""Ledger export — the PRISM register as the desk's own Dashboard-shaped workbook.

    GET /v1/export/ledger-xlsx

The counterpart of ``POST /v1/import/atlas-xlsx``: the file this route produces uses the
ledger's own sheet names, banner rows and headers (Leads / Deals / Lending Tracker /
Syndication Tracker with its two sections / Partnership Tracker / Asset Mon Tracker /
the three masters / Mandate Tracker), and RE-IMPORTS through that endpoint with nothing
lost — the round trip is the contract, asserted by tests.

Like the import, this is a restore-class, whole-tenant operation: Admin only, freshly
revalidated, and audited. The workbook shaping itself lives in ``app.seed.ledger_xlsx``
(the format module); this route only reads the register and streams the file.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from fastapi import Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models import (
    AssetMonetisation,
    Counterparty,
    Deal,
    Entity,
    Lead,
    LendingTracker,
    Person,
    SyndicationLender,
    SyndicationTracker,
)
from app.seed.ledger_xlsx import build_ledger_workbook

router = api_router(tags=["Export"])


def _row(obj, fields: tuple[str, ...]) -> dict:
    return {f: getattr(obj, f, None) for f in fields}


@router.get("/v1/export/ledger-xlsx",
            summary="Export the register as the desk's ledger workbook (governed)")
async def export_ledger_xlsx(
    ctx: RequestContext = Depends(get_context),
) -> StreamingResponse:
    # Same guardrail as the import it mirrors: whole-tenant data movement is Admin only.
    from app.authz import enforce_operation

    enforce_operation(ctx.user, "backup_restore")
    from app.authz.revalidate import revalidate_sensitive
    await revalidate_sensitive(ctx, "backup_restore")

    async def _all(model, *order):
        return (await ctx.session.execute(
            select(model)
            .where(model.tenant_id == ctx.tenant_id, model.deleted_at.is_(None))
            .order_by(*order))).scalars().all()

    # Entity order fixes the file's Client IDs (EF-001…): by code, stable across exports.
    entities = await _all(Entity, Entity.code, Entity.legal_name)
    leads = await _all(Lead, Lead.lead_no, Lead.created_at)
    deals = await _all(Deal, Deal.created_at, Deal.deal_no)
    lending = await _all(LendingTracker, LendingTracker.created_at,
                         LendingTracker.tracker_no)
    syn_trackers = await _all(SyndicationTracker, SyndicationTracker.created_at,
                              SyndicationTracker.tracker_no)
    syn_lenders = await _all(SyndicationLender, SyndicationLender.created_at,
                             SyndicationLender.lender_name)
    am = await _all(AssetMonetisation, AssetMonetisation.created_at,
                    AssetMonetisation.tracker_no)
    counterparties = await _all(Counterparty, Counterparty.name)
    people = await _all(Person, Person.full_name)

    # The data register, as references only (never the bytes — inline_content stays
    # out of the query). Superseded rows are history behind their replacement and
    # would double-count the slot, so only the CURRENT document of each upload rides.
    from app.models import Document

    doc_rows = (await ctx.session.execute(
        select(Document.subject_type, Document.subject_id, Document.entity_id,
               Document.section, Document.title, Document.status,
               Document.original_filename, Document.size_bytes, Document.uploaded_by,
               Document.uploaded_at, Document.expires_on, Document.notes)
        .where(Document.tenant_id == ctx.tenant_id, Document.deleted_at.is_(None),
               Document.superseded_by.is_(None))
        .order_by(Document.subject_type, Document.uploaded_at))).all()

    # Each subject named by ITS OWN reference — the number a person would quote.
    _reg_label = {"Lead": "Leads", "Deal": "Deals", "Entity": "Client Master",
                  "Lending": "Lending", "Syndication": "Syndication",
                  "AssetMonetisation": "Asset Monetisation",
                  "Counterparty": "Lender Master"}
    _refs: dict[str, dict[str, str]] = {
        "Lead": {str(x.id): (x.lead_no or x.company or "") for x in leads},
        "Deal": {str(x.id): (x.deal_no or x.code or "") for x in deals},
        "Entity": {str(x.id): (x.code or "") for x in entities},
        "Lending": {str(x.id): (x.tracker_no or "") for x in lending},
        "Syndication": {str(x.id): (x.tracker_no or "") for x in syn_trackers},
        "AssetMonetisation": {str(x.id): (x.tracker_no or "") for x in am},
        "Counterparty": {str(x.id): (x.name or "") for x in counterparties},
    }
    documents = [{
        "entity_id": d.entity_id,
        "register": _reg_label.get(d.subject_type, d.subject_type),
        "reference": _refs.get(d.subject_type, {}).get(str(d.subject_id), ""),
        "section": d.section, "title": d.title, "status": d.status,
        "original_filename": d.original_filename,
        "size_kb": round(d.size_bytes / 1024, 1) if d.size_bytes else None,
        "uploaded_by": d.uploaded_by,
        "uploaded_at": d.uploaded_at.strftime("%Y-%m-%d %H:%M") if d.uploaded_at else None,
        "expires_on": d.expires_on, "notes": d.notes,
    } for d in doc_rows]

    generated_at = datetime.now(UTC)
    wb = build_ledger_workbook({
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "tenant": str(ctx.tenant_id),
        "entities": [_row(e, ("id", "code", "legal_name", "sector", "lens", "state",
                              "pan", "notes")) for e in entities],
        "leads": [_row(x, ("lead_no", "entity_id", "company", "sector", "lens",
                           "source", "source_name", "rm", "status", "temperature",
                           "contact", "designation", "phone", "last_interaction_date",
                           "next_action", "next_action_date", "notes"))
                  for x in leads],
        "deals": [_row(x, ("entity_id", "code", "stage", "temperature", "rm", "source",
                           "source_detail", "date_received", "is_lending",
                           "is_syndication", "is_asset_mon", "remarks"))
                  for x in deals],
        "lending": [_row(x, ("entity_id", "amount_cr", "rm", "analyst", "stage",
                             "stage_updated_at", "pending_with", "sanction_date",
                             "proposed_disbursement_amount",
                             "proposed_disbursement_date", "disbursed_amount",
                             "disbursement_date", "remarks"))
                    for x in lending],
        "syn_trackers": [_row(x, ("id", "entity_id", "line", "status", "amount_cr",
                                  "rm", "analyst", "pending_with", "mandate_status",
                                  "remarks", "facility", "tenor", "priority",
                                  "syndication_type", "im_status", "potential",
                                  "existing", "price", "mandate_status3", "toi"))
                         for x in syn_trackers],
        "syn_lenders": [_row(x, ("syndication_id", "lender_name", "status", "since",
                                 "response_date", "amount_cr", "note"))
                        for x in syn_lenders],
        "am": [_row(x, ("entity_id", "rm", "analyst", "state", "indicative_value_cr",
                        "size_mw", "nature", "deal_type", "investor", "investor_type",
                        "status", "teaser_date", "notes")) for x in am],
        "counterparties": [_row(x, ("name", "counterparty_type", "short_name",
                                    "is_active", "sectors", "notes"))
                           for x in counterparties],
        "people": [_row(x, ("role", "name", "full_name", "email", "notes")) for x in people],
        "documents": documents,
    })

    actor = ctx.user.email if ctx.user is not None else ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="ledger.export",
        resource_type="export", resource_id="ledger-xlsx",
        request_id=request_id_ctx.get(),
        changes={"entities": len(entities), "leads": len(leads), "deals": len(deals),
                 "lending": len(lending), "syn_trackers": len(syn_trackers),
                 "syn_lenders": len(syn_lenders), "asset_mon": len(am),
                 "counterparties": len(counterparties), "people": len(people),
                 "documents": len(documents)}))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"prism-ledger-{generated_at.strftime('%Y%m%d-%H%M')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
