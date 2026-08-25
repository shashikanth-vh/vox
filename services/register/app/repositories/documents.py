"""Document catalog writes + the Data Register rollup (ATLAS-faithful behaviour).

Registering a document, in one transaction:
1. Validates the subject exists (tenant-scoped) and derives ``entity_id`` / ``deal_id``
   from it (shared with interactions), so entity- and deal-level document sets aggregate
   across a company's leads, deals and trackers.
2. Resolves where the bytes are: an inline small-file (``content_base64``, bounded), an
   object-storage reference (``storage_uri``), or a metadata-only record (name/size only).
3. Persists the catalog row (audit + tenant handling via the generic repository).

The Data Register rollup joins the checklist *template* against the documents *on file*
for a subject and returns exactly what the ATLAS modal renders: sections, each slot marked
on-file/pending, section counts and an overall percent-complete.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage as storage_mod
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationAppError
from app.models import Document, DocumentChecklistItem
from app.repositories.crud import CRUDRepository
from app.repositories.subjects import SUBJECTS, derive_links, load_subject
from app.schemas import resources as s

_repo = CRUDRepository(
    Document,
    searchable=["title", "doc_type", "original_filename", "notes"],
    filterable=["subject_type", "subject_id", "section", "slot_key", "status",
                "entity_id", "deal_id", "is_required"],
)


def _resolve_bytes(data: dict) -> None:
    """Interpret ``content_base64`` (if given) into inline storage, mutating ``data``.

    Enforces the inline size ceiling; larger files must go to object storage and be
    registered by ``storage_uri`` instead. Fills size/checksum/backend when we hold bytes.
    """
    raw_b64 = data.pop("content_base64", None)
    if not raw_b64:
        # No inline bytes: classify the record by whatever reference was supplied.
        if data.get("storage_uri") and not data.get("storage_backend"):
            uri = str(data["storage_uri"])
            data["storage_backend"] = "s3" if uri.startswith("s3://") else "external"
        return

    if data.get("storage_uri"):
        raise ValidationAppError(
            "Provide either content_base64 (inline) or storage_uri (object storage), not both."
        )
    try:
        blob = base64.b64decode(str(raw_b64), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationAppError(f"content_base64 is not valid base64: {exc}") from exc

    limit = get_settings().documents_inline_max_bytes
    if len(blob) > limit:
        raise ValidationAppError(
            f"Inline document is {len(blob)} bytes, over the {limit}-byte limit. "
            "Upload it to object storage and register it with storage_uri instead."
        )
    data["inline_content"] = blob
    data["storage_backend"] = "inline"
    data["size_bytes"] = len(blob)
    # payload carries checksum=None explicitly, so setdefault won't fill it — check truthiness.
    if not data.get("checksum"):
        data["checksum"] = hashlib.sha256(blob).hexdigest()


async def register_document(
    session: AsyncSession, tenant_id: uuid.UUID, actor: str, data: dict
) -> Document:
    stype = data.get("subject_type")
    sid = data.get("subject_id")
    if not stype or not sid:
        raise ValidationAppError("subject_type and subject_id are required.")
    if stype not in SUBJECTS:
        raise ValidationAppError(
            f"Unknown subject_type '{stype}'. One of: {', '.join(SUBJECTS)}."
        )

    subject = await load_subject(session, tenant_id, stype, sid)
    if subject is None:
        raise NotFoundError(f"{stype} '{sid}' not found.")

    data["entity_id"], data["deal_id"] = derive_links(stype, subject)
    if not data.get("uploaded_by"):
        data["uploaded_by"] = actor
    if not data.get("uploaded_at"):
        data["uploaded_at"] = datetime.now(UTC)
    if not data.get("status"):
        data["status"] = "On File"
    # Defense in depth for the multipart upload path (which bypasses DocumentCreate):
    # lifecycle statuses are outcomes of the validate/reject/replace endpoints, never a
    # registration input.
    try:
        s._direct_document_status(str(data["status"]))
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from None

    _resolve_bytes(data)
    return await _repo.create(session, tenant_id, actor, data)


def _object_key(
    tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID, filename: str | None
) -> str:
    """A collision-free, path-like object key: tenant/subject/uuid-safeName."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", (filename or "file")).strip("._") or "file"
    return f"{tenant_id}/{subject_type}/{subject_id}/{uuid.uuid4().hex}-{safe}"


