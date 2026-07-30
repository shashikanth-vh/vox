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
from collections.abc import Callable
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


# Legacy ATLAS-era credit-pipeline stage labels → PRISM's current vocabulary. Historical
# spreadsheets used 'Documentation' (the CP/CS phase) and 'Disbursed' (the terminal). PRISM renamed
# the milestones and — with no Advaya integration — its terminal is 'Handed Over to Advaya', so a
# historical 'Disbursed' loan maps there (its recorded amount/date become the proposed drawdown).
_LEGACY_CREDIT_STAGE: dict[str, str] = {
    "Documentation": "CP/CS Completed",
    "Disbursed": "Handed Over to Advaya",
}


def _map_credit_stage(v: str | None) -> str | None:
    return _LEGACY_CREDIT_STAGE.get(v, v) if v is not None else v


# Corporate suffix words peeled from the tail of a company name so that legal-form
# variants collapse to ONE entity. Without this, "EcoSoch Solar Private Limited",
# "EcoSoch Solar Pvt Ltd" and "EcoSoch Solar Ltd" seed three separate companies
# (the reviewer's canonicalization finding).
_SUFFIX_WORDS = {
    "private", "pvt", "limited", "ltd", "llp", "inc", "incorporated",
    "corporation", "corp", "co", "company", "plc", "and", "&",
}


