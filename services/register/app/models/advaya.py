"""The Advaya-handoff record — the authoritative, immutable outcome of handing a sanctioned,
documented facility to Advaya (the downstream loan-management system) for disbursement.

The ``advaya_acknowledgement`` governance-evidence kind that gates ``Disbursement Pending`` is
VERIFIED against an ``Accepted`` row here (same tenant + lending line + payload digest), so the
acknowledgement can be generated only from a handoff Advaya actually accepted — never asserted by a
caller. Single-winner
on ``(tenant_id, handoff_key)``; the row is write-once (a trigger blocks UPDATE/DELETE)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class AdvayaHandoverPackage(RegisterBase):
    """The durable, immutable HANDOVER PACKAGE — the authoritative snapshot of exactly what PRISM
    handed over to Advaya. Created transactionally with the advance to 'Handed Over to Advaya' (the
    stage moves ONLY after this snapshot is committed), so the record proves WHAT was handed over,
    not merely that someone changed the stage.

    Immutable: a trigger blocks DELETE and every UPDATE except a one-time set of the (initially NULL)
    manual ``advaya_reference`` — the operator's Advaya-side reference, available only later.
    Single-winner on ``(tenant_id, handover_key)`` (one live handover per Lending line)."""

    __tablename__ = "advaya_handover_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "handover_key",
                         name="advaya_handover_packages_tenant_key"),
    )

    handover_key: Mapped[str] = mapped_column(String(200), nullable=False)
    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(64))
    # Authoritative amounts, snapshotted from the Lending row server-side (never trusted from input).
    facility_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    proposed_disbursement_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    proposed_disbursement_date: Mapped[date | None] = mapped_column()
    cpcs_checklist_version: Mapped[int | None] = mapped_column(Integer)
    # [{reference, sha256}] executed-document references + integrity hashes.
    executed_document_refs: Mapped[list | None] = mapped_column(JSONB)
    # Server-GENERATED package manifest: reference (storage key), digest (server-computed), and the
    # inline document bytes (base64) when the storage backend is inline.
    package_reference: Mapped[str | None] = mapped_column(String(300))
    package_sha256: Mapped[str | None] = mapped_column(String(64))
    package_document: Mapped[str | None] = mapped_column(Text)
    initiated_by: Mapped[str | None] = mapped_column(String(120))
    initiated_by_id: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_by_id: Mapped[str | None] = mapped_column(String(64))
    delivery_method: Mapped[str | None] = mapped_column(String(60))
    recipient: Mapped[str | None] = mapped_column(String(200))
    advaya_reference: Mapped[str | None] = mapped_column(String(200))  # manual, set once, later
    # Prepared (maker's draft) → HandedOver (checker-approved, stage advanced, then frozen).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Prepared")
    note: Mapped[str | None] = mapped_column(Text)
    # The full immutable snapshot (denormalised copy of everything above + audit context).
    snapshot: Mapped[dict | None] = mapped_column(JSONB)


class AdvayaHandoff(RegisterBase):
    __tablename__ = "advaya_handoffs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "handoff_key", name="advaya_handoffs_tenant_key"),
    )

    handoff_key: Mapped[str] = mapped_column(String(200), nullable=False)
    lending_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)   # Accepted / Rejected
    acknowledgement_id: Mapped[str | None] = mapped_column(String(200))
    workflow_id: Mapped[str | None] = mapped_column(String(200))
    run_id: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
