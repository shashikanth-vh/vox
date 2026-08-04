"""Seed the tenant's DEFAULT sanction letter template.

The sanction letter is produced from a template the deployment owns (design §D):
resolution is *case-specific upload wins, tenant default otherwise*. The default ships
here — the credit team's own letterhead document (``templates/
sanction_letter_default.docx``), registered as a Data Register document so replacing it
later is an UPLOAD by an Admin, never a deploy.

Stored as a tenant-level document: ``subject_type="Template"`` with the tenant id as the
subject (templates belong to the deployment, not to any one lending line), section
"Sanction", ``doc_type="sanction_template"``, bytes inline (the file is far under the
inline threshold, and a template must survive object storage being down — a sanction
letter that cannot be produced because MinIO is unwell is not acceptable).

Idempotent by checksum: re-running bootstrap never duplicates it, and shipping a NEWER
default in a later release inserts the new version alongside the old (newest wins at
resolution time; the old row stays for the audit trail of letters produced from it).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import Document

_TEMPLATE = Path(__file__).parent / "templates" / "sanction_letter_default.docx"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def seed_sanction_template(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert the shipped default sanction template if this exact file is not already
    on record. Returns 1 when inserted, 0 when already present (or no file shipped)."""
    if not _TEMPLATE.exists():
        return 0
    payload = _TEMPLATE.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    existing = (await session.execute(select(Document.id).where(
        Document.tenant_id == tenant_id,
        Document.subject_type == "Template",
        Document.doc_type == "sanction_template",
        Document.checksum == checksum,
        Document.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
    if existing is not None:
        return 0
    session.add(Document(
        tenant_id=tenant_id,
        subject_type="Template", subject_id=tenant_id,
        section="Sanction", doc_type="sanction_template",
        title="Sanction Letter — EVAM default template",
        status="On File", storage_backend="inline",
        content_type=_DOCX, size_bytes=len(payload), checksum=checksum,
        original_filename="sanction_letter_default.docx",
        inline_content=payload, uploaded_by="bootstrap",
        uploaded_at=datetime.now(UTC),
        notes="Shipped default. Replace by uploading a newer sanction_template — "
              "resolution picks the newest; a case-specific upload on the lending "
              "line overrides it.",
        created_by="bootstrap"))
    # This sessionmaker does not autoflush — flush now, so a second call inside the
    # same bootstrap run sees the row and stays idempotent.
    await session.flush()
    return 1
