"""Controlled vocabularies for the Register.

Sourced from the ATLAS reference set (the ``ref`` object in the prototype) and the
PRISM architecture doc. These are stored as TEXT in PostgreSQL (not native ENUM types,
which are painful to evolve) and validated at the API boundary via these enums. The
same values are seeded into ``ref_values`` so front-ends can render dropdowns straight
from the Register.
"""

from __future__ import annotations

from enum import StrEnum


class Sector(StrEnum):
    SOLAR_EPC = "Solar - EPC"
    SOLAR_DEVELOPER = "Solar - Developer"
    SOLAR_ROOFTOP = "Solar - Rooftop"
    SOLAR_OEM = "Solar - OEM"
    SOLAR_GENERAL = "Solar - General"
    BESS = "BESS / Energy Storage"
    EV_MOBILITY = "EV Mobility"
    BIOFUELS = "Biofuels / Biogas / CBG"
    INDUSTRIAL_DECARB = "Industrial Decarbonisation"
    INDUSTRIAL_WATER = "Industrial Water"
    WATER_WASH = "Water Treatment / WASH"
    CLIMATE_DATA_IOT = "Climate Data & IoT"
    INDUSTRIAL_EFFICIENCY = "Industrial Efficiency"
    AGRI_DRONE = "Agri / Drone"
    OTHER = "Other"


class Lens(StrEnum):
    MITIGATION = "Mitigation"
    ADAPTATION = "Adaptation"


class Temperature(StrEnum):
    HOT = "Hot"
    WARM = "Warm"
    COLD = "Cold"


