"""Request/response schemas for every Register resource.

Design choice: categorical fields are typed as constrained strings rather than hard
enums, because the Register is a *source of truth ingesting messy upstream data* — it
must store faithfully what ATLAS/VOX send. The canonical vocabularies still live in
``app.core.enums`` and are served from ``/v1/ref`` so front-ends render correct
dropdowns. Structural typing (numbers, dates, booleans, UUIDs, lengths) is strict.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.base import CreateModel, ReadModel, UpdateModel


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #
class PersonCreate(CreateModel):
    name: str = Field(max_length=120)
    full_name: str = Field(max_length=200)
    role: str = Field(max_length=30)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    geography: str | None = Field(default=None, max_length=120)
    sectors: str | None = Field(default=None, max_length=300)
    started_on: date | None = None
    reports_to: str | None = Field(default=None, max_length=200)
    inactive: bool = False
    notes: str | None = None


class PersonUpdate(UpdateModel):
    name: str | None = Field(default=None, max_length=120)
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    geography: str | None = Field(default=None, max_length=120)
    sectors: str | None = Field(default=None, max_length=300)
    started_on: date | None = None
    reports_to: str | None = Field(default=None, max_length=200)
    inactive: bool | None = None
    notes: str | None = None


class PersonRead(ReadModel):
    name: str
    full_name: str
    role: str
    email: str | None
    phone: str | None
    geography: str | None
    sectors: str | None
    started_on: date | None
    reports_to: str | None
    inactive: bool
    notes: str | None


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
class EntityCreate(CreateModel):
    code: str = Field(max_length=60)
    legal_name: str = Field(max_length=300)
    display_name: str | None = Field(default=None, max_length=300)
    entity_type: str = Field(default="Company", max_length=40)
    cin: str | None = Field(default=None, max_length=21)
    pan: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=15)
    sector: str | None = Field(default=None, max_length=60)
    sub_sector: str | None = Field(default=None, max_length=120)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)
    location: str | None = Field(default=None, max_length=200)
    register_status: str | None = Field(default=None, max_length=40)
    lifecycle: str | None = Field(default=None, max_length=40)
    promoter_group_code: str | None = Field(default=None, max_length=60)
    about: str | None = None
    toi: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class EntityUpdate(UpdateModel):
    code: str | None = Field(default=None, max_length=60)
    legal_name: str | None = Field(default=None, max_length=300)
    display_name: str | None = Field(default=None, max_length=300)
    entity_type: str | None = Field(default=None, max_length=40)
    cin: str | None = Field(default=None, max_length=21)
    pan: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=15)
    sector: str | None = Field(default=None, max_length=60)
    sub_sector: str | None = Field(default=None, max_length=120)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)
    location: str | None = Field(default=None, max_length=200)
    register_status: str | None = Field(default=None, max_length=40)
    lifecycle: str | None = Field(default=None, max_length=40)
    promoter_group_code: str | None = Field(default=None, max_length=60)
    about: str | None = None
    toi: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class EntityRead(ReadModel):
    code: str
    legal_name: str
    display_name: str | None
    entity_type: str
    cin: str | None
    pan: str | None
    gstin: str | None
    sector: str | None
    sub_sector: str | None
    lens: str | None
    state: str | None
    location: str | None
    register_status: str | None
    lifecycle: str | None
    promoter_group_code: str | None
    about: str | None
    toi: str | None
    notes: str | None
    tags: list[str] | None


# --------------------------------------------------------------------------- #
# Counterparties
# --------------------------------------------------------------------------- #
class CounterpartyCreate(CreateModel):
    name: str = Field(max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    counterparty_type: str | None = Field(default=None, max_length=40)
    is_active: bool = True
    sectors: str | None = Field(default=None, max_length=400)
    ticket_min_cr: float | None = None
    ticket_max_cr: float | None = None
    notes: str | None = None


class CounterpartyUpdate(UpdateModel):
    name: str | None = Field(default=None, max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    counterparty_type: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None
    sectors: str | None = Field(default=None, max_length=400)
    ticket_min_cr: float | None = None
    ticket_max_cr: float | None = None
    notes: str | None = None


class CounterpartyRead(ReadModel):
    name: str
    short_name: str | None
    counterparty_type: str | None
    is_active: bool
    sectors: str | None
    ticket_min_cr: float | None
    ticket_max_cr: float | None
    notes: str | None


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
class LeadCreate(CreateModel):
    lead_no: str | None = Field(
        default=None, max_length=40,
        description="The lead's business number, unique per tenant. Omit it and the "
                    "register assigns the next free L-0001, L-0002, … automatically.")
    entity_id: uuid.UUID | None = None
    company: str = Field(max_length=300)
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    source: str | None = Field(default=None, max_length=40)
    source_name: str | None = Field(default=None, max_length=200)
    rm: str | None = Field(default=None, max_length=120)
    status: str = Field(default="Active", max_length=20)
    temperature: str | None = Field(default=None, max_length=10)
    contact: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    last_interaction_date: date | None = None
    next_action: str | None = None
    next_action_date: date | None = None
    converted_deal_id: uuid.UUID | None = None
    conv: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class LeadUpdate(UpdateModel):
    lead_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None
    company: str | None = Field(default=None, max_length=300)
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    source: str | None = Field(default=None, max_length=40)
    source_name: str | None = Field(default=None, max_length=200)
    rm: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=20)
    temperature: str | None = Field(default=None, max_length=10)
    contact: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    last_interaction_date: date | None = None
    next_action: str | None = None
    next_action_date: date | None = None
    converted_deal_id: uuid.UUID | None = None
    conv: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class LeadRead(ReadModel):
    lead_no: str | None
    entity_id: uuid.UUID | None
    company: str
    sector: str | None
    lens: str | None
    source: str | None
    source_name: str | None
    rm: str | None
    status: str
    temperature: str | None
    contact: str | None
    designation: str | None
    phone: str | None
    last_interaction_date: date | None
    next_action: str | None
    next_action_date: date | None
    converted_deal_id: uuid.UUID | None
    conv: str | None
    notes: str | None


# --------------------------------------------------------------------------- #
# Deals
# --------------------------------------------------------------------------- #
# THE Deal stage — the ORIGINATION-FUNNEL vocabulary (rbac.DEAL_FUNNEL_STAGES) as a typed
# alias, so an unknown value 422s at the schema, mirroring the lifecycle screens. A deal
# carries NO credit lifecycle: credit execution (and its evidence/maker-checker governance)
# lives on the lending tracker line. The deprecated deal-level credit stage is parked in
# the DB as credit_stage_legacy and deliberately absent from these API models.
DealFunnelStage = Literal["New Inquiry", "In Screening", "In Pipeline", "On Hold",
                          "Screened Out", "Closed Won", "Closed Lost"]


class DealCreate(CreateModel):
    deal_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID
    code: str | None = Field(default=None, max_length=60)
    product_type: str | None = Field(default=None, max_length=60)
    is_lending: bool = False
    is_syndication: bool = False
    is_asset_mon: bool = False
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    stage: DealFunnelStage | None = None
    temperature: str | None = Field(default=None, max_length=10)
    source: str | None = Field(default=None, max_length=40)
    source_detail: str | None = Field(default=None, max_length=200)
    source_name: str | None = Field(default=None, max_length=200)
    date_received: date | None = None
    ic_date: date | None = None
    sanction_date: date | None = None
    disbursement_date: date | None = None
    exit_date: date | None = None
    remarks: str | None = None


class DealUpdate(UpdateModel):
    deal_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None
    code: str | None = Field(default=None, max_length=60)
    product_type: str | None = Field(default=None, max_length=60)
    is_lending: bool | None = None
    is_syndication: bool | None = None
    is_asset_mon: bool | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    stage: DealFunnelStage | None = None
    temperature: str | None = Field(default=None, max_length=10)
    source: str | None = Field(default=None, max_length=40)
    source_detail: str | None = Field(default=None, max_length=200)
    source_name: str | None = Field(default=None, max_length=200)
    date_received: date | None = None
    ic_date: date | None = None
    sanction_date: date | None = None
    disbursement_date: date | None = None
    exit_date: date | None = None
    remarks: str | None = None


class DealRead(ReadModel):
    deal_no: str | None
    entity_id: uuid.UUID
    code: str | None
    product_type: str | None
    is_lending: bool
    is_syndication: bool
    is_asset_mon: bool
    rm: str | None
    analyst: str | None
    stage: str | None
    stage_history: list[dict[str, Any]] | None = None
    reconciliation_status: str | None = None
    temperature: str | None
    source: str | None
    source_detail: str | None
    source_name: str | None
    date_received: date | None
    ic_date: date | None
    sanction_date: date | None
    disbursement_date: date | None
    exit_date: date | None
    remarks: str | None


# --------------------------------------------------------------------------- #
# Lending tracker
# --------------------------------------------------------------------------- #
class LendingCreate(CreateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    amount_cr: float | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    stage: str | None = Field(default=None, max_length=40)
    stage_updated_at: date | None = None
    sanction_date: date | None = None
    proposed_disbursement_amount: float | None = None
    proposed_disbursement_date: date | None = None
    disbursed_amount: float | None = None
    disbursement_date: date | None = None
    pending_with: str | None = Field(default=None, max_length=20)
    remarks: str | None = None
    stage_history: list[dict[str, Any]] | None = None


class LendingUpdate(UpdateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    amount_cr: float | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    stage: str | None = Field(default=None, max_length=40)
    stage_updated_at: date | None = None
    sanction_date: date | None = None
    proposed_disbursement_amount: float | None = None
    proposed_disbursement_date: date | None = None
    disbursed_amount: float | None = None
    disbursement_date: date | None = None
    pending_with: str | None = Field(default=None, max_length=20)
    remarks: str | None = None
    stage_history: list[dict[str, Any]] | None = None


class LendingRead(ReadModel):
    tracker_no: str | None
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    amount_cr: float | None
    rm: str | None
    analyst: str | None
    stage: str | None
    stage_updated_at: date | None
    sanction_date: date | None
    proposed_disbursement_amount: float | None = None
    proposed_disbursement_date: date | None = None
    disbursed_amount: float | None
    disbursement_date: date | None
    pending_with: str | None
    remarks: str | None
    stage_history: list[dict[str, Any]] | None
    reconciliation_status: str | None = None


# --------------------------------------------------------------------------- #
# Syndication tracker
# --------------------------------------------------------------------------- #
class SyndicationCreate(CreateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    toi: str | None = Field(default=None, max_length=200)
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    lc: str | None = Field(default=None, max_length=120)
    priority: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, max_length=40)
    amount_cr: float | None = None
    line: str | None = Field(default=None, max_length=120)
    facility: str | None = None
    tenor: str | None = Field(default=None, max_length=20)
    mandate_status: str | None = None
    potential: str | None = None
    im_status: str | None = Field(default=None, max_length=40)
    sanctioned_lender: str | None = None
    ip_lender: str | None = None
    date_of_sanction: date | None = None
    month_of_sanction: str | None = Field(default=None, max_length=12)
    nature: str | None = None
    existing: str | None = None
    price: str | None = None
    syndication_type: str | None = Field(default=None, max_length=80)
    mandate_status3: str | None = Field(default=None, max_length=40)
    pending_with: str | None = Field(default=None, max_length=20)
    remarks: str | None = None
    status_history: list[dict[str, Any]] | None = None


class SyndicationUpdate(UpdateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    toi: str | None = Field(default=None, max_length=200)
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    lc: str | None = Field(default=None, max_length=120)
    priority: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, max_length=40)
    amount_cr: float | None = None
    line: str | None = Field(default=None, max_length=120)
    facility: str | None = None
    tenor: str | None = Field(default=None, max_length=20)
    mandate_status: str | None = None
    potential: str | None = None
    im_status: str | None = Field(default=None, max_length=40)
    sanctioned_lender: str | None = None
    ip_lender: str | None = None
    date_of_sanction: date | None = None
    month_of_sanction: str | None = Field(default=None, max_length=12)
    nature: str | None = None
    existing: str | None = None
    price: str | None = None
    syndication_type: str | None = Field(default=None, max_length=80)
    mandate_status3: str | None = Field(default=None, max_length=40)
    pending_with: str | None = Field(default=None, max_length=20)
    remarks: str | None = None
    status_history: list[dict[str, Any]] | None = None


class SyndicationRead(ReadModel):
    tracker_no: str | None
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    toi: str | None
    rm: str | None
    analyst: str | None
    lc: str | None
    priority: str | None
    status: str | None
    amount_cr: float | None
    line: str | None
    facility: str | None
    tenor: str | None
    mandate_status: str | None
    potential: str | None
    im_status: str | None
    sanctioned_lender: str | None
    ip_lender: str | None
    date_of_sanction: date | None
    month_of_sanction: str | None
    nature: str | None
    existing: str | None
    price: str | None
    syndication_type: str | None
    mandate_status3: str | None
    pending_with: str | None
    remarks: str | None
    status_history: list[dict[str, Any]] | None
    reconciliation_status: str | None = None
    # Embedded nested lenders (populated from the ORM relationship) so an ATLAS
    # syndication row carries its lenders inline — no second call required.
    lenders: list[SyndicationLenderRead] | None = None


# --------------------------------------------------------------------------- #
# Syndication lenders (nested under a syndication)
# --------------------------------------------------------------------------- #
class SyndicationLenderCreate(CreateModel):
    # Optional because the nested endpoint /v1/syndication/{id}/lenders supplies it from
    # the path; on the flat POST /v1/syndication-lenders, pass it in the body.
    syndication_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    lender_name: str = Field(max_length=200)
    is_existing: bool = False
    status: str | None = Field(default=None, max_length=40)
    since: date | None = None
    response_date: date | None = None
    chased_date: date | None = None
    note: str | None = None
    status_history: list[dict[str, Any]] | None = None


class SyndicationLenderUpdate(UpdateModel):
    counterparty_id: uuid.UUID | None = None
    lender_name: str | None = Field(default=None, max_length=200)
    is_existing: bool | None = None
    status: str | None = Field(default=None, max_length=40)
    since: date | None = None
    response_date: date | None = None
    chased_date: date | None = None
    note: str | None = None
    status_history: list[dict[str, Any]] | None = None


class SyndicationLenderRead(ReadModel):
    syndication_id: uuid.UUID
    counterparty_id: uuid.UUID | None
    lender_name: str
    is_existing: bool
    status: str | None
    since: date | None
    response_date: date | None
    chased_date: date | None
    note: str | None
    status_history: list[dict[str, Any]] | None


# --------------------------------------------------------------------------- #
# Asset monetisation
# --------------------------------------------------------------------------- #
class AssetMonCreate(CreateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    state: str | None = Field(default=None, max_length=60)
    indicative_value_cr: float | None = None
    size_mw: float | None = None
    nature: str | None = Field(default=None, max_length=40)
    deal_type: str | None = Field(default=None, max_length=80)
    investor: str | None = None
    investor_type: str | None = Field(default=None, max_length=60)
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    teaser_date: date | None = None
    notes: str | None = None


class AssetMonUpdate(UpdateModel):
    tracker_no: str | None = Field(default=None, max_length=40)
    entity_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    state: str | None = Field(default=None, max_length=60)
    indicative_value_cr: float | None = None
    size_mw: float | None = None
    nature: str | None = Field(default=None, max_length=40)
    deal_type: str | None = Field(default=None, max_length=80)
    investor: str | None = None
    investor_type: str | None = Field(default=None, max_length=60)
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    teaser_date: date | None = None
    notes: str | None = None


class AssetMonRead(ReadModel):
    tracker_no: str | None
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    state: str | None
    indicative_value_cr: float | None
    size_mw: float | None
    nature: str | None
    deal_type: str | None
    investor: str | None
    investor_type: str | None
    rm: str | None = None
    analyst: str | None = None
    status: str | None
    status_history: list[dict[str, Any]] | None = None
    reconciliation_status: str | None = None
    teaser_date: date | None
    notes: str | None


# --------------------------------------------------------------------------- #
# Financials (versioned)
# --------------------------------------------------------------------------- #
class FinancialLineItem(BaseModel):
    """One row of a financial statement, so a UI can render P&L/BS/CF/Ratios grids
    against a stable contract instead of guessing at an untyped blob."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=60)                       # machine key, e.g. "revenue"
    label: str = Field(max_length=160)                    # display label, e.g. "Total Revenue"
    value: float | None = None
    section: str | None = Field(default=None, max_length=40)  # ref: Financial Section
    order: int | None = None                              # display order within its section
    unit: str | None = Field(default=None, max_length=20)


