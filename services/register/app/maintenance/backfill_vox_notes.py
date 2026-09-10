"""Give every VOX-filed interaction its WHOLE meeting summary back.

Until fixes206 the approval step cut the conversation's meeting summary to the
interaction's 300-character headline column and never filled ``notes`` — so the
drawer showed "…a proposal for SBI to take over the ICICI facility plus provide"
and the sentence ended there, while the full text sat one table away on the
conversation's structured report.

This one-time pass finds interactions minted by a VOX approval whose stored
summary is a truncated prefix of the conversation's meeting summary, and writes
the COMPLETE text into ``notes`` (only where notes is empty — a person's own
note is never touched). The headline stays as it is; the row's "more" expansion
now shows everything.

DRY-RUN by default — prints what it would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.backfill_vox_notes
    docker exec compose-register-1 python -m app.maintenance.backfill_vox_notes --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.backfill_vox_notes"


def _meeting_summary(report: dict | None) -> str:
    common = (report or {}).get("common") or {}
    cell = common.get("meeting_summary")
    value = cell.get("value") if isinstance(cell, dict) else None
    return str(value).strip() if value else ""


async def run(apply: bool) -> None:
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models.interactions import Interaction
    from app.models.vox import VoxConversation

    init_engine()
    sm = get_sessionmaker()
    filled = skipped = 0
    async with sm() as session:
        convs = (await session.execute(
            select(VoxConversation)
            .where(VoxConversation.interaction_id.is_not(None),
                   VoxConversation.structured_report.is_not(None))
            .order_by(VoxConversation.created_at.asc())
        )).scalars().all()
        for c in convs:
            full = _meeting_summary(c.structured_report)
            if len(full) <= 300:
                skipped += 1
                continue
            itx = (await session.execute(
                select(Interaction).where(Interaction.id == c.interaction_id,
                                          Interaction.tenant_id == c.tenant_id)
            )).scalar_one_or_none()
            if itx is None or itx.notes:          # gone, or a person already wrote here
                skipped += 1
                continue
            head = (itx.summary or "").rstrip("…").strip()
            if not head or not full.startswith(head[:200]):
                skipped += 1                       # summary was hand-edited — leave it
                continue
            print(f"{'FILL' if apply else 'would fill'} interaction {itx.id}: "
                  f"notes <- {len(full)} chars (headline was {len(itx.summary or '')})")
            filled += 1
            if not apply:
                continue
            itx.notes = full
            itx.updated_by = ACTOR
            session.add(AuditLog(
                tenant_id=itx.tenant_id, actor=ACTOR,
                action="maintenance.vox_notes_backfilled",
                resource_type="interactions", resource_id=str(itx.id),
                changes={"notes": {"from": None, "to": f"{len(full)} chars"}}))
        if apply:
            await session.commit()
        else:
            await session.rollback()
    print(f"\n{'filled' if apply else 'would fill'}: {filled} · left alone: {skipped}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
