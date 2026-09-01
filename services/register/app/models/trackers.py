"""The three business trackers that hang off a deal:

* LendingTracker        — Evam's own-book lending (ATLAS 'lending')
* SyndicationTracker    — Mobilise / syndication (ATLAS 'syn') with per-lender rows
* SyndicationLender     — a single lender's posture within a syndication
* AssetMonetisation     — Recycle / asset monetisation (ATLAS 'am')

Stage/status history is preserved as an append-only JSONB array on each row, faithful
to the ATLAS ``h`` structure, so the full lifecycle of a deal is reconstructable.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import RegisterBase


class LendingTracker(RegisterBase):
    __tablename__ = "lending_tracker"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tracker_no", name="lending_tracker_tenant_no"),
        Index("ix_lending_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_lending_tenant_stage", "tenant_id", "stage"),
    )

    # See Lead.__auto_number__ — assigned on create so a converted line is quotable.
    __auto_number__ = ("tracker_no", "L", 3)

    tracker_no: Mapped[str | None] = mapped_column(String(40))  # e.g. "L001"
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    amount_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    rm: Mapped[str | None] = mapped_column(String(120))
    analyst: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[str | None] = mapped_column(String(40))
    stage_updated_at: Mapped[date | None] = mapped_column(Date)
    sanction_date: Mapped[date | None] = mapped_column(Date)
    # PROPOSED drawdown — fixed at 'Ready for Disbursement' and carried into the Advaya handover
    # package. These are what PRISM proposes; they are NOT proof that money moved.
    proposed_disbursement_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    proposed_disbursement_date: Mapped[date | None] = mapped_column(Date)
    # ACTUAL disbursement — reserved for a real disbursement confirmation from the downstream
    # system (a future Advaya integration); PRISM never sets these on its own authority.
    disbursed_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    disbursement_date: Mapped[date | None] = mapped_column(Date)
    pending_with: Mapped[str | None] = mapped_column(String(20))
    remarks: Mapped[str | None] = mapped_column(Text)
    stage_history: Mapped[list | None] = mapped_column(JSONB)
    reconciliation_status: Mapped[str | None] = mapped_column(String(20))


class SyndicationTracker(RegisterBase):
    __tablename__ = "syndication_tracker"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tracker_no", name="syndication_tracker_tenant_no"),
        Index("ix_syn_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_syn_tenant_status", "tenant_id", "status"),
    )

    __auto_number__ = ("tracker_no", "S", 3)

    tracker_no: Mapped[str | None] = mapped_column(String(40))  # e.g. "S001"
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    toi: Mapped[str | None] = mapped_column(String(200))  # type of industry / interest
    rm: Mapped[str | None] = mapped_column(String(120))
    analyst: Mapped[str | None] = mapped_column(String(120))
    lc: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str | None] = mapped_column(String(40))
    amount_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    line: Mapped[str | None] = mapped_column(String(120))  # Line of Lending
    facility: Mapped[str | None] = mapped_column(Text)
    tenor: Mapped[str | None] = mapped_column(String(20))
    mandate_status: Mapped[str | None] = mapped_column(Text)
    potential: Mapped[str | None] = mapped_column(Text)  # can hold a full lender list
    im_status: Mapped[str | None] = mapped_column(String(40))  # IM in Place
    sanctioned_lender: Mapped[str | None] = mapped_column(Text)
    ip_lender: Mapped[str | None] = mapped_column(Text)
    date_of_sanction: Mapped[date | None] = mapped_column(Date)
    month_of_sanction: Mapped[str | None] = mapped_column(String(12))
    nature: Mapped[str | None] = mapped_column(Text)
    existing: Mapped[str | None] = mapped_column(Text)
    price: Mapped[str | None] = mapped_column(Text)
    syndication_type: Mapped[str | None] = mapped_column(String(80))
    mandate_status3: Mapped[str | None] = mapped_column(String(40))
    pending_with: Mapped[str | None] = mapped_column(String(20))
    remarks: Mapped[str | None] = mapped_column(Text)
    status_history: Mapped[list | None] = mapped_column(JSONB)
    reconciliation_status: Mapped[str | None] = mapped_column(String(20))

    # Nested lenders, embedded inline in the read model so the ATLAS syndication row is
    # shape-compatible (lenders[] alongside the tracker). Excludes soft-deleted rows;
    # eager `selectin` keeps a list page to two queries instead of N+1.
    lenders: Mapped[list[SyndicationLender]] = relationship(
        "SyndicationLender",
        primaryjoin=(
            "and_(SyndicationTracker.id == foreign(SyndicationLender.syndication_id), "
            "SyndicationLender.deleted_at.is_(None))"
        ),
        order_by="SyndicationLender.created_at",
        viewonly=True,
        lazy="selectin",
    )


class SyndicationLender(RegisterBase):
    """One lender's posture on one syndication (the nested ATLAS ``lenders[]``)."""

    __tablename__ = "syndication_lenders"
    __table_args__ = (
        Index("ix_synlender_tenant_syn", "tenant_id", "syndication_id"),
        Index("ix_synlender_tenant_status", "tenant_id", "status"),
    )

    syndication_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("syndication_tracker.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL")
    )
    lender_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_existing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str | None] = mapped_column(String(40))
    # The bank's sanctioned allocation (₹ Cr) — recorded WITH the Sanctioned status;
    # the mandate's ask vs. sum-of-allocations arithmetic reads from here.
    amount_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    since: Mapped[date | None] = mapped_column(Date)
    response_date: Mapped[date | None] = mapped_column(Date)
    chased_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    # The conversation snapshot the chase list shows: the words behind chased_date /
    # response_date, rolled on by the same interaction write that stamps the dates.
    # `note` stays the hand-typed remark — no longer overwritten by chases/replies.
    last_chase_note: Mapped[str | None] = mapped_column(Text)
    last_reply_note: Mapped[str | None] = mapped_column(Text)
    status_history: Mapped[list | None] = mapped_column(JSONB)


class AssetMonetisation(RegisterBase):
    __tablename__ = "asset_monetisation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tracker_no", name="asset_mon_tenant_no"),
        Index("ix_am_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_am_tenant_status", "tenant_id", "status"),
    )

    __auto_number__ = ("tracker_no", "A", 3)

    tracker_no: Mapped[str | None] = mapped_column(String(40))  # e.g. "A001"
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str | None] = mapped_column(String(60))
    indicative_value_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    size_mw: Mapped[float | None] = mapped_column(Numeric(12, 2))
    nature: Mapped[str | None] = mapped_column(String(40))  # Seller / Buyer
    deal_type: Mapped[str | None] = mapped_column(String(80))  # Capital Market / Project Advisory
    investor: Mapped[str | None] = mapped_column(Text)
    investor_type: Mapped[str | None] = mapped_column(String(60))
    # Ownership, at parity with the lending/syndication trackers: the AM desk's RM and
    # analyst. Team scoping, scorecards, and book rollups all key on these.
    rm: Mapped[str | None] = mapped_column(String(120))
    analyst: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str | None] = mapped_column(String(40))
    status_history: Mapped[list | None] = mapped_column(JSONB)
    reconciliation_status: Mapped[str | None] = mapped_column(String(20))
    teaser_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
