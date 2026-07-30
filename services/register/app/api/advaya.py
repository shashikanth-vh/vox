"""Internal Advaya-handoff endpoints — the authoritative record of a disbursement handoff outcome.

Restricted to the workflow service principal (``svc_workflows``): the Advaya-handoff workflow records
the OUTCOME here (Accepted / Rejected) once Advaya has responded, and the ``advaya_acknowledgement``
evidence that gates ``Disbursed`` is verified against an ``Accepted`` row (matching payload digest).
Single-winner on ``(tenant, handoff_key)`` — a replay returns the original; the row is immutable.

    POST /v1/internal/advaya-handoffs                record a handoff outcome (single-winner)
    GET  /v1/internal/advaya-handoffs/{handoff_key}  read it (404 if none)
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.authz.engine import service_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.advaya import AdvayaHandoff

router = api_router()

_ALLOWED_SERVICES = {"svc_workflows"}


class HandoffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handoff_key: str = Field(min_length=1, max_length=200)
    lending_id: str = Field(min_length=1, max_length=64)
    payload_sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    status: str = Field(pattern="^(Accepted|Rejected)$")
    acknowledgement_id: str | None = Field(default=None, max_length=200)
    workflow_id: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    note: str | None = None


def _require_service() -> None:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError("Only the workflow service principal may record Advaya handoffs.")


def _serialize(row: AdvayaHandoff) -> dict[str, Any]:
    return {
        "id": str(row.id), "handoff_key": row.handoff_key, "lending_id": row.lending_id,
        "payload_sha256": row.payload_sha256, "status": row.status,
        "acknowledgement_id": row.acknowledgement_id, "workflow_id": row.workflow_id,
        "run_id": row.run_id, "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/v1/internal/advaya-handoffs", tags=["Internal"], status_code=201,
             summary="Record an Advaya handoff outcome (single-winner)")
async def record_handoff(payload: HandoffIn, ctx: RequestContext = Depends(get_context)):
    _require_service()
    values = {
        "tenant_id": ctx.tenant_id, "handoff_key": payload.handoff_key,
        "lending_id": payload.lending_id, "payload_sha256": payload.payload_sha256,
        "status": payload.status, "acknowledgement_id": payload.acknowledgement_id,
        "workflow_id": payload.workflow_id, "run_id": payload.run_id, "note": payload.note,
        "created_by": ctx.actor,
    }
    won = (await ctx.session.execute(
        pg_insert(AdvayaHandoff).values(**values)
        .on_conflict_do_nothing(constraint="advaya_handoffs_tenant_key")
        .returning(AdvayaHandoff.id))).scalar_one_or_none()
    if won is not None:
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=ctx.actor, action="advaya.handoff",
            resource_type="advaya_handoffs", resource_id=str(won),
            request_id=request_id_ctx.get(),
            changes={"handoff_key": payload.handoff_key, "lending_id": payload.lending_id,
                     "status": payload.status, "acknowledgement_id": payload.acknowledgement_id}))
        row = (await ctx.session.execute(
            select(AdvayaHandoff).where(AdvayaHandoff.id == won))).scalar_one()
        return _serialize(row)
    existing = (await ctx.session.execute(select(AdvayaHandoff).where(
        AdvayaHandoff.tenant_id == ctx.tenant_id,
        AdvayaHandoff.handoff_key == payload.handoff_key))).scalar_one()
    # Replaying the SAME outcome is idempotent; a contradictory one is refused.
    if existing.status == payload.status and existing.payload_sha256 == payload.payload_sha256:
        return _serialize(existing)
    raise ConflictError(
        f"Handoff '{payload.handoff_key}' already recorded as {existing.status!r}; "
        "a contradictory outcome is refused.")


@router.get("/v1/internal/advaya-handoffs/{handoff_key:path}", tags=["Internal"],
            summary="Read a recorded Advaya handoff outcome")
async def get_handoff(handoff_key: str, ctx: RequestContext = Depends(get_context)):
    _require_service()
    row = (await ctx.session.execute(select(AdvayaHandoff).where(
        AdvayaHandoff.tenant_id == ctx.tenant_id,
        AdvayaHandoff.handoff_key == handoff_key))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No Advaya handoff recorded for '{handoff_key}'.")
    return _serialize(row)
