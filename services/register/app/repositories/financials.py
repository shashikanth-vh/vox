"""Version-aware writes for the Financials table (PRISM 'Property 3': versioned).

Financial data is append-only per (entity, statement_type, period_end): submitting a
restated set of numbers creates a *new version* and demotes the previous current one,
rather than overwriting. This preserves exactly what data drove which decision when —
essential for credit audit and regulatory defensibility.

Concurrency: computing the next ``version_no`` under parallel submissions is guarded by
a transaction-scoped PostgreSQL advisory lock keyed on (entity, statement_type,
period_end), so two simultaneous submissions serialise on that key alone (not the whole
table) and can never mint the same version number or leave two rows flagged current.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_ctx
from app.db.base import AuditLog
from app.models.prism import Financial


async def _advisory_lock(session: AsyncSession, *parts: object) -> None:
    key = "|".join(str(p) for p in parts)
    # Two 32-bit keys derived from a stable hash; transaction-scoped (released on commit).
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k), 0)"), {"k": key}
    )


async def create_version(
    session: AsyncSession, tenant_id: uuid.UUID, actor: str, data: dict
) -> Financial:
    entity_id = data["entity_id"]
    statement_type = data["statement_type"]
    period_end: date = data["period_end"]

    await _advisory_lock(session, tenant_id, entity_id, statement_type, period_end.isoformat())

    current_max = (
        await session.execute(
            select(func.coalesce(func.max(Financial.version_no), 0)).where(
                Financial.tenant_id == tenant_id,
                Financial.entity_id == entity_id,
                Financial.statement_type == statement_type,
                Financial.period_end == period_end,
            )
        )
    ).scalar_one()
    next_version = int(current_max) + 1

    # Demote any existing current row for this (entity, statement_type, period_end).
    await session.execute(
        update(Financial)
        .where(
            Financial.tenant_id == tenant_id,
            Financial.entity_id == entity_id,
            Financial.statement_type == statement_type,
            Financial.period_end == period_end,
            Financial.is_current.is_(True),
        )
        .values(is_current=False)
    )

    obj = Financial(**data)
    obj.tenant_id = tenant_id
    obj.version_no = next_version
    obj.is_current = True
    obj.created_by = actor
    obj.updated_by = actor
    session.add(obj)
    await session.flush()
    session.add(
        AuditLog(
            tenant_id=tenant_id, actor=actor, action="create",
            resource_type="financials", resource_id=str(obj.id),
            request_id=request_id_ctx.get(),
            changes={"version_no": next_version, "statement_type": statement_type},
        )
    )
    await session.refresh(obj)
    return obj
