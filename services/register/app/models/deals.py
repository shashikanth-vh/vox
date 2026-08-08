"""Leads and Deals.

A Deal is PRISM master table 2: one entity → many deals, product-aware, carrying the
three business flags (Lending / Syndication / Asset Monetisation) that a single deal can
hold at once. A Lead is the pre-deal ATLAS pipeline row that converts into an entity+deal.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class Lead(RegisterBase):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "lead_no", name="leads_tenant_lead_no"),
        Index("ix_leads_tenant_status", "tenant_id", "status"),
    )

    # (field, prefix, width) — the repository hands out the next free number on create.
    # Declared on the MODEL rather than on the route so it applies wherever the row is
    # created, not only through POST /v1/leads.
    __auto_number__ = ("lead_no", "L-", 4)

    lead_no: Mapped[str | None] = mapped_column(String(40))  # e.g. "LD-002"
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"), index=True
    )
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(60))
    lens: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str | None] = mapped_column(String(40))
    source_name: Mapped[str | None] = mapped_column(String(200))
    rm: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Active",
                                        server_default="Active")
    temperature: Mapped[str | None] = mapped_column(String(10))
    contact: Mapped[str | None] = mapped_column(String(200))
    designation: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    last_interaction_date: Mapped[date | None] = mapped_column(Date)
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_date: Mapped[date | None] = mapped_column(Date)
    converted_deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")
    )
    conv: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)


class Deal(RegisterBase):
    __tablename__ = "deals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "deal_no", name="deals_tenant_deal_no"),
        Index("ix_deals_tenant_entity", "tenant_id", "entity_id"),
    )

    # A deal is quoted by its CLIENT's code — that is what the seed does and what the
    # Deals grid labels "Group Code". So the number is derived from the entity rather
    # than being a sequence of its own: (field, fk column, source table, source column).
    # A company that comes back for a second facility gets "<code>-2", never a clash.
    # Live-created deals used to leave this NULL, which is why the grid showed a blank
    # Group Code and the audit trail read "label → null".
    __auto_number_from__ = ("deal_no", "entity_id", "entities", "code")

    deal_no: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # ATLAS keeps deal keyed by client code too; retained for round-tripping.
    code: Mapped[str | None] = mapped_column(String(60), index=True)

    product_type: Mapped[str | None] = mapped_column(String(60), index=True)

    # A deal can be in Lending AND Syndication AND Asset Monetisation simultaneously.
    is_lending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_syndication: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_asset_mon: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    rm: Mapped[str | None] = mapped_column(String(120))
    analyst: Mapped[str | None] = mapped_column(String(120))
    # The climate lens (Mitigation / Adaptation) at deal level — carried over from the
    # lead at conversion and from the Leads sheet on import (0014).
    lens: Mapped[str | None] = mapped_column(String(20))
    # THE deal stage — the ORIGINATION FUNNEL, verbatim MIS vocabulary (New Inquiry / In
    # Screening / In Pipeline / On Hold / Screened Out / Closed Won / Closed Lost; see rbac
    # DEAL_FUNNEL_STAGES). A deal carries no credit lifecycle: the bank/NBFC pipeline is
    # governed on the LENDING TRACKER line.
    stage: Mapped[str | None] = mapped_column(String(30))
    # Append-only stage history (parity with the other trackers), so import-driven Deal stage
    # changes are recorded, not silently overwritten.
    stage_history: Mapped[list | None] = mapped_column(JSONB)
    # Set to 'Required' when a governed import RETAINED this row despite missing mandatory data —
    # operational reads exclude it (reconciliation_status IS NULL) until an Admin resolves it.
    reconciliation_status: Mapped[str | None] = mapped_column(String(20))
    temperature: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str | None] = mapped_column(String(40))
    source_detail: Mapped[str | None] = mapped_column(String(200))
    source_name: Mapped[str | None] = mapped_column(String(200))

    date_received: Mapped[date | None] = mapped_column(Date)
    ic_date: Mapped[date | None] = mapped_column(Date)
    sanction_date: Mapped[date | None] = mapped_column(Date)
    disbursement_date: Mapped[date | None] = mapped_column(Date)
    exit_date: Mapped[date | None] = mapped_column(Date)

    remarks: Mapped[str | None] = mapped_column(Text)
