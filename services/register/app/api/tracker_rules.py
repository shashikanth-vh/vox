"""Tracker write rules (Field Rules sheet, lending slice).

The Lending grid promises "stage edits stamp the date automatically" — and until
now only the LEDGER IMPORT ever filled ``stage_updated_at``: a stage moved from
the UI (or by the approval workflows, which PATCH the same route) left the column
NULL, the grid quietly displayed the row's generic ``updated_at`` instead, and
the Stage-updated date FILTER — which queries the real column — dropped every
such row. Display and filter told two different stories.

This rule makes the promise true at the register: any write that CHANGES the
stage stamps ``stage_updated_at`` with today's date, unless the caller supplied
an explicit date (imports and corrections keep their own). A write that does not
touch the stage, or repeats the current stage, never restamps.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.models.trackers import LendingTracker


async def lending_pre_write(ctx: Any, body: dict, obj_id: Any) -> None:
    # Creates dump the FULL schema (stage_updated_at: None rides along unset), so an
    # "explicit date" is a present NON-NULL value, not a present key.
    if not body.get("stage") or body.get("stage_updated_at") is not None:
        return                           # no stage in this write, or an explicit date wins
    if obj_id is not None:
        current = (await ctx.session.execute(
            select(LendingTracker.stage).where(LendingTracker.id == obj_id,
                                               LendingTracker.tenant_id == ctx.tenant_id)
        )).scalar()
        if (current or "") == body["stage"]:
            return                       # same stage — the standing date stands
    body["stage_updated_at"] = datetime.now(UTC).date()
