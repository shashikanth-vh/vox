"""Entities, People and Counterparties — the spine of the entity-centric Register."""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class Entity(RegisterBase):
    """PRISM master table 1 — Entities.

    A single legal entity, CIN-anchored where possible. The spine everything links to:
    one company appears once and every deal/financial/touchpoint links back to it. This
    is the entity-centric (not deal-centric) commitment at the heart of the Register.
    """

    __tablename__ = "entities"
    __table_args__ = (
        # ATLAS borrower-code is unique per tenant (used for promoter-level grouping).
        UniqueConstraint("tenant_id", "code", name="entities_tenant_code"),
        Index("ix_entities_tenant_sector", "tenant_id", "sector"),
        Index("ix_entities_tenant_cin", "tenant_id", "cin"),
    )

    # Human-friendly stable code (e.g. "AADHYA"); also the promoter grouping key.
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(300))
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, default="Company",
                                             server_default="Company")

    # Statutory identifiers (nullable; not every entity has all of them).
    cin: Mapped[str | None] = mapped_column(String(21), index=True)
    pan: Mapped[str | None] = mapped_column(String(10))
    gstin: Mapped[str | None] = mapped_column(String(15))

    sector: Mapped[str | None] = mapped_column(String(60), index=True)
    sub_sector: Mapped[str | None] = mapped_column(String(120))
    lens: Mapped[str | None] = mapped_column(String(20))  # Mitigation / Adaptation

    state: Mapped[str | None] = mapped_column(String(60))
    location: Mapped[str | None] = mapped_column(String(200))

    register_status: Mapped[str | None] = mapped_column(String(40), index=True)  # backfill lifecycle
    promoter_group_code: Mapped[str | None] = mapped_column(String(60), index=True)

    about: Mapped[str | None] = mapped_column(Text)
    toi: Mapped[str | None] = mapped_column(Text)  # "type of interest" free note from ATLAS
    notes: Mapped[str | None] = mapped_column(Text)

    # Curated-list / showcase membership (ATLAS Core-33, Adaptation-10, dashboards). A
    # server-authoritative list of tags so those groupings are consistent for every user,
    # not a hard-coded UI array. GIN-indexed for `?tags=` membership filters.
    tags: Mapped[list | None] = mapped_column(JSONB)


class Person(RegisterBase):
    """Evam team members (RM / Analyst / Ops / Management / Admin).

    Register keeps auth light — this is the team directory that deals and trackers
    reference by name; identity/SSO lives upstream.
    """

    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("tenant_id", "full_name", name="people_tenant_full_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)  # short handle, e.g. "Shubh"
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(30))
    geography: Mapped[str | None] = mapped_column(String(120))
    sectors: Mapped[str | None] = mapped_column(String(300))
    started_on: Mapped[date | None] = mapped_column(Date)
    reports_to: Mapped[str | None] = mapped_column(String(200))
    inactive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text)


class Counterparty(RegisterBase):
    """Lenders, investors, offtakers and other counterparties (ATLAS 'lenders').

    Climate credit is fundamentally about counterparty quality — this registry is what
    the syndication and offtaker-intelligence workflows build on.
    """

    __tablename__ = "counterparties"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="counterparties_tenant_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(60))
    counterparty_type: Mapped[str | None] = mapped_column(String(40), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sectors: Mapped[str | None] = mapped_column(String(400))
    # Optional gating/appetite metrics for lender-routing intelligence.
    ticket_min_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    ticket_max_cr: Mapped[float | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)
