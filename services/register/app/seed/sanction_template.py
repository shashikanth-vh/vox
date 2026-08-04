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

_DIR = Path(__file__).parent / "templates"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# doc_type -> (file, section, title, notes). Same resolution rule for all of them:
# case-specific upload wins, tenant default otherwise; replacing a default is an
# UPLOAD of a newer document of the same doc_type, never a deploy.
_DEFAULTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("sanction_template", "sanction_letter_default.docx", "Sanction",
     "Sanction Letter — EVAM default template",
     "Shipped default. Replace by uploading a newer sanction_template — resolution "
     "picks the newest; a case-specific upload on the lending line overrides it."),
    ("cam_prompt", "cam_prompt_default.docx", "CAM",
     "CAM master prompt — EVAM default (v2)",
     "The credit team's drafting brief for the CAM workbench. The workbench offers it "
     "whenever no case-specific prompt is uploaded; replace by uploading a newer "
     "cam_prompt."),
    ("cam_example", "cam_example_default.docx", "CAM",
     "CAM example — Pinnacle Lithium Power (format reference)",
     "A finished CAM the engine can be shown as a format reference alongside the "
     "source documents."),
)


async def seed_sanction_template(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert every shipped default template that is not already on record (by
    checksum). Returns how many rows were inserted; 0 = everything already present."""
    inserted = 0
    for doc_type, filename, section, title, notes in _DEFAULTS:
        path = _DIR / filename
        if not path.exists():
            continue
        payload = path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        existing = (await session.execute(select(Document.id).where(
            Document.tenant_id == tenant_id,
            Document.subject_type == "Template",
            Document.doc_type == doc_type,
            Document.checksum == checksum,
            Document.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(Document(
            tenant_id=tenant_id,
            subject_type="Template", subject_id=tenant_id,
            section=section, doc_type=doc_type,
            title=title,
            status="On File", storage_backend="inline",
            content_type=_DOCX, size_bytes=len(payload), checksum=checksum,
            original_filename=filename,
            inline_content=payload, uploaded_by="bootstrap",
            uploaded_at=datetime.now(UTC),
            notes=notes,
            created_by="bootstrap"))
        inserted += 1
    if inserted:
        # This sessionmaker does not autoflush — flush now, so a second call inside
        # the same bootstrap run sees the rows and stays idempotent.
        await session.flush()
    return inserted
