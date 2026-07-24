"""System tables: tenants, reference vocabularies, idempotency keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    """A tenant boundary. The default tenant is Evam; co-lenders, DSAs, OEMs and
    portal users get added over time. Every business row carries ``tenant_id``."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class TenantSettings(Base, TimestampMixin):
    """Per-tenant business configuration (a single JSONB blob keyed by tenant).

    Holds things the UI must read consistently for every user/session — e.g. the ATLAS
    alerting thresholds (stale-lead days, lender-silent days, undisbursed cutoff). Kept
    out of ``Tenant`` so config can evolve without touching the tenant boundary, and
    served via ``GET/PUT /v1/settings``.
    """

    __tablename__ = "tenant_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class RefValue(Base, TimestampMixin):
    """Controlled-vocabulary entries so front-ends fetch dropdowns from the Register."""

    __tablename__ = "ref_values"
    __table_args__ = (UniqueConstraint("category", "value", name="ref_values_category_value"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str | None] = mapped_column(String(160))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class IdempotencyKey(Base):
    """Stores the outcome of a mutating request keyed by client-supplied
    ``Idempotency-Key``, so a retried POST returns the original result instead of
    creating a duplicate row (critical for an at-least-once network world)."""

    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
