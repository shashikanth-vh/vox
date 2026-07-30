"""Governance evidence — the IMMUTABLE, append-only record that a real-world governance milestone
actually occurred before a sensitive lifecycle transition (a Credit Committee approval, an issued
sanction letter, an executed facility agreement, a completed document set …).

Ordered transitions and mandatory fields prove *sequence* and *shape*; an evidence object proves
the *work happened*. Each row is a durable reference to the artefact — its kind, an external
reference (document id / URI / decision ref), an optional integrity digest, and who/when — that the
shared policy engine's evidence gate checks before it will allow the transition. Rows are
WRITE-ONCE: a database trigger (migration 0011) rejects UPDATE and DELETE, so evidence can be added
but never silently altered or removed after the fact."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class GovernanceEvidence(RegisterBase):
    __tablename__ = "governance_evidence"

    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The governance milestone this artefact evidences, matched against policy.EVIDENCE_FOR_STAGE
    # (e.g. 'credit_committee_approval', 'sanction_letter', 'executed_agreement').
    evidence_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    # A durable pointer to the artefact itself — a document-store id, a URI, a committee minute ref.
    reference: Mapped[str] = mapped_column(String(500), nullable=False)
    # Optional integrity digest of the referenced artefact, so a later reader can prove it is the
    # same document that was evidenced (tamper-evidence for the referenced object). MANDATORY for
    # governance-grade kinds (enforced by the attach endpoint / kind registry).
    sha256: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    # The identity that attached the evidence (a workflow activity's delegated caller, or a human).
    recorded_by: Mapped[str | None] = mapped_column(String(120))
    # Provenance: the authoritative Temporal run + decision record that produced GOVERNANCE
    # evidence (mandatory for governance kinds), so a committee approval / sanction letter is
    # traceable to the run and single-winner decision that authorised it — not a free-typed string.
    workflow_id: Mapped[str | None] = mapped_column(String(200))
    run_id: Mapped[str | None] = mapped_column(String(200))
    decision_ref: Mapped[str | None] = mapped_column(String(200))
    # A corrected row points at the one it replaces (supersession); the superseded row is no longer
    # "currently valid" once this active row exists.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GovernanceEvidenceStatus(RegisterBase):
    """Append-only validity ledger for an evidence row. The evidence table itself is immutable, so
    a mistaken or fraudulent record is neutralised by APPENDING a terminal status here
    ('Revoked' / 'Invalidated' / 'Superseded') — never by mutating or deleting the original. The
    policy loader treats an evidence row as currently valid only when it has NO terminal status."""

    __tablename__ = "governance_evidence_status"

    evidence_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120))