class Priority(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class LeadStatus(StrEnum):
    ACTIVE = "Active"
    CONVERTED = "Converted"
    DROPPED = "Dropped"


class SourceType(StrEnum):
    RM = "RM"
    DSA = "DSA"
    OEM = "OEM"
    INBOUND = "Inbound"
    REFERRAL = "Referral"
    EVENT = "Event"
    WEBSITE = "Website"
    DIRECT = "Direct"
    OTHER = "Other"


class ProductType(StrEnum):
    """Product-aware: every deal carries one of these (PRISM 'Property 2')."""

    TERM_LOAN = "Term Loan"
    WORKING_CAPITAL = "Working Capital"
    PO_FINANCE = "Purchase Order Finance"
    MEZZANINE = "Mezzanine"
    CO_LENDING = "Co-lending"
    SYNDICATION = "Syndication"
    ASSET_MON_ADVISORY = "Asset Monetisation Advisory"
    OEM_BUNDLE = "OEM Bundle Equipment Finance"


class RegisterStatus(StrEnum):
    """Entity-level lifecycle used by the backfill (PRISM data-backfill)."""

    PIPELINE = "Pipeline"
    SANCTIONED = "Sanctioned"
    REJECTED = "Rejected"
    MARKET_INTELLIGENCE = "Market Intelligence"


class EntityType(StrEnum):
    COMPANY = "Company"
    PROMOTER = "Promoter"
    DIRECTOR = "Director"
    RELATED_PARTY = "Related Party"


class PersonRole(StrEnum):
    ADMIN = "Admin"
    MANAGEMENT = "Management"
    RM = "RM"
    ANALYST = "Analyst"
    OPS = "Ops"


class LendingStage(StrEnum):
    DATA_AWAITED = "Data Awaited"
    DILIGENCE = "Diligence"
    NOTE_CIRCULATED = "Note Circulated"
    SANCTIONED = "Sanctioned"
    CP_CS_COMPLETED = "CP/CS Completed"
    READY_FOR_DISBURSEMENT = "Ready for Disbursement"
    HANDED_OVER_TO_ADVAYA = "Handed Over to Advaya"
    DISBURSEMENT_PENDING = "Disbursement Pending"
    REJECTED = "Rejected"
    ON_HOLD = "On Hold"


class ProposalStatus(StrEnum):
    """Syndication per-deal / per-lender status (ATLAS 'Status of Proposal')."""

    DEAL_SOURCED = "Deal Sourced"
    DOCS_PENDING = "Docs Pending"
    IM_IN_PREP = "IM in Prep"
    IM_CIRCULATED = "IM Circulated"
    QUERIES_RECEIVED = "Queries Received"
    IP_RECEIVED = "IP Received"
    SANCTIONED = "Sanctioned"
    DISBURSED = "Disbursed"
    ON_HOLD = "On Hold"
    WITHDRAWN = "Withdrawn"
    REJECTED = "Rejected"
    DROPPED = "Dropped"


class AssetMonStatus(StrEnum):
    TEASER_PREPARED = "Teaser Prepared"
    TEASER_SHARED = "Teaser Shared"
    IN_DISCUSSION = "In Discussion"
    NBO_RECEIVED = "NBO Received"
    BO_RECEIVED = "BO Received"
    SPA_DOCUMENTATION = "SPA / Documentation"
    CLOSED = "Closed"
    DROPPED = "Dropped"


class CounterpartyType(StrEnum):
    BANK = "Bank"
    NBFC = "NBFC"
    DFI = "DFI"
    AIF_FUND = "AIF/Fund"
    INVESTOR = "Investor"
    STRATEGIC = "Strategic"
    ADVISOR = "Advisor"
    OTHER = "Other"


class PendingWith(StrEnum):
    CLIENT = "Client"
    LENDER = "Lender"
    RM = "RM"
    ANALYST = "Analyst"
    OPS = "Ops"


class InteractionType(StrEnum):
    IN_PERSON = "In-Person Meeting"
    VIRTUAL = "Virtual Meeting / Video Call"
    PHONE = "Phone Call"
    WHATSAPP = "WhatsApp / Text message"
    EMAIL = "Email / Written Correspondence"
    SITE_VISIT = "Site Visit / Due Diligence"
    MGMT_PRESENTATION = "Management Presentation"
    TERM_SHEET = "Term Sheet Negotiation"
    INTERNAL_REVIEW = "Internal Review / Credit Committee"


class InteractionDirection(StrEnum):
    INBOUND = "Inbound"
    OUTBOUND = "Outbound"
    INTERNAL = "Internal"


class InteractionSubject(StrEnum):
    """What an interaction is logged against (ATLAS 'refType')."""

    LEAD = "Lead"
    DEAL = "Deal"
    ENTITY = "Entity"
    COUNTERPARTY = "Counterparty"
    LENDING = "Lending"
    SYNDICATION = "Syndication"
    ASSET_MONETISATION = "AssetMonetisation"


class InteractionSource(StrEnum):
    MANUAL = "Manual"
    VOCX = "VocX"      # voice touchpoint capture service (formerly "VOX")
    VOX = "VOX"        # legacy value kept for rows written before the rename
    EMAIL = "Email"
    SYSTEM = "System"


class StatementType(StrEnum):
    AUDITED = "Audited"
    PROVISIONAL = "Provisional"
    PROJECTION = "Projection"
    BANK_STATEMENT = "Bank Statement Extract"
    GST_RETURN = "GST Return"
    AA_PULL = "Account Aggregator Pull"
    CMA_SPREAD = "CMA Spread"


class PeriodType(StrEnum):
    ANNUAL = "Annual"
    HALF_YEARLY = "Half Yearly"
    QUARTERLY = "Quarterly"
    MONTHLY = "Monthly"


class AssetType(StrEnum):
    PPA = "PPA"
    EPC = "EPC"
    OM = "O&M"
    OFFTAKE = "Offtake"
    PROJECT_SPV = "Project SPV"
    SECURITY_CHARGE = "Security Charge"
    OTHER = "Other"


class IntelType(StrEnum):
    CIBIL = "CIBIL"
    AA = "Account Aggregator"
    MCA = "MCA"
    PROBE42 = "Probe42"
    GST = "GST"
    PULSE = "PULSE"
    COURT_CASE = "Court Case"
    SECTOR_BENCHMARK = "Sector Benchmark"
    COMPARABLE = "Comparable Deal"
    NEWS = "News"


class Signal(StrEnum):
    RED = "RED"
    AMBER = "AMBER"
    GREEN = "GREEN"


class MonitoringRecordType(StrEnum):
    COVENANT_COMPLIANCE = "Covenant Compliance"
    SECURITY_CREATION = "Security Creation"
    PERIODIC_SUBMISSION = "Periodic Submission"
    BEHAVIOURAL_SCORE = "Behavioural Score"
    MIS_SNAPSHOT = "MIS Snapshot"
    REPORT_REGISTER = "Report Register"


class DocumentSection(StrEnum):
    """The ATLAS Data Register checklist sections."""

    KYC_CONSTITUTIONAL = "KYC & Constitutional"
    FINANCIALS = "Financials"
    BANKING_DEBT = "Banking & Debt"
    COMPLIANCE_BUREAU = "Compliance & Bureau"
    PROJECT_TECHNICAL = "Project & Technical"
    DEAL_DOCUMENTS = "Deal Documents"


class DocumentStatus(StrEnum):
    ON_FILE = "On File"
    PENDING = "Pending"
    WAIVED = "Waived"
    REJECTED = "Rejected"
    SUPERSEDED = "Superseded"
