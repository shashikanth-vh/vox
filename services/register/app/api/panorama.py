"""Company 360 — one company's whole story across the register, in one read.

``GET /v1/panorama?entity_id=…`` (or ``?company=<name>`` when the caller only has
a name, e.g. a lead that never settled an entity) returns everything the register
knows about ONE company: its client-master identity, open leads and lead history,
the live product lines (lending / syndication / asset monetisation) with their
stage ladders, the latest interactions and VOCX field notes, the Data Register's
documents, the prospect-universe row, the people we actually talk to, and a
deterministic BRIEF composed from all of it.

Three rules keep it honest:

* **One anchor.** Everything hangs off the client master (entity). A name-only
  lookup resolves through the same canonical-name identity the lead birth and
  the prospect import use, and the response says which anchor matched.
* **RBAC per section, server-side.** Each section is included only at the
  caller's matrix access for that module; a section the role cannot see comes
  back in ``restricted`` — named, not silently missing — and a SCOPED view
  applies the same company-scope condition the list routes apply.
* **Read-only.** The panorama never writes; the numbers are the register's live
  rows at the moment of the call (``generated_at``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import Query
from sqlalchemy import func, select

from app.api.tracxn import real_cin
from app.core.errors import NotFoundError, ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from fastapi import Depends

router = api_router(tags=["Company 360"])

# The forward ladders the drawer's steppers render — the same vocabulary
# evam_backend_core.lifecycle enforces on writes (terminal Rejected / On Hold /
# Dropped states are shown as the row's stage text, not as ladder steps).
LENDING_LADDER = ["Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
                  "CP/CS Completed", "Ready for Disbursement", "Disbursed"]
SYNDICATION_LADDER = ["Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
                      "Queries Received", "IP Received", "Sanctioned", "Disbursed"]
AM_LADDER = ["Teaser Prepared", "Teaser Shared", "In Discussion", "NBO Received",
             "BO Received", "SPA / Documentation", "Closed"]


def _iso(v: Any) -> str | None:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return None


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _clip(s: str | None, n: int) -> str:
    """Cut at a word boundary with an ellipsis — a brief that stops mid-word
    ("asset sa") reads as a glitch, not a summary."""
    t = (s or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


# Every line lands in ONE bucket, and the words follow the bucket:
#   in-flight — being worked: counts as a live deal, its amount is the ASK;
#   on hold   — paused: named, its amount shown as on hold, never as an ask;
#   done      — finished successfully (Disbursed / Closed): the BOOK, not an ask;
#   dead      — Rejected / Withdrawn / Dropped: neither counted nor summed.
LENDING_DEAD = {"Rejected"}
LENDING_DONE = {"Disbursed"}
SYN_DEAD = {"Withdrawn", "Rejected", "Dropped"}
SYN_DONE = {"Disbursed"}
AM_DEAD = {"Dropped"}
AM_DONE = {"Closed"}
ON_HOLD = {"On Hold"}


def _bucket(state: str | None, dead: set, done: set) -> str:
    s = state or ""
    if s in dead:
        return "dead"
    if s in done:
        return "done"
    if s in ON_HOLD:
        return "hold"
    return "flight"


async def _resolve_anchor(ctx: RequestContext, entity_id: str | None,
                          company: str | None) -> tuple[Any | None, str, str]:
    """(entity row | None, matched_by, display name). A name-only company that has
    no client master yet still gets a panorama — of whatever carries that name."""
    from app.models import Entity

    if entity_id:
        try:
            eid = uuid.UUID(entity_id)
        except ValueError as exc:
            raise ValidationAppError("entity_id must be a UUID.") from exc
        ent = (await ctx.session.execute(select(Entity).where(
            Entity.tenant_id == ctx.tenant_id, Entity.id == eid,
            Entity.deleted_at.is_(None)))).scalar_one_or_none()
        if ent is None:
            raise NotFoundError(f"entity '{entity_id}' not found.")
        return ent, "entity", ent.display_name or ent.legal_name

    name = (company or "").strip()
    if not name:
        raise ValidationAppError("Pass entity_id or company.")
    low = name.lower()
    ent = (await ctx.session.execute(select(Entity).where(
        Entity.tenant_id == ctx.tenant_id, Entity.deleted_at.is_(None),
        func.lower(Entity.legal_name) == low)
        .limit(1))).scalar_one_or_none()
    if ent is None:
        ent = (await ctx.session.execute(select(Entity).where(
            Entity.tenant_id == ctx.tenant_id, Entity.deleted_at.is_(None),
            func.lower(func.coalesce(Entity.display_name, "")) == low)
            .limit(1))).scalar_one_or_none()
    if ent is None:
        # The same canonical key the lead birth and the prospect import share.
        from evam_backend_core.company_identity import canonical_name

        from app.models import Prospect

        key = canonical_name(name)
        p = (await ctx.session.execute(select(Prospect).where(
            Prospect.tenant_id == ctx.tenant_id, Prospect.deleted_at.is_(None),
            Prospect.name_key == key).limit(1))).scalar_one_or_none()
        if p is not None and p.entity_id is not None:
            ent = (await ctx.session.execute(select(Entity).where(
                Entity.tenant_id == ctx.tenant_id, Entity.id == p.entity_id,
                Entity.deleted_at.is_(None)))).scalar_one_or_none()
        if ent is not None:
            return ent, "prospect", ent.display_name or ent.legal_name
    if ent is not None:
        return ent, "name", ent.display_name or ent.legal_name
    return None, "name-only", name


@router.get("/v1/panorama", summary="Company 360 — one company across the register")
async def company_panorama(
    ctx: RequestContext = Depends(get_context),
    entity_id: str | None = Query(default=None),
    company: str | None = Query(default=None, max_length=300),
) -> dict[str, Any]:
    from app.authz import Access
    from app.authz.engine import view_access
    from app.authz.scope import build_scope, company_scoped_condition
    from app.models import (
        AssetMonetisation,
        Deal,
        Document,
        Entity,  # noqa: F401 - resolved in _resolve_anchor
        Interaction,
        Lead,
        LendingTracker,
        Prospect,
        SyndicationTracker,
    )

    if ctx.user is None:
        raise ValidationAppError("The panorama needs a signed-in identity.")

    ent, matched_by, display = await _resolve_anchor(ctx, entity_id, company)
    eid = ent.id if ent is not None else None
    scope = await build_scope(ctx, ctx.user)

    def access(view: str) -> Access:
        return view_access(ctx.user, view)

    restricted: list[str] = []

    async def rows(model, view: str, section: str, *conds, order=None, limit=200,
                   options=None):
        """The module's rows for this company under the caller's matrix access.
        NONE → the section is named in ``restricted`` and returns []. SCOPED →
        the same company-scope condition the list routes apply."""
        acc = access(view)
        if acc is Access.NONE:
            restricted.append(section)
            return []
        stmt = select(model).where(model.tenant_id == ctx.tenant_id,
                                   model.deleted_at.is_(None), *conds)
        if acc is Access.SCOPED:
            stmt = stmt.where(company_scoped_condition(scope, model))
        if order is not None:
            stmt = stmt.order_by(order)
        if options is not None:
            stmt = stmt.options(*options)
        return (await ctx.session.execute(stmt.limit(limit))).scalars().all()

    # ---- leads: the open book, plus how many ever became deals -----------------
    lead_conds = ([Lead.entity_id == eid] if eid is not None
                  else [func.lower(Lead.company) == display.lower()])
    leads = await rows(Lead, "leads", "leads", *lead_conds,
                       order=Lead.last_interaction_date.desc().nulls_last())
    open_leads = [l for l in leads if l.converted_deal_id is None
                  and (l.status or "Active") == "Active"]
    converted = [l for l in leads if l.converted_deal_id is not None]

    # ---- deals and the three product trackers ---------------------------------
    by_entity = [] if eid is None else [Deal.entity_id == eid]
    deals = [] if eid is None else await rows(Deal, "deals", "deals", *by_entity)
    lending = [] if eid is None else await rows(
        LendingTracker, "lending", "lending", LendingTracker.entity_id == eid)
    from sqlalchemy.orm import selectinload

    synd = [] if eid is None else await rows(
        SyndicationTracker, "syndication", "syndication",
        SyndicationTracker.entity_id == eid,
        options=[selectinload(SyndicationTracker.lenders)])
    am = [] if eid is None else await rows(
        AssetMonetisation, "asset_monetisation", "asset_monetisation",
        AssetMonetisation.entity_id == eid)

    # ---- the timeline: interactions ride the deals/leads views ----------------
    inter_view = "deals" if access("deals") is not Access.NONE else "leads"
    inters = [] if eid is None else await rows(
        Interaction, inter_view, "interactions", Interaction.entity_id == eid,
        order=Interaction.occurred_at.desc(), limit=10)

    # ---- documents: the Data Register (clients view, company-scoped) ----------
    docs = [] if eid is None else await rows(
        Document, "clients", "documents", Document.entity_id == eid,
        order=Document.uploaded_at.desc().nulls_last(), limit=50)

    # ---- prospect universe row -------------------------------------------------
    prospect = None
    if access("prospects") is Access.NONE:
        restricted.append("prospects")
    else:
        from sqlalchemy import or_

        from evam_backend_core.company_identity import canonical_name

        # By master link OR by canonical name: a prospect that never went through
        # promotion (no entity_id yet) is still this company's universe row.
        key = canonical_name(display)
        p_cond = (or_(Prospect.entity_id == eid, Prospect.name_key == key)
                  if eid is not None else Prospect.name_key == key)
        prospect = (await ctx.session.execute(select(Prospect).where(
            Prospect.tenant_id == ctx.tenant_id, Prospect.deleted_at.is_(None),
            p_cond).limit(1))).scalar_one_or_none()

    # ---- contacts: the people we actually talk to ------------------------------
    contacts: list[dict[str, Any]] = []
    seen = set()
    for l in leads:
        if l.contact and l.contact.lower() not in seen:
            seen.add(l.contact.lower())
            contacts.append({"name": l.contact, "designation": l.designation,
                             "phone": l.phone, "source": l.lead_no or "lead"})
    for i in inters:
        nm = (i.contact_name or "").strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            contacts.append({"name": nm, "designation": None, "phone": None,
                             "source": "interaction"})

    lines = ([("lending", r, _bucket(r.stage, LENDING_DEAD, LENDING_DONE))
              for r in lending]
             + [("syndication", r, _bucket(r.status, SYN_DEAD, SYN_DONE))
                for r in synd]
             + [("am", r, _bucket(r.status, AM_DEAD, AM_DONE)) for r in am])
    in_flight = [(k, r) for k, r, b in lines if b == "flight"]
    on_hold = [(k, r) for k, r, b in lines if b == "hold"]
    done = [(k, r) for k, r, b in lines if b == "done"]

    def _amt(kind: str, r: Any, booked: bool = False) -> float:
        if kind == "am":
            return _num(r.indicative_value_cr) or 0.0
        if booked and kind == "lending":
            return _num(r.disbursed_amount) or _num(r.amount_cr) or 0.0
        return _num(r.amount_cr) or 0.0

    ask_cr = sum(_amt(k, r) for k, r in in_flight if k != "am")
    booked_cr = sum(_amt(k, r, booked=True) for k, r in done if k != "am")
    on_hold_cr = sum(_amt(k, r) for k, r in on_hold if k != "am")
    last_touch = None
    for i in inters:
        last_touch = _iso(i.occurred_at)
        break
    if last_touch is None:
        dts = [l.last_interaction_date for l in leads if l.last_interaction_date]
        last_touch = _iso(max(dts)) if dts else None

    # ---- the brief: deterministic, from the same rows the sections show --------
    bits: list[str] = []
    if ent is not None:
        head = display
        extras = ", ".join(x for x in [ent.sector, ent.state] if x)
        bits.append(f"{head} ({extras})." if extras else f"{head}.")
    else:
        bits.append(f"{display} — no client master yet; showing what carries the name.")
    # The desk's names: nothing on the UI shows a tracker number, so the brief
    # names a line by product and amount, never by code.
    _KIND = {"lending": "Lending", "syndication": "Platform deal",
             "am": "Asset monetisation"}

    def _ask(k: str, r: Any) -> str:
        amt = _amt(k, r)
        return f"{_KIND[k]} ask of ₹{amt:g} Cr" if amt else _KIND[k]
    fl_lending = [r for k, r in in_flight if k == "lending"]
    if fl_lending:
        r = fl_lending[0]
        amt = _num(r.amount_cr)
        amount_txt = f"₹{amt:g} Cr " if amt else ""
        pend = f", pending with {r.pending_with}" if r.pending_with else ""
        bits.append(f"Live lending ask of {amount_txt}at {r.stage or 'unknown stage'}{pend}.")
    for k, r in done[:2]:
        amt = _amt(k, r, booked=True)
        bits.append(f"{_KIND[k]} {'disbursed' if k != 'am' else 'closed'}"
                    + (f" ₹{amt:g} Cr." if amt else "."))
    for k, r in on_hold[:2]:
        amt = _amt(k, r)
        bits.append(f"{_KIND[k]} on hold" + (f" (₹{amt:g} Cr)." if amt else "."))
    for k, r, b in lines:
        if b == "dead":
            state = (r.stage if k == "lending" else r.status) or "closed"
            bits.append(f"{_ask(k, r)} was {state.lower()}.")
            break
    for k, r in in_flight:
        if k == "syndication":
            bits.append(f"{_ask(k, r)} at {r.status or '—'}.")
            break
    for k, r in in_flight:
        if k == "am":
            bits.append(f"Asset monetisation at {r.status or '—'}.")
            break
    # Two renderings of the same sentences: the DIGEST (tight clips, the
    # 30-second read) and the FULL text behind its "more" click — same facts,
    # longer leash, still bounded so a pasted essay cannot flood the card.
    bits_full: list[str] = list(bits)

    def _both(short: str, full: str) -> None:
        bits.append(short)
        bits_full.append(full)

    if open_leads:
        l = open_leads[0]
        who = ", ".join(x for x in (l.temperature, l.rm or "unassigned") if x)
        head = f"Open lead {l.lead_no or ''} ({who})"
        # The digest clips hard; the EXPANDED rendering is for the reader who
        # asked for everything, so its bound exists only to stop a pasted
        # essay — a real field note fits whole.
        note_s = _clip(l.next_action or l.notes, 140)
        note_f = _clip(l.next_action or l.notes, 4000)
        _both(head + (f": {note_s}" if note_s else "."),
              head + (f": {note_f}" if note_f else "."))
    if converted:
        _both(f"{len(converted)} lead(s) became deals.",
              f"{len(converted)} lead(s) became deals.")
    if inters:
        i = inters[0]
        when = i.occurred_at.date().isoformat() if i.occurred_at else ""
        summ = _clip(i.summary or i.notes, 160)
        if summ:
            _both(f"Last touch {when}: {summ}",
                  f"Last touch {when}: {_clip(i.notes or i.summary, 4000)}")
    if prospect is not None and prospect.remarks:
        _both(f"Desk remark: {_clip(prospect.remarks, 120)}",
              f"Desk remark: {_clip(prospect.remarks, 2000)}")

    # ---- the Data Register's own view of what is still missing -------------
    # Deterministic and per-tenant (the checklist template), so the brief can
    # say "5 required documents to request" for EVERY company, graded or not.
    checklist: dict[str, Any] = {"required_total": 0, "required_on_file": 0,
                                 "missing": []}
    if eid is not None and access("documents") is not Access.NONE:
        try:
            from app.repositories.documents import data_register

            reg = await data_register(ctx.session, ctx.tenant_id, "Entity", eid,
                                      scope="entity", verify_subject=False)
            for sec in reg.get("sections", []):
                for it in sec.get("items", []):
                    if not it.get("is_required"):
                        continue
                    checklist["required_total"] += 1
                    if it.get("on_file") or it.get("documents"):
                        checklist["required_on_file"] += 1
                    else:
                        checklist["missing"].append({
                            "section": sec.get("section"),
                            "slot_key": it.get("slot_key"), "label": it.get("label")})
        except Exception:  # noqa: BLE001 - the checklist is an extra, never a blocker
            checklist = {"required_total": 0, "required_on_file": 0, "missing": [],
                         "note": "checklist unavailable"}

    # "Since" is the earliest dated FACT — a stage reached in May on a row the
    # import created in August began in May, not August.
    def _dt(x: Any) -> datetime | None:
        if x is None:
            return None
        if isinstance(x, datetime):
            return x if x.tzinfo else x.replace(tzinfo=UTC)
        if isinstance(x, date):
            return datetime(x.year, x.month, x.day, tzinfo=UTC)
        return None

    stamps = [d for d in (
        [_dt(l.created_at) for l in leads] + [_dt(l.last_interaction_date) for l in leads]
        + [_dt(r.created_at) for r in lending] + [_dt(r.stage_updated_at) for r in lending]
        + [_dt(r.sanction_date) for r in lending]
        + [_dt(r.created_at) for r in synd] + [_dt(r.created_at) for r in am]
        + [_dt(i.occurred_at) for i in inters]) if d]
    owners = sorted({x for x in (
        [l.rm for l in leads] + [r.rm for r in lending] + [r.rm for r in synd]
        + [r.rm for r in am]) if x})

    return {
        "since": _iso(min(stamps)) if stamps else None,
        "owners": owners,
        "checklist": checklist,
        "anchor": {
            "entity_id": str(eid) if eid else None,
            "matched_by": matched_by,
            "name": display,
            "cin": real_cin(ent)
            or next((l.cin.strip() for l in leads if (l.cin or "").strip()), None)
            or (prospect.cin if prospect else None),
            "sector": getattr(ent, "sector", None),
            "sub_sector": getattr(ent, "sub_sector", None),
            "state": getattr(ent, "state", None)
            or (prospect.state if prospect else None),
            "domain": (prospect.domain if prospect else None),
            "about": getattr(ent, "about", None),
        },
        "restricted": restricted,
        "stats": {
            "open_leads": len(open_leads),
            "leads_converted": len(converted),
            # The house's two-layer model, spoken correctly: a company has DEALS
            # (the commercial relationships) and each deal carries PRODUCT LINES
            # (lending / syndication / asset monetisation). Zeon is 1 deal with
            # 2 products — never "2 deals". deal_count is None when the deals
            # view is restricted for this caller (unknown, not zero).
            "deal_count": None if "deals" in restricted else len(deals),
            # Product lines that are neither dead nor merely historical — with
            # the composition beside it so "2" can say "1 disbursed · 1 on hold".
            "live_deals": len(in_flight) + len(on_hold) + len(done),
            "deals_in_flight": len(in_flight),
            "deals_on_hold": len(on_hold),
            "deals_done": len(done),
            "exposure_ask_cr": round(ask_cr, 2) if ask_cr else None,
            "booked_cr": round(booked_cr, 2) if booked_cr else None,
            "on_hold_cr": round(on_hold_cr, 2) if on_hold_cr else None,
            "last_touch": last_touch,
            "documents": len(docs),
        },
        "leads": [{
            "lead_no": l.lead_no, "status": l.status, "temperature": l.temperature,
            "rm": l.rm, "sector": l.sector, "source": l.source,
            "last_interaction_date": _iso(l.last_interaction_date),
            "next_action": l.next_action,
            "next_action_date": _iso(l.next_action_date),
            "notes": l.notes, "converted": l.converted_deal_id is not None,
            "created_at": _iso(l.created_at),
        } for l in leads],
        "deals": [{
            "deal_no": d.deal_no, "code": d.code, "stage": d.stage,
            "product_type": d.product_type, "rm": d.rm,
        } for d in deals],
        "lending": [{
            "tracker_no": r.tracker_no, "stage": r.stage,
            "amount_cr": _num(r.amount_cr), "pending_with": r.pending_with,
            "rm": r.rm, "analyst": r.analyst,
            "stage_updated_at": _iso(r.stage_updated_at),
            "sanction_date": _iso(r.sanction_date),
            "disbursed_amount": _num(r.disbursed_amount),
            "remarks": r.remarks, "ladder": LENDING_LADDER,
            "created_at": _iso(r.created_at),
        } for r in lending],
        "syndication": [{
            "tracker_no": r.tracker_no, "status": r.status,
            "amount_cr": _num(r.amount_cr), "pending_with": r.pending_with,
            "rm": r.rm, "ladder": SYNDICATION_LADDER,
            "created_at": _iso(r.created_at),
            "lenders": [{
                "name": x.lender_name, "status": x.status,
                "amount_cr": _num(x.amount_cr),
                "last_chase": x.last_chase_note, "last_reply": x.last_reply_note,
                "note": x.note, "since": _iso(x.since),
                "response_date": _iso(x.response_date),
                "chased_date": _iso(x.chased_date),
                "updated_at": _iso(x.updated_at),
            } for x in (r.lenders or [])],
        } for r in synd],
        "asset_monetisation": [{
            "tracker_no": r.tracker_no, "status": r.status,
            "indicative_value_cr": _num(r.indicative_value_cr),
            "deal_type": r.deal_type, "investor": r.investor, "rm": r.rm,
            "ladder": AM_LADDER, "created_at": _iso(r.created_at),
        } for r in am],
        "interactions": [{
            "occurred_at": _iso(i.occurred_at), "type": i.interaction_type,
            "summary": i.summary, "notes": (i.notes or "")[:300] or None,
            "by": i.performed_by, "contact": i.contact_name,
            "lender": i.lender_name,
        } for i in inters],
        "documents": [{
            "id": str(d.id),
            "title": d.title, "section": d.section, "doc_type": d.doc_type,
            "filename": d.original_filename,
            "uploaded_at": _iso(d.uploaded_at), "uploaded_by": d.uploaded_by,
        } for d in docs],
        "prospect": None if prospect is None else {
            "prospect_no": prospect.prospect_no, "status": prospect.status,
            "verticals": prospect.verticals, "sub_sectors": prospect.sub_sectors,
            "remarks": prospect.remarks, "revenue_cr": _num(prospect.revenue_cr),
            "net_profit_cr": _num(prospect.net_profit_cr),
            "ebitda_cr": _num(prospect.ebitda_cr),
            "founded_year": prospect.founded_year,
            "emails": prospect.emails, "phones": prospect.phones,
            "lead_count": len(prospect.lead_ids or []),
        },
        "contacts": contacts[:10],
        "brief": " ".join(bits),
        "brief_full": " ".join(bits_full),
        "generated_at": datetime.now(UTC).isoformat(),
    }
