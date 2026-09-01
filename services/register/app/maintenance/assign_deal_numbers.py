"""Give every deal its own number, so a mandate's "deal …" chip lands somewhere.

A deal is quoted by its company's code, and a second facility gets "<code>-2" —
but the auto-numberer needs the ENTITY's code as its stem and silently skips
when that code is blank (typical for lead-born companies whose entity got its
code later). Those deals carry deal_no NULL, the Deals grid falls back to the
bare group code, and two deals of one company read identically — the exact
ambiguity the mandate → deal backtrack was built to remove.

This one-time pass assigns deal_no to every deal missing one, oldest first,
using the same allocator the live path uses (never touching a deal that
already has a number, so nothing anyone has quoted changes). Stem = the
entity's code, else the deal's own client code; a deal with neither is
REPORTED, not guessed at.

DRY-RUN by default — prints what it would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.assign_deal_numbers
    docker exec compose-register-1 python -m app.maintenance.assign_deal_numbers --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.assign_deal_numbers"


async def run(apply: bool) -> None:
    from evam_backend_core.crud import allocate_suffixed

    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models import Deal, Entity

    init_engine()
    sm = get_sessionmaker()
    assigned = skipped = stemless = 0
    async with sm() as session:
        rows = (await session.execute(
            select(Deal).where(Deal.deleted_at.is_(None))
            .order_by(Deal.created_at.asc())
        )).scalars().all()
        for deal in rows:
            if (deal.deal_no or "").strip():
                skipped += 1
                continue
            ent_code = (await session.execute(
                select(Entity.code).where(Entity.id == deal.entity_id,
                                          Entity.tenant_id == deal.tenant_id)
            )).scalar()
            stem = (ent_code or "").strip() or (deal.code or "").strip()
            if not stem:
                stemless += 1
                print(f"NO STEM (left untouched — company has no code): deal {deal.id}")
                continue
            number = await allocate_suffixed(session, Deal, deal.tenant_id, "deal_no", stem)
            print(f"{'ASSIGN' if apply else 'would assign'} {number!r:24s} "
                  f"(stem {stem!r}, created {deal.created_at.date()})")
            assigned += 1
            if not apply:
                # Dry run must still see this allocation, or two stem-mates would
                # both print the same plain number — reserve it in-session only.
                deal.deal_no = number
                continue
            deal.deal_no = number
            deal.updated_by = ACTOR
            session.add(AuditLog(
                tenant_id=deal.tenant_id, actor=ACTOR,
                action="maintenance.deal_number_assigned",
                resource_type="deals", resource_id=str(deal.id),
                changes={"deal_no": {"from": None, "to": number}}))
        if apply:
            await session.commit()
        else:
            await session.rollback()
    print(f"\n{'assigned' if apply else 'would assign'}: {assigned} · "
          f"already numbered: {skipped} · no stem: {stemless}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
