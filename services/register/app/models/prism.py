"""The four forward-looking PRISM master tables.

Full schema now (schema is the "most expensive mistake" to get wrong), wired for the
workflows that populate them later — CIPHER (financials), covenant/EPC tracking
(contracts & assets), VOX/PULSE/AA (touchpoints, external intelligence) and the
behavioural-hygiene loop (monitoring & reporting).

Note the distinction between two versioning concepts on Financials:
* ``version``    — optimistic-lock counter (inherited) guarding a single row edit.
* ``version_no`` — the financial data version (audited-March vs restated-June), the
                   append-only provenance versioning PRISM 'Property 3' requires.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class Financial(RegisterBase):
    """PRISM master table 3 — Financials (time-series, versioned)."""

    __tablename__ = "financials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "entity_id", "statement_type", "period_end", "version_no",
            name="financials_unique_version",
        ),
        Index("ix_financials_entity_current", "tenant_id", "entity_id", "is_current"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_type: Mapped[str] = mapped_column(String(40), nullable=False)
    period_type: Mapped[str | None] = mapped_column(String(20))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[str | None] = mapped_column(String(12))
    as_of_date: Mapped[date | None] = mapped_column(Date)

    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    provenance: Mapped[str | None] = mapped_column(Text)
    source_document_ref: Mapped[str | None] = mapped_column(String(300))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    # Basis + magnitude — critical to read a statement correctly: standalone vs
    # consolidated, audited vs provisional, and whether numbers are absolute/lakh/crore.
    is_consolidated: Mapped[bool | None] = mapped_column(Boolean)
    is_audited: Mapped[bool | None] = mapped_column(Boolean)
    scale: Mapped[str | None] = mapped_column(String(20))  # ref: Scale (Absolute/Lakh/Crore/…)

    # Convenience metrics for querying; the full spread lives in ``data``.
    revenue: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ebitda: Mapped[float | None] = mapped_column(Numeric(18, 2))
    pat: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total_debt: Mapped[float | None] = mapped_column(Numeric(18, 2))
    net_worth: Mapped[float | None] = mapped_column(Numeric(18, 2))
    dscr: Mapped[float | None] = mapped_column(Numeric(8, 3))
    data: Mapped[dict | None] = mapped_column(JSONB)


class ContractAsset(RegisterBase):
    """PRISM master table 4 — Contracts & Assets (the climate-specific extension)."""

    __tablename__ = "contracts_assets"
    __table_args__ = (Index("ix_contracts_tenant_entity", "tenant_id", "entity_id"),)

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300))
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL")
    )
    counterparty_name: Mapped[str | None] = mapped_column(String(200))  # e.g. DISCOM / offtaker
    capacity_mw: Mapped[float | None] = mapped_column(Numeric(12, 2))
    tariff: Mapped[float | None] = mapped_column(Numeric(10, 4))
    location: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(60))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    tenor_years: Mapped[float | None] = mapped_column(Numeric(6, 2))
    contract_value_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict | None] = mapped_column(JSONB)


# PRISM master table 5 — Touchpoints — is implemented as the `interactions` table
# (app/models/interactions.py). The architecture calls it "Touchpoints"; the ATLAS UI and
# VOX call it "interactions" — one record type, not two.


class ExternalIntelligence(RegisterBase):
    """PRISM master table 6 — External Intelligence (outside-world data)."""

    __tablename__ = "external_intelligence"
    __table_args__ = (
        Index("ix_extintel_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_extintel_tenant_type", "tenant_id", "intel_type"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    intel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str | None] = mapped_column(String(120))  # provider
    signal: Mapped[str | None] = mapped_column(String(10))  # RED / AMBER / GREEN (PULSE)
    title: Mapped[str | None] = mapped_column(String(400))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    # Shared triage state so a multi-user RED/AMBER feed isn't re-shown to everyone.
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_dismissed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class MonitoringReporting(RegisterBase):
    """PRISM master table 7 — Monitoring & Reporting (behavioural intelligence)."""

    __tablename__ = "monitoring_reporting"
    __table_args__ = (
        Index("ix_monitoring_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_monitoring_tenant_type", "tenant_id", "record_type"),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), index=True
    )
    record_type: Mapped[str] = mapped_column(String(40), nullable=False)
    covenant_name: Mapped[str | None] = mapped_column(String(200))
    due_date: Mapped[date | None] = mapped_column(Date)
    submitted_date: Mapped[date | None] = mapped_column(Date)
    on_time: Mapped[bool | None] = mapped_column(Boolean)
    delay_days: Mapped[int | None] = mapped_column(Integer)
    security_created_within_days: Mapped[int | None] = mapped_column(Integer)
    extension_count: Mapped[int | None] = mapped_column(Integer)
    behavioural_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    period: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str | None] = mapped_column(String(60))
    feeds_irg: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Financial-covenant compliance (e.g. DSCR required ≥1.20, actual 1.05 → breached).
    target_value: Mapped[float | None] = mapped_column(Numeric(18, 4))
    actual_value: Mapped[float | None] = mapped_column(Numeric(18, 4))
    breached: Mapped[bool | None] = mapped_column(Boolean)
    waiver_status: Mapped[str | None] = mapped_column(String(60))  # ref: Waiver Status
    # A GRANTED waiver is time-boxed and decision-backed (increment 8): it cites the
    # durable waiver decision (kind-checked, senior authority) and lapses on
    # ``waiver_valid_until`` — the covenant sweep flips it to 'Expired' and the breach is
    # live again. Set ONLY through the /waive endpoint and the sweep.
    waiver_valid_until: Mapped[date | None] = mapped_column(Date)
    waiver_decision_ref: Mapped[str | None] = mapped_column(String(200))
    waiver_note: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)