async def store_and_register(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    content_type: str | None,
    slot_key: str | None = None,
    section: str | None = None,
    title: str | None = None,
    doc_type: str | None = None,
    is_required: bool = False,
    status: str = "On File",
    notes: str | None = None,
) -> Document:
    """Store uploaded bytes in the configured object store (or inline) and catalog them.

    Validates the subject *before* uploading, so we never leave an orphan object in the
    bucket for a subject that doesn't exist.
    """
    if subject_type not in SUBJECTS:
        raise ValidationAppError(
            f"Unknown subject_type '{subject_type}'. One of: {', '.join(SUBJECTS)}."
        )
    if await load_subject(session, tenant_id, subject_type, subject_id) is None:
        raise NotFoundError(f"{subject_type} '{subject_id}' not found.")

    payload: dict[str, Any] = {
        "subject_type": subject_type, "subject_id": subject_id,
        "section": section, "slot_key": slot_key, "doc_type": doc_type,
        "title": title or filename or slot_key or "document",
        "is_required": is_required, "status": status,
        "content_type": content_type, "size_bytes": len(data),
        "checksum": hashlib.sha256(data).hexdigest(),
        "original_filename": filename, "notes": notes,
    }

    store = storage_mod.get_storage()
    if store is not None:
        key = _object_key(tenant_id, subject_type, subject_id, filename)
        stored = await store.put(key, data, content_type)
        payload["storage_uri"] = stored.uri
        payload["storage_backend"] = stored.backend
    else:
        # No object store configured → inline fallback (bounded by the size ceiling).
        payload["content_base64"] = base64.b64encode(data).decode()

    new = await register_document(session, tenant_id, actor, payload)
    # A SLOT holds one live document — that is what the checklist row shows. Uploading
    # into an occupied slot IS the replace: every other live doc in the slot becomes
    # Superseded, chained to the successor (nothing deleted; the paper trail stays).
    # Without this, "Replace" quietly stacked a second live row and the dialog kept
    # showing the old file — the field's "replace is not working".
    if slot_key and section:
        from sqlalchemy import select
        others = (await session.execute(select(Document).where(
            Document.tenant_id == tenant_id,
            Document.subject_type == subject_type,
            Document.subject_id == subject_id,
            Document.section == section,
            Document.slot_key == slot_key,
            Document.id != new.id,
            Document.deleted_at.is_(None),
            Document.status.notin_(("Superseded", "Removed", "Rejected"))))).scalars().all()
        for old in others:
            old.status = "Superseded"
            old.superseded_by = new.id
            old.updated_by = actor
        if others:
            await session.flush()
    return new


# --------------------------------------------------------------------------- #
# Access scope — a company's documents are shared across all its records
# --------------------------------------------------------------------------- #
async def _resolve_scope(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    scope: str,
    verify_subject: bool,
) -> tuple[bool, uuid.UUID | None, Any]:
    """Work out whether to read documents company-wide (by entity) or for this one record.

    Returns ``(use_entity, entity_id, subject)``. ``use_entity`` is True when the record
    resolves to a company and the caller didn't force ``scope="subject"``.
    """
    if subject_type not in SUBJECTS:
        raise ValidationAppError(
            f"Unknown subject_type '{subject_type}'. One of: {', '.join(SUBJECTS)}."
        )
    subject = await load_subject(session, tenant_id, subject_type, subject_id)
    if verify_subject and subject is None:
        raise NotFoundError(f"{subject_type} '{subject_id}' not found.")
    entity_id = derive_links(subject_type, subject)[0] if subject is not None else None
    use_entity = scope != "subject" and entity_id is not None
    return use_entity, entity_id, subject


def _doc_scope_conditions(
    use_entity: bool, entity_id: uuid.UUID | None, subject_type: str, subject_id: uuid.UUID
) -> list[Any]:
    """WHERE conditions selecting a company's documents (entity scope) or one record's."""
    if use_entity:
        return [Document.entity_id == entity_id]
    return [Document.subject_type == subject_type, Document.subject_id == subject_id]


