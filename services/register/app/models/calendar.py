"""Calendar events — first-class meeting / follow-up records (Release-1 increment 7).

One row per scheduled meeting or follow-up. Previously the only calendar trace was a
``meta.calendar`` hand-off note on interactions; this table gives the meeting a real
lifecycle the rest of the platform (ATLAS "Today", VOX follow-ups, workflow SLAs) can
build on:

    Scheduled ──▶ Completed   (it happened; who closed it and what came of it)
             └──▶ Cancelled   (it won't; who called it off and why)

Reschedules UPDATE the Scheduled row (audited; optimistic version bump) rather than
spawning a new one — the event's identity is the commitment, not the time slot. Terminal
rows are frozen by a DB trigger and can never be deleted (migration 0017): a meeting that
took place — or was called off — is a fact on the record.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class CalendarEvent(RegisterBase):
    """A scheduled meeting/follow-up, subject-bound like interactions and documents."""

    __tablename__ = "calendar_events"

    # Polymorphic subject (a lead, deal, entity, tracker…). Optional — a standalone
    # meeting is legal. No DB FK on subject_id (it can point at any record type).
    subject_type: Mapped[str | None] = mapped_column(String(30))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL")
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(300))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organizer: Mapped[str] = mapped_column(String(200), nullable=False)  # e-mail
    attendees: Mapped[list | None] = mapped_column(JSONB)                # [e-mail, …]

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="Scheduled", server_default="Scheduled"
    )  # ref: Calendar Event Status
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )  # manual / VOX / workflow
    workflow_id: Mapped[str | None] = mapped_column(String(200))   # run that created it
    external_ref: Mapped[str | None] = mapped_column(String(200))  # Google/Outlook id

    completed_by: Mapped[str | None] = mapped_column(String(200))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_note: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(String(200))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_note: Mapped[str | None] = mapped_column(Text)