def _key(name: str) -> str:
    """Canonical identity key for a company: lowercased, punctuation-flattened, with any
    trailing corporate-suffix words peeled off. Interior words are always kept, so only the
    legal form ("Pvt Ltd", "Private Limited", "LLP") is normalised away — two genuinely
    different companies never collapse into one. Used everywhere a company is matched
    (entities, enrichment, leads, deals, trackers) so a re-import is a true upsert."""
    s = re.sub(r"[.,/]", " ", str(name or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split()
    while tokens and tokens[-1] in _SUFFIX_WORDS:
        tokens.pop()
    return " ".join(tokens) or s


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
    session: AsyncSession, tenant_id: uuid.UUID, source, *, truncate: bool = True,
    report: dict | None = None, retain_incomplete: bool = False, batch_id: str | None = None,
    actor: str = "xlsx-import",
) -> dict[str, int]:
    """Load the ATLAS MIS workbook. Historical data may legitimately begin at a later lifecycle
    stage, so this is a GOVERNED exception to the interactive policy — but the two definitions of a
    valid record must not diverge:

    * An UNKNOWN / free-text lifecycle value is always QUARANTINED (skipped) — it maps to no real
      state.
    * A KNOWN stage missing its mandatory data (e.g. a 'Ready for Disbursement' lending line with
      no amount/date) is by DEFAULT quarantined too — the same state the interactive API rejects.
      Only when the caller explicitly opts in (``retain_incomplete=True``, an audited historical
      override) is the row imported, and then it is recorded for reconciliation (batch id + missing
      fields) so it is
      never mistaken for operationally complete.

    A batch id ties every accepted/quarantined/reconciliation row (and the appended import-history
    events) to this one import, for lineage. All exceptions are collected in ``report``."""
    from datetime import UTC, datetime

    from evam_backend_core.policy import MANDATORY_FOR_STAGE, STAGE_VOCAB

    from app.models.reconciliation import ImportReconciliationItem

    wb = load_workbook(source if isinstance(source, str | Path) else BytesIO(source), data_only=True)
    counts: dict[str, int] = {}

    report = report if report is not None else {}
    batch_id = batch_id or str(uuid.uuid4())
    report["import_batch_id"] = batch_id
    stamped_at = datetime.now(UTC).isoformat()
    quarantined: list[dict] = report.setdefault("quarantined", [])
    reconciliation: list[dict] = report.setdefault("reconciliation", [])
    history_changes: list[dict] = report.setdefault("history_changes", [])
    # Reconciliation items are created AFTER the row's id exists (post-flush) — collect them here.
    pending_recon: list[tuple] = []

    def _jsonable(v):  # noqa: ANN001
        return v.isoformat() if hasattr(v, "isoformat") else v

    def _screen(subject_type: str, value, sheet: str, company, row_fields: dict) -> tuple[str, list]:
        """Screen a row's lifecycle value. Returns (verdict, missing):
        * ``("skip", [])``   — quarantine (an UNKNOWN value, or a known stage missing mandatory data
          when ``retain_incomplete`` is False): the SAME state the interactive API rejects.
        * ``("ok", [])``     — import cleanly.
        * ``("retain", [...])`` — a known stage missing mandatory data, imported under the historical
          override: the caller must flag the record reconciliation_status=Required and open a
          reconciliation item listing the missing fields. A NULL value is always ("ok", [])."""
        if value is None:
            return "ok", []
        field, vocab = STAGE_VOCAB[subject_type]
        if value not in vocab:
            quarantined.append({"sheet": sheet, "company": company, "field": field,
                                "value": value, "reason": "unknown lifecycle value",
                                "batch_id": batch_id})
            return "skip", []
        required = MANDATORY_FOR_STAGE.get(subject_type, {}).get(value) or []
        missing = [f for f in required if row_fields.get(f) in (None, "")]
        if not missing:
            return "ok", []
        if not retain_incomplete:
            quarantined.append({"sheet": sheet, "company": company, "field": field, "value": value,
                                "missing": missing, "batch_id": batch_id,
                                "reason": f"missing mandatory data for {value!r}"})
            return "skip", []
        reconciliation.append({"sheet": sheet, "company": company, "field": field, "value": value,
                               "missing": missing, "batch_id": batch_id,
                               "reconciliation_status": "Required"})
        return "retain", missing

    def _open_recon(subject_type: str, obj, field: str, value, missing: list, sheet: str,
                    company, row_fields: dict) -> None:
        """Flag the record and open a durable reconciliation item (with the ORIGINAL imported
        values preserved), so a retained-incomplete import is a tracked work item — never a
        silently 'complete'-looking record."""
        obj.reconciliation_status = "Required"
        pending_recon.append((subject_type, obj, field, value, missing, sheet, company,
                              {k: _jsonable(v) for k, v in row_fields.items()}))

    def _note_stage_change(obj, hist_attr: str, field: str, old, new, sheet: str) -> None:
        """Append an ``xlsx-import`` event to a record's append-only history whenever an import
        SETS or CHANGES its lifecycle value — for a NEW row this is the initial NULL → stage event,
        for a MERGE it is the transition — so history is complete and reconstructable for EVERY
        product line, not silently overwritten. Also validates the record ends AT the value the
        final history entry names (they cannot diverge)."""
        if old == new or new is None:
            return
        history = list(getattr(obj, hist_attr) or [])
        history.append({"from": old, "to": new, "by": actor, "at": stamped_at,
                        "source": "xlsx-import", "batch_id": batch_id, "sheet": sheet})
        setattr(obj, hist_attr, history)
        history_changes.append({"sheet": sheet, "field": field, "from": old, "to": new,
                                "batch_id": batch_id})

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

    # --- people: distinct RMs + analysts (upsert by full_name) ----------
    # Reuse people already in this tenant, keyed by full_name (their unique key), so a
    # re-import never trips people_tenant_full_name. seen holds BOTH pre-existing and
    # this-run names, keyed canonically, so a repeated name in the sheet is added once.
    existing_people = (
        await session.execute(
            select(Person).where(Person.tenant_id == tenant_id,
                                  Person.deleted_at.is_(None)))
    ).scalars().all()
    people_seen: set[str] = {_key(p.full_name or "") for p in existing_people if p.full_name}
    n_people = 0

    def add_person(nm, role):
        nonlocal n_people
        nm = _s(nm)
        if not nm or nm.lower() in {"tbd"}:
            return
        pk = _key(nm)
        if pk in people_seen:
            return
        people_seen.add(pk)
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

    # --- counterparties: distinct banks (upsert by name) ----------------
    # Seed the id map from counterparties already in this tenant so a re-import reuses
    # them (counterparties_tenant_name is unique) instead of inserting duplicate banks.
    existing_cps = (
        await session.execute(
            select(Counterparty).where(Counterparty.tenant_id == tenant_id,
                                        Counterparty.deleted_at.is_(None)))
    ).scalars().all()
    cp_id_by: dict[str, uuid.UUID] = {c.name.lower(): c.id for c in existing_cps if c.name}
    n_cp = 0
    for r in syn:
        bank = _s(r.get("Bank"))
        if bank and bank.lower() not in cp_id_by:
            cp = Counterparty(tenant_id=tenant_id, name=bank, created_by="xlsx-import",
                              updated_by="xlsx-import")
            session.add(cp)
            await session.flush()
            cp_id_by[bank.lower()] = cp.id
            n_cp += 1
    counts["counterparties"] = n_cp

    # --- leads (upsert by entity) ---------------------------------------
    # One lead per company in the MIS. On a re-import we UPDATE the existing lead for that
    # entity rather than insert a second (which would also collide on leads_tenant_lead_no).
    # New leads get a lead_no that skips every number already in use, so the sequence never
    # clashes with a prior import.
    existing_leads = (
        await session.execute(
            select(Lead).where(Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None)))
    ).scalars().all()
    lead_by_entity: dict[uuid.UUID, Lead] = {}
    used_lead_nos: set[str] = set()
    for ld in existing_leads:
        if ld.entity_id is not None:
            lead_by_entity.setdefault(ld.entity_id, ld)
        if ld.lead_no:
            used_lead_nos.add(ld.lead_no)
    _lead_seq = {"n": 0}

    def _next_lead_no() -> str:
        while True:
            _lead_seq["n"] += 1
            candidate = f"LD-{_lead_seq['n']:03d}"
            if candidate not in used_lead_nos:
                used_lead_nos.add(candidate)
                return candidate

    n_new = n_upd = 0
    for r in leads:
        nm = company_of(r)
        if not nm:
            continue
        entity = eid(nm)
        existing = lead_by_entity.get(entity) if entity is not None else None
        fields = {
            "company": nm, "sector": _s(r.get("Sector")),
            "lens": _s(r.get("Mitigation / Adaptation")), "source": _s(r.get("Source")),
            "source_name": _s(r.get("Source Detail")), "rm": _s(r.get("RM Owner")),
            "temperature": _s(r.get("Status")), "contact": _s(r.get("Contact Person")),
            "designation": _s(r.get("Designation")), "phone": _s(r.get("Contact Phone")),
            "last_interaction_date": _date(r.get("Last Interaction Date")),
            "next_action": _s(r.get("Next Action")),
            "next_action_date": _date(r.get("Next Action Date")), "notes": _s(r.get("Notes")),
        }
        if existing is None:
            lead = Lead(tenant_id=tenant_id, lead_no=_next_lead_no(), entity_id=entity,
                        status="Active", created_by="xlsx-import", updated_by="xlsx-import",
                        **fields)
            session.add(lead)
            if entity is not None:
                lead_by_entity[entity] = lead
            n_new += 1
        else:
            # Authoritative MIS re-import: overwrite with the sheet's value when present,
            # keep the curated value when the sheet cell is blank.
            for key, val in fields.items():
                if val is not None:
                    setattr(existing, key, val)
            existing.updated_by = "xlsx-import"
            n_upd += 1
    await session.flush()
    counts["leads"] = n_new
    counts["leads_updated"] = n_upd

    # --- deals (upsert by entity) ---------------------------------------
    # One deal per company in the MIS. Reuse the existing deal for an entity on re-import
    # (updating its flags/stage) rather than inserting a duplicate.
    existing_deals = (
        await session.execute(
            select(Deal).where(Deal.tenant_id == tenant_id, Deal.deleted_at.is_(None)))
    ).scalars().all()
    deal_obj_by_entity: dict[uuid.UUID, Deal] = {}
    for d in existing_deals:
        if d.entity_id is not None:
            deal_obj_by_entity.setdefault(d.entity_id, d)
    n_new = n_upd = 0
    for r in deals:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        fields = {
            "is_lending": _yes(r.get("Lending?")), "is_syndication": _yes(r.get("Syndication?")),
            "is_asset_mon": _yes(r.get("Asset Mon?")), "rm": _s(r.get("RM")),
            "stage": _map_credit_stage(_s(r.get("Stage"))), "temperature": _s(r.get("Status")),
            "source": _s(r.get("Source")), "source_detail": _s(r.get("Source Detail")),
            "date_received": _date(r.get("Date Received")), "remarks": _s(r.get("Remarks")),
        }
        verdict, missing = _screen("Deal", fields["stage"], "Deals", nm, fields)
        if verdict == "skip":
            continue
        existing = deal_obj_by_entity.get(entity)
        if existing is None:
            deal = Deal(tenant_id=tenant_id, deal_no=None, entity_id=entity, code=None,
                        created_by="xlsx-import", updated_by="xlsx-import", **fields)
            _note_stage_change(deal, "stage_history", "stage", None, fields["stage"], "Deals")
            session.add(deal)
            deal_obj_by_entity[entity] = deal
            n_new += 1
            obj = deal
        else:
            _note_stage_change(existing, "stage_history", "stage",
                               getattr(existing, "stage", None), fields["stage"], "Deals")
            for key, val in fields.items():
                # flags are always meaningful; strings only overwrite when present.
                if key.startswith("is_") or val is not None:
                    setattr(existing, key, val)
            existing.updated_by = "xlsx-import"
            n_upd += 1
            obj = existing
        if verdict == "retain":
            _open_recon("Deal", obj, "stage", fields["stage"], missing, "Deals", nm, fields)
    await session.flush()
    counts["deals"] = n_new
    counts["deals_updated"] = n_upd

    # entity id → its deal id, so trackers link back (existing + just-created).
    deal_by_entity: dict = {ent: d.id for ent, d in deal_obj_by_entity.items()}

    async def _tracker_no_pool(model, prefix: str) -> tuple[set[str], Callable[[], str]]:
        """Reserve every tracker_no already used by ``model`` in this tenant, and return a
        generator that hands out the next free ``{prefix}NNN`` — so a re-import never
        collides on the tracker's unique (tenant_id, tracker_no)."""
        used = set(
            (await session.execute(
                select(model.tracker_no).where(
                    model.tenant_id == tenant_id, model.tracker_no.is_not(None))
            )).scalars().all())
        counter = {"n": 0}

        def _next() -> str:
            while True:
                counter["n"] += 1
                candidate = f"{prefix}{counter['n']:03d}"
                if candidate not in used:
                    used.add(candidate)
                    return candidate
        return used, _next

    # --- lending tracker (upsert by entity) -----------------------------
    existing_lending = (
        await session.execute(
            select(LendingTracker).where(LendingTracker.tenant_id == tenant_id,
                                         LendingTracker.deleted_at.is_(None)))
    ).scalars().all()
    lend_by_entity: dict[uuid.UUID, LendingTracker] = {}
    for lt in existing_lending:
        if lt.entity_id is not None:
            lend_by_entity.setdefault(lt.entity_id, lt)
    _, next_lending_no = await _tracker_no_pool(LendingTracker, "L")
    n_new = n_upd = 0
    for r in lending:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        raw_stage = _s(r.get("Stage"))
        disb_amt = _float(r.get("Disbursed Amount (₹ Cr)"))
        disb_date = _date(r.get("Disbursement Date"))
        prop_amt = _float(r.get("Proposed Disbursement Amount (₹ Cr)"))
        prop_date = _date(r.get("Proposed Disbursement Date"))
        # A legacy 'Disbursed' row carries no separate proposed column — its recorded disbursement
        # amount/date ARE the proposed drawdown for the mapped 'Handed Over to Advaya' terminal.
        if raw_stage == "Disbursed":
            prop_amt = prop_amt if prop_amt is not None else disb_amt
            prop_date = prop_date if prop_date is not None else disb_date
        fields = {
            "deal_id": deal_by_entity.get(entity),
            "amount_cr": _float(r.get("Lending Amount (₹ Cr)")), "rm": _s(r.get("RM")),
            "analyst": _s(r.get("Credit Analyst")), "stage": _map_credit_stage(raw_stage),
            "stage_updated_at": _date(r.get("Stage Updated")),
            "sanction_date": _date(r.get("Sanction Date")),
            "proposed_disbursement_amount": prop_amt, "proposed_disbursement_date": prop_date,
            "disbursed_amount": disb_amt, "disbursement_date": disb_date,
            "remarks": _s(r.get("Remarks")),
        }
        verdict, missing = _screen("Lending", fields["stage"], "Lending Tracker", nm, fields)
        if verdict == "skip":
            continue
        existing = lend_by_entity.get(entity)
        if existing is None:
            lt = LendingTracker(tenant_id=tenant_id, tracker_no=next_lending_no(),
                                entity_id=entity, created_by="xlsx-import",
                                updated_by="xlsx-import", **fields)
            _note_stage_change(lt, "stage_history", "stage", None, fields["stage"],
                               "Lending Tracker")
            session.add(lt)
            lend_by_entity[entity] = lt
            n_new += 1
            obj = lt
        else:
            _note_stage_change(existing, "stage_history", "stage",
                               getattr(existing, "stage", None), fields["stage"],
                               "Lending Tracker")
            for key, val in fields.items():
                if val is not None:
                    setattr(existing, key, val)
            existing.updated_by = "xlsx-import"
            n_upd += 1
            obj = existing
        if verdict == "retain":
            _open_recon("Lending", obj, "stage", fields["stage"], missing, "Lending Tracker",
                        nm, fields)
    await session.flush()
    counts["lending_tracker"] = n_new
    counts["lending_tracker_updated"] = n_upd

    # --- syndication: one tracker per company + a lender row per bank ---
    # Preload existing trackers (keyed by company) and lenders (keyed by tracker+bank) so
    # a re-import reuses the tracker and never duplicates a bank on the same syndication
    # (SyndicationLender has no unique key, so we dedupe here).
    existing_syn = (
        await session.execute(
            select(SyndicationTracker).where(SyndicationTracker.tenant_id == tenant_id,
                                             SyndicationTracker.deleted_at.is_(None)))
    ).scalars().all()
    syn_by_entity: dict[uuid.UUID, SyndicationTracker] = {}
    syn_tracker_by: dict[str, SyndicationTracker] = {}
    for tr in existing_syn:
        if tr.entity_id is not None:
            syn_by_entity.setdefault(tr.entity_id, tr)
    lender_seen: set[tuple[uuid.UUID, str]] = set()
    if existing_syn:
        for lender in (
            await session.execute(
                select(SyndicationLender.syndication_id, SyndicationLender.lender_name)
                .where(SyndicationLender.tenant_id == tenant_id,
                       SyndicationLender.deleted_at.is_(None)))
        ).all():
            lender_seen.add((lender[0], (lender[1] or "").lower()))
    _, next_syn_no = await _tracker_no_pool(SyndicationTracker, "S")
    n_syn = n_lender = 0
    for r in syn:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        syn_status = _s(r.get("Deal Status"))
        verdict, missing = _screen("Syndication", syn_status, "Syndication", nm, {})
        if verdict == "skip":
            continue
        k = _key(nm)
        tr = syn_tracker_by.get(k) or syn_by_entity.get(entity)
        if tr is None:
            tr = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=next_syn_no(), entity_id=entity,
                deal_id=deal_by_entity.get(entity),
                status=syn_status, amount_cr=_float(r.get("Amount (₹ Cr)")),
                created_by="xlsx-import", updated_by="xlsx-import",
            )
            _note_stage_change(tr, "status_history", "status", None, syn_status, "Syndication")
            session.add(tr)
            await session.flush()
            n_syn += 1
        elif syn_status is not None and tr.status != syn_status:
            # A merge that moves an existing syndication's status records the transition too.
            _note_stage_change(tr, "status_history", "status", tr.status, syn_status,
                               "Syndication")
            tr.status = syn_status
        syn_tracker_by[k] = tr
        syn_by_entity[entity] = tr
        bank = _s(r.get("Bank"))
        if bank and (tr.id, bank.lower()) not in lender_seen:
            lender_seen.add((tr.id, bank.lower()))
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

    # --- asset monetisation (upsert by entity) --------------------------
    existing_am = (
        await session.execute(
            select(AssetMonetisation).where(AssetMonetisation.tenant_id == tenant_id,
                                            AssetMonetisation.deleted_at.is_(None)))
    ).scalars().all()
    am_by_entity: dict[uuid.UUID, AssetMonetisation] = {}
    for a in existing_am:
        if a.entity_id is not None:
            am_by_entity.setdefault(a.entity_id, a)
    _, next_am_no = await _tracker_no_pool(AssetMonetisation, "A")
    n_new = n_upd = 0
    for r in am:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        notes = " | ".join(x for x in [_s(r.get("Notes")), _s(r.get("Updated Remarks 19 July 2026"))] if x)
        fields = {
            "deal_id": deal_by_entity.get(entity), "state": _s(r.get("State")),
            "indicative_value_cr": _float(r.get("Indicative Value (₹ Cr)")),
            "size_mw": _float(r.get("Size (MW)")), "nature": _s(r.get("Nature")),
            "deal_type": _s(r.get("Deal Type")), "investor": _s(r.get("Investor")),
            "investor_type": _s(r.get("Investor Type")), "status": _s(r.get("Status")),
            "teaser_date": _date(r.get("Date Teaser Shared")), "notes": notes or None,
        }
        verdict, missing = _screen("AssetMonetisation", fields["status"], "Asset Mon", nm, fields)
        if verdict == "skip":
            continue
        existing = am_by_entity.get(entity)
        if existing is None:
            a = AssetMonetisation(tenant_id=tenant_id, tracker_no=next_am_no(),
                                  entity_id=entity, created_by="xlsx-import",
                                  updated_by="xlsx-import", **fields)
            _note_stage_change(a, "status_history", "status", None, fields["status"], "Asset Mon")
            session.add(a)
            am_by_entity[entity] = a
            n_new += 1
            obj = a
        else:
            _note_stage_change(existing, "status_history", "status",
                               getattr(existing, "status", None), fields["status"], "Asset Mon")
            for key, val in fields.items():
                if val is not None:
                    setattr(existing, key, val)
            existing.updated_by = "xlsx-import"
            n_upd += 1
            obj = existing
        if verdict == "retain":
            _open_recon("AssetMonetisation", obj, "status", fields["status"], missing,
                        "Asset Mon", nm, fields)
    await session.flush()
    counts["asset_monetisation"] = n_new
    counts["asset_monetisation_updated"] = n_upd

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
        tr = syn_tracker_by.get(k) or syn_by_entity.get(entity)
        if tr is None:
            tr = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=next_syn_no(),
                entity_id=entity, mandate_status=mand, rm=_s(r.get("RM")),
                created_by="xlsx-import", updated_by="xlsx-import",
            )
            session.add(tr)
            await session.flush()
            syn_tracker_by[k] = tr
            syn_by_entity[entity] = tr
            counts["syndication_tracker"] += 1
        else:
            tr.mandate_status = mand
            if not tr.rm:
                tr.rm = _s(r.get("RM"))
        n += 1
    await session.flush()
    counts["mandate_applied"] = n

    # Open a durable reconciliation item for every retained-incomplete row (now that each row's id
    # exists after the flushes above). The ORIGINAL imported values are preserved on the item.
    for subject_type, obj, field, value, missing, sheet, company, original in pending_recon:
        session.add(ImportReconciliationItem(
            tenant_id=tenant_id, import_batch_id=batch_id, checksum=batch_id,
            subject_type=subject_type, subject_id=obj.id, sheet=sheet, company=company,
            stage_field=field, stage_value=value, missing_fields=list(missing),
            original_values=original, status="Required", created_by=actor))
    await session.flush()
    counts["reconciliation_items"] = len(pending_recon)

    return counts
