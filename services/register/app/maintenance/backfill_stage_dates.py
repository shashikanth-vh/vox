"""Give every lending line its stage date, so the Stage-updated filter sees it.

Until fixes187 the register never stamped ``stage_updated_at`` on a stage edit —
only the ledger import filled it. Rows staged from the UI (and by the approval
workflows) carry NULL there; the grid papered over it by displaying the row's
generic ``updated_at``, but the Stage-updated DATE FILTER queries the real
column, so those rows vanished from any date range.

This one-time pass fills ``stage_updated_at`` where it is NULL, from the best
evidence the row already carries: the row's last modification date (its
``updated_at``), else its creation date. Rows that already have a date are never
touched, so nothing the ledger imported changes.

DRY-RUN by default — prints what it would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.backfill_stage_dates
    docker exec compose-register-1 python -m app.maintenance.backfill_stage_dates --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.backfill_stage_dates"


async def run(apply: bool) -> None:
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models.trackers import LendingTracker

    init_engine()
    sm = get_sessionmaker()
    filled = skipped = 0
    async with sm() as session:
        rows = (await session.execute(
            select(LendingTracker)
            .where(LendingTracker.deleted_at.is_(None))
            .order_by(LendingTracker.created_at.asc())
        )).scalars().all()
        for r in rows:
            if r.stage_updated_at is not None:
                skipped += 1
                continue
            src = r.updated_at or r.created_at
            if src is None:
                continue
            day = src.date()
            print(f"{'FILL' if apply else 'would fill'} {r.tracker_no or r.id}: "
                  f"stage {r.stage!r} -> stage_updated_at {day}")
            filled += 1
            if not apply:
                continue
            r.stage_updated_at = day
            r.updated_by = ACTOR
            session.add(AuditLog(
                tenant_id=r.tenant_id, actor=ACTOR,
                action="maintenance.stage_date_backfilled",
                resource_type="lending", resource_id=str(r.id),
                changes={"stage_updated_at": {"from": None, "to": str(day)}}))
        if apply:
            await session.commit()
        else:
            await session.rollback()
    print(f"\n{'filled' if apply else 'would fill'}: {filled} · already dated: {skipped}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