class FinancialData(BaseModel):
    """Typed contract for the financials ``data`` payload. Lenient at the top level
    (``extra='allow'``) so legacy/richer blobs still read, but ``line_items`` are
    validated so a statements grid has something reliable to bind to."""

    model_config = ConfigDict(extra="allow")
    line_items: list[FinancialLineItem] = Field(default_factory=list)
    notes: str | None = None


class FinancialCreate(CreateModel):
    entity_id: uuid.UUID
    statement_type: str = Field(max_length=40)
    period_type: str | None = Field(default=None, max_length=20)
    period_start: date | None = None
    period_end: date
    fiscal_year: str | None = Field(default=None, max_length=12)
    as_of_date: date | None = None
    provenance: str | None = None
    source_document_ref: str | None = Field(default=None, max_length=300)
    currency: str = Field(default="INR", max_length=3)
    is_consolidated: bool | None = None
    is_audited: bool | None = None
    scale: str | None = Field(default=None, max_length=20)
    revenue: float | None = None
    ebitda: float | None = None
    pat: float | None = None
    total_debt: float | None = None
    net_worth: float | None = None
    dscr: float | None = None
    data: FinancialData | None = None


class FinancialUpdate(UpdateModel):
    period_type: str | None = Field(default=None, max_length=20)
    period_start: date | None = None
    fiscal_year: str | None = Field(default=None, max_length=12)
    as_of_date: date | None = None
    provenance: str | None = None
    source_document_ref: str | None = Field(default=None, max_length=300)
    currency: str | None = Field(default=None, max_length=3)
    is_consolidated: bool | None = None
    is_audited: bool | None = None
    scale: str | None = Field(default=None, max_length=20)
    revenue: float | None = None
    ebitda: float | None = None
    pat: float | None = None
    total_debt: float | None = None
    net_worth: float | None = None
    dscr: float | None = None
    data: FinancialData | None = None


