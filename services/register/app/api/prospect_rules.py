"""Prospect universe rules and endpoints — the promotion path and the two doors.

The generic CRUD router serves /v1/prospects (view "prospects", writes gated by
work_prospect). This module adds what the generic router cannot say:

* ``prospect_pre_write`` — the FIELD-LEVEL gate. Every desk role may work a
  prospect (status, remarks); the curated master data itself (identity, sectors,
  contacts, financials) needs manage_prospects, so an RM's slip cannot corrupt a
  list the whole desk relies on.
* ``POST /v1/prospects/import`` — preview/apply over uploaded xlsx files, one
  engine with the maintenance CLI (app.seed.prospects_xlsx). Preview writes
  nothing; apply is audited with the batch checksum, MIS-import style.
* ``GET /v1/prospects/export-xlsx`` — the universe (honouring the grid's
  filters) in EXACTLY the workbook shape the import reads: export, curate
  offline, bring it straight back in. Round trip asserted by tests.
* ``GET /v1/prospects/facets`` — the chip counts (verticals, sub-sectors,
  statuses) the Masters grid renders.
* ``POST /v1/prospects/{id}/create-lead`` — the promotion. Requires add_lead
  (it creates a Lead), settles the client master through the SAME
  settle_company rule as every other lead birth, appends to the prospect's
  lead trail and marks it lead_created. Repeatable by design: one company,
  many asks over time.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from app.core.errors import NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog

router = api_router(tags=["Prospects"])

# The fields any desk role may write on a prospect row (work_prospect). Everything
# else in the update schema is curated master data → manage_prospects.
_DESK_FIELDS = {"status", "remarks"}


async def prospect_pre_write(ctx: Any, body: dict, obj_id: Any = None) -> None:
    protected = set(body.keys()) - _DESK_FIELDS
    if protected:
        from app.authz import enforce_operation

        enforce_operation(ctx.user, "manage_prospects")

    # The canonical dedupe key follows the name wherever the name is written —
    # the import engine sets it on its own path, and this hook covers the manual
    # create/edit path, so a hand-entered company still deduplicates against the
    # next Excel drop instead of silently forking.
    if body.get("name"):
        from evam_backend_core.company_identity import canonical_name

        body["name_key"] = canonical_name(str(body["name"]))

    # One live prospect per CIN is a partial unique index; pre-checking here turns
    # the raw constraint error into a refusal that names the row already holding
    # it — the same courtesy person_pre_write extends for a duplicate mailbox.
    cin = (str(body.get("cin") or "")).strip()
    if cin:
        from sqlalchemy import select

        from app.models import Prospect

        conds = [Prospect.tenant_id == ctx.tenant_id,
                 Prospect.deleted_at.is_(None), Prospect.cin == cin]
        if obj_id is not None:
            conds.append(Prospect.id != obj_id)
        holder = (await ctx.session.execute(
            select(Prospect.prospect_no, Prospect.name)
            .where(*conds).limit(1))).first()
        if holder is not None:
            raise ValidationAppError(
                f"{holder[1]} ({holder[0] or 'no code'}) already carries CIN {cin} — "
                f"one live prospect per CIN. Edit that row instead of adding a second.")


def _require_view(ctx: RequestContext) -> None:
    from app.authz import Access
    from app.authz.engine import view_access

    if view_access(ctx.user, "prospects") is Access.NONE:
        from app.core.errors import ForbiddenError

        raise ForbiddenError("You do not have access to the prospects view.")


# ---------------------------------------------------------------------------
# Import — preview / apply (the Tools dialog's two steps)
# ---------------------------------------------------------------------------

async def _read_uploads(files: list[UploadFile],
                        verticals: str | None) -> list[tuple[str, bytes, str | None]]:
    overrides: dict[str, str] = {}
    if verticals:
        try:
            parsed = json.loads(verticals)
            if isinstance(parsed, dict):
                overrides = {str(k): str(v) for k, v in parsed.items()}
        except ValueError as exc:
            raise ValidationAppError(
                "verticals must be a JSON object of {filename: vertical}") from exc
    out: list[tuple[str, bytes, str | None]] = []
    for f in files:
        name = f.filename or "upload.xlsx"
        if not name.lower().endswith((".xlsx", ".xlsm")):
            raise ValidationAppError(f"{name}: upload .xlsx workbooks.")
        content = await f.read()
        if len(content) > 20 * 1024 * 1024:
            raise ValidationAppError(f"{name}: larger than 20 MB.")
        out.append((name, content, overrides.get(name)))
    if not out:
        raise ValidationAppError("No files uploaded.")
    return out


@router.post("/v1/prospects/import",
             summary="Import prospect lists (preview writes nothing; apply is audited)")
async def import_prospects(
    ctx: RequestContext = Depends(get_context),
    files: list[UploadFile] = File(..., description="One or more research .xlsx lists"),
    mode: str = Query(default="preview", pattern="^(preview|apply)$"),
    verticals: str | None = Query(
        default=None,
        description='Optional JSON {"<filename>": "<vertical>"} overriding filename '
                    "detection per file."),
) -> dict[str, Any]:
    from app.authz import enforce_operation
    from app.seed.prospects_xlsx import apply_plan, build_plan, summarize_plan

    _require_view(ctx)
    # Curated master data enters here — the same gate as editing it.
    enforce_operation(ctx.user, "manage_prospects")

    payload = await _read_uploads(files, verticals)
    plan = await build_plan(ctx.session, ctx.tenant_id, payload)
    summary = summarize_plan(plan)
    if mode == "preview":
        return {"mode": "preview", **summary}

    actor = ctx.user.email if ctx.user is not None else ctx.actor
    result = await apply_plan(ctx.session, ctx.tenant_id, actor, plan)
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="prospects.import",
        resource_type="prospects", resource_id=plan["batch"],
        request_id=request_id_ctx.get(),
        changes={"files": summary["files"], "counts": summary["counts"],
                 "created": result["created"][:200], "merged": result["merged"][:200],
                 "conflicts": summary["conflicts"][:100]}))
    return {"mode": "apply", **summary,
            "created": len(result["created"]), "merged": len(result["merged"])}


# ---------------------------------------------------------------------------
# Export — the same workbook shape the import reads
# ---------------------------------------------------------------------------

_EXPORT_HEADERS = [
    ("Company Name", "name"), ("Vertical", "verticals"),
    ("EVAM SECTOR", "sub_sectors"), ("Domain Name", "domain"), ("CIN", "cin"),
    ("Overview", "overview"), ("Company Emails", "emails"),
    ("Company Phone Numbers", "phones"), ("Founded Year", "founded_year"),
    ("State", "state"), ("City", "city"), ("Country", "country"),
    ("Annual Revenue (INR Cr)", "revenue_cr"),
    ("Annual Net Profit (INR Cr)", "net_profit_cr"),
    ("Annual EBITDA (INR Cr)", "ebitda_cr"),
    ("Total Funding (INR Cr)", "total_funding_cr"),
    ("Latest Funded Amount (INR Cr)", "latest_funding_cr"),
    ("Latest Valuation (INR Cr)", "latest_valuation_cr"),
    ("Latest Funded Date", "latest_funded_on"),
    # Informational on export; the import never reads these two (a person's
    # status/remarks are theirs alone — the merge policy's rule, kept even
    # through a round trip).
    ("Status", "status"), ("Remarks", "remarks"),
]


@router.get("/v1/prospects/export-xlsx",
            summary="Export the prospect universe (re-imports losslessly)")
async def export_prospects_xlsx(
    ctx: RequestContext = Depends(get_context),
    q: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None, max_length=120),
    state: str | None = Query(default=None, max_length=60),
    verticals: str | None = Query(default=None, max_length=200),
    sub_sectors: str | None = Query(default=None, max_length=300),
) -> StreamingResponse:
    import openpyxl

    from app.authz import enforce_operation
    from app.models import Prospect

    _require_view(ctx)
    enforce_operation(ctx.user, "export_csv")

    conds = [Prospect.tenant_id == ctx.tenant_id, Prospect.deleted_at.is_(None)]
    if status:
        conds.append(Prospect.status.in_([s for s in status.split(",") if s]))
    if state:
        conds.append(Prospect.state == state)
    for column, value in ((Prospect.verticals, verticals),
                          (Prospect.sub_sectors, sub_sectors)):
        if value:
            from sqlalchemy import or_

            conds.append(or_(*[column.contains([v])
                               for v in value.split(",") if v]))
    if q:
        from sqlalchemy import or_

        like = f"%{q}%"
        conds.append(or_(Prospect.name.ilike(like), Prospect.cin.ilike(like),
                         Prospect.domain.ilike(like), Prospect.remarks.ilike(like)))
    rows = (await ctx.session.execute(
        select(Prospect).where(*conds).order_by(Prospect.name))).scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    # "Eligible" in the sheet name is load-bearing: it is what the import's sheet
    # chooser prefers, so this file re-imports through the same door unchanged.
    ws.title = "Eligible Prospects"
    ws.append([h for h, _ in _EXPORT_HEADERS])
    for p in rows:
        out = []
        for _, fld in _EXPORT_HEADERS:
            value = getattr(p, fld)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            out.append(value)
        ws.append(out)
    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    actor = ctx.user.email if ctx.user is not None else ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="prospects.export",
        resource_type="prospects",
        resource_id=hashlib.sha256(content).hexdigest(),
        request_id=request_id_ctx.get(),
        changes={"rows": len(rows),
                 "filters": {"q": q, "status": status, "state": state,
                             "verticals": verticals, "sub_sectors": sub_sectors}}))
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="prism-prospects-{stamp}.xlsx"'})


# ---------------------------------------------------------------------------
# Facets — the chips' live counts
# ---------------------------------------------------------------------------

@router.get("/v1/prospects/facets", summary="Chip counts for the Prospects grid")
async def prospect_facets(
    ctx: RequestContext = Depends(get_context),
    verticals: str | None = Query(
        default=None, max_length=200,
        description="Restrict the sub-sector counts to these verticals "
                    "(the second chip row follows the first)."),
) -> dict[str, Any]:
    _require_view(ctx)

    async def counts(column: str, restrict_sql: str = "",
                     params: dict | None = None) -> dict[str, int]:
        # jsonb_typeof guard: a row whose tag column holds JSON null (or legacy
        # scalar junk) must not fail the whole facet query.
        sql = text(f"""
            SELECT value, count(*) FROM prospects,
                   jsonb_array_elements_text({column}) AS value
            WHERE tenant_id = :tenant AND deleted_at IS NULL
              AND jsonb_typeof({column}) = 'array' {restrict_sql}
            GROUP BY value ORDER BY count(*) DESC, value
        """)  # noqa: S608 - column names are module constants
        got = await ctx.session.execute(
            sql, {"tenant": str(ctx.tenant_id), **(params or {})})
        return {str(k): int(n) for k, n in got.all()}

    restrict, params = "", {}
    if verticals:
        wanted = [v for v in verticals.split(",") if v]
        if wanted:
            restrict = "AND verticals ?| :wanted"
            params = {"wanted": wanted}

    from app.models import Prospect

    status_rows = (await ctx.session.execute(
        select(Prospect.status, func.count()).where(
            Prospect.tenant_id == ctx.tenant_id, Prospect.deleted_at.is_(None))
        .group_by(Prospect.status))).all()
    total = (await ctx.session.execute(
        select(func.count()).select_from(Prospect).where(
            Prospect.tenant_id == ctx.tenant_id,
            Prospect.deleted_at.is_(None)))).scalar_one()
    return {
        "total": int(total),
        "verticals": await counts("verticals"),
        "sub_sectors": await counts("sub_sectors", restrict, params),
        "statuses": {str(k): int(n) for k, n in status_rows},
    }


# ---------------------------------------------------------------------------
# Promotion — Create lead (repeatable)
# ---------------------------------------------------------------------------

class ProspectLeadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rm: str | None = Field(default=None, max_length=120)
    sector: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/v1/prospects/{prospect_id}/create-lead",
             summary="Promote: create a lead for this prospect (repeatable)")
async def create_lead_from_prospect(
    prospect_id: uuid.UUID,
    payload: ProspectLeadIn | None = None,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    from app.api.lead_rules import settle_company
    from app.authz import enforce_operation
    from app.models import Lead, Prospect
    from app.repositories.crud import CRUDRepository

    _require_view(ctx)
    # It creates a Lead — the lead gate decides, exactly as at POST /v1/leads.
    enforce_operation(ctx.user, "add_lead")

    prospect = await ctx.session.get(Prospect, prospect_id)
    if prospect is None or prospect.deleted_at is not None \
            or prospect.tenant_id != ctx.tenant_id:
        raise NotFoundError("No such prospect.")

    payload = payload or ProspectLeadIn()
    actor = ctx.user.email if ctx.user is not None else ctx.actor

    # The same birth-linking as every lead: one canonical answer to "same company?".
    outcome, entity_id = await settle_company(
        ctx.session, ctx.tenant_id, prospect.name,
        sector=payload.sector, actor=actor,
        note=f"Created from prospect {prospect.prospect_no or prospect.id}.")

    contact_bits = []
    if prospect.emails:
        contact_bits.append("emails: " + ", ".join(prospect.emails[:3]))
    if prospect.phones:
        contact_bits.append("phones: " + ", ".join(prospect.phones[:3]))
    seeded_notes = " · ".join(
        s for s in (payload.notes,
                    f"From prospect {prospect.prospect_no or ''}".strip(),
                    "; ".join(contact_bits) or None) if s)

    lead = await CRUDRepository(Lead).create(
        ctx.session, ctx.tenant_id, actor,
        {"company": prospect.name, "entity_id": entity_id,
         "sector": payload.sector, "rm": payload.rm,
         "source": "Prospecting", "source_name": prospect.prospect_no,
         "status": "Active", "notes": seeded_notes or None})

    trail = list(prospect.lead_ids or [])
    trail.append(str(lead.id))
    prospect.lead_ids = trail
    prospect.status = "lead_created"
    if entity_id is not None and prospect.entity_id is None:
        prospect.entity_id = entity_id
    prospect.updated_by = actor
    await ctx.session.flush()

    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="prospect.create_lead",
        resource_type="prospects", resource_id=str(prospect.id),
        request_id=request_id_ctx.get(),
        changes={"lead_id": str(lead.id), "lead_no": lead.lead_no,
                 "company_outcome": outcome,
                 "entity_id": str(entity_id) if entity_id else None,
                 "lead_count": len(trail)}))
    return {"lead_id": str(lead.id), "lead_no": lead.lead_no,
            "entity_id": str(entity_id) if entity_id else None,
            "company_outcome": outcome, "lead_count": len(trail)}
