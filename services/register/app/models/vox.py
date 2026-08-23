"""VOX conversations — the firm's shared memory of every external conversation.

The Build Specification's data model (Section 12), adapted to the Register's world:
tenant-aware rows under the platform's RLS instead of Supabase policies, MinIO object
keys instead of Supabase Storage paths, and the caller identity the gateway already
verifies instead of auth.uid().

Four tables, four disciplines:

* ``vox_conversations`` — the central row. The verbatim transcript is permanent (D6);
  the audio reference is erasable on a shorter clock; ``capture_id`` makes retries
  replay instead of duplicate; ``prompt_version``/``registry_version`` stamp every row
  so a registry bump never mutates history.
* ``vox_conversation_use_cases`` — the single source of truth for tagging. A
  lending+asset_monetisation conversation is retrievable under both filters.
* ``vox_consent_records`` — written once, then frozen by trigger (D5). The
  certification survives erasure of the audio and of the conversation content.
* ``vox_conversation_edits`` — append-only field-level audit; every post-AI change
  flows through the atomic edit path and lands here, one row per changed field.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, RegisterBase

CONVERSATION_STATUSES = (
    "queued", "uploading", "processing", "ready", "submitted",
    "processing_failed", "failed_permanently",
)

RECORDING_MODES = ("post_meeting", "live")

# The pipeline's forward-only order. A transition backwards (except retry paths handled
# in the API) is a bug, not a state.
_STATUS_ORDER = {s: i for i, s in enumerate(
    ("queued", "uploading", "processing", "ready", "submitted"))}


class VoxConsentRecord(RegisterBase):
    """The live-mode consent certification — evidence, so immutable (D5). A DB trigger
    blocks UPDATE and DELETE; the row outlives the audio and the structured content."""

    __tablename__ = "vox_consent_records"

    # conversation_id is nullable: the consent is written the moment recording STARTS,
    # before the conversation row exists; the conversation later points back via
    # consent_id, and this side is backfilled best-effort.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    user_email: Mapped[str] = mapped_column(String(200), nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(200))
    certified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    certification_text: Mapped[str] = mapped_column(Text, nullable=False)
    # optional keys: platform, os_version, app_version, device_model, gps_accuracy_m
    device_meta: Mapped[dict | None] = mapped_column(JSONB)


class VoxConversation(RegisterBase):
    __tablename__ = "vox_conversations"
    __table_args__ = (
        # The capture id is the idempotency key: a re-uploaded capture REPLAYS the
        # original conversation instead of duplicating it.
        UniqueConstraint("tenant_id", "capture_id", name="vox_conversations_tenant_capture"),
        Index("ix_vox_conversations_entity_time", "tenant_id", "entity_id", "meeting_date"),
        Index("ix_vox_conversations_recorder", "tenant_id", "recorder_email"),
        Index("ix_vox_conversations_status", "tenant_id", "status"),
    )

    # Who spoke. Writing is personal: only the recorder (or Management/Admin) edits.
    recorder_email: Mapped[str] = mapped_column(String(200), nullable=False)
    recorder_name: Mapped[str | None] = mapped_column(String(200))

    # Where it is filed. entity_id stays null until resolved (the Queue holds it);
    # lead_id pins the specific lead when the company runs several; the interaction row
    # minted on approval links the conversation into the PRISM timeline.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"), index=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"))
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    # Candidate names the model heard, awaiting resolution (match/no-match/ambiguous).
    entity_candidates: Mapped[list | None] = mapped_column(JSONB)
    # A "create new lead" chosen at review time is an INTENT until approval: the
    # lead row is materialised by approve, never by the tap that proposed it.
    proposed_lead_company: Mapped[str | None] = mapped_column(String(300))
    proposed_lead_rm: Mapped[str | None] = mapped_column(String(120))
    # The reviewer's corrected copy of the transcript. The verbatim original in
    # raw_transcript is evidence and never changes; regeneration structures THIS
    # text when present. preserved_overrides carries user-confirmed cells across
    # a regeneration so the reviewer's work survives the rebuild.
    corrected_transcript: Mapped[str | None] = mapped_column(Text)
    preserved_overrides: Mapped[dict | None] = mapped_column(JSONB)

    recording_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    capture_id: Mapped[str | None] = mapped_column(String(120))

    # Audio is a MinIO object key, never bytes; erasable while the row is not.
    audio_ref: Mapped[str | None] = mapped_column(String(512))
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    raw_transcript: Mapped[str | None] = mapped_column(Text)          # verbatim, permanent (D6)
    transcript_segments: Mapped[list | None] = mapped_column(JSONB)   # timestamped, with suspect marks
    structured_report: Mapped[dict | None] = mapped_column(JSONB)     # the schema-contract object

    # Denormalised for filters; the atomic edit path keeps them in step with the JSONB.
    sector: Mapped[str | None] = mapped_column(String(60))
    subsector: Mapped[str | None] = mapped_column(String(120))
    meeting_date: Mapped[date | None] = mapped_column(Date)
    language_detected: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued",
                                        server_default="queued")
    # Which pipeline step the row is at (uploaded/transcribed/structured/matched/ready)
    # — the honest Processing screen reads this.
    processing_stage: Mapped[str | None] = mapped_column(String(40))
    processing_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                             server_default="0")

    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("vox_consent_records.id", ondelete="SET NULL"))

    prompt_version: Mapped[str | None] = mapped_column(String(20))
    registry_version: Mapped[str | None] = mapped_column(String(20))

    # Set (with an audit row) when an authorised erasure hard-deleted the content;
    # the consent record and this marker are what remain.
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoxConversationUseCase(Base):
    """Single source of truth for use-case tagging (the join table the spec demands —
    a conversation retrievable under every use case it carries)."""

    __tablename__ = "vox_conversation_use_cases"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("vox_conversations.id", ondelete="CASCADE"),
        primary_key=True)
    use_case: Mapped[str] = mapped_column(String(30), primary_key=True)


class VoxConversationEdit(Base):
    """Append-only field-level audit. UPDATE/DELETE are blocked by trigger; the atomic
    edit path INSERTs one row per changed field, so nothing is ever lost — including
    both sides of a two-device last-write-wins."""

    __tablename__ = "vox_conversation_edits"
    __table_args__ = (
        Index("ix_vox_edits_conversation", "conversation_id", "edited_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("vox_conversations.id", ondelete="CASCADE"),
        nullable=False)
    editor_email: Mapped[str] = mapped_column(String(200), nullable=False)
    editor_name: Mapped[str | None] = mapped_column(String(200))
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)  # e.g. lending.requirement_quantum_cr
    old_value: Mapped[dict | list | str | None] = mapped_column(JSONB)
    new_value: Mapped[dict | list | str | None] = mapped_column(JSONB)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())


def status_rank(status: str) -> int | None:
    """Forward-only rank for the happy path; failure states rank as None."""
    return _STATUS_ORDER.get(status)
