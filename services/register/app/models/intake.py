"""Enquiries that arrived through a door other than the desk — the website form,
the WhatsApp bot — and what the register made of each one.

One row per enquiry number, written once (idempotent on redelivery), holding the
payload as received, who approved it where, and the OUTCOME: the lead it became,
or the deal / lead it was logged on as an interaction because the company was
already being worked, or 'rejected'. "What happened to EV483920" is one lookup.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class IntakeBase(DeclarativeBase):
    pass


class LeadEnquiry(IntakeBase):
    __tablename__ = "lead_enquiries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "enquiry_no", name="lead_enquiries_tenant_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    enquiry_no: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="website")
    status: Mapped[str] = mapped_column(String(20), nullable=False)       # approved | rejected
    intent: Mapped[str | None] = mapped_column(String(20))                 # capital | assets
    company_name: Mapped[str | None] = mapped_column(String(300))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    # lead_created | interaction_on_deal | interaction_on_lead | rejected
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    rm: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)                         # why this outcome
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
