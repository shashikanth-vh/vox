"""A deal's product badges follow its tracker lines.

``deals.is_lending`` / ``is_syndication`` / ``is_asset_mon`` are what the Deals
grid shows as the product badges and what the dashboard's "pipeline" column per
RM counts. They used to be written exactly twice — at lead conversion and by the
drawer's Add product — and never again: a lending line added to an existing deal
from the Lending grid, a mandate deleted, a line moved between deals, all left
the badges telling yesterday's story (the 2 Oct tally found seven deals whose
flags disagreed with the lines behind them).

The rule is now derived: after any write to a tracker line, the deal it belongs
to (and, on a move, the deal it left) has each flag set to "a live line of that
kind exists". A flag set by hand is still honoured until the next line write
touches that deal, which keeps the conversion path exactly as it was.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import exists, select, update

from app.models import AssetMonetisation, Deal, LendingTracker, SyndicationTracker

_LINES = (
    ("is_lending", LendingTracker),
    ("is_syndication", SyndicationTracker),
    ("is_asset_mon", AssetMonetisation),
)


async def sync_deal_flags(session: Any, tenant_id: uuid.UUID, deal_id: Any) -> dict | None:
    """Recompute the three product flags of one deal from its live lines.
    Returns the flags written, or None when the deal does not exist."""
    if deal_id is None:
        return None
    did = uuid.UUID(str(deal_id))
    await session.flush()
    deal = (await session.execute(
        select(Deal.id).where(Deal.id == did, Deal.tenant_id == tenant_id))).first()
    if deal is None:
        return None
    flags: dict[str, bool] = {}
    for flag, model in _LINES:
        flags[flag] = bool((await session.execute(select(exists().where(
            model.tenant_id == tenant_id, model.deal_id == did,
            model.deleted_at.is_(None))))).scalar())
    await session.execute(update(Deal).where(Deal.id == did, Deal.tenant_id == tenant_id)
                          .values(**flags))
    return flags


async def sync_deal_flags_after_write(ctx: Any, obj: Any, event: str,
                                      previous: Any = None) -> None:
    """ResourceSpec.post_write hook for the three tracker resources: ``obj`` is the
    line as written (or as it stood before a delete), ``previous`` the row before an
    update, so a line moved to another deal refreshes both deals."""
    seen: set[str] = set()
    for row in (obj, previous):
        did = getattr(row, "deal_id", None) if row is not None else None
        if did is None or str(did) in seen:
            continue
        seen.add(str(did))
        await sync_deal_flags(ctx.session, ctx.tenant_id, did)
