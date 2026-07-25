"""User management & RBAC — the tables behind the ATLAS RBAC spec (v3.1).

Four tables realise the spec's model:

* ``users``            — login identities (the spec's *Employees* governance table):
                         @evamfinance.com e-mail, active flag, ``reports_to`` (drives Head
                         visibility scope).
* ``user_roles``       — role stacking: a user may hold several of the 10 catalogue roles
                         simultaneously; when they overlap the HIGHER role's permission
                         applies (resolved in ``app.authz.engine``).
* ``line_assignments`` — the assignment-driven permission primitive: assigning a user to a
                         product line (Lending / Syndication / AM) or a lead/deal grants
                         role-appropriate write **on that line only**, until unassigned.
                         Two assignees can co-exist on a line (Syn RM + Deal Analyst).
* ``change_requests``  — the request → approve/reject flow for stage/status changes:
                         non-approvers raise a request; Admin / Management / the relevant
                         vertical Head decides; approval applies the change.

Deliberately auth-light (per the platform brief): passwords/SSO live upstream at the
Doors; the Register stores identities, roles and assignments and *enforces* what they
may do to the source of truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class User(RegisterBase):
    """A platform user (the spec's Employee record). Drives all RBAC."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="users_tenant_email"),
        Index("ix_users_tenant_active", "tenant_id", "is_active"),
    )

    email: Mapped[str] = mapped_column(String(200), nullable=False)  # must match tenant domain
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Short handle used across ATLAS grids (matches trackers' rm/analyst columns).
    short_name: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                            server_default="true")
    # MANDATORY for ICs, optional for Heads (spec: drives Head visibility scope).
    reports_to: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Optional link to the existing team-directory row.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("people.id", ondelete="SET NULL")
    )
    phone: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class UserRole(RegisterBase):
    """One catalogue role held by a user. Multiple rows = role stacking."""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "role", name="user_roles_unique"),
        Index("ix_user_roles_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)  # ref: RBAC Role
    granted_by: Mapped[str | None] = mapped_column(String(200))


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

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
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
