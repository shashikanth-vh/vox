"""Shared polymorphic-subject resolution (ATLAS refType / refId).

Interactions and Documents both attach to a *polymorphic subject* — a Lead, Deal,
Entity, Counterparty, or a Lending / Syndication / Asset-Monetisation tracker — and both
denormalise ``entity_id`` / ``deal_id`` from that subject so entity- and deal-level views
aggregate across a company's whole footprint. This module is the single definition of
that mapping and its derivation rules, so the two write paths can never drift.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetMonetisation,
    Counterparty,
    Deal,
    Entity,
    Lead,
    LendingTracker,
    SyndicationTracker,
)

# subject_type (ATLAS refType) → the ORM model that backs it.
SUBJECTS: dict[str, type] = {
    "Lead": Lead,
    "Deal": Deal,
    "Entity": Entity,
    "Counterparty": Counterparty,
    "Lending": LendingTracker,
    "Syndication": SyndicationTracker,
    "AssetMonetisation": AssetMonetisation,
}


async def load_subject(
    session: AsyncSession, tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID
):
    """Load the subject record (tenant-scoped), or ``None`` if it doesn't exist."""
    model: Any = SUBJECTS[subject_type]
    return (
        await session.execute(
            select(model).where(model.id == subject_id, model.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


def derive_links(subject_type: str, subject) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return ``(entity_id, deal_id)`` denormalised from the subject record."""
    if subject_type == "Entity":
        return subject.id, None
    if subject_type == "Deal":
        return subject.entity_id, subject.id
    if subject_type == "Lead":
        return subject.entity_id, None
    if subject_type in ("Lending", "Syndication", "AssetMonetisation"):
        return subject.entity_id, getattr(subject, "deal_id", None)
    return None, None  # Counterparty
