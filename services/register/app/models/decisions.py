"""The workflow-decision resource — the single source of truth for a lead-conversion
approve/reject, with a database-enforced single-winner guarantee."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import RegisterBase


class WorkflowDecision(RegisterBase):
    """One immutable decision per workflow. The ``UNIQUE (tenant_id, workflow_id)`` constraint
    makes the FIRST persisted decision the winner: a replay of the same decision returns the
    original row, and the opposite decision is rejected (409). Provenance (``decided_by`` and
    the grant) is set server-side from the verified approver context, never client fields."""

    __tablename__ = "workflow_decisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id", name="workflow_decisions_tenant_wf"),
    )

    workflow_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lead_id: Mapped[str | None] = mapped_column(String(64))
    # Subject binding — set for a governance (e.g. Credit Committee) decision so governance evidence
    # can be verified against the actual decision for THIS subject (a lead conversion leaves these
    # null; it is bound by lead_id).
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(200))
    # Committee-decision references, so the workflow derives the evidence artefact pointers from the
    # authoritative record — never the untrusted signal.
    committee_reference: Mapped[str | None] = mapped_column(String(500))
    sanction_letter_reference: Mapped[str | None] = mapped_column(String(500))
    # Conditional approval: the committee's conditions and the sanction validity window.
    conditions: Mapped[str | None] = mapped_column(Text)
    valid_days: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)   # Approved / Rejected
    decided_by: Mapped[str] = mapped_column(String(200), nullable=False)
    decided_by_id: Mapped[str | None] = mapped_column(String(64))
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list,
                                        server_default="[]")
    operations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict,
                                             server_default="{}")
    views: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict,
                                        server_default="{}")
    note: Mapped[str | None] = mapped_column(Text)


class WorkflowDecisionOutbox(RegisterBase):
    """The delivery-tracking sibling of :class:`WorkflowDecision`. One row per decision, created
    in the SAME transaction as the decision (transactional outbox), then driven by the
    background reconciler: ``pending`` → ``applied`` (the run converted) or ``dead`` (the run
    closed without applying, or retries exhausted). Deliberately MUTABLE — no immutability
    trigger — because tracking evolving delivery state is its whole job."""

    __tablename__ = "workflow_decision_outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id",
                         name="workflow_decision_outbox_tenant_wf"),
    )

    workflow_id: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending",
                                        server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fencing token of the CURRENT claim: a delivery update must present it, so a stalled
    # claimant whose lease expired can't overwrite a row another replica has since re-claimed.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
