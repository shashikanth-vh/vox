"""Import the Evam ATLAS MIS spreadsheet (the 6-sheet consolidated xlsx) into the Register.

This loads the *authoritative* MIS — not the HTML-prototype snapshot in atlas_data.json —
so the Register mirrors the real spreadsheet. Sheet → table mapping:

    Leads            → leads
    Deals            → deals            (Lending?/Syndication?/Asset Mon? → the 3 flags)
    Lending Tracker  → lending_tracker
    Syndication      → syndication_tracker (one per company) + syndication_lenders (per bank)
    Asset Mon        → asset_monetisation (one row per MANDATE — a company may have several)
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
from decimal import Decimal
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
from app.seed.ledger_xlsx import (
    LEAD_STATUS_CANON as _LEAD_STATUS_CANON,
    LENDER_RANK as _LENDER_RANK,
    LENDER_VOCAB as _LENDER_VOCAB,
    SYN_RANK as _SYN_RANK,
    canon_lender_status as _canon_lender_status,
    canon_temp as _canon_temp,
    extras_note as _extras_note,
    join_notes as _join_notes,
    ledger_syn_rows as _ledger_syn_rows,
    parse_field_tags as _parse_field_tags,
    sheet_rows as _sheet,
)

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
# the milestones and — with no Advaya integration — its terminal is 'Disbursed', so a
# historical 'Disbursed' loan maps there (its recorded amount/date become the proposed drawdown).
_LEGACY_CREDIT_STAGE: dict[str, str] = {
    "Documentation": "CP/CS Completed",
}


def _map_credit_stage(v: str | None) -> str | None:
    return _LEGACY_CREDIT_STAGE.get(v, v) if v is not None else v


# Wording variants observed in the live MIS — canonicalised (and REPORTED as
# translations) rather than quarantined. Keys are lowercase/space-collapsed.
_WORDING_ALIASES: dict[str, dict[str, str]] = {
    "Syndication": {
        "ip received": "IP Received",
        "im under preparation": "IM in Prep",
        "im sent": "IM Circulated",
        "final sanction received": "Sanctioned",
        # Ledger deal-level derivations ("Most Advanced Stage") that reach us via rows.
        "all rejected": "Rejected",
    },
}


def _canon_value(subject_type: str, value: str | None) -> str | None:
    """Case/whitespace-insensitive canonical form of a lifecycle value, plus the curated
    wording aliases above. Unknown values return UNCHANGED — the screening step then
    quarantines them with a named reason (the fail-closed default for future drift)."""
    if value is None:
        return None
    key = " ".join(str(value).split()).lower()
    alias = _WORDING_ALIASES.get(subject_type, {}).get(key)
    if alias is not None:
        return alias
    from evam_backend_core.rbac import STAGE_VOCAB as _SV
    rule = _SV.get(subject_type)
    if rule:
        for canonical in rule[1]:
            if canonical.lower() == key:
                return canonical
    return value


def _canon_funnel(value: str | None) -> str | None:
    """The Deals sheet's ORIGINATION-FUNNEL vocabulary (rbac.DEAL_FUNNEL_STAGES),
    matched case/whitespace-insensitively; None when the value is not a funnel term."""
    if value is None:
        return None
    from evam_backend_core.rbac import DEAL_FUNNEL_STAGES
    key = " ".join(str(value).split()).lower()
    for canonical in DEAL_FUNNEL_STAGES:
        if canonical.lower() == key:
            return canonical
    return None

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
        # Reconciliation snapshots go into a JSONB column via json.dumps — every non-primitive
        # the row can carry must be coerced (a raw UUID/Decimal 500s the whole import).
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, Decimal):
            return float(v)
        return v

    translated: list[dict] = report.setdefault("translated", [])
    derived: list[dict] = report.setdefault("derived", [])

    def _c(subject_type: str, sheet: str, company, value):
        """Canonicalize a lifecycle value (case/whitespace + curated wording aliases) and
        RECORD the translation in the report when it changed — the Excel stays the source
        of truth, and the report shows exactly what normalisation did."""
        out = _canon_value(subject_type, value)
        if value is not None and out != value:
            translated.append({"sheet": sheet, "company": company,
                               "from": value, "to": out, "batch_id": batch_id})
        return out

    def _screen(subject_type: str, value, sheet: str, company, row_fields: dict,
                force_retain: bool = False) -> tuple[str, list]:
        """Screen a row's lifecycle value. Returns (verdict, missing):
        * ``("skip", [])``   — quarantine (an UNKNOWN value, or a known stage missing mandatory data
          when ``retain_incomplete`` is False): the SAME state the interactive API rejects.
        * ``("ok", [])``     — import cleanly.
        * ``("retain", [...])`` — a known stage missing mandatory data, imported under the historical
          override: the caller must flag the record reconciliation_status=Required and open a
          reconciliation item listing the missing fields. A NULL value is always ("ok", []).
        ``force_retain`` upgrades the missing-mandatory skip to a retain even when the run did not
        opt into retain_incomplete — for rows that represent a REAL exposure (a disbursed facility)
        which the business rule says may never be dropped. Unknown values still skip."""
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
        if not retain_incomplete and not force_retain:
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
    # Two workbook generations: the v4 'Syndication' flat sheet, or the live ledger's
    # two-section 'Syndication Tracker' (lender-level rows are the authoritative part).
    syn = _sheet(wb, "Syndication") or _ledger_syn_rows(wb)
    am = _sheet(wb, "Asset Mon") or _sheet(wb, "Asset Mon Tracker")
    mandate = _sheet(wb, "Mandate Tracker")
    partnership = _sheet(wb, "Partnership Tracker")
    client_master = _sheet(wb, "Client Master")
    lender_master = _sheet(wb, "Lender Master")
    people_master = _sheet(wb, "People Master")

    # --- entities: every distinct company across all sheets -------------
    def company_of(row: dict) -> str | None:
        return _s(row.get("Company Name"))

    # The ledger's join key: tracker rows may carry only the Client ID (a company cell
    # can be blank) — resolve the company through the Deals sheet so no row is orphaned.
    cid_company: dict[str, str] = {}
    for r in deals:
        cid, nm = _s(r.get("Client ID")), company_of(r)
        if cid and nm:
            cid_company[cid] = nm
    for sheet_rows in (leads, lending, syn, am, mandate, partnership):
        for r in sheet_rows:
            cid = _s(r.get("Client ID"))
            if not company_of(r) and cid and cid in cid_company:
                r["Company Name"] = cid_company[cid]

    # Client Master: the canonical company registry (Group Code / legal name / default
    # sector / PAN / notes) — consulted at entity creation so codes match the ledger.
    cm_by_key: dict[str, dict] = {}
    for r in client_master:
        nm = _s(r.get("Company Legal Name"))
        if nm:
            cm_by_key.setdefault(_key(nm), r)

    # enrichment lookups (first non-empty wins), keyed by normalized name
    sector_by, lens_by, state_by = {}, {}, {}
    for r in leads:
        k = _key(company_of(r) or "")
        sector_by.setdefault(k, _s(r.get("Sector")))
        lens_by.setdefault(k, _s(r.get("Mitigation / Adaptation")))
        state_by.setdefault(k, _s(r.get("Location")))
    # The company's credit analyst, from whichever tracker line names one first — the
    # Deals grid shows it at deal level, but the ledger only carries it per line.
    analyst_by: dict[str, str] = {}
    for r in lending + syn:
        k = _key(company_of(r) or "")
        a = _s(r.get("Credit Analyst"))
        if a and k not in analyst_by:
            analyst_by[k] = a
    for r in deals:
        k = _key(company_of(r) or "")
        sector_by.setdefault(k, _s(r.get("Sector")))
        state_by.setdefault(k, _s(r.get("Location")))
    for r in am:
        k = _key(company_of(r) or "")
        state_by.setdefault(k, _s(r.get("State")))

    names: dict[str, str] = {}  # key -> original display name
    for sheet in (leads, deals, lending, syn, am, mandate, partnership):
        for r in sheet:
            nm = company_of(r)
            if nm:
                names.setdefault(_key(nm), nm)
    for k, r in cm_by_key.items():
        names.setdefault(k, _s(r.get("Company Legal Name")))

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
        cm = cm_by_key.get(k) or {}
        cm_code = _s(cm.get("Group Code"))
        cm_notes = _join_notes(
            _s(cm.get("Group Notes")),
            f"[PAN: {_s(cm.get('PAN (optional)'))}]" if _s(cm.get("PAN (optional)")) else None)
        if ent is None:
            # The ledger's own Group Code is the entity code whenever the Client Master
            # names one (round-trip identity); a collision or a company the master does
            # not know falls back to the generated code.
            code = cm_code if cm_code and cm_code not in codegen.used else codegen.make(nm)
            codegen.used.add(code)
            ent = Entity(
                tenant_id=tenant_id, code=code, legal_name=nm,
                sector=sector_by.get(k) or _s(cm.get("Sector (default)")),
                lens=lens_by.get(k), state=state_by.get(k),
                pan=_s(cm.get("PAN (optional)")), notes=_s(cm.get("Group Notes")),
                register_status="Pipeline", created_by="xlsx-import",
                updated_by="xlsx-import")
            session.add(ent)
            await session.flush()
            n_new += 1
        else:
            # Enrich only empty fields — never clobber curated data on a merge.
            if not ent.sector and (sector_by.get(k) or _s(cm.get("Sector (default)"))):
                ent.sector = sector_by.get(k) or _s(cm.get("Sector (default)"))
            if not ent.lens and lens_by.get(k):
                ent.lens = lens_by[k]
            if not ent.state and state_by.get(k):
                ent.state = state_by[k]
            if not ent.pan and _s(cm.get("PAN (optional)")):
                ent.pan = _s(cm.get("PAN (optional)"))
            if not ent.notes and cm_notes:
                ent.notes = _s(cm.get("Group Notes"))
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
    person_by_full = {_key(p.full_name or ""): p for p in existing_people if p.full_name}
    n_people = n_pm = 0

    # People Master FIRST — the ledger's authoritative team directory (Role / Initials /
    # Full Name). It creates or enriches by full name; the initials become the short
    # handle. Names harvested from RM/analyst cells afterwards only fill the gaps.
    for r in people_master:
        full = _s(r.get("Full Name"))
        if not full:
            continue
        initials, role = _s(r.get("Initials")), _s(r.get("Role"))
        pnotes = _s(r.get("Notes"))
        # Optional Email column: the join key between this roster row and the person's
        # sign-in account (Access matches by email first) — fill it in the sheet and
        # nobody stitches identities by hand in the UI.
        pemail = _s(r.get("Email"))
        p = person_by_full.get(_key(full))
        if p is None:
            p = Person(tenant_id=tenant_id, name=initials or full.split()[0],
                       full_name=full, role=role or "RM", notes=pnotes, email=pemail,
                       created_by="xlsx-import", updated_by="xlsx-import")
            session.add(p)
            people_seen.add(_key(full))
            person_by_full[_key(full)] = p
            n_pm += 1
        else:
            if role:
                p.role = role
            if initials and (not p.name or p.name == (p.full_name or "").split()[0]):
                p.name = initials
            if pnotes and not p.notes:
                p.notes = pnotes
            if pemail:
                p.email = pemail
            p.updated_by = "xlsx-import"

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
    for r in deals + lending + am + mandate + partnership:
        add_person(r.get("RM"), "RM")
    for r in lending:
        add_person(r.get("Credit Analyst"), "Analyst")
    for r in syn:
        add_person(r.get("Credit Analyst"), "Analyst")
    await session.flush()
    counts["people"] = n_people
    counts["people_master_applied"] = n_pm

    # --- counterparties: distinct banks (upsert by name) ----------------
    # Seed the id map from counterparties already in this tenant so a re-import reuses
    # them (counterparties_tenant_name is unique) instead of inserting duplicate banks.
    existing_cps = (
        await session.execute(
            select(Counterparty).where(Counterparty.tenant_id == tenant_id,
                                        Counterparty.deleted_at.is_(None)))
    ).scalars().all()
    cp_id_by: dict[str, uuid.UUID] = {c.name.lower(): c.id for c in existing_cps if c.name}
    cp_obj_by: dict[str, Counterparty] = {c.name.lower(): c for c in existing_cps if c.name}
    n_cp = 0

    async def add_counterparty(name: str | None, **extra) -> None:
        nonlocal n_cp
        name = _s(name)
        if not name:
            return
        cp = cp_obj_by.get(name.lower())
        if cp is None:
            cp = Counterparty(tenant_id=tenant_id, name=name, created_by="xlsx-import",
                              updated_by="xlsx-import",
                              **{k: v for k, v in extra.items() if v is not None})
            session.add(cp)
            await session.flush()
            cp_id_by[name.lower()] = cp.id
            cp_obj_by[name.lower()] = cp
            n_cp += 1
        else:
            for k, v in extra.items():
                if v is None:
                    continue
                # The master's active flag is authoritative; other fields only fill gaps.
                if k == "is_active" or getattr(cp, k, None) in (None, ""):
                    setattr(cp, k, v)
            cp.updated_by = "xlsx-import"

    # Lender Master FIRST (type / short name / active flag / preferred sectors / notes
    # — the derived engagement counts are recomputed by PRISM, never imported), then
    # any lender named only on a tracker row.
    for r in lender_master:
        active = _s(r.get("Active?"))
        await add_counterparty(
            r.get("Lender Name"),
            counterparty_type=_s(r.get("Type")), short_name=_s(r.get("Short Name")),
            is_active=None if active is None else active.lower() in ("yes", "y", "true"),
            sectors=_s(r.get("Preferred Sectors")), notes=_s(r.get("Notes")))
    for r in syn:
        await add_counterparty(r.get("Bank"))
    for r in partnership:
        await add_counterparty(r.get("Partner Lender"))
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
    _LEADS_USED = {"Lead ID", "Company Name", "Sector", "Mitigation / Adaptation",
                   "Source", "Source Detail", "RM Owner", "Status", "Status#2",
                   "Contact Person", "Designation", "Contact Phone", "Location",
                   "Last Interaction Date", "Next Action", "Next Action Date", "Notes"}
    # Leads whose ledger lifecycle says Converted — linked to their deal AFTER the
    # deals pass (the deal may not exist yet at this point of the run).
    converted_leads: list = []
    # Entities whose lead was CREATED this run — a later row for the same company is a
    # within-file duplicate whose merge is reported, not silently absorbed.
    lead_new_entities: set[uuid.UUID] = set()
    for r in leads:
        nm = company_of(r)
        if not nm:
            # A row with no company cannot become a lead — but it is not silently
            # dropped either: it lands in the report with its content preserved.
            content = _extras_note(r, set()) or ""
            quarantined.append({"sheet": "Leads", "company": None, "field": "company",
                                "value": content[:500] or None,
                                "reason": "row has no company name",
                                "batch_id": batch_id})
            continue
        entity = eid(nm)
        existing = lead_by_entity.get(entity) if entity is not None else None
        # The ledger's Leads sheet carries TWO Status columns: lifecycle first
        # (Active / Converted to Deal / Dropped), temperature second. The v4 sheet has
        # only the temperature one. Typos in the live data canonicalise with a record.
        dual = "Status#2" in r
        temp_raw = _s(r.get("Status#2")) if dual else _s(r.get("Status"))
        temp, changed = _canon_temp(temp_raw)
        if changed:
            translated.append({"sheet": "Leads", "company": nm, "field": "temperature",
                               "from": temp_raw, "to": temp, "batch_id": batch_id})
        life_raw = _s(r.get("Status")) if dual else None
        life = _LEAD_STATUS_CANON.get(" ".join((life_raw or "").split()).lower())
        if life_raw and life and life != life_raw:
            translated.append({"sheet": "Leads", "company": nm, "field": "status",
                               "from": life_raw, "to": life, "batch_id": batch_id})
        fields = {
            "company": nm, "sector": _s(r.get("Sector")),
            "lens": _s(r.get("Mitigation / Adaptation")), "source": _s(r.get("Source")),
            "source_name": _s(r.get("Source Detail")), "rm": _s(r.get("RM Owner")),
            "temperature": temp, "contact": _s(r.get("Contact Person")),
            "designation": _s(r.get("Designation")), "phone": _s(r.get("Contact Phone")),
            "last_interaction_date": _date(r.get("Last Interaction Date")),
            "next_action": _s(r.get("Next Action")),
            "next_action_date": _date(r.get("Next Action Date")),
            "notes": _join_notes(_s(r.get("Notes")), _extras_note(r, _LEADS_USED)),
        }
        if existing is None:
            # The ledger's own Lead ID is the lead number when it is free — round-trip
            # identity; a clash or a blank falls back to the generated sequence.
            ledger_no = _s(r.get("Lead ID"))
            lead_no = ledger_no if ledger_no and ledger_no not in used_lead_nos \
                else _next_lead_no()
            used_lead_nos.add(lead_no)
            lead = Lead(tenant_id=tenant_id, lead_no=lead_no, entity_id=entity,
                        status=life or "Active", created_by="xlsx-import",
                        updated_by="xlsx-import", **fields)
            session.add(lead)
            if entity is not None:
                lead_by_entity[entity] = lead
                lead_new_entities.add(entity)
            n_new += 1
            if life == "Converted" and entity is not None:
                converted_leads.append((lead, entity))
        else:
            if entity is not None and entity in lead_new_entities:
                # A SECOND row for the same company inside one file: PRISM keeps one
                # lead per company, so the rows merge (non-blank cells win) — recorded,
                # because "one row disappeared" must never be a surprise.
                derived.append({"sheet": "Leads", "company": nm, "batch_id": batch_id,
                                "note": "second row for the same company merged onto "
                                        "its lead (one lead per company)"})
            # Authoritative MIS re-import: overwrite with the sheet's value when present,
            # keep the curated value when the sheet cell is blank.
            for key, val in fields.items():
                if val is not None:
                    setattr(existing, key, val)
            if life and existing.status != "Converted":
                existing.status = life
                if life == "Converted" and entity is not None:
                    converted_leads.append((existing, entity))
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
        # The MIS Deals sheet speaks the ORIGINATION FUNNEL (New Inquiry / In Screening /
        # In Pipeline / Screened Out / Closed Won / Closed Lost / On Hold) — and since the
        # two-layer migration that funnel IS the deal's stage. A deal carries no credit
        # lifecycle: credit values belong on the Lending Tracker sheet, so a non-funnel
        # value here quarantines with a named reason (fail-closed; the sheet's vocabulary
        # drifted or a credit value landed on the wrong sheet).
        raw_stage = _s(r.get("Stage"))
        funnel = _canon_funnel(raw_stage)
        if funnel is not None and funnel != raw_stage:
            translated.append({"sheet": "Deals", "company": nm,
                               "from": raw_stage, "to": funnel, "batch_id": batch_id})
        temp_raw = _s(r.get("Status"))
        temp, t_changed = _canon_temp(temp_raw)
        if t_changed:
            translated.append({"sheet": "Deals", "company": nm, "field": "temperature",
                               "from": temp_raw, "to": temp, "batch_id": batch_id})
        # The ledger's fourth product flag: a Partnership (co-lending) engagement lives
        # on the platform-deals plane, so it raises is_syndication too — recorded as a
        # derivation, with the original flag preserved on the remarks.
        part_flag = _yes(r.get("Partnership?"))
        if part_flag and not _yes(r.get("Syndication?")):
            derived.append({"sheet": "Deals", "company": nm, "batch_id": batch_id,
                            "note": "Partnership? = Yes → is_syndication (partnership "
                                    "tracker rides the platform-deals plane)"})
        _DEALS_USED = {"Client ID", "Group Code", "Company Name", "Sector", "Location",
                       "Source", "Source Detail", "Status", "RM", "Lending?",
                       "Syndication?", "Partnership?", "Asset Mon?", "Stage",
                       "Date Received", "Remarks"}
        fields = {
            "is_lending": _yes(r.get("Lending?")),
            "is_syndication": _yes(r.get("Syndication?")) or part_flag,
            "is_asset_mon": _yes(r.get("Asset Mon?")), "rm": _s(r.get("RM")),
            # The Deals grid shows these at deal level; the ledger carries them on the
            # LEADS sheet (Mitigation/Adaptation) and the TRACKER lines (Credit
            # Analyst) — copy them across so the columns aren't born empty.
            "lens": lens_by.get(_key(nm)),
            "analyst": analyst_by.get(_key(nm)),
            "stage": funnel, "temperature": temp,
            "source": _s(r.get("Source")), "source_detail": _s(r.get("Source Detail")),
            "date_received": _date(r.get("Date Received")),
            # ATLAS quotes a deal by its client's code — the ledger's Group Code.
            "code": _s(r.get("Group Code")),
            "remarks": _join_notes(
                _s(r.get("Remarks")),
                # Tag once — a re-imported export already carries it in Remarks.
                "[Partnership: Yes]"
                if part_flag and "[Partnership: Yes]" not in (_s(r.get("Remarks")) or "")
                else None,
                _extras_note(r, _DEALS_USED)),
        }
        # _screen against STAGE_VOCAB["Deal"] (the funnel): a canonical funnel value passes;
        # anything else (e.g. a credit-lifecycle word) quarantines by name.
        verdict, missing = _screen("Deal", funnel if funnel is not None else raw_stage,
                                   "Deals", nm, fields)
        if verdict == "skip":
            continue
        existing = deal_obj_by_entity.get(entity)
        if existing is None:
            deal = Deal(tenant_id=tenant_id, deal_no=None, entity_id=entity,
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

    # Ledger leads marked 'Converted to Deal' link to their company's deal now that
    # the deals exist — the lead keeps its history instead of dangling.
    for lead, entity in converted_leads:
        if getattr(lead, "converted_deal_id", None) is None and deal_by_entity.get(entity):
            lead.converted_deal_id = deal_by_entity[entity]

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

    # --- lending tracker (one line PER SHEET ROW) -----------------------
    # A company may hold more than one facility. Every sheet row is its own line; a merge
    # re-import matches a company's rows in SHEET ORDER (1st sheet row updates its 1st
    # line, …), creating any extras — distinct facilities are never blended into one row.
    existing_lending = (
        await session.execute(
            select(LendingTracker).where(LendingTracker.tenant_id == tenant_id,
                                         LendingTracker.deleted_at.is_(None))
            # tracker_no is handed out sequentially at creation, so it preserves the original
            # sheet order even when created_at ties within one import batch (id is random).
            .order_by(LendingTracker.created_at, LendingTracker.tracker_no,
                      LendingTracker.id))
    ).scalars().all()
    lend_by_entity: dict[uuid.UUID, list[LendingTracker]] = {}
    for lt in existing_lending:
        if lt.entity_id is not None:
            lend_by_entity.setdefault(lt.entity_id, []).append(lt)
    _, next_lending_no = await _tracker_no_pool(LendingTracker, "L")
    lend_row_ix: dict[uuid.UUID, int] = {}
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
        # A 'Disbursed' row's mandatory proposed amount/date are derived from what the sheet
        # actually carries, in order of fidelity: an explicit proposed column, the recorded
        # disbursement columns, and finally the facility amount + the stage-updated date (the
        # live MIS has no disbursement columns at all — those two ARE its record of the
        # drawdown). Every fallback derivation is REPORTED, never silent.
        if raw_stage == "Disbursed":
            prop_amt = prop_amt if prop_amt is not None else disb_amt
            prop_date = prop_date if prop_date is not None else disb_date
            if prop_amt is None:
                amt = _float(r.get("Lending Amount (₹ Cr)"))
                if amt is not None:
                    prop_amt = amt
                    derived.append({"sheet": "Lending Tracker", "company": nm,
                                    "field": "proposed_disbursement_amount",
                                    "from_column": "Lending Amount (₹ Cr)", "value": amt,
                                    "batch_id": batch_id})
            if prop_date is None:
                dt = _date(r.get("Stage Updated"))
                if dt is not None:
                    prop_date = dt
                    derived.append({"sheet": "Lending Tracker", "company": nm,
                                    "field": "proposed_disbursement_date",
                                    "from_column": "Stage Updated", "value": dt.isoformat(),
                                    "batch_id": batch_id})
        _LENDING_USED = {"Client ID", "Company Name", "Lending Amount (₹ Cr)", "RM",
                         "Credit Analyst", "Stage", "Stage Updated", "Pending With",
                         "Sanction Date", "Date Sanctioned",
                         "Proposed Disbursement Amount (₹ Cr)",
                         "Proposed Disbursement Date", "Disbursed Amount (₹ Cr)",
                         "Disbursement Date", "Remarks"}
        fields = {
            "deal_id": deal_by_entity.get(entity),
            "amount_cr": _float(r.get("Lending Amount (₹ Cr)")), "rm": _s(r.get("RM")),
            "analyst": _s(r.get("Credit Analyst")),
            "stage": _map_credit_stage(_c("Lending", "Lending Tracker", nm, raw_stage)),
            "stage_updated_at": _date(r.get("Stage Updated")),
            "pending_with": _s(r.get("Pending With")),
            # v4 says "Sanction Date"; the live ledger says "Date Sanctioned".
            "sanction_date": _date(r.get("Sanction Date")) or _date(r.get("Date Sanctioned")),
            "proposed_disbursement_amount": prop_amt, "proposed_disbursement_date": prop_date,
            "disbursed_amount": disb_amt, "disbursement_date": disb_date,
            "remarks": _join_notes(_s(r.get("Remarks")), _extras_note(r, _LENDING_USED)),
        }
        # force_retain: a facility the sheet says is DISBURSED is a real exposure — if the
        # mandatory drawdown data cannot even be derived, it imports FLAGGED for
        # reconciliation rather than being dropped (zero-omission rule).
        verdict, missing = _screen("Lending", fields["stage"], "Lending Tracker", nm, fields,
                                   force_retain=(fields["stage"] == "Disbursed"))
        if verdict == "skip":
            continue
        lx = lend_row_ix.get(entity, 0)
        lend_row_ix[entity] = lx + 1
        llst = lend_by_entity.setdefault(entity, [])
        existing = llst[lx] if lx < len(llst) else None
        if existing is None:
            lt = LendingTracker(tenant_id=tenant_id, tracker_no=next_lending_no(),
                                entity_id=entity, created_by="xlsx-import",
                                updated_by="xlsx-import", **fields)
            _note_stage_change(lt, "stage_history", "stage", None, fields["stage"],
                               "Lending Tracker")
            session.add(lt)
            llst.append(lt)
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
    partnership_by_entity: dict[uuid.UUID, SyndicationTracker] = {}
    for tr in existing_syn:
        if tr.entity_id is None:
            continue
        # Partnership (co-lending) trackers live on the same table, flagged by line —
        # they must never absorb a company's SYNDICATION rows, nor vice versa.
        if (tr.line or "") == "Partnership":
            partnership_by_entity.setdefault(tr.entity_id, tr)
        else:
            syn_by_entity.setdefault(tr.entity_id, tr)
    lender_seen: set[tuple[uuid.UUID, str]] = set()
    # This run's lender objects, so a DUPLICATE ledger row (the same bank listed twice
    # on one company — real in the live file, e.g. two facilities) MERGES its substance
    # onto the existing row instead of silently vanishing (zero loss).
    lender_obj_by: dict[tuple[uuid.UUID, str], SyndicationLender] = {}
    if existing_syn:
        for lrow in (
            await session.execute(
                select(SyndicationLender)
                .where(SyndicationLender.tenant_id == tenant_id,
                       SyndicationLender.deleted_at.is_(None)))
        ).scalars().all():
            lender_seen.add((lrow.syndication_id, (lrow.lender_name or "").lower()))
            lender_obj_by[(lrow.syndication_id, (lrow.lender_name or "").lower())] = lrow
    # Keys CREATED this run — a repeat of one of these is a within-file duplicate
    # (reported as a merge); a repeat of a preloaded key is a re-import update (silent).
    lender_new: set[tuple[uuid.UUID, str]] = set()

    def _merge_lender(lk, status, since, response, ticket, note, sheet, nm, bank) -> None:
        """A SECOND row for the same bank on the same tracker (real in the live ledger —
        e.g. two facilities with one lender) MERGES instead of vanishing: the pipeline
        position only moves forward, blank dates fill and later dates win, a differing
        ticket and every unseen note fragment are appended as tags. Re-importing an
        identical file is therefore a no-op — nothing duplicates, nothing is lost."""
        obj = lender_obj_by.get(lk)
        if obj is None:
            return
        if status and _LENDER_RANK.get(status, 0) > _LENDER_RANK.get(obj.status or "", 0):
            obj.status = status
        if since is not None and (obj.since is None or since > obj.since):
            obj.since = since
        if response is not None and (obj.response_date is None
                                     or response > obj.response_date):
            obj.response_date = response
        if ticket is not None:
            if obj.amount_cr is None:
                obj.amount_cr = ticket
            elif float(obj.amount_cr) != float(ticket):
                also = f"[Also ticket: {ticket} Cr]"
                if not obj.note or also not in obj.note:
                    obj.note = _join_notes(obj.note, also)
        if note and (not obj.note or note not in obj.note):
            obj.note = _join_notes(obj.note, note)
        obj.updated_by = "xlsx-import"
        if lk in lender_new:
            derived.append({"sheet": sheet, "company": nm, "batch_id": batch_id,
                            "note": f"duplicate row for lender '{bank}' merged onto "
                                    "its existing row (zero loss)"})

    _, next_syn_no = await _tracker_no_pool(SyndicationTracker, "S")
    n_syn = n_lender = 0
    # Rows sorted so each company's MOST ADVANCED status is processed last — the
    # tracker's status update-per-row therefore lands on the best rank, matching the
    # ledger's own derived "Most Advanced Stage" (Rejected ranks lowest, so a mandate
    # is Rejected only when every bank declined).
    syn.sort(key=lambda r: (
        _key(_s(r.get("Company Name")) or ""),
        # A PRISM-export CARRIER row (it names the tracker's own status) sorts LAST so
        # its exact status/ask land after — and therefore over — the lender-derived ones.
        99 if _s(r.get("Tracker Status")) or _float(r.get("Tracker Ask (₹ Cr)")) is not None
        else _SYN_RANK.get(_canon_value("Syndication", _s(r.get("Status"))) or "", 0)))
    for r in syn:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        # v4 semantics: the per-bank "Status" column carries the REAL pipeline position
        # (IM Circulated / Queries Received / IP Received / …); "Deal Status" is a
        # coarse lifecycle overlay (Deal Live / Deal Dropped / Deal Closed). The tracker
        # takes the bank status (canonicalised), with the overlay forcing terminals:
        # Dropped → Dropped, Closed → Disbursed (syndication's completed terminal). A
        # live deal with no per-bank status enters at Deal Sourced.
        deal_status_raw = _s(r.get("Deal Status"))
        raw_bank_status = _s(r.get("Status"))
        # A PRISM lender-pipeline word the mandate vocabulary doesn't know
        # ('Identified', 'Declined') is NOT an unknown value: it just doesn't move the
        # tracker — the lender row below still carries it.
        _lw, _ = _canon_lender_status(raw_bank_status)
        _, _syn_vocab = STAGE_VOCAB["Syndication"]
        _probe = _canon_value("Syndication", raw_bank_status)
        if _probe is not None and _probe not in _syn_vocab and _lw in _LENDER_VOCAB:
            bank_status = None
        else:
            bank_status = _c("Syndication", "Syndication", nm, raw_bank_status)
        overlay_key = " ".join((deal_status_raw or "").split()).lower()
        overlay = {"deal dropped": "Dropped", "deal closed": "Disbursed"}.get(overlay_key)
        if overlay:
            translated.append({"sheet": "Syndication", "company": nm,
                               "from": deal_status_raw, "to": overlay, "batch_id": batch_id})
        # A PRISM-export carrier row names the tracker's OWN status/ask outright —
        # it wins over anything derived from lender rows (the sort runs it last).
        explicit_status = _c("Syndication", "Syndication", nm, _s(r.get("Tracker Status")))
        explicit_ask = _float(r.get("Tracker Ask (₹ Cr)"))
        syn_status = (overlay or explicit_status or bank_status
                      or ("Deal Sourced" if deal_status_raw else None))
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
                rm=_s(r.get("RM")), analyst=_s(r.get("Credit Analyst")),
                created_by="xlsx-import", updated_by="xlsx-import",
            )
            _note_stage_change(tr, "status_history", "status", None, syn_status, "Syndication")
            session.add(tr)
            await session.flush()
            n_syn += 1
        else:
            if syn_status is not None and tr.status != syn_status:
                # A merge that moves an existing syndication's status records the transition too.
                _note_stage_change(tr, "status_history", "status", tr.status, syn_status,
                                   "Syndication")
                tr.status = syn_status
            # Ledger rows carry the ask / RM / analyst on every lender line — fill gaps.
            if tr.amount_cr is None and _float(r.get("Amount (₹ Cr)")) is not None:
                tr.amount_cr = _float(r.get("Amount (₹ Cr)"))
            if not tr.rm and _s(r.get("RM")):
                tr.rm = _s(r.get("RM"))
            if not tr.analyst and _s(r.get("Credit Analyst")):
                tr.analyst = _s(r.get("Credit Analyst"))
        if explicit_ask is not None:
            tr.amount_cr = explicit_ask          # the tracker's own ask, exact
        syn_tracker_by[k] = tr
        syn_by_entity[entity] = tr
        bank = _s(r.get("Bank"))
        if bank:
            lk = (tr.id, bank.lower())
            # The per-LENDER status speaks PRISM's lender vocabulary (Rejected →
            # Declined; a lender-level Disbursed lands Sanctioned with the original
            # word preserved) — every change is reported as a translation.
            raw_lender = _s(r.get("Status"))
            lender_status, l_changed = _canon_lender_status(raw_lender)
            if l_changed:
                translated.append({"sheet": "Syndication", "company": nm,
                                   "field": "lender_status", "from": raw_lender,
                                   "to": lender_status, "batch_id": batch_id})
            d_data = _date(r.get("Date Data Received"))
            d_im = _date(r.get("Date IM Circulated"))
            d_ip = _date(r.get("Date In-Principle"))
            d_sanc = _date(r.get("Date Sanctioned"))
            # since = when the CURRENT state began (the latest milestone date on file);
            # response_date = the bank's latest substantive reply. Milestone dates that
            # neither field can carry are preserved as note tags — zero loss.
            since = d_sanc or d_ip or d_im or d_data
            response = d_sanc or d_ip
            date_tags = [f"[{label}: {d.isoformat()}]" for label, d in (
                ("Data received", d_data), ("IM circulated", d_im),
                ("In-principle", d_ip), ("Sanctioned", d_sanc))
                if d is not None and d not in (since, response)]
            accepted = _s(r.get("Accepted by Client"))
            note = _join_notes(
                f"[Accepted by client: {accepted}]" if accepted else None,
                "[Ledger status: Disbursed]"
                if (raw_lender or "").strip().lower() == "disbursed" else None,
                " ".join(date_tags) or None,
                _s(r.get("Remarks")))
            # The ledger's per-lender ask (absent from v4 rows, where the amount column
            # is the deal-level figure and stays off the lender row).
            ticket = _float(r.get("Ticket Size (₹ Cr)"))
            if lk not in lender_seen:
                lender_seen.add(lk)
                lrow = SyndicationLender(
                    tenant_id=tenant_id, syndication_id=tr.id, lender_name=bank,
                    counterparty_id=cp_id_by.get(bank.lower()), status=lender_status,
                    since=since, response_date=response, amount_cr=ticket,
                    note=note, created_by="xlsx-import", updated_by="xlsx-import",
                )
                session.add(lrow)
                lender_obj_by[lk] = lrow
                lender_new.add(lk)
                n_lender += 1
            else:
                _merge_lender(lk, lender_status, since, response, ticket, note,
                              "Syndication", nm, bank)
        else:
            # A detailed-section row with NO lender named yet (the desk logged the
            # company's position before shortlisting a bank — 16 such rows in the live
            # file), or a PRISM-export carrier row. Field tags a PRISM export wrote
            # ([Facility: …], [Tenor: …]) are lifted back into their tracker fields;
            # the rest (dates, remarks) lands on the tracker's remarks so nothing is
            # dropped — and re-importing the same file appends nothing twice.
            tag_fields, rest_remarks = _parse_field_tags(_s(r.get("Remarks")))
            for f, v in tag_fields.items():
                setattr(tr, f, v)
            all_dates = " ".join(
                f"[{label}: {d.isoformat()}]" for label, d in (
                    ("Data received", _date(r.get("Date Data Received"))),
                    ("IM circulated", _date(r.get("Date IM Circulated"))),
                    ("In-principle", _date(r.get("Date In-Principle"))),
                    ("Sanctioned", _date(r.get("Date Sanctioned"))))
                if d is not None) or None
            accepted = _s(r.get("Accepted by Client"))
            row_note = _join_notes(
                f"[Accepted by client: {accepted}]" if accepted else None,
                all_dates, rest_remarks)
            if row_note and (not tr.remarks or row_note not in tr.remarks):
                tr.remarks = _join_notes(tr.remarks, row_note)
    counts["syndication_tracker"] = n_syn
    counts["syndication_lenders"] = n_lender

    # --- partnership (co-lending) tracker: one platform-deals row per company -------
    # The ledger tracks a FOURTH product line — co-lending partnerships, one sheet row
    # per PARTNER LENDER. PRISM models it on the platform-deals plane: one
    # SyndicationTracker per company flagged line='Partnership' (kept apart from the
    # company's syndication mandate), each partner a lender row. The partner's stage
    # speaks the shared lender vocabulary; a sanctioned amount lands on the lender's
    # allocation; the rejection reason and every unmapped column survive on the note.
    _PART_USED = {"Client ID", "Company Name", "RM", "Partner Lender", "Stage",
                  "Stage Updated", "Pending With", "Sanctioned Amount (₹ Cr)",
                  "Rejection Reason", "Remarks"}
    partnership.sort(key=lambda r: (
        _key(_s(r.get("Company Name")) or ""),
        _SYN_RANK.get(_canon_value("Syndication", _s(r.get("Stage"))) or "", 0)))
    n_part = n_part_lender = 0
    for r in partnership:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        raw_stage = _s(r.get("Stage"))
        # A partnership row's Stage may speak either vocabulary: the mandate pipeline
        # (Docs Pending / IM Circulated / … — it then positions the tracker too) or the
        # PER-LENDER pipeline ('Identified', 'Declined' — a PRISM export writes these
        # for shortlisted partners). A lender-vocabulary word is NOT an unknown value:
        # it simply doesn't move the tracker; the partner row below still carries it.
        part_status = _c("Syndication", "Partnership Tracker", nm, raw_stage)
        _lw, _ = _canon_lender_status(raw_stage)
        lender_word = _lw in _LENDER_VOCAB if _lw else False
        _, _syn_vocab = STAGE_VOCAB["Syndication"]
        if part_status is not None and part_status not in _syn_vocab:
            if not lender_word:
                verdict, _missing = _screen("Syndication", part_status,
                                            "Partnership Tracker", nm, {})
                if verdict == "skip":
                    continue
            part_status = None      # lender-vocabulary word: tracker keeps its status
        tr = partnership_by_entity.get(entity)
        if tr is None:
            tr = SyndicationTracker(
                tenant_id=tenant_id, tracker_no=next_syn_no(), entity_id=entity,
                deal_id=deal_by_entity.get(entity), line="Partnership",
                status=part_status or "Deal Sourced", rm=_s(r.get("RM")),
                pending_with=_s(r.get("Pending With")),
                created_by="xlsx-import", updated_by="xlsx-import")
            _note_stage_change(tr, "status_history", "status", None, tr.status,
                               "Partnership Tracker")
            session.add(tr)
            await session.flush()
            partnership_by_entity[entity] = tr
            n_part += 1
        else:
            if part_status is not None and tr.status != part_status:
                _note_stage_change(tr, "status_history", "status", tr.status, part_status,
                                   "Partnership Tracker")
                tr.status = part_status
            if not tr.rm and _s(r.get("RM")):
                tr.rm = _s(r.get("RM"))
            if not tr.pending_with and _s(r.get("Pending With")):
                tr.pending_with = _s(r.get("Pending With"))
        # A live partnership engagement puts the company on the platform-deals plane
        # even when its Deals row forgot the flag — reconciled here (and recorded), so
        # the flag always agrees with the tracker and a re-import converges.
        d = deal_obj_by_entity.get(entity)
        if d is not None and not d.is_syndication:
            d.is_syndication = True
            derived.append({"sheet": "Partnership Tracker", "company": nm,
                            "batch_id": batch_id,
                            "note": "partnership engagement raises the deal's platform "
                                    "flag (is_syndication)"})
        partner = _s(r.get("Partner Lender"))
        reason = _s(r.get("Rejection Reason"))
        p_note = _join_notes(f"[Rejection reason: {reason}]" if reason else None,
                             _s(r.get("Remarks")),
                             _extras_note(r, _PART_USED))
        if partner:
            lk = (tr.id, partner.lower())
            raw_p = raw_stage
            p_status, p_changed = _canon_lender_status(raw_p)
            if p_changed:
                translated.append({"sheet": "Partnership Tracker", "company": nm,
                                   "field": "lender_status", "from": raw_p,
                                   "to": p_status, "batch_id": batch_id})
            p_since = _date(r.get("Stage Updated"))
            p_amount = _float(r.get("Sanctioned Amount (₹ Cr)"))
            if lk not in lender_seen:
                lender_seen.add(lk)
                lrow = SyndicationLender(
                    tenant_id=tenant_id, syndication_id=tr.id, lender_name=partner,
                    counterparty_id=cp_id_by.get(partner.lower()),
                    status=p_status or "Identified",
                    since=p_since, amount_cr=p_amount, note=p_note,
                    created_by="xlsx-import", updated_by="xlsx-import")
                session.add(lrow)
                lender_obj_by[lk] = lrow
                lender_new.add(lk)
                n_part_lender += 1
            else:
                _merge_lender(lk, p_status, p_since, None, p_amount, p_note,
                              "Partnership Tracker", nm, partner)
        else:
            # A partnership row with NO partner named yet (the desk logged the company
            # before shortlisting a lender), or a PRISM-export carrier row. Field tags
            # go back into their tracker fields; the row's dates/remarks/extras land on
            # the tracker's remarks so nothing is dropped — and re-importing the same
            # file appends nothing twice.
            tag_fields, rest_note = _parse_field_tags(p_note)
            for f, v in tag_fields.items():
                setattr(tr, f, v)
            row_note = _join_notes(
                f"[Stage updated: {_date(r.get('Stage Updated')).isoformat()}]"
                if _date(r.get("Stage Updated")) else None,
                f"[Sanctioned amount: {_float(r.get('Sanctioned Amount (₹ Cr)'))} Cr]"
                if _float(r.get("Sanctioned Amount (₹ Cr)")) is not None else None,
                rest_note)
            if row_note and (not tr.remarks or row_note not in tr.remarks):
                tr.remarks = _join_notes(tr.remarks, row_note)
    counts["partnership_tracker"] = n_part
    counts["partnership_lenders"] = n_part_lender

    # --- asset monetisation (one row PER MANDATE) -----------------------
    # A company may be selling SEVERAL assets at once (the MIS lists e.g. a 58MW
    # Solar+BESS sale, a 100MW land advisory and a dropped 60MW project for ONE company).
    # Every sheet row is its own record; a merge re-import matches a company's rows in
    # SHEET ORDER, creating any extras — distinct mandates are never blended into one row.
    existing_am = (
        await session.execute(
            select(AssetMonetisation).where(AssetMonetisation.tenant_id == tenant_id,
                                            AssetMonetisation.deleted_at.is_(None))
            .order_by(AssetMonetisation.created_at, AssetMonetisation.tracker_no,
                      AssetMonetisation.id))
    ).scalars().all()
    am_by_entity: dict[uuid.UUID, list[AssetMonetisation]] = {}
    for a in existing_am:
        if a.entity_id is not None:
            am_by_entity.setdefault(a.entity_id, []).append(a)
    _, next_am_no = await _tracker_no_pool(AssetMonetisation, "A")
    am_row_ix: dict[uuid.UUID, int] = {}
    n_new = n_upd = 0
    for r in am:
        nm = company_of(r)
        entity = eid(nm)
        if entity is None:
            continue
        _AM_USED = {"Client ID", "Company Name", "RM", "State",
                    "Indicative Value (₹ Cr)", "Size (MW)", "Nature", "Deal Type",
                    "Investor", "Investor Type", "Status", "Date Teaser Shared",
                    "Notes", "Analyst", "Updated Remarks 19 July 2026"}
        notes = _join_notes(_s(r.get("Notes")),
                            _s(r.get("Updated Remarks 19 July 2026")),
                            _extras_note(r, _AM_USED))
        fields = {
            "deal_id": deal_by_entity.get(entity), "state": _s(r.get("State")),
            "indicative_value_cr": _float(r.get("Indicative Value (₹ Cr)")),
            "size_mw": _float(r.get("Size (MW)")), "nature": _s(r.get("Nature")),
            "deal_type": _s(r.get("Deal Type")), "investor": _s(r.get("Investor")),
            "investor_type": _s(r.get("Investor Type")),
            "rm": _s(r.get("RM")), "analyst": _s(r.get("Analyst")),
            "status": _c("AssetMonetisation", "Asset Mon", nm, _s(r.get("Status"))),
            "teaser_date": _date(r.get("Date Teaser Shared")), "notes": notes or None,
        }
        verdict, missing = _screen("AssetMonetisation", fields["status"], "Asset Mon", nm, fields)
        if verdict == "skip":
            continue
        ax = am_row_ix.get(entity, 0)
        am_row_ix[entity] = ax + 1
        alst = am_by_entity.setdefault(entity, [])
        existing = alst[ax] if ax < len(alst) else None
        if existing is None:
            a = AssetMonetisation(tenant_id=tenant_id, tracker_no=next_am_no(),
                                  entity_id=entity, created_by="xlsx-import",
                                  updated_by="xlsx-import", **fields)
            _note_stage_change(a, "status_history", "status", None, fields["status"], "Asset Mon")
            session.add(a)
            alst.append(a)
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
        # The ledger's per-mandate product flags survive on the mandate status itself.
        flags = ", ".join(f"{f}: {_s(r.get(f))}" for f in ("Syndication", "Partnership")
                          if _s(r.get(f)))
        if flags:
            mand = _join_notes(mand, f"[{flags}]") or mand
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
