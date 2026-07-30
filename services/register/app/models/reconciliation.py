"""Import reconciliation — a durable, operational record of every governed-import row that was
RETAINED despite missing its stage's mandatory data. It carries the batch/checksum lineage, the
missing fields, the ORIGINAL imported values (preserved), an owner, and an open/closed status an
Admin resolves — so an incomplete historical import is a tracked work item, never a silently
"complete"-looking record."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import RegisterBase


class ImportReconciliationItem(RegisterBase):
    __tablename__ = "import_reconciliation_items"

    import_batch_id: Mapped[str] = mapped_column(String(80), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(80))
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    sheet: Mapped[str | None] = mapped_column(String(80))
    company: Mapped[str | None] = mapped_column(String(200))
    stage_field: Mapped[str | None] = mapped_column(String(40))
    stage_value: Mapped[str | None] = mapped_column(String(80))
    missing_fields: Mapped[list | None] = mapped_column(JSONB)
    original_values: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Required",
                                        server_default="Required")
    owner: Mapped[str | None] = mapped_column(String(120))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
