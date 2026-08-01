"""Notifications — the durable record behind every channel (Release-1 increment 7).

Two tables, the same shape the decision outbox proved out:

* :class:`Notification` — one row per notification a human should see. This IS the
  in-app inbox (recipient-scoped reads, mark-read) and the anchor every external channel
  hangs off. Creation is idempotent by ``dedupe_key`` — a Temporal activity retry, or a
  workflow replay, can never double-notify.

* :class:`NotificationDelivery` — the transactional outbox for the EXTERNAL channels
  (email / sms / webhook): one row per channel per notification, created in the SAME
  transaction as the notification, then driven by the notifier sweep in the workflows
  service (``python -m app.notifier``): lease + fencing-token claims, exponential
  backoff, and a ``dead`` terminal state an Admin can requeue. The in-app record needs
  no delivery row — landing in this table is the delivery.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import RegisterBase


class Notification(RegisterBase):
    """One notification for one recipient (the in-app inbox row)."""

    __tablename__ = "notifications"

    recipient: Mapped[str] = mapped_column(String(200), nullable=False)   # e-mail
    recipient_role: Mapped[str | None] = mapped_column(String(60))        # context only
    event: Mapped[str] = mapped_column(String(120), nullable=False)       # machine key
    severity: Mapped[str] = mapped_column(
        String(12), nullable=False, default="info", server_default="info"
    )  # info / warning / critical
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)

    # What it's about (polymorphic, like interactions/documents) and which run raised it.
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    workflow_id: Mapped[str | None] = mapped_column(String(200))

    # Idempotency anchor: UNIQUE (tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL.
    dedupe_key: Mapped[str | None] = mapped_column(String(240))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict | None] = mapped_column(JSONB)


class NotificationDelivery(RegisterBase):
    """One external-channel delivery attempt record (outbox row) per notification+channel.

    Deliberately MUTABLE — tracking evolving delivery state is its whole job. The claim
    machinery (lease + fencing token) is identical to ``workflow_decision_outbox``: a
    stalled claimant whose lease expired cannot overwrite a row another replica since
    re-claimed, and a terminal row (delivered / dead) never regresses silently.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "notification_id", "channel",
                         name="notification_deliveries_unique"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False
    )
    channel: Mapped[str] = mapped_column(String(12), nullable=False)  # email / sms / webhook
    target: Mapped[str] = mapped_column(String(300), nullable=False)  # addr / phone / URL
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending",
                                        server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
