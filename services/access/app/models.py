"""Access service ORM models — identity facts + the admin-editable access matrix.

* ``tenants``        — minimal tenant registry (mirrors the platform default).
* ``users``          — the Employees governance table (spec: drives all RBAC).
* ``user_roles``     — role stacking (a user may hold several catalogue roles).
* ``access_grants``  — THE MATRIX AS DATA: one row per (kind, item, role) cell, editable
                       by Admin-role users only; seeded from the versioned spec artifact
                       (``evam_backend_core.rbac``). Guardrail cells are immutable.
* ``matrix_versions``— monotonically increasing per-tenant version; gateways cache
                       ``/v1/resolve`` responses and refresh when the version moves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from evam_backend_core.db.base import Base, RecordBase, TimestampMixin
from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                            server_default="true")


class User(RecordBase):
    """A platform user (the spec's Employee record)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="users_tenant_email"),
        Index("ix_users_tenant_active", "tenant_id", "is_active"),
    )

    email: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                            server_default="true")
    reports_to: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    phone: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    # REVOCATION EPOCH — bumped on every role grant/revoke and (de)activation. Carried in
    # the signed authorization context; a sensitive-operation revalidation that re-resolves
    # the user rejects a context minted under an older epoch.
    permissions_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0,
                                                   server_default="0")


class UserRole(RecordBase):
    """One catalogue role held by a user. Multiple rows = role stacking."""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "role", name="user_roles_unique"),
        Index("ix_user_roles_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(200))


class AccessGrant(RecordBase):
    """One cell of the access matrix: (kind, item, role) → access level."""

    __tablename__ = "access_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "item", "role", name="access_grants_cell"),
        Index("ix_access_grants_kind", "tenant_id", "kind"),
    )

    kind: Mapped[str] = mapped_column(String(12), nullable=False)   # "view" | "operation"
    item: Mapped[str] = mapped_column(String(60), nullable=False)   # e.g. "lending", "delete_row"
    role: Mapped[str] = mapped_column(String(30), nullable=False)   # catalogue role
    access: Mapped[str] = mapped_column(String(12), nullable=False)  # NONE/READ/SCOPED/FULL/APPROVE
    # PROVENANCE — 'baseline' (seeded from the approved compiled matrix, provenance-tagged
    # with its policy version) or 'override' (edited at runtime by an Admin). Keeps "what we
    # approved" and "what we changed since" separable forever.
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="baseline",
                                        server_default="baseline")


class MatrixVersion(Base, TimestampMixin):
    """Per-tenant monotone version — bumped on every grant change; gateways poll it."""

    __tablename__ = "matrix_versions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1,
                                         server_default="1")


class AccessAudit(Base, TimestampMixin):
    """IMMUTABLE audit event for every authorization-governance change: role grants and
    revocations, user (de)activation, matrix-cell edits, seeds. Append-only — so every
    later authorization decision can answer who acted, under which tenant and policy
    version, and which baseline/overrides produced the permission."""

    __tablename__ = "access_audit"
    __table_args__ = (
        Index("ix_access_audit_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)   # e.g. role.grant
    item: Mapped[str | None] = mapped_column(String(200))             # user email / cell
    detail: Mapped[dict | None] = mapped_column(JSONB)
    policy_version: Mapped[str | None] = mapped_column(String(20))


# Timestamp type is referenced for Alembic autogenerate completeness.
_ = datetime