async def documents_for_subject(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    *,
    scope: str = "auto",
    limit: int = 200,
) -> list[Document]:
    """The documents visible from a record — company-wide by default (see ``data_register``)."""
    use_entity, entity_id, _ = await _resolve_scope(
        session, tenant_id, subject_type, subject_id, scope, verify_subject=True
    )
    rows = (
        await session.execute(
            select(Document)
            .where(
                Document.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
                *_doc_scope_conditions(use_entity, entity_id, subject_type, subject_id),
            )
            .order_by(Document.uploaded_at.desc().nullslast(), Document.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def data_register(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    *,
    scope: str = "auto",
    verify_subject: bool = True,
) -> dict[str, Any]:
    """The ATLAS "Data Register" view for one subject: checklist template joined with the
    documents on file, grouped into sections with per-section and overall progress.

    ``scope`` controls which documents count:
    * ``"auto"`` (default) / ``"entity"`` — every document for the *company* this record
      belongs to, no matter which record (lead / deal / lending / syndication / …) it was
      uploaded against. Upload the COI once against the lead, and it shows here for the
      deal, the lending tracker and the entity alike.
    * ``"subject"`` — only documents attached to this exact record.
    Records with no entity (an unlinked lead, a counterparty) always fall back to subject
    scope.
    """
    use_entity, entity_id, _ = await _resolve_scope(
        session, tenant_id, subject_type, subject_id, scope, verify_subject
    )

    items = (
        await session.execute(
            select(DocumentChecklistItem)
            .where(
                DocumentChecklistItem.tenant_id == tenant_id,
                DocumentChecklistItem.is_active.is_(True),
                DocumentChecklistItem.deleted_at.is_(None),
                or_(
                    DocumentChecklistItem.applies_to == "*",
                    DocumentChecklistItem.applies_to == subject_type,
                ),
            )
            .order_by(
                DocumentChecklistItem.section_order,
                DocumentChecklistItem.section,
                DocumentChecklistItem.sort_order,
                DocumentChecklistItem.label,
            )
        )
    ).scalars().all()

    docs = (
        await session.execute(
            select(Document)
            .where(
                Document.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
                *_doc_scope_conditions(use_entity, entity_id, subject_type, subject_id),
            )
            .order_by(Document.uploaded_at.desc().nullslast(), Document.created_at.desc())
        )
    ).scalars().all()

    docs_by_slot: dict[str, list[Document]] = {}
    ad_hoc: list[Document] = []
    for d in docs:
        if d.slot_key:
            docs_by_slot.setdefault(d.slot_key, []).append(d)
        else:
            ad_hoc.append(d)

    def _dump(d: Document) -> dict:
        return s.DocumentRead.model_validate(d).model_dump(mode="json")

    sections: list[dict[str, Any]] = []
    section_index: dict[str, dict[str, Any]] = {}
    template_slots: set[str] = set()
    req_total = req_on_file = 0

    for it in items:
        template_slots.add(it.slot_key)
        sec = section_index.get(it.section)
        if sec is None:
            sec = {
                "section": it.section, "section_order": it.section_order,
                "required_total": 0, "required_on_file": 0, "items": [],
            }
            section_index[it.section] = sec
            sections.append(sec)

        slot_docs = docs_by_slot.get(it.slot_key, [])
        on_file = len(slot_docs) > 0
        if it.is_required:
            req_total += 1
            sec["required_total"] += 1
            if on_file:
                req_on_file += 1
                sec["required_on_file"] += 1
        sec["items"].append({
            "slot_key": it.slot_key, "label": it.label, "section": it.section,
            "is_required": it.is_required, "hint": it.hint,
            "on_file": on_file, "count": len(slot_docs),
            "documents": [_dump(x) for x in slot_docs],
        })

    # Documents whose slot_key isn't in the template (e.g. a retired slot) still surface.
    for slot, extra in docs_by_slot.items():
        if slot not in template_slots:
            ad_hoc.extend(extra)

    percent = round(req_on_file / req_total * 100) if req_total else 100
    return {
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "scope": "entity" if use_entity else "subject",
        "entity_id": str(entity_id) if entity_id else None,
        "required_total": req_total,
        "required_on_file": req_on_file,
        "percent_complete": percent,
        "document_count": len(docs),
        "sections": sections,
        "ad_hoc": [_dump(x) for x in ad_hoc],
    }
