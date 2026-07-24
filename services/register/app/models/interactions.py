"""Interactions — PRISM master table 5 (the architecture calls it "Touchpoints").

Every interaction a user (or VOX) logs against any record — a Lead, Deal, Entity,
Counterparty, or a Lending / Syndication / Asset-Monetisation tracker — lands here as a
timestamped row, giving each record a chronological activity feed. This is the one and
only interaction/touchpoint record: the PRISM doc names it "Touchpoints"; the ATLAS UI and
VOX call it "interactions" (ATLAS ``refType`` / ``refId`` = ``subject_type`` / ``subject_id``).
It holds both the quick manual note and the full VOX-captured touchpoint (transcript, GPS,
attendees, structured intel).

Roll-ups on write (see the repository):
* ``entity_id`` / ``deal_id`` are denormalised from the subject so an entity's or deal's
  timeline spans interactions logged against any of its trackers.
* For a Syndication interaction tied to a specific lender + direction, the lender's
  response / chased date is updated (inbound = they replied, outbound = we chased).

VOX writes here with ``source = "VOX"``, the structured note in ``notes``, plus
``transcript``, ``gps_*``, ``language`` and ``key_intel``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class Interaction(RegisterBase):
    __tablename__ = "interactions"
    __table_args__ = (
        Index("ix_interactions_subject_time", "tenant_id", "subject_type", "subject_id",
              "occurred_at"),
        Index("ix_interactions_entity_time", "tenant_id", "entity_id", "occurred_at"),
        Index("ix_interactions_deal_time", "tenant_id", "deal_id", "occurred_at"),
        Index("ix_interactions_tenant_type", "tenant_id", "interaction_type"),
        Index("ix_interactions_syn_lender", "tenant_id", "syndication_lender_id"),
    )

    # Polymorphic subject (ATLAS refType / refId). subject_id has no DB FK because it can
    # point at any record type; the write path validates it exists (tenant-scoped).
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # Denormalised roll-up dimensions (real FKs) so entity/deal timelines aggregate across
    # a company's leads, deals and trackers.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE")
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="CASCADE")
    )

    # Syndication lender dimension.
    syndication_lender_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("syndication_lenders.id", ondelete="SET NULL")
    )
    lender_name: Mapped[str | None] = mapped_column(String(200))

    # What happened, and when on the timeline.
    interaction_type: Mapped[str] = mapped_column(String(60), nullable=False)  # ref: Interaction Type
    direction: Mapped[str | None] = mapped_column(String(20))  # Inbound / Outbound / Internal
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    summary: Mapped[str | None] = mapped_column(String(300))  # short one-line
    notes: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)

    # Who / with whom.
    performed_by: Mapped[str | None] = mapped_column(String(120))  # RM/analyst who logged it
    contact_name: Mapped[str | None] = mapped_column(String(200))  # counterparty-side person

    # Follow-up captured on the same interaction (drives leads' next-action fields).
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_date: Mapped[date | None] = mapped_column(Date)
    next_meeting_date: Mapped[date | None] = mapped_column(Date)

    # VOX / rich-touchpoint fields (folded in from the old Touchpoints master table).
    transcript: Mapped[str | None] = mapped_column(Text)          # VOX transcript
    language: Mapped[str | None] = mapped_column(String(20))      # en / hi / hinglish
    gps_lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    gps_lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    location: Mapped[str | None] = mapped_column(String(200))
    attendees: Mapped[list | None] = mapped_column(JSONB)
    key_intel: Mapped[dict | None] = mapped_column(JSONB)         # structured credit-intel note
    next_steps: Mapped[list | None] = mapped_column(JSONB)
    source_ref: Mapped[str | None] = mapped_column(String(200))   # VOX recording id / email id

    source: Mapped[str | None] = mapped_column(String(20))  # Manual / VOX / Email / System
    # First-class document references for the modal's "attach a file" affordance — a list
    # of {name, url|ref, content_type?, size?}. The Register stores references, not bytes.
    attachments: Mapped[list | None] = mapped_column(JSONB)
    meta: Mapped[dict | None] = mapped_column(JSONB)  # other external ids / free metadata
