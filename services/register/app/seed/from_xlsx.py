"""Import the Evam ATLAS MIS spreadsheet (the 6-sheet consolidated xlsx) into the Register.

This loads the *authoritative* MIS — not the HTML-prototype snapshot in atlas_data.json —
so the Register mirrors the real spreadsheet. Sheet → table mapping:

    Leads            → leads
    Deals            → deals            (Lending?/Syndication?/Asset Mon? → the 3 flags)
    Lending Tracker  → lending_tracker
    Syndication      → syndication_tracker (one per company) + syndication_lenders (per bank)
    Asset Mon        → asset_monetisation
    Mandate Tracker  → syndication_tracker.mandate_status (per company)

Every distinct Company Name across all sheets becomes one entity (entity-centric). Distinct
RMs/analysts become people; distinct banks become counterparties.

Usage:
    python -m app.seed.from_xlsx data/Evam_ATLAS_MIS_Consolidated_v4.xlsx            # replace
    python -m app.seed.from_xlsx <path> --no-truncate                                # merge/upsert
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select, text
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
from app.models.system import RefValue
from app.seed.refdata import REF_VALUES

log = get_logger(__name__)

# Business tables cleared on a replace import — child → parent so FKs stay satisfied.
# DELETE ... WHERE tenant_id (NOT TRUNCATE) so a replace import for one tenant can NEVER
# wipe another tenant's rows. Documents are soft-deleted content; leave them alone.
_BUSINESS_TABLES_ORDERED = [
    "interactions", "financials", "contracts_assets", "external_intelligence",
    "monitoring_reporting", "syndication_lenders", "syndication_tracker",
    "lending_tracker", "asset_monetisation", "deals", "leads", "counterparties",
    "people", "entities",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v.strip()[:19], fmt).date()
            except ValueError:
                continue
    return None


def _float(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _yes(v) -> bool:
    return str(v).strip().lower() in {"yes", "y", "true", "1"}


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


def _sheet(wb, title: str) -> list[dict]:
    """Return a sheet as a list of {header: value} dicts (non-empty rows only)."""
    if title not in wb.sheetnames:
        return []
    ws = wb[title]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    out = []
    for r in rows[1:]:
        if not any(c not in (None, "") for c in r):
            continue
        out.append({header[i]: r[i] for i in range(min(len(header), len(r)))})
    return out


class _CodeGen:
    """Generate stable, unique entity codes from company names."""

    _STOP = {"private", "pvt", "limited", "ltd", "llp", "india", "the", "and", "co", "company"}

    def __init__(self) -> None:
        self.used: set[str] = set()

    def make(self, name: str) -> str:
        words = [w for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
                 if w not in self._STOP]
        base = "".join(words).upper()[:12] or "ENTITY"
        code = base
        i = 1
        while code in self.used:
            i += 1
            code = f"{base[:10]}{i}"
        self.used.add(code)
        return code


# --------------------------------------------------------------------------- #
# main import
# --------------------------------------------------------------------------- #
async def import_workbook(
    session: AsyncSession, tenant_id: uuid.UUID, source, *, truncate: bool = True
) -> dict[str, int]:
    wb = load_workbook(source if isinstance(source, str | Path) else BytesIO(source), data_only=True)
    counts: dict[str, int] = {}

    if truncate:
        # TENANT-SCOPED wipe (the reviewer's data-loss fix): delete only THIS tenant's
        # rows, child tables first. A TRUNCATE would have cleared every tenant.
        for table in _BUSINESS_TABLES_ORDERED:
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tid"),  # noqa: S608 - table from a fixed allowlist
                {"tid": tenant_id})

    # reference vocabularies
    existing_ref = set((await session.execute(text("SELECT category, value FROM ref_values"))).all())
    added_ref = 0
    for category, values in REF_VALUES.items():
        for i, value in enumerate(values):
            if (category, value) not in existing_ref:
                session.add(RefValue(category=category, value=value, label=value, sort_order=i))
                added_ref += 1
    await session.flush()
    counts["ref_values_added"] = added_ref

    leads = _sheet(wb, "Leads")
    deals = _sheet(wb, "Deals")
    lending = _sheet(wb, "Lending Tracker")
    syn = _sheet(wb, "Syndication")
    am = _sheet(wb, "Asset Mon")
    mandate = _sheet(wb, "Mandate Tracker")

    # --- entities: every distinct company across all sheets -------------
    def company_of(row: dict) -> str | None:
        return _s(row.get("Company Name"))

    # enrichment lookups (first non-empty wins), keyed by normalized name
    sector_by, lens_by, state_by = {}, {}, {}
    for r in leads:
        k = _key(company_of(r) or "")
        sector_by.setdefault(k, _s(r.get("Sector")))
        lens_by.setdefault(k, _s(r.get("Mitigation / Adaptation")))
    for r in deals:
        k = _key(company_of(r) or "")
        sector_by.setdefault(k, _s(r.get("Sector")))
        state_by.setdefault(k, _s(r.get("Location")))
    for r in am:
        k = _key(company_of(r) or "")
        state_by.setdefault(k, _s(r.get("State")))

    names: dict[str, str] = {}  # key -> original display name
    for sheet in (leads, deals, lending, syn, am, mandate):
        for r in sheet:
            nm = company_of(r)
            if nm:
                names.setdefault(_key(nm), nm)

    # Existing entities for THIS tenant, keyed canonically → merge reuses them (a real
    # upsert) instead of inserting a second EcoSoch every import.
    existing_entities = (
        await session.execute(
            select(Entity).where(Entity.tenant_id == tenant_id,
                                 Entity.deleted_at.is_(None)))
    ).scalars().all()
    existing_by_key: dict[str, Entity] = {}
    for e in existing_entities:
        existing_by_key.setdefault(_key(e.legal_name or ""), e)
        if e.display_name:
            existing_by_key.setdefault(_key(e.display_name), e)

    codegen = _CodeGen()
    for e in existing_entities:  # reserve existing codes so new ones don't collide
        if e.code:
            codegen.used.add(e.code)
    entity_id_by: dict[str, uuid.UUID] = {}
    n_new, n_updated = 0, 0
    for k, nm in names.items():
        ent = existing_by_key.get(k)
        if ent is None:
            ent = Entity(
                tenant_id=tenant_id, code=codegen.make(nm), legal_name=nm,
                sector=sector_by.get(k), lens=lens_by.get(k), state=state_by.get(k),
                register_status="Pipeline", created_by="xlsx-import",
                updated_by="xlsx-import")
            session.add(ent)
            await session.flush()
            n_new += 1
        else:
            # Enrich only empty fields — never clobber curated data on a merge.
            if not ent.sector and sector_by.get(k):
                ent.sector = sector_by[k]
            if not ent.lens and lens_by.get(k):
                ent.lens = lens_by[k]
            if not ent.state and state_by.get(k):
                ent.state = state_by[k]
            ent.updated_by = "xlsx-import"
            n_updated += 1
        entity_id_by[k] = ent.id
    counts["entities"] = n_new
    counts["entities_matched"] = n_updated

    def eid(name) -> uuid.UUID | None:
        return entity_id_by.get(_key(name or ""))

    # --- people: distinct RMs + analysts --------------------------------
    people_seen: set[str] = set()
    n_people = 0

    def add_person(nm, role):
        nonlocal n_people
        nm = _s(nm)
        if not nm or nm.lower() in {"tbd"} or nm in people_seen:
            return
        people_seen.add(nm)
        session.add(Person(tenant_id=tenant_id, name=nm.split()[0], full_name=nm, role=role,
                            created_by="xlsx-import", updated_by="xlsx-import"))
        n_people += 1
    for r in leads:
        add_person(r.get("RM Owner"), "RM")
    for r in deals + lending + am + mandate:
        add_person(r.get("RM"), "RM")
    for r in lending:
        add_person(r.get("Credit Analyst"), "Analyst")
    await session.flush()
    counts["people"] = n_people

    # --- counterparties: distinct banks from Syndication ----------------
    cp_id_by: dict[str, uuid.UUID] = {}
    for r in syn:
        bank = _s(r.get("Bank"))
        if bank and bank.lower() not in cp_id_by:
            cp = Counterparty(tenant_id=tenant_id, name=bank, created_by="xlsx-import",
                              updated_by="xlsx-import")
            session.add(cp)
            await session.flush()
            cp_id_by[bank.lower()] = cp.id
    counts["counterparties"] = len(cp_id_by)

    # --- leads ----------------------------------------------------------
    n = 0
    for i, r in enumerate(leads, 1):
        nm = company_of(r)
        if not nm:
            continue
        session.add(Lead(
            tenant_id=tenant_id, lead_no=f"LD-{i:03d}", entity_id=eid(nm), company=nm,
            sector=_s(r.get("Sector")), lens=_s(r.get("Mitigation / Adaptation")),
            source=_s(r.get("Source")), source_name=_s(r.get("Source Detail")),
            rm=_s(r.get("RM Owner")), status="Active", temperature=_s(r.get("Status")),
            contact=_s(r.get("Contact Person")), designation=_s(r.get("Designation")),
            phone=_s(r.get("Contact Phone")), last_interaction_date=_date(r.get("Last Interaction Date")),
            next_action=_s(r.get("Next Action")), next_action_date=_date(r.get("Next Action Date")),
            notes=_s(r.get("Notes")), created_by="xlsx-import", updated_by="xlsx-import",
        ))
        n += 1
    await session.flush()
    counts["leads"] = n

    # --- deals ----------------------------------------------------------
    n = 0
    for r in deals:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        session.add(Deal(
            tenant_id=tenant_id, deal_no=None, entity_id=entity, code=None,
            is_lending=_yes(r.get("Lending?")), is_syndication=_yes(r.get("Syndication?")),
            is_asset_mon=_yes(r.get("Asset Mon?")), rm=_s(r.get("RM")), stage=_s(r.get("Stage")),
            temperature=_s(r.get("Status")), source=_s(r.get("Source")),
            source_detail=_s(r.get("Source Detail")), date_received=_date(r.get("Date Received")),
            remarks=_s(r.get("Remarks")), created_by="xlsx-import", updated_by="xlsx-import",
        ))
        n += 1
    await session.flush()
    counts["deals"] = n

    # One deal per company in the MIS → map entity id to its deal so trackers link back.
    deal_rows = (
        await session.execute(select(Deal.id, Deal.entity_id).where(Deal.tenant_id == tenant_id))
    ).all()
    deal_by_entity: dict = {}
    for did, ent in deal_rows:
        deal_by_entity.setdefault(ent, did)

    # --- lending tracker ------------------------------------------------
    n = 0
    for i, r in enumerate(lending, 1):
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        session.add(LendingTracker(
            tenant_id=tenant_id, tracker_no=f"L{i:03d}", entity_id=entity,
            deal_id=deal_by_entity.get(entity),
            amount_cr=_float(r.get("Lending Amount (₹ Cr)")), rm=_s(r.get("RM")),
            analyst=_s(r.get("Credit Analyst")), stage=_s(r.get("Stage")),
            stage_updated_at=_date(r.get("Stage Updated")),
            sanction_date=_date(r.get("Sanction Date")),
            disbursed_amount=_float(r.get("Disbursed Amount (₹ Cr)")),
            disbursement_date=_date(r.get("Disbursement Date")),
            remarks=_s(r.get("Remarks")),
            created_by="xlsx-import", updated_by="xlsx-import",
        ))
        n += 1
    await session.flush()
    counts["lending_tracker"] = n

    # --- syndication: one tracker per company + a lender row per bank ---
    syn_tracker_by: dict[str, SyndicationTracker] = {}
    n_syn = n_lender = 0
    for r in syn:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        k = _key(nm)
        tr = syn_tracker_by.get(k)
        if tr is None:
            tr = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=f"S{n_syn + 1:03d}", entity_id=entity,
                deal_id=deal_by_entity.get(entity),
                status=_s(r.get("Deal Status")), amount_cr=_float(r.get("Amount (₹ Cr)")),
                created_by="xlsx-import", updated_by="xlsx-import",
            )
            session.add(tr)
            await session.flush()
            syn_tracker_by[k] = tr
            n_syn += 1
        bank = _s(r.get("Bank"))
        if bank:
            accepted = _s(r.get("Accepted by Client"))
            note = _s(r.get("Remarks"))
            if accepted:
                note = f"[Accepted by client: {accepted}] " + (note or "")
            session.add(SyndicationLender(
                tenant_id=tenant_id, syndication_id=tr.id, lender_name=bank,
                counterparty_id=cp_id_by.get(bank.lower()), status=_s(r.get("Status")),
                note=note, created_by="xlsx-import", updated_by="xlsx-import",
            ))
            n_lender += 1
    counts["syndication_tracker"] = n_syn
    counts["syndication_lenders"] = n_lender

    # --- asset monetisation ---------------------------------------------
    n = 0
    for i, r in enumerate(am, 1):
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        notes = " | ".join(x for x in [_s(r.get("Notes")), _s(r.get("Updated Remarks 19 July 2026"))] if x)
        session.add(AssetMonetisation(
            tenant_id=tenant_id, tracker_no=f"A{i:03d}", entity_id=entity,
            deal_id=deal_by_entity.get(entity), state=_s(r.get("State")),
            indicative_value_cr=_float(r.get("Indicative Value (₹ Cr)")),
            size_mw=_float(r.get("Size (MW)")), nature=_s(r.get("Nature")),
            deal_type=_s(r.get("Deal Type")), investor=_s(r.get("Investor")),
            investor_type=_s(r.get("Investor Type")), status=_s(r.get("Status")),
            teaser_date=_date(r.get("Date Teaser Shared")), notes=notes or None,
            created_by="xlsx-import", updated_by="xlsx-import",
        ))
        n += 1
    await session.flush()
    counts["asset_monetisation"] = n

    # --- mandate tracker → syndication_tracker.mandate_status -----------
    n = 0
    for r in mandate:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        sent = _s(r.get("Mandate Sent/Not Sent"))
        signed = _s(r.get("Signed/Pending"))
        mand = " - ".join(x for x in [sent, signed] if x)
        k = _key(nm or "")
        tr = syn_tracker_by.get(k)
        if tr is None:
            tr = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=f"S{len(syn_tracker_by) + 1:03d}",
                entity_id=entity, mandate_status=mand, rm=_s(r.get("RM")),
                created_by="xlsx-import", updated_by="xlsx-import",
            )
            session.add(tr)
            await session.flush()
            syn_tracker_by[k] = tr
            counts["syndication_tracker"] += 1
        else:
            tr.mandate_status = mand
            if not tr.rm:
                tr.rm = _s(r.get("RM"))
        n += 1
    await session.flush()
    counts["mandate_applied"] = n

    return counts
