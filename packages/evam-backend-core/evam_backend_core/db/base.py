"""Declarative base and the column mixins every business table shares.

Three structural properties every PRISM service's tables inherit, defined once:

* **Tenant-aware**  — ``tenant_id`` on every row.
* **Versioned**     — ``version`` integer wired into SQLAlchemy's ``version_id_col`` so a
                      concurrent overwrite raises ``StaleDataError`` (→ 409) instead of a
                      silent lost update.
* **Auditable**     — ``created_at/by`` + ``updated_at/by`` and a soft-delete
                      (``deleted_at``) so nothing in the source of truth is hard-deleted.

``RecordBase`` is the concrete base; ``RegisterBase`` is kept as an alias for services
that grew up calling it that.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Deterministic constraint naming → clean, diffable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_by: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[str | None] = mapped_column(String(120))


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class RecordBase(Base, TimestampMixin, SoftDeleteMixin):
    """Concrete base for every tenant-aware, versioned, auditable business table."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)

    # Optimistic-locking counter. SQLAlchemy bumps and checks this on every UPDATE.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}


# Back-compat alias.
RegisterBase = RecordBase


class AuditLog(Base):
    """Append-only audit trail. Never updated, never soft-deleted."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(60), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    changes: Mapped[dict | None] = mapped_column(JSONB)