class FinancialRead(ReadModel):
    entity_id: uuid.UUID
    statement_type: str
    period_type: str | None
    period_start: date | None
    period_end: date
    fiscal_year: str | None
    as_of_date: date | None
    version_no: int
    is_current: bool
    provenance: str | None
    source_document_ref: str | None
    currency: str
    is_consolidated: bool | None
    is_audited: bool | None
    scale: str | None
    revenue: float | None
    ebitda: float | None
    pat: float | None
    total_debt: float | None
    net_worth: float | None
    dscr: float | None
    data: FinancialData | None


# --------------------------------------------------------------------------- #
# Contracts & assets
# --------------------------------------------------------------------------- #
class ContractAssetCreate(CreateModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    asset_type: str = Field(max_length=40)
    title: str | None = Field(default=None, max_length=300)
    counterparty_id: uuid.UUID | None = None
    counterparty_name: str | None = Field(default=None, max_length=200)
    capacity_mw: float | None = None
    tariff: float | None = None
    location: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=60)
    start_date: date | None = None
    end_date: date | None = None
    tenor_years: float | None = None
    contract_value_cr: float | None = None
    status: str | None = Field(default=None, max_length=60)
    details: dict[str, Any] | None = None


class ContractAssetUpdate(UpdateModel):
    deal_id: uuid.UUID | None = None
    asset_type: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=300)
    counterparty_id: uuid.UUID | None = None
    counterparty_name: str | None = Field(default=None, max_length=200)
    capacity_mw: float | None = None
    tariff: float | None = None
    location: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=60)
    start_date: date | None = None
    end_date: date | None = None
    tenor_years: float | None = None
    contract_value_cr: float | None = None
    status: str | None = Field(default=None, max_length=60)
    details: dict[str, Any] | None = None


