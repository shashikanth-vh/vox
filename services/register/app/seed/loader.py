"""Load the ATLAS mock dataset into the Register.

Maps the ATLAS prototype's collections onto the canonical Register schema:

    people        → people
    lenders       → counterparties
    clients{}     → entities            (entity-centric: one row per company code)
    leads         → leads
    deals         → deals               (entity resolved via code)
    lending       → lending_tracker
    syn           → syndication_tracker  + syndication_lenders (nested)
    am            → asset_monetisation

Idempotent: re-running upserts on natural keys (entity code, person full_name,
counterparty name, tracker_no) so the seed can be applied repeatedly without duplicates.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
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
from app.models.system import RefValue, Tenant
from app.seed.refdata import REF_VALUES

log = get_logger(__name__)

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "atlas_data.json"


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def _date(v: Any) -> date | None:
    if not v or not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # bare "YYYY-MM" or unparseable → not a date column value
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _float(v: Any) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# --------------------------------------------------------------------------- #
# tenant + ref
# --------------------------------------------------------------------------- #
async def ensure_tenant(session: AsyncSession, code: str, name: str) -> uuid.UUID:
    row = (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()
    if row is None:
        row = Tenant(code=code, name=name)
        session.add(row)
        await session.flush()
    return row.id


async def seed_ref_values(session: AsyncSession) -> int:
    """RECONCILE ref_values with :data:`REF_VALUES` — add, re-order, retire, revive.

    Adding-only was not enough: a deployment seeded before a vocabulary was corrected kept
    serving the old values forever, so the dropdown fix never reached the browser (the
    overlapping Tenor buckets survived three releases that way). Now a value that has left
    a managed category is DEACTIVATED rather than deleted — ``/v1/ref`` stops offering it,
    while rows that already hold it are untouched and still readable — and one that comes
    back is revived. Categories NOT in REF_VALUES (anything an operator added by hand) are
    left completely alone.
    """
    rows = (await session.execute(select(RefValue))).scalars().all()
    by_key = {(r.category, r.value): r for r in rows}
    changed = 0
    for category, values in REF_VALUES.items():
        wanted = {v: i for i, v in enumerate(values)}
        for value, i in wanted.items():
            row = by_key.get((category, value))
            if row is None:
                session.add(RefValue(category=category, value=value, label=value, sort_order=i))
                changed += 1
            elif row.sort_order != i or not row.is_active:
                row.sort_order, row.is_active = i, True
                changed += 1
        for row in rows:
            if row.category == category and row.value not in wanted and row.is_active:
                row.is_active = False          # retired, not deleted
                changed += 1
    await session.flush()
    return changed


# --------------------------------------------------------------------------- #
# domain loaders
# --------------------------------------------------------------------------- #
async def _upsert(session: AsyncSession, model, where, **values):  # noqa: ANN001
    obj = (await session.execute(select(model).where(*where))).scalar_one_or_none()
    if obj is None:
        obj = model(**values)
        session.add(obj)
        await session.flush()
    return obj


async def seed_all(session: AsyncSession, data: dict, tenant_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}

    # Curated showcase memberships (ATLAS core-33 / adaptation-10) → entity tags.
    tags_by_code: dict[str, list[str]] = {}
    for pair in data.get("core33", []):
        code = pair[0] if isinstance(pair, list | tuple) else pair
        if code:
            tags_by_code.setdefault(str(code), []).append("Core 33")
    for code in data.get("adapt10", []):
        if code:
            tags_by_code.setdefault(str(code), []).append("Adaptation 10")

    # people
    n = 0
    for p in data.get("people", []):
        if not p.get("full"):
            continue
        await _upsert(
            session, Person,
            [Person.tenant_id == tenant_id, Person.full_name == p["full"]],
            tenant_id=tenant_id, name=_s(p.get("name")) or p["full"], full_name=p["full"],
            role=_s(p.get("role")) or "RM", email=_s(p.get("email")), phone=_s(p.get("phone")),
            geography=_s(p.get("geography")), sectors=_s(p.get("sectors")),
            started_on=_date(p.get("startedOn")),
            reports_to=_s(p.get("reportsTo")), inactive=bool(p.get("inactive")),
            notes=_s(p.get("notes")), created_by="seed", updated_by="seed",
        )
        n += 1
    counts["people"] = n

    # counterparties (lenders)
    cp_by_name: dict[str, uuid.UUID] = {}
    n = 0
    for lender in data.get("lenders", []):
        name = _s(lender.get("name"))
        if not name:
            continue
        obj = await _upsert(
            session, Counterparty,
            [Counterparty.tenant_id == tenant_id, Counterparty.name == name],
            tenant_id=tenant_id, name=name, short_name=_s(lender.get("short")),
            counterparty_type=_s(lender.get("type")),
            is_active=(_s(lender.get("active")) or "Yes").lower() != "no",
            sectors=_s(lender.get("sectors")), notes=_s(lender.get("notes")),
            created_by="seed", updated_by="seed",
        )
        cp_by_name[name.lower()] = obj.id
        n += 1
    counts["counterparties"] = n

    # entities (clients)
    code_to_id: dict[str, uuid.UUID] = {}
    n = 0
    for code, c in data.get("clients", {}).items():
        obj = await _upsert(
            session, Entity,
            [Entity.tenant_id == tenant_id, Entity.code == code],
            tenant_id=tenant_id, code=code, legal_name=_s(c.get("name")) or code,
            sector=_s(c.get("sector")), lens=_s(c.get("lens")), state=_s(c.get("state")),
            about=_s(c.get("about")), notes=_s(c.get("notes")), toi=_s(c.get("toi")),
            tags=tags_by_code.get(code) or None,
            created_by="seed", updated_by="seed",
        )
        code_to_id[code] = obj.id
        n += 1
    counts["entities"] = n

    async def resolve_entity(code: str | None) -> uuid.UUID | None:
        if not code:
            return None
        if code in code_to_id:
            return code_to_id[code]
        # Create a stub entity for a tracker/deal code without a client record.
        obj = await _upsert(
            session, Entity,
            [Entity.tenant_id == tenant_id, Entity.code == code],
            tenant_id=tenant_id, code=code, legal_name=code,
            register_status="Market Intelligence", created_by="seed", updated_by="seed",
        )
        code_to_id[code] = obj.id
        return obj.id

    # leads
    n = 0
    for ld in data.get("leads", []):
        lead_no = _s(ld.get("id"))
        where = (
            [Lead.tenant_id == tenant_id, Lead.lead_no == lead_no]
            if lead_no else [Lead.id == uuid.uuid4()]  # force insert if no natural key
        )
        exists = None
        if lead_no:
            exists = (await session.execute(select(Lead).where(*where))).scalar_one_or_none()
        if exists is None:
            lead = Lead(
                tenant_id=tenant_id, lead_no=lead_no, company=_s(ld.get("company")) or "(unknown)",
                sector=_s(ld.get("sector")), lens=_s(ld.get("lens")), source=_s(ld.get("source")),
                source_name=_s(ld.get("sourceName")), rm=_s(ld.get("rm")),
                status=_s(ld.get("status")) or "Active", temperature=_s(ld.get("temp")),
                contact=_s(ld.get("contact")), phone=_s(ld.get("phone")),
                last_interaction_date=_date(ld.get("last")), next_action=_s(ld.get("next")),
                next_action_date=_date(ld.get("nextDate")), conv=_s(ld.get("conv")),
                notes=_s(ld.get("notes")), created_by="seed", updated_by="seed",
            )
            # Preserve the original ATLAS lead date when present (else DB now() applies).
            _ca = _date(ld.get("createdAt"))
            if _ca:
                lead.created_at = _ca
            session.add(lead)
            n += 1
    await session.flush()
    counts["leads"] = n

    # deals
    n = 0
    for d in data.get("deals", []):
        code = _s(d.get("code"))
        entity_id = await resolve_entity(code)
        if entity_id is None:
            continue
        deal_no = _s(d.get("code"))  # ATLAS keys a deal by client code
        exists = (
            await session.execute(
                select(Deal).where(Deal.tenant_id == tenant_id, Deal.deal_no == deal_no)
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(Deal(
                tenant_id=tenant_id, deal_no=deal_no, entity_id=entity_id, code=code,
                is_lending=bool(d.get("lend")), is_syndication=bool(d.get("syn")),
                is_asset_mon=bool(d.get("am")), rm=_s(d.get("rm")), analyst=_s(d.get("an")),
                temperature=_s(d.get("temp")), source=_s(d.get("source")),
                source_detail=_s(d.get("sourceDetail")), source_name=_s(d.get("sourceName")),
                date_received=_date(d.get("createdAt")), remarks=_s(d.get("remarks")),
                created_by="seed", updated_by="seed",
            ))
            n += 1
    await session.flush()
    counts["deals"] = n

    # Map client code → deal id so trackers can link back to their deal (ATLAS keys a
    # deal by client code, and each tracker carries the same code).
    deal_rows = (
        await session.execute(select(Deal.id, Deal.code).where(Deal.tenant_id == tenant_id))
    ).all()
    deal_by_code = {c: i for i, c in deal_rows if c}

    # lending
    n = 0
    for lg in data.get("lending", []):
        lg_code = _s(lg.get("code"))
        entity_id = await resolve_entity(lg_code)
        if entity_id is None:
            continue
        tracker_no = _s(lg.get("id"))
        exists = (
            await session.execute(
                select(LendingTracker).where(
                    LendingTracker.tenant_id == tenant_id, LendingTracker.tracker_no == tracker_no
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(LendingTracker(
                tenant_id=tenant_id, tracker_no=tracker_no, entity_id=entity_id,
                deal_id=deal_by_code.get(lg_code),
                amount_cr=_float(lg.get("amt")), rm=_s(lg.get("rm")), analyst=_s(lg.get("an")),
                stage=_s(lg.get("stage")), stage_updated_at=_date(lg.get("updated")),
                sanction_date=_date(lg.get("sanc")), pending_with=_s(lg.get("pendingWith")),
                remarks=_s(lg.get("remarks")), stage_history=lg.get("h"),
                created_by="seed", updated_by="seed",
            ))
            n += 1
    await session.flush()
    counts["lending"] = n

    # syndication + nested lenders
    n_syn = n_lender = 0
    for sy in data.get("syn", []):
        sy_code = _s(sy.get("code"))
        entity_id = await resolve_entity(sy_code)
        if entity_id is None:
            continue
        tracker_no = _s(sy.get("id"))
        syn_obj = (
            await session.execute(
                select(SyndicationTracker).where(
                    SyndicationTracker.tenant_id == tenant_id,
                    SyndicationTracker.tracker_no == tracker_no,
                )
            )
        ).scalar_one_or_none()
        if syn_obj is None:
            syn_obj = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=tracker_no, entity_id=entity_id,
                deal_id=deal_by_code.get(sy_code),
                toi=_s(sy.get("toi")), rm=_s(sy.get("rm")), analyst=_s(sy.get("an")),
                lc=_s(sy.get("lc")), priority=_s(sy.get("pri")), status=_s(sy.get("status")),
                amount_cr=_float(sy.get("amt")), line=_s(sy.get("line")),
                facility=_s(sy.get("fac")), tenor=_s(sy.get("tenor")),
                mandate_status=_s(sy.get("mstat")), potential=_s(sy.get("pot")),
                im_status=_s(sy.get("im")), sanctioned_lender=_s(sy.get("sancL")),
                ip_lender=_s(sy.get("ipL")), date_of_sanction=_date(sy.get("dos")),
                month_of_sanction=_s(sy.get("mos")), nature=_s(sy.get("nature")),
                existing=_s(sy.get("exist")), price=_s(sy.get("price")),
                syndication_type=_s(sy.get("synType")), mandate_status3=_s(sy.get("mstat3")),
                pending_with=_s(sy.get("pendingWith")), remarks=_s(sy.get("remarks")),
                status_history=sy.get("h"), created_by="seed", updated_by="seed",
            )
            session.add(syn_obj)
            await session.flush()
            n_syn += 1
            for ln in sy.get("lenders", []) or []:
                lname = _s(ln.get("name"))
                if not lname:
                    continue
                session.add(SyndicationLender(
                    tenant_id=tenant_id, syndication_id=syn_obj.id, lender_name=lname,
                    counterparty_id=cp_by_name.get(lname.lower()),
                    is_existing=bool(ln.get("ex")), status=_s(ln.get("st")),
                    since=_date(ln.get("since")), response_date=_date(ln.get("resp")),
                    chased_date=_date(ln.get("chased")), note=_s(ln.get("note")),
                    status_history=ln.get("h"), created_by="seed", updated_by="seed",
                ))
                n_lender += 1
    await session.flush()
    counts["syndication"] = n_syn
    counts["syndication_lenders"] = n_lender

    # asset monetisation
    n = 0
    for am in data.get("am", []):
        am_code = _s(am.get("code"))
        entity_id = await resolve_entity(am_code)
        if entity_id is None:
            continue
        tracker_no = _s(am.get("id"))
        exists = (
            await session.execute(
                select(AssetMonetisation).where(
                    AssetMonetisation.tenant_id == tenant_id,
                    AssetMonetisation.tracker_no == tracker_no,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(AssetMonetisation(
                tenant_id=tenant_id, tracker_no=tracker_no, entity_id=entity_id,
                deal_id=deal_by_code.get(am_code),
                state=_s(am.get("state")), indicative_value_cr=_float(am.get("val")),
                size_mw=_float(am.get("mw")), nature=_s(am.get("nature")),
                deal_type=_s(am.get("dtype")), investor=_s(am.get("inv")),
                investor_type=_s(am.get("itype")), status=_s(am.get("status")),
                teaser_date=_date(am.get("teaser")), notes=_s(am.get("notes")),
                created_by="seed", updated_by="seed",
            ))
            n += 1
    await session.flush()
    counts["asset_monetisation"] = n

    return counts


def load_data_file(path: Path | None = None) -> dict:
    return json.loads((path or DATA_PATH).read_text())
