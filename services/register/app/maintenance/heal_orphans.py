"""Heal LIVE orphan trackers — product lines the Excel import created with no Deals row.

The MIS import honestly created lending/syndication/asset-monetisation rows whose
companies were missing from the Deals sheet, leaving ``deal_id`` NULL. Lending's live
orphans self-heal on their first committee send; a live SYNDICATION mandate or ASSET
deal never will — its client relationship stays invisible in the Deals grid forever.

This one-time maintenance walks all three trackers and, for each LIVE orphan (state
not in the dead set — Rejected / Dropped / Withdrawn / Closed / Lost), creates the
Deals row for its entity (correct product flag, RM carried over, keyed by the client's
code) and links the tracker back. DEAD orphans are left exactly as they are: minting
deals for closed history would inject dead-looking rows into the live funnel.

DRY-RUN by default — prints what it would do. ``--apply`` executes, each creation
written to the audit log.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.heal_orphans
    docker exec compose-register-1 python -m app.maintenance.heal_orphans --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

DEAD = {"Rejected", "Dropped", "Withdrawn", "Closed", "Lost"}
ACTOR = "maintenance.heal_orphans"


async def run(apply: bool) -> None:
    from evam_backend_core.crud import CRUDRepository

    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models.deals import Deal
    from app.models.registry import Entity
    from app.models.trackers import AssetMonetisation, LendingTracker, SyndicationTracker

    init_engine()
    sm = get_sessionmaker()
    deals = CRUDRepository(Deal)
    specs = [
        ("lending", LendingTracker, "stage", "is_lending"),
        ("syndication", SyndicationTracker, "status", "is_syndication"),
        ("asset_mon", AssetMonetisation, "status", "is_asset_mon"),
    ]
    healed = skipped = 0
    async with sm() as session:
        for label, model, state_field, flag in specs:
            rows = (await session.execute(
                select(model).where(model.deal_id.is_(None),
                                    model.deleted_at.is_(None)))).scalars().all()
            for t in rows:
                state = getattr(t, state_field) or ""
                ent = (await session.execute(
                    select(Entity).where(Entity.id == t.entity_id))).scalar_one_or_none()
                who = f"{label} {t.tracker_no or t.id} · {(ent.legal_name if ent else '?')!s:.48} · {state or '—'}"
                if state in DEAD:
                    print(f"  skip (dead)   {who}")
                    skipped += 1
                    continue
                # ONE row per client relationship: when the entity already has a live
                # deal, LINK it (and raise the product flag) — a second deal for the
                # same relationship is the exact duplication the grid promises not
                # to show.
                existing = (await session.execute(
                    select(Deal).where(Deal.tenant_id == t.tenant_id,
                                       Deal.entity_id == t.entity_id,
                                       Deal.deleted_at.is_(None)))).scalars().first()
                if not apply:
                    verb = (f"WOULD link deal {existing.deal_no}" if existing
                            else "WOULD create a deal")
                    print(f"  {verb:<28}  {who}")
                    healed += 1
                    continue
                if existing:
                    deal = existing
                    setattr(deal, flag, True)
                    deal.updated_by = ACTOR
                else:
                    deal = await deals.create(session, t.tenant_id, ACTOR, {
                        "entity_id": t.entity_id,
                        "code": ent.code if ent else None,
                        flag: True,
                        "rm": getattr(t, "rm", None),
                    })
                    await session.flush()
                t.deal_id = deal.id
                t.updated_by = ACTOR
                session.add(AuditLog(
                    tenant_id=t.tenant_id, actor=ACTOR, action="maintenance.heal_orphan_deal",
                    resource_type=model.__tablename__, resource_id=str(t.id),
                    changes={"tracker_no": t.tracker_no, "deal_id": str(deal.id),
                             "deal_no": deal.deal_no, "state": state,
                             "linked_existing": bool(existing)}))
                print(f"  healed        {who}  ->  deal {deal.deal_no}"
                      + (" (existing)" if existing else " (created)"))
                healed += 1
        if apply:
            await session.commit()
    mode = "APPLIED" if apply else "DRY-RUN (nothing written — rerun with --apply)"
    print(f"\n{mode}: {healed} healed, {skipped} left as dead history.")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
