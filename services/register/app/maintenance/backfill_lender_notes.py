"""Backfill each lender row's conversation snapshot from the interactions ledger.

The chase list now shows the last chase note and the last lender reply on every
lender row (``last_chase_note`` / ``last_reply_note``, rolled on by each new
interaction write). Rows that predate the columns would start blank even though
the words exist in the timeline — this one-time pass copies the most recent
noted outbound and inbound interaction per lender row, so the board shows the
conversation history the desk already logged.

Only interactions carrying actual note text are considered: a bare "chased"
with no words stays a date-only fact, exactly as a live write would leave it.
Rows whose snapshot is already populated are never touched.

DRY-RUN by default — prints what it would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.backfill_lender_notes
    docker exec compose-register-1 python -m app.maintenance.backfill_lender_notes --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.backfill_lender_notes"


async def run(apply: bool) -> None:
    from app.db.session import get_sessionmaker, init_engine
    from app.models import Interaction
    from app.models.trackers import SyndicationLender

    init_engine()
    sm = get_sessionmaker()
    filled = skipped = bare = 0
    async with sm() as session:
        rows = (await session.execute(
            select(SyndicationLender).where(SyndicationLender.deleted_at.is_(None))
        )).scalars().all()
        for row in rows:
            wants_chase = row.last_chase_note is None
            wants_reply = row.last_reply_note is None
            if not wants_chase and not wants_reply:
                skipped += 1
                continue
            # The lender's own timeline, newest first. Older rows were logged before
            # syndication_lender_id existed, so match by (mandate, lender name) too.
            inters = (await session.execute(
                select(Interaction).where(
                    Interaction.tenant_id == row.tenant_id,
                    Interaction.deleted_at.is_(None),
                    Interaction.subject_type == "Syndication",
                    Interaction.subject_id == row.syndication_id,
                    Interaction.lender_name == row.lender_name,
                ).order_by(Interaction.occurred_at.desc())
            )).scalars().all()
            chase = reply = None
            for it in inters:
                text = (it.notes or "").strip()
                if not text:
                    continue
                d = str(it.direction or "").lower()
                if d == "outbound" and chase is None:
                    chase = text
                elif d == "inbound" and reply is None:
                    reply = text
                if chase is not None and reply is not None:
                    break
            if chase is None and reply is None:
                bare += 1
                continue
            filled += 1
            tag = "FILL " if apply else "would fill"
            if wants_chase and chase:
                print(f"{tag} {row.lender_name!r:40s} chase: {chase[:70]!r}")
            if wants_reply and reply:
                print(f"{tag} {row.lender_name!r:40s} reply: {reply[:70]!r}")
            if not apply:
                continue
            if wants_chase and chase:
                row.last_chase_note = chase
            if wants_reply and reply:
                row.last_reply_note = reply
            row.updated_by = ACTOR
        if apply:
            await session.commit()
    print(f"\n{'filled' if apply else 'would fill'}: {filled} lender row(s) · "
          f"already populated: {skipped} · no noted interactions: {bare}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
