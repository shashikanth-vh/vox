"""Documents — the catalog behind ATLAS's "Data Register".

Two tables that together realise the ATLAS Data Register modal (a per-subject document
checklist with upload slots + progress):

* ``document_checklist`` — the **template**: which document slots exist, grouped into
  sections (KYC & Constitutional, Financials, …) and flagged required/optional. Per-tenant
  configurable reference data; seeded with Evam's default checklist.
* ``documents`` — the **catalog** (one row per document on file). It is the *index*: it
  stores document **references** (a ``storage_uri`` into object storage) plus metadata
  (name, size, checksum, owner, time). Large file **bytes live in object storage**
  (S3/MinIO), never in this table; only small files may be kept inline (``inline_content``)
  as a prototype fallback until object storage is wired — mirroring the ATLAS behaviour
  ("files up to 400 KB stay viewable, larger files are recorded").

Like interactions, a document attaches to a **polymorphic subject** (ATLAS refType/refId)
and denormalises ``entity_id`` / ``deal_id`` so an entity's or deal's document set spans
everything logged against its leads, deals and trackers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class DocumentChecklistItem(RegisterBase):
    """One slot in the Data Register checklist template (per-tenant config).

    ``applies_to`` is a subject type ("Lead", "Entity", …) or ``"*"`` for "every subject".
    The rollup endpoint returns the items whose ``applies_to`` matches, grouped by section.
    """

    __tablename__ = "document_checklist"
    __table_args__ = (
        UniqueConstraint("tenant_id", "applies_to", "slot_key", name="document_checklist_unique"),
        Index("ix_doc_checklist_applies", "tenant_id", "applies_to"),
    )

    applies_to: Mapped[str] = mapped_column(
        String(30), nullable=False, default="*", server_default="*"
    )
    section: Mapped[str] = mapped_column(String(80), nullable=False)  # ref: Document Section
    section_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    slot_key: Mapped[str] = mapped_column(String(60), nullable=False)  # machine key, e.g. "coi"
    label: Mapped[str] = mapped_column(String(200), nullable=False)  # display, e.g. "Company PAN"
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    hint: Mapped[str | None] = mapped_column(Text)


class Document(RegisterBase):
    """A document on file — a **reference** (plus metadata), not the bytes.

    The bytes belong in object storage; ``storage_uri`` points at them. ``inline_content``
    is a bounded small-file fallback so the checklist works before object storage is wired.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_subject", "tenant_id", "subject_type", "subject_id"),
        Index("ix_documents_entity", "tenant_id", "entity_id"),
        Index("ix_documents_slot", "tenant_id", "subject_type", "subject_id", "slot_key"),
    )

    # Polymorphic subject (ATLAS refType/refId). No DB FK on subject_id — it can point at
    # any record type; the write path validates it exists (tenant-scoped).
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # Denormalised roll-up dimensions (real FKs) so entity/deal document sets aggregate.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE")
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL")
    )

    # Which checklist slot this fills (null = an ad-hoc "Add another document").
    section: Mapped[str | None] = mapped_column(String(80))  # ref: Document Section
    slot_key: Mapped[str | None] = mapped_column(String(60))
    doc_type: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="On File", server_default="On File"
    )  # ref: Document Status

    # Where the bytes are. storage_uri = object-storage reference (s3://… / https://…).
    storage_backend: Mapped[str | None] = mapped_column(String(20))  # s3 / inline / external
    storage_uri: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(64))  # sha256 hex
    original_filename: Mapped[str | None] = mapped_column(String(300))
    # Bounded small-file fallback (prototype); NULL for object-storage-backed or
    # metadata-only records. Real deployments keep bytes in object storage.
    inline_content: Mapped[bytes | None] = mapped_column(LargeBinary)

    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
