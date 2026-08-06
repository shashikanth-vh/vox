"""Number-series mint — the next value in a controlled numbering series.

Instrument numbers (first user: the credit-note reference sent to committee) come from
a per-tenant register, not from whoever is typing. The mint is an atomic upsert on the
(tenant, series_key) row, so two concurrent sends can never draw the same number —
there is no read-then-write window at all.

Service lane only: the orchestrator mints on the maker's behalf at send time; a human
never calls this directly (they only ever SEE the resulting reference).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.authz.engine import service_ctx
from app.core.errors import ForbiddenError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models import NumberSeries

router = api_router(prefix="/v1/internal/number-series", tags=["Internal"])

_ALLOWED_SERVICES = {"svc_workflows"}


class SeriesNextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_key: str = Field(min_length=1, max_length=200)


@router.post("/next", summary="Mint the next value in a numbering series (service lane)")
async def next_in_series(
        payload: SeriesNextIn, ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError("Only the workflow service principal may mint series numbers.")
    stmt = (
        pg_insert(NumberSeries)
        .values(tenant_id=ctx.tenant_id, series_key=payload.series_key, last_value=1,
                created_by=ctx.actor, updated_by=ctx.actor)
        .on_conflict_do_update(
            constraint="number_series_tenant_key",
            set_={"last_value": NumberSeries.last_value + 1, "updated_by": ctx.actor})
        .returning(NumberSeries.last_value)
    )
    value = (await ctx.session.execute(stmt)).scalar_one()
    await ctx.session.flush()
    return {"series_key": payload.series_key, "value": int(value)}
