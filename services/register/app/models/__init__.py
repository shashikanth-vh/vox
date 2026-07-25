"""All ORM models. Importing this module registers every table on ``Base.metadata``."""

from __future__ import annotations

from app.db.base import AuditLog, Base
from app.models.deals import Deal, Lead
from app.models.documents import Document, DocumentChecklistItem
from app.models.interactions import Interaction
from app.models.prism import (
    ContractAsset,
    ExternalIntelligence,
    Financial,
    MonitoringReporting,
)
from app.models.registry import Counterparty, Entity, Person
from app.models.system import IdempotencyKey, RefValue, Tenant, TenantSettings
from app.models.trackers import (
    AssetMonetisation,
    LendingTracker,
    SyndicationLender,
    SyndicationTracker,
)
from app.models.users import ChangeRequest, LineAssignment, User, UserRole

__all__ = [
    "Base",
    "AuditLog",
    "Tenant",
    "TenantSettings",
    "RefValue",
    "IdempotencyKey",
    "Entity",
    "Person",
    "Counterparty",
    "Lead",
    "Deal",
    "Document",
    "DocumentChecklistItem",
    "Interaction",
    "LendingTracker",
    "SyndicationTracker",
    "SyndicationLender",
    "AssetMonetisation",
    "Financial",
    "ContractAsset",
    "ExternalIntelligence",
    "MonitoringReporting",
    "User",
    "UserRole",
    "LineAssignment",
    "ChangeRequest",
]