class ContractAssetRead(ReadModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    asset_type: str
    title: str | None
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None
    capacity_mw: float | None
    tariff: float | None
    location: str | None
    state: str | None
    start_date: date | None
    end_date: date | None
    tenor_years: float | None
    contract_value_cr: float | None
    status: str | None
    details: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# External intelligence
# --------------------------------------------------------------------------- #
class ExternalIntelCreate(CreateModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    intel_type: str = Field(max_length=40)
    source: str | None = Field(default=None, max_length=120)
    signal: str | None = Field(default=None, max_length=10)
    title: str | None = Field(default=None, max_length=400)
    summary: str | None = None
    url: str | None = None
    observed_at: datetime | None = None
    pulled_at: datetime | None = None
    payload: dict[str, Any] | None = None


class ExternalIntelUpdate(UpdateModel):
    deal_id: uuid.UUID | None = None
    intel_type: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=120)
    signal: str | None = Field(default=None, max_length=10)
    title: str | None = Field(default=None, max_length=400)
    summary: str | None = None
    url: str | None = None
    observed_at: datetime | None = None
    pulled_at: datetime | None = None
    payload: dict[str, Any] | None = None
    acknowledged_by: str | None = Field(default=None, max_length=120)
    acknowledged_at: datetime | None = None
    is_dismissed: bool | None = None


class ExternalIntelRead(ReadModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    intel_type: str
    source: str | None
    signal: str | None
    title: str | None
    summary: str | None
    url: str | None
    observed_at: datetime | None
    pulled_at: datetime | None
    payload: dict[str, Any] | None
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    is_dismissed: bool


# --------------------------------------------------------------------------- #
# Interactions (user-interaction timeline)
# --------------------------------------------------------------------------- #
class InteractionCreate(CreateModel):
    # subject_type/subject_id are optional here because the nested endpoints
    # (/v1/<subject>/{id}/interactions) inject them from the path; the generic
    # POST /v1/interactions requires both (validated server-side).
    subject_type: str | None = Field(default=None, max_length=30)
    subject_id: uuid.UUID | None = None
    interaction_type: str = Field(max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: datetime | None = None
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    outcome: str | None = None
    performed_by: str | None = Field(default=None, max_length=120)
    contact_name: str | None = Field(default=None, max_length=200)
    # Syndication lender dimension (either the id, or the name to resolve).
    syndication_lender_id: uuid.UUID | None = None
    lender_name: str | None = Field(default=None, max_length=200)
    next_action: str | None = None
    next_action_date: date | None = None
    next_meeting_date: date | None = None
    # VOX / rich-touchpoint fields.
    transcript: str | None = None
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    source_ref: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=20)
    attachments: list[dict[str, Any]] | None = None
    meta: dict[str, Any] | None = None


class InteractionUpdate(UpdateModel):
    # Interactions are append-only in the API; this schema exists only for internal
    # completeness (the interactions resource does not expose PATCH).
    interaction_type: str | None = Field(default=None, max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: datetime | None = None
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    outcome: str | None = None
    performed_by: str | None = Field(default=None, max_length=120)
    contact_name: str | None = Field(default=None, max_length=200)
    next_action: str | None = None
    next_action_date: date | None = None
    next_meeting_date: date | None = None
    transcript: str | None = None
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    source_ref: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=20)
    attachments: list[dict[str, Any]] | None = None
    meta: dict[str, Any] | None = None


class InteractionRead(ReadModel):
    subject_type: str
    subject_id: uuid.UUID
    entity_id: uuid.UUID | None
    deal_id: uuid.UUID | None
    syndication_lender_id: uuid.UUID | None
    lender_name: str | None
    interaction_type: str
    direction: str | None
    occurred_at: datetime
    summary: str | None
    notes: str | None
    outcome: str | None
    performed_by: str | None
    contact_name: str | None
    next_action: str | None
    next_action_date: date | None
    next_meeting_date: date | None
    transcript: str | None
    language: str | None
    gps_lat: float | None
    gps_lng: float | None
    location: str | None
    attendees: list[Any] | None
    key_intel: dict[str, Any] | None
    next_steps: list[Any] | None
    source_ref: str | None
    source: str | None
    attachments: list[dict[str, Any]] | None
    meta: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# Monitoring & reporting
# --------------------------------------------------------------------------- #
class MonitoringCreate(CreateModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    record_type: str = Field(max_length=40)
    covenant_name: str | None = Field(default=None, max_length=200)
    due_date: date | None = None
    submitted_date: date | None = None
    on_time: bool | None = None
    delay_days: int | None = None
    security_created_within_days: int | None = None
    extension_count: int | None = None
    behavioural_score: float | None = None
    period: str | None = Field(default=None, max_length=20)
    status: str | None = Field(default=None, max_length=60)
    feeds_irg: bool = True
    target_value: float | None = None
    actual_value: float | None = None
    breached: bool | None = None
    waiver_status: str | None = Field(default=None, max_length=60)
    details: dict[str, Any] | None = None


class MonitoringUpdate(UpdateModel):
    deal_id: uuid.UUID | None = None
    record_type: str | None = Field(default=None, max_length=40)
    covenant_name: str | None = Field(default=None, max_length=200)
    due_date: date | None = None
    submitted_date: date | None = None
    on_time: bool | None = None
    delay_days: int | None = None
    security_created_within_days: int | None = None
    extension_count: int | None = None
    behavioural_score: float | None = None
    period: str | None = Field(default=None, max_length=20)
    status: str | None = Field(default=None, max_length=60)
    feeds_irg: bool | None = None
    target_value: float | None = None
    actual_value: float | None = None
    breached: bool | None = None
    waiver_status: str | None = Field(default=None, max_length=60)
    details: dict[str, Any] | None = None


class MonitoringRead(ReadModel):
    entity_id: uuid.UUID
    deal_id: uuid.UUID | None
    record_type: str
    covenant_name: str | None
    due_date: date | None
    submitted_date: date | None
    on_time: bool | None
    delay_days: int | None
    security_created_within_days: int | None
    extension_count: int | None
    behavioural_score: float | None
    period: str | None
    status: str | None
    feeds_irg: bool
    target_value: float | None
    actual_value: float | None
    breached: bool | None
    waiver_status: str | None
    details: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# Documents (the catalog behind ATLAS's "Data Register")
# --------------------------------------------------------------------------- #
# Lifecycle statuses (Verified / Rejected / Superseded / Expired) are OUTCOMES, minted
# only by the dedicated /validate /reject /replace endpoints and the internal expiry
# sweep — never set directly through the generic create/update payloads.
_DIRECT_DOCUMENT_STATUSES = {"On File", "Pending", "Waived"}


def _direct_document_status(v: str) -> str:
    if v not in _DIRECT_DOCUMENT_STATUSES:
        raise ValueError(
            f"status {v!r} cannot be set directly; lifecycle statuses are reached via "
            "the validate/reject/replace endpoints (or the expiry sweep). "
            f"Directly settable: {sorted(_DIRECT_DOCUMENT_STATUSES)}."
        )
    return v


class DocumentCreate(CreateModel):
    # subject_type/subject_id are optional here because the nested endpoints
    # (/v1/<subject>/{id}/documents) inject them from the path; the generic
    # POST /v1/documents requires both (validated server-side).
    subject_type: str | None = Field(default=None, max_length=30)
    subject_id: uuid.UUID | None = None
    section: str | None = Field(default=None, max_length=80)
    slot_key: str | None = Field(default=None, max_length=60)
    doc_type: str | None = Field(default=None, max_length=120)
    title: str = Field(max_length=300)
    is_required: bool = False
    status: str = Field(default="On File", max_length=40)
    # Where the bytes live. Supply *one* of:
    #   storage_uri  — already uploaded to object storage (s3://… / https://…), OR
    #   content_base64 — small file kept inline (bounded by documents_inline_max_bytes), OR
    #   neither      — a metadata-only record (name/size recorded, no bytes stored).
    storage_uri: str | None = None
    storage_backend: str | None = Field(default=None, max_length=20)
    content_type: str | None = Field(default=None, max_length=120)
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = Field(default=None, max_length=64)
    original_filename: str | None = Field(default=None, max_length=300)
    content_base64: str | None = None  # write-only; decoded + stored inline, never returned
    uploaded_by: str | None = Field(default=None, max_length=120)
    uploaded_at: datetime | None = None
    notes: str | None = None
    meta: dict[str, Any] | None = None
    # Validity window (e.g. an insurance policy or sanction letter): the expiry sweep
    # marks the document 'Expired' once this date lapses.
    expires_on: date | None = None

    @field_validator("status")
    @classmethod
    def _status_settable(cls, v: str) -> str:
        return _direct_document_status(v)


class DocumentUpdate(UpdateModel):
    section: str | None = Field(default=None, max_length=80)
    slot_key: str | None = Field(default=None, max_length=60)
    doc_type: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=300)
    is_required: bool | None = None
    status: str | None = Field(default=None, max_length=40)
    storage_uri: str | None = None
    storage_backend: str | None = Field(default=None, max_length=20)
    content_type: str | None = Field(default=None, max_length=120)
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = Field(default=None, max_length=64)
    original_filename: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    meta: dict[str, Any] | None = None
    expires_on: date | None = None

    @field_validator("status")
    @classmethod
    def _status_settable(cls, v: str | None) -> str | None:
        return None if v is None else _direct_document_status(v)


class DocumentRead(ReadModel):
    subject_type: str
    subject_id: uuid.UUID
    entity_id: uuid.UUID | None
    deal_id: uuid.UUID | None
    section: str | None
    slot_key: str | None
    doc_type: str | None
    title: str
    is_required: bool
    status: str
    storage_backend: str | None
    storage_uri: str | None
    content_type: str | None
    size_bytes: int | None
    checksum: str | None
    original_filename: str | None
    uploaded_by: str | None
    uploaded_at: datetime | None
    notes: str | None
    meta: dict[str, Any] | None
    expires_on: date | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    status_note: str | None = None
    superseded_by: uuid.UUID | None = None
    # inline_content (the raw bytes) is intentionally NOT exposed here — fetch it from
    # GET /v1/documents/{id}/content.


# --------------------------------------------------------------------------- #
# Document checklist template (the Data Register's configurable slot list)
# --------------------------------------------------------------------------- #
class DocumentChecklistCreate(CreateModel):
    applies_to: str = Field(default="*", max_length=30)
    section: str = Field(max_length=80)
    section_order: int = 0
    slot_key: str = Field(max_length=60)
    label: str = Field(max_length=200)
    is_required: bool = False
    sort_order: int = 0
    is_active: bool = True
    hint: str | None = None


class DocumentChecklistUpdate(UpdateModel):
    applies_to: str | None = Field(default=None, max_length=30)
    section: str | None = Field(default=None, max_length=80)
    section_order: int | None = None
    slot_key: str | None = Field(default=None, max_length=60)
    label: str | None = Field(default=None, max_length=200)
    is_required: bool | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    hint: str | None = None


class DocumentChecklistRead(ReadModel):
    applies_to: str
    section: str
    section_order: int
    slot_key: str
    label: str
    is_required: bool
    sort_order: int
    is_active: bool
    hint: str | None


# --------------------------------------------------------------------------- #
# Tenant settings (per-tenant business config, e.g. alert thresholds)
# --------------------------------------------------------------------------- #
class SettingsUpdate(BaseModel):
    """Replace the tenant's settings blob. Merged over the built-in defaults on read."""

    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any]


class SettingsRead(BaseModel):
    settings: dict[str, Any]


# Resolve the forward reference in SyndicationRead.lenders now that
# SyndicationLenderRead is defined above.
SyndicationRead.model_rebuild()
