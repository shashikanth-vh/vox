"""Interaction timeline writes (polymorphic subject, ATLAS-faithful behaviour).

Creating an interaction, in one transaction:
1. Validates the subject exists (tenant-scoped) and derives ``entity_id`` / ``deal_id``
   from it, so entity- and deal-level timelines aggregate across leads, deals and every
   tracker (lending / syndication / asset-monetisation).
2. Persists the interaction (audit + tenant handling via the generic repository).
3. For a Syndication interaction tied to a lender, updates that lender's response date
   (inbound) or chased date (outbound) — the ATLAS ``resp`` / ``chased`` behaviour.
4. Rolls the latest interaction onto a Lead's ``last_interaction_date`` / ``next_action``.

VOX uses the same path with ``source = "VOX"``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationAppError
from app.models import (
    Interaction,
    Lead,
    SyndicationLender,
)
from app.repositories.crud import CRUDRepository
from app.repositories.subjects import SUBJECTS as _SUBJECTS
from app.repositories.subjects import derive_links as _derive_links
from app.repositories.subjects import load_subject as _load_subject

_repo = CRUDRepository(
    Interaction,
    searchable=["summary", "notes", "outcome", "contact_name", "performed_by", "lender_name"],
    filterable=["subject_type", "subject_id", "interaction_type", "direction", "entity_id",
                "deal_id", "source"],
)


async def create_interaction(
    session: AsyncSession, tenant_id: uuid.UUID, actor: str, data: dict
) -> Interaction:
    stype = data.get("subject_type")
    sid = data.get("subject_id")
    if not stype or not sid:
        raise ValidationAppError("subject_type and subject_id are required.")
    if stype not in _SUBJECTS:
        raise ValidationAppError(
            f"Unknown subject_type '{stype}'. One of: {', '.join(_SUBJECTS)}."
        )

    subject = await _load_subject(session, tenant_id, stype, sid)
    if subject is None:
        raise NotFoundError(f"{stype} '{sid}' not found.")

    data["entity_id"], data["deal_id"] = _derive_links(stype, subject)
    data.setdefault("occurred_at", None)
    if not data["occurred_at"]:
        data["occurred_at"] = datetime.now(UTC)
    if not data.get("performed_by"):
        data["performed_by"] = actor
    if not data.get("source"):
        data["source"] = "Manual"

    # Resolve the syndication lender (by id or by name) for a Syndication interaction.
    lender: SyndicationLender | None = None
    if stype == "Syndication":
        if data.get("syndication_lender_id"):
            lender = (
                await session.execute(
                    select(SyndicationLender).where(
                        SyndicationLender.id == data["syndication_lender_id"],
                        SyndicationLender.tenant_id == tenant_id,
                        SyndicationLender.syndication_id == sid,
                    )
                )
            ).scalar_one_or_none()
        elif data.get("lender_name"):
            lender = (
                await session.execute(
                    select(SyndicationLender).where(
                        SyndicationLender.tenant_id == tenant_id,
                        SyndicationLender.syndication_id == sid,
                        SyndicationLender.lender_name == data["lender_name"],
                    )
                )
            ).scalar_one_or_none()
        if lender is not None:
            data["syndication_lender_id"] = lender.id
            data["lender_name"] = lender.lender_name

    obj = await _repo.create(session, tenant_id, actor, data)

    # Update the lender's response / chased date from the direction — and carry the
    # words with the clock: the note (if any) rolls onto the row's conversation
    # snapshot. A note-less chase/reply clears the snapshot text rather than leaving
    # an older quote against the new date — date and words always tell one story.
    if lender is not None and data.get("direction"):
        d = str(data["direction"]).lower()
        said = (data.get("notes") or "").strip() or None
        if d == "inbound":
            await session.execute(
                update(SyndicationLender).where(SyndicationLender.id == lender.id)
                .values(response_date=obj.occurred_at.date(), last_reply_note=said)
            )
        elif d == "outbound":
            await session.execute(
                update(SyndicationLender).where(SyndicationLender.id == lender.id)
                .values(chased_date=obj.occurred_at.date(), last_chase_note=said)
            )

    # Roll the latest interaction onto the parent lead's summary fields.
    if stype == "Lead":
        vals: dict = {"last_interaction_date": obj.occurred_at.date()}
        if data.get("next_action"):
            vals["next_action"] = data["next_action"]
        if data.get("next_action_date"):
            vals["next_action_date"] = data["next_action_date"]
        await session.execute(
            update(Lead).where(Lead.id == sid, Lead.tenant_id == tenant_id).values(**vals)
        )

    return obj


async def timeline(
    session: AsyncSession, tenant_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID,
    *, limit: int = 100,
) -> list[Interaction]:
    conds = [Interaction.tenant_id == tenant_id, Interaction.deleted_at.is_(None)]
    if subject_type == "Entity":
        # Entity-level timeline aggregates every interaction rolled up to the entity.
        conds.append(Interaction.entity_id == subject_id)
    else:
        conds.append(Interaction.subject_type == subject_type)
        conds.append(Interaction.subject_id == subject_id)
    rows = (
        await session.execute(
            select(Interaction).where(*conds)
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
