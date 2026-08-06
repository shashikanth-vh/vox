"""One-line durable notifications from the register's own maker-checker lanes.

When a checker decides — approves, returns, rejects — the MAKER has to learn of it
without re-opening the screen: the decision writes an inbox row (the same
``notifications`` table the workflow plane's ops events land in), and the maker's
Today reads it. In-transaction with the decision itself, so a notification can never
exist for a decision that rolled back; idempotent on ``dedupe_key`` so a replay never
double-mails.
"""

from __future__ import annotations

import uuid as _uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.security import RequestContext
from app.models.notifications import Notification


async def notify_maker(ctx: RequestContext, *, recipient: str | None, event: str,
                       title: str, body: str | None = None, severity: str = "info",
                       subject_type: str | None = None, subject_id: str | None = None,
                       dedupe_key: str | None = None) -> None:
    """Mint one inbox notification. Silently skips a missing recipient and never
    notifies the actor about their own act."""
    who = (recipient or "").strip()
    if not who or who == ctx.actor:
        return
    stmt = pg_insert(Notification).values(
        id=_uuid.uuid4(), tenant_id=ctx.tenant_id,
        recipient=who[:200], event=event[:120], severity=severity,
        title=title[:300], body=body,
        subject_type=subject_type, subject_id=subject_id,
        dedupe_key=(dedupe_key[:240] if dedupe_key else None),
        created_by=ctx.actor,
    ).on_conflict_do_nothing()
    await ctx.session.execute(stmt)
