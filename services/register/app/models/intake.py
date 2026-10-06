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

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
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
    # submitted (parked, waiting for the RM's click) | approved | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(20))                 # capital | assets
    company_name: Mapped[str | None] = mapped_column(String(300))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    # Who decided it and when — the approver for an approval, the rejecter for a
    # rejection; as sent by the website, or the address the PRISM link was issued to.
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    # pending | lead_created | interaction_on_deal | interaction_on_lead | rejected
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    rm: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)                         # why this outcome
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    # The chase: when the reminder went to the approvers, and when the expired
    # enquiry was handed to the BD Head with fresh links.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadEnquiryToken(IntakeBase):
    """One Approve or Reject link, as issued for a parked enquiry.

    The link the RM gets in the e-mail carries a random token; only its SHA-256
    is kept here, so a copy of the table cannot be turned into a valid link. A
    token is bound to one enquiry, one action and (when the website said who it
    mails) one recipient, expires, and is spent by the first decision — the
    enquiry's status, not the token's, is what every later click reads.
    """

    __tablename__ = "lead_enquiry_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    enquiry_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("lead_enquiries.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)          # approve | reject
    recipient: Mapped[str | None] = mapped_column(String(200))             # the address it was issued to
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
