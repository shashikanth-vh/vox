"""RBAC tables that must live NEXT TO THE DATA (three-service architecture).

Identity (users, roles, the admin-editable access matrix) lives in the **Access
service**; the Gateway forwards verified identity per request. The Register keeps the
two tables whose semantics are inseparable from the business rows:

* ``line_assignments`` — the assignment-driven permission primitive: assigning a user to a
                         product line (Lending / Syndication / AM) or a lead/deal grants
                         role-appropriate write **on that line only**, until unassigned.
                         ``user_id`` references an Access-service user (no local FK).
* ``change_requests``  — the request → approve/reject flow for stage/status changes:
                         approval APPLIES the change atomically with the tracker row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class LineAssignment(RegisterBase):
    """Assignment-driven permission: user × (line or lead/deal) × assignment role.

    ``ended_at IS NULL`` = active. A Deal Analyst can hold assignments on Lending,
    Syndication and AM lines simultaneously across multiple deals; a Syn line can carry a
    Syn RM and a Deal Analyst at the same time.
    """

    __tablename__ = "line_assignments"
    __table_args__ = (
        Index("ix_assign_subject", "tenant_id", "subject_type", "subject_id"),
        Index("ix_assign_user_active", "tenant_id", "user_id", "ended_at"),
    )

    # References an Access-service user (identity lives there; no local FK).
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Lead / Deal / Lending / Syndication / AssetMonetisation (ATLAS refType).
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # BDRM / Deal Analyst / Syn RM / AM RM — the capacity in which they're assigned.
    assignment_role: Mapped[str] = mapped_column(String(30), nullable=False)
    assigned_by: Mapped[str | None] = mapped_column(String(200))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)


class ChangeRequest(RegisterBase):
    """A requested stage/status change awaiting approval (the spec's Copilot flow)."""

    __tablename__ = "change_requests"
    __table_args__ = (
        Index("ix_chreq_status", "tenant_id", "status"),
        Index("ix_chreq_subject", "tenant_id", "subject_type", "subject_id"),
    )

    # Which line the change applies to: Lending / Syndication / AssetMonetisation / Lead / Deal.
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    field: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. "stage", "status"
    from_value: Mapped[str | None] = mapped_column(String(120))
    to_value: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    requested_by: Mapped[str] = mapped_column(String(200), nullable=False)  # user email
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Pending",
                                        server_default="Pending")  # Pending/Approved/Rejected/Cancelled
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
