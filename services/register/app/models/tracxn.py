"""The market feed's larder — Tracxn answers cached per (tenant, CIN, endpoint).

Tracxn bills per call and a company's filed financials do not change by the
hour, so every answer is kept and served until it ages out (the adapter's TTL)
or a refresh is asked for. The payload is stored RAW: normalisation happens on
read, so a mapping fix never requires refetching what was already paid for.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TracxnBase(DeclarativeBase):
    pass


class TracxnCache(TracxnBase):
    __tablename__ = "tracxn_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cin", "endpoint", name="tracxn_cache_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    cin: Mapped[str] = mapped_column(String(40), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
