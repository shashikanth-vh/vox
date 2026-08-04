"""Instantiates the CRUD repository + router for every Register table."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.crud_router import ResourceSpec, build_crud_router
from app.api.people_rules import person_pre_write
from app.models import (
    AssetMonetisation,
    ContractAsset,
    Counterparty,
    Deal,
    Document,
    DocumentChecklistItem,
    Entity,
    ExternalIntelligence,
    Financial,
    Interaction,
    Lead,
    LendingTracker,
    MonitoringReporting,
    Person,
    SyndicationLender,
    SyndicationTracker,
)
from app.repositories.crud import CRUDRepository
from app.schemas import resources as s

# (model, prefix, name, tags, searchable, filterable, create, update, read)
_SPECS: list[ResourceSpec] = [
    ResourceSpec(
        name="entity", prefix="/v1/entities", tags=["Entities"],
        repo=CRUDRepository(Entity, searchable=["legal_name", "code", "display_name", "cin"],
                            filterable=["sector", "lens", "register_status", "entity_type",
                                        "state", "promoter_group_code", "code"]),
        create_schema=s.EntityCreate, update_schema=s.EntityUpdate, read_schema=s.EntityRead,
        filterable=["sector", "lens", "register_status", "entity_type", "state",
                    "promoter_group_code", "code"],
        subject_type="Entity", view_name="clients",
    ),
    ResourceSpec(
        name="person", prefix="/v1/people", tags=["People"],
        repo=CRUDRepository(Person, searchable=["name", "full_name", "email"],
                            filterable=["role", "inactive"]),
        create_schema=s.PersonCreate, update_schema=s.PersonUpdate, read_schema=s.PersonRead,
        filterable=["role", "inactive"],
        # Directory data: readable by any employee (employees view = READ for all roles),
        # editable only by Admin/Management (edit_employee).
        view_name="employees", write_operation="edit_employee",
        # One person, one mailbox — the e-mail is what Access, VocX and every other
        # service resolve this person by (app.core.people).
        pre_write=person_pre_write,
    ),
    ResourceSpec(
        name="counterparty", prefix="/v1/counterparties", tags=["Counterparties"],
        repo=CRUDRepository(Counterparty, searchable=["name", "short_name"],
                            filterable=["counterparty_type", "is_active"]),
        create_schema=s.CounterpartyCreate, update_schema=s.CounterpartyUpdate,
        read_schema=s.CounterpartyRead, filterable=["counterparty_type", "is_active"],
        # Bank/counterparty directory: broad read (tools view), mutation restricted.
        view_name="tools", write_operation="manage_counterparty",
    ),
    ResourceSpec(
        name="lead", prefix="/v1/leads", tags=["Leads"],
        repo=CRUDRepository(Lead, searchable=["company", "contact", "rm", "notes"],
                            filterable=["status", "temperature", "sector", "rm", "source",
                                        "entity_id", "converted_deal_id"]),
        create_schema=s.LeadCreate, update_schema=s.LeadUpdate, read_schema=s.LeadRead,
        filterable=["status", "temperature", "sector", "rm", "source", "entity_id",
                    "converted_deal_id"],
        subject_type="Lead", view_name="leads",
        # Omitted lead_no → the next free L-0001, L-0002, … for the tenant.
    ),
    ResourceSpec(
        name="deal", prefix="/v1/deals", tags=["Deals"],
        repo=CRUDRepository(Deal, searchable=["code", "rm", "analyst", "remarks"],
                            filterable=["product_type", "stage", "temperature", "is_lending",
                                        "is_syndication", "is_asset_mon", "entity_id", "rm", "code"]),
        create_schema=s.DealCreate, update_schema=s.DealUpdate, read_schema=s.DealRead,
        filterable=["product_type", "stage", "temperature", "is_lending", "is_syndication",
                    "is_asset_mon", "entity_id", "rm", "code"],
        subject_type="Deal", view_name="deals",
    ),
    ResourceSpec(
        name="lending record", prefix="/v1/lending", tags=["Lending Tracker"],
        repo=CRUDRepository(LendingTracker, searchable=["tracker_no", "rm", "remarks"],
                            filterable=["stage", "pending_with", "entity_id", "deal_id", "rm"]),
        create_schema=s.LendingCreate, update_schema=s.LendingUpdate, read_schema=s.LendingRead,
        filterable=["stage", "pending_with", "entity_id", "deal_id", "rm"],
        subject_type="Lending", view_name="lending",
    ),
    ResourceSpec(
        name="syndication record", prefix="/v1/syndication", tags=["Syndication Tracker"],
        repo=CRUDRepository(SyndicationTracker, searchable=["tracker_no", "remarks", "toi"],
                            filterable=["status", "priority", "entity_id", "deal_id", "rm", "pending_with"]),
        create_schema=s.SyndicationCreate, update_schema=s.SyndicationUpdate,
        read_schema=s.SyndicationRead,
        filterable=["status", "priority", "entity_id", "deal_id", "rm", "pending_with"],
        subject_type="Syndication", view_name="syndication",
    ),
    ResourceSpec(
        # The FLAT lender list — read-only and gated by the syndication view. All MUTATION
        # goes through the SECURED nested routes (/v1/syndication/{id}/lenders), which
        # enforce the parent tracker's line scope; leaving create/update/delete on the flat
        # route open was a scope bypass, so they are disabled here.
        name="syndication lender", prefix="/v1/syndication-lenders", tags=["Syndication Lenders"],
        repo=CRUDRepository(SyndicationLender, searchable=["lender_name", "note"],
                            filterable=["status", "syndication_id", "counterparty_id", "is_existing"]),
        create_schema=s.SyndicationLenderCreate, update_schema=s.SyndicationLenderUpdate,
        read_schema=s.SyndicationLenderRead,
        filterable=["status", "syndication_id", "counterparty_id", "is_existing"],
        include_create=False, include_update=False, include_delete=False,
        view_name="syndication",
        # A SCOPED syndication user sees only lenders on lines whose company is in scope
        # (via the parent tracker), not every lender in the tenant.
        parent_scope=(SyndicationTracker, "syndication_id"),
    ),
    ResourceSpec(
        name="asset-monetisation record", prefix="/v1/asset-monetisation", tags=["Asset Monetisation"],
        repo=CRUDRepository(AssetMonetisation, searchable=["investor", "notes"],
                            filterable=["status", "nature", "entity_id", "deal_id", "state"]),
        create_schema=s.AssetMonCreate, update_schema=s.AssetMonUpdate, read_schema=s.AssetMonRead,
        filterable=["status", "nature", "entity_id", "deal_id", "state"],
        subject_type="AssetMonetisation", view_name="asset_monetisation",
    ),
    ResourceSpec(
        name="financial", prefix="/v1/financials", tags=["Financials"],
        repo=CRUDRepository(Financial, searchable=["provenance", "fiscal_year"],
                            filterable=["statement_type", "entity_id", "is_current", "fiscal_year"]),
        create_schema=s.FinancialCreate, update_schema=s.FinancialUpdate, read_schema=s.FinancialRead,
        filterable=["statement_type", "entity_id", "is_current", "fiscal_year"],
        include_create=False,  # creation goes through the version-aware endpoint
        view_name="fi_master", company_scoped=True, write_operation="edit_fi_record",
    ),
    ResourceSpec(
        name="contract/asset", prefix="/v1/contracts-assets", tags=["Contracts & Assets"],
        repo=CRUDRepository(ContractAsset, searchable=["title", "counterparty_name"],
                            filterable=["asset_type", "entity_id", "deal_id", "state"]),
        create_schema=s.ContractAssetCreate, update_schema=s.ContractAssetUpdate,
        read_schema=s.ContractAssetRead,
        filterable=["asset_type", "entity_id", "deal_id", "state"],
        view_name="clients", company_scoped=True, write_operation="edit_contract",
    ),
    ResourceSpec(
        name="interaction", prefix="/v1/interactions", tags=["Interactions"],
        repo=CRUDRepository(Interaction,
                            searchable=["summary", "notes", "outcome", "contact_name",
                                        "performed_by", "lender_name"],
                            filterable=["subject_type", "subject_id", "interaction_type",
                                        "direction", "entity_id", "deal_id", "source"]),
        create_schema=s.InteractionCreate, update_schema=s.InteractionUpdate,
        read_schema=s.InteractionRead,
        filterable=["subject_type", "subject_id", "interaction_type", "direction",
                    "entity_id", "deal_id", "source"],
        include_create=False,  # creation goes through the timeline-aware endpoint
        include_update=False,  # append-only: interactions are never edited...
        include_delete=False,  # ...nor deleted (ATLAS: "Records are append-only")
        view_name="clients", company_scoped=True,
    ),
    ResourceSpec(
        name="external-intelligence record", prefix="/v1/external-intelligence",
        tags=["External Intelligence"],
        repo=CRUDRepository(ExternalIntelligence, searchable=["title", "summary", "source"],
                            filterable=["intel_type", "signal", "entity_id", "deal_id"]),
        create_schema=s.ExternalIntelCreate, update_schema=s.ExternalIntelUpdate,
        read_schema=s.ExternalIntelRead,
        filterable=["intel_type", "signal", "entity_id", "deal_id"],
        view_name="clients", company_scoped=True, write_operation="edit_intel",
    ),
    ResourceSpec(
        name="monitoring record", prefix="/v1/monitoring", tags=["Monitoring & Reporting"],
        repo=CRUDRepository(MonitoringReporting, searchable=["covenant_name", "status"],
                            filterable=["record_type", "entity_id", "deal_id", "feeds_irg"]),
        create_schema=s.MonitoringCreate, update_schema=s.MonitoringUpdate, read_schema=s.MonitoringRead,
        filterable=["record_type", "entity_id", "deal_id", "feeds_irg"],
        view_name="clients", company_scoped=True, write_operation="edit_monitoring",
    ),
    ResourceSpec(
        name="document", prefix="/v1/documents", tags=["Documents"],
        repo=CRUDRepository(Document,
                            searchable=["title", "doc_type", "original_filename", "notes"],
                            filterable=["subject_type", "subject_id", "section", "slot_key",
                                        "status", "entity_id", "deal_id", "is_required"]),
        create_schema=s.DocumentCreate, update_schema=s.DocumentUpdate, read_schema=s.DocumentRead,
        filterable=["subject_type", "subject_id", "section", "slot_key", "status",
                    "entity_id", "deal_id", "is_required"],
        include_create=False,  # creation goes through the subject-aware register endpoint
        view_name="clients", company_scoped=True, write_operation="upload_remove_documents",
    ),
    ResourceSpec(
        name="document checklist item", prefix="/v1/document-checklist", tags=["Documents"],
        repo=CRUDRepository(DocumentChecklistItem, searchable=["label", "slot_key", "section"],
                            filterable=["applies_to", "section", "slot_key", "is_required",
                                        "is_active"]),
        create_schema=s.DocumentChecklistCreate, update_schema=s.DocumentChecklistUpdate,
        read_schema=s.DocumentChecklistRead,
        filterable=["applies_to", "section", "slot_key", "is_required", "is_active"],
        # Document-checklist = tenant config: broad read (tools), Admin/Management write.
        view_name="tools", write_operation="manage_checklist",
    ),
]


def build_resource_router() -> APIRouter:
    parent = APIRouter()
    for spec in _SPECS:
        parent.include_router(build_crud_router(spec))
    return parent
