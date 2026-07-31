"""The internal workflow-decision endpoints — the single source of truth for a
lead-conversion approve/reject.

These are NOT general CRUD and NOT reachable by ordinary callers: they are restricted to the
workflow **service principal** (``svc_workflows``), and a write additionally requires a
verified **delegated approver** context (the signed internal context the orchestrator mints),
from which the provenance is taken server-side — never a client-supplied field.

    POST /v1/internal/decisions               record a decision (single-winner; 409 on opposite)
    GET  /v1/internal/decisions/{wf_id}       read the decision for a workflow (404 if none)
    POST /v1/internal/decisions/deliveries/claim   reconciler: claim due pending deliveries
    POST /v1/internal/decisions/{wf_id}/delivery   reconciler: mark a delivery applied/retry/dead
    GET  /v1/internal/decisions/deliveries/stats   reconciler: delivery counts (for metrics)
    GET  /v1/internal/tenants                  reconciler: active tenant codes to scan

The single-winner guarantee is enforced by the database: ``UNIQUE (tenant_id, workflow_id)``.
The FIRST decision wins atomically; replaying the SAME decision returns the original record;
the OPPOSITE decision is rejected with 409 — even after the workflow has completed.

Recording a decision ALSO creates a delivery-outbox row in the SAME transaction, so a durable
"deliver me" record exists the instant the decision is accepted — the reconciler drives it to
``applied`` or ``dead``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.authz.engine import service_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.decisions import WorkflowDecision, WorkflowDecisionOutbox
from app.models.system import Tenant

router = api_router()

# Only the workflow plane may record or read conversion decisions.
_ALLOWED_SERVICES = {"svc_workflows"}

# Fixed delivery-update statements — single constant literals (adjacent-literal concatenation,
# NOT interpolation; all inputs are bound params). The WHERE guard fences on the current claim
# token and requires status='pending', so a stalled claimant can't regress a terminal row.
_DELIVERY_SQL = {
    "applied": "UPDATE workflow_decision_outbox SET status='applied', applied_at=now(),"
               " leased_until=NULL, claim_token=NULL, last_error=NULL"
               " WHERE tenant_id = :tid AND workflow_id = :wf AND status = 'pending'"
               " AND claim_token = CAST(:token AS uuid) RETURNING id",
    "dead": "UPDATE workflow_decision_outbox SET status='dead', leased_until=NULL,"
            " claim_token=NULL, last_error=:err"
            " WHERE tenant_id = :tid AND workflow_id = :wf AND status = 'pending'"
            " AND claim_token = CAST(:token AS uuid) RETURNING id",
    "retry": "UPDATE workflow_decision_outbox SET status='pending', leased_until=NULL,"
             " claim_token=NULL, last_error=:err,"
             " next_attempt_at = now() + make_interval(secs => :backoff)"
             " WHERE tenant_id = :tid AND workflow_id = :wf AND status = 'pending'"
             " AND claim_token = CAST(:token AS uuid) RETURNING id",
}


# Roles that constitute Credit Committee authority for a governance decision.
_COMMITTEE_AUTHORITY = {"Credit Head", "Management", "Admin"}


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(max_length=200)
    # Business decisions are Approved/Rejected; CONTROL records (kind="control") capture the
    # run-lifecycle actions — cancel, return-for-information, resubmit — with the same
    # durability and single-winner semantics, so every human action on a run has an
    # immutable, subject-bound audit record the workflow can verify.
    decision: str = Field(pattern="^(Approved|Rejected|Cancelled|ReturnedForInformation|Resubmitted)$")
    lead_id: str | None = Field(default=None, max_length=64)
    note: str | None = None
    # ``kind`` distinguishes a lead-conversion decision from a governance (Credit Committee)
    # one and from a run-control record; a committee decision binds to a subject and requires
    # committee authority.
    kind: str = Field(default="lead_conversion", pattern="^(lead_conversion|committee|control)$")
    subject_type: str | None = Field(default=None, max_length=40)
    subject_id: str | None = Field(default=None, max_length=64)
    run_id: str | None = Field(default=None, max_length=200)
    committee_reference: str | None = Field(default=None, max_length=500)
    sanction_letter_reference: str | None = Field(default=None, max_length=500)


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=200)
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class RedriveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A recovery reason is MANDATORY — redrive re-activates a financial decision, so the audit
    # must say WHY. min_length=1 rejects an absent/empty string at validation; we also strip and
    # re-check so a whitespace-only reason is refused.
    reason: str = Field(min_length=1, max_length=2000)
    # Optional incident / change ticket reference, preserved in the audit event.
    ticket: str | None = Field(default=None, max_length=200)


class DeliveryUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # applied → the run converted with this outcome; retry → still pending, back off;
    # dead → give up (authoritative terminal mismatch / invalid reference).
    status: str = Field(pattern="^(applied|retry|dead)$")
    # The token of the CURRENT claim — the update only applies if it still matches, so a stale
    # claimant whose lease expired cannot overwrite a row another replica has re-claimed.
    claim_token: str = Field(min_length=1)
    error: str | None = None
    backoff_seconds: int = Field(default=60, ge=0, le=86400)


def _require_service() -> None:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError("Only the workflow service principal may access decisions.")


def _serialize(row: WorkflowDecision) -> dict:
    return {
        "id": str(row.id), "workflow_id": row.workflow_id, "lead_id": row.lead_id,
        "subject_type": row.subject_type, "subject_id": row.subject_id, "run_id": row.run_id,
        "committee_reference": row.committee_reference,
        "sanction_letter_reference": row.sanction_letter_reference,
        "decision": row.decision, "decided_by": row.decided_by,
        "decided_by_id": row.decided_by_id, "roles": list(row.roles or []),
        "operations": dict(row.operations or {}), "views": dict(row.views or {}),
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/v1/internal/decisions", tags=["Internal"], status_code=201,
             summary="Record a lead-conversion decision (single-winner)")
async def record_decision(payload: DecisionIn, ctx: RequestContext = Depends(get_context)):
    _require_service()
    # Provenance is SERVER-CONTROLLED: it comes from the verified delegated approver in the
    # signed internal context, not from any request body field. No delegated identity → refuse.
    if ctx.user is None or not ctx.user.email:
        raise ForbiddenError(
            "A verified approver identity (signed internal context) is required to record a "
            "decision.")
    approver = ctx.user
    # A COMMITTEE decision is bound to a subject and reserved to committee authority — a BD Head or
    # RM identity cannot record one, even through the workflow plane.
    if payload.kind == "committee":
        if not (payload.subject_type and payload.subject_id):
            raise ValidationAppError(
                "A committee decision must bind to a subject_type + subject_id.")
        if not (set(approver.roles or []) & _COMMITTEE_AUTHORITY):
            raise ForbiddenError(
                "Recording a Credit Committee decision requires committee authority "
                f"(one of {sorted(_COMMITTEE_AUTHORITY)}).")
    # Control records and business outcomes must not masquerade as one another: the value
    # space is disjoint by kind, so a spoofed "control" write can never mint an approval and
    # a business decision can never be replayed as a cancellation.
    control_outcomes = {"Cancelled", "ReturnedForInformation", "Resubmitted"}
    if (payload.kind == "control") != (payload.decision in control_outcomes):
        raise ValidationAppError(
            "kind='control' requires a control outcome (Cancelled / ReturnedForInformation / "
            "Resubmitted), and control outcomes are valid ONLY with kind='control'.")
    # effective_* are dict[str, Access]; store the access NAMES (JSON-friendly, and what the
    # worker feeds back into a delegated context).
    ops = {k: v.name for k, v in (approver.effective_operations or {}).items()}
    views = {k: v.name for k, v in (approver.effective_views or {}).items()}
    values = {
        "tenant_id": ctx.tenant_id, "workflow_id": payload.workflow_id,
        "lead_id": payload.lead_id, "decision": payload.decision,
        "subject_type": payload.subject_type, "subject_id": payload.subject_id,
        "run_id": payload.run_id, "committee_reference": payload.committee_reference,
        "sanction_letter_reference": payload.sanction_letter_reference,
        "decided_by": approver.email, "decided_by_id": str(approver.id),
        "roles": sorted(approver.roles or []),
        "operations": ops, "views": views,
        "note": payload.note, "created_by": ctx.actor,
    }
    # Atomic single-winner: INSERT ... ON CONFLICT DO NOTHING. A row back = we won; no row =
    # a decision already exists for this (tenant, workflow).
    stmt = (pg_insert(WorkflowDecision)
            .values(**values)
            .on_conflict_do_nothing(constraint="workflow_decisions_tenant_wf")
            .returning(WorkflowDecision.id))
    won_id = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if won_id is not None:
        # TRANSACTIONAL OUTBOX: create the delivery row in the SAME transaction as the decision,
        # so an accepted decision always has a durable "deliver me" record (pending, due now).
        # A CONTROL record is an audit anchor, not an appliable outcome — no delivery row.
        if payload.kind != "control":
            await ctx.session.execute(
                pg_insert(WorkflowDecisionOutbox)
                .values(tenant_id=ctx.tenant_id, workflow_id=payload.workflow_id,
                        decision=payload.decision, status="pending", created_by=ctx.actor)
                .on_conflict_do_nothing(constraint="workflow_decision_outbox_tenant_wf"))
        row = (await ctx.session.execute(
            select(WorkflowDecision).where(WorkflowDecision.id == won_id))).scalar_one()
        return _serialize(row)

    existing = (await ctx.session.execute(
        select(WorkflowDecision).where(
            WorkflowDecision.tenant_id == ctx.tenant_id,
            WorkflowDecision.workflow_id == payload.workflow_id))).scalar_one()
    if existing.decision == payload.decision:
        # Idempotent replay of the SAME decision → return the original, unchanged. ENSURE an
        # outbox row exists too, so a decision recorded before the outbox existed (or one whose
        # row was somehow lost) becomes deliverable on the next replay.
        if payload.kind != "control":
            await ctx.session.execute(
                pg_insert(WorkflowDecisionOutbox)
                .values(tenant_id=ctx.tenant_id, workflow_id=payload.workflow_id,
                        decision=payload.decision, status="pending", created_by=ctx.actor)
                .on_conflict_do_nothing(constraint="workflow_decision_outbox_tenant_wf"))
        return _serialize(existing)
    # The OPPOSITE decision already won — reject, even if the workflow has since completed.
    raise ConflictError(
        f"Workflow '{payload.workflow_id}' already has decision '{existing.decision}'; "
        f"'{payload.decision}' is refused.")


@router.get("/v1/internal/decisions/{workflow_id}", tags=["Internal"],
            summary="Read the recorded decision for a workflow")
async def get_decision(workflow_id: str, ctx: RequestContext = Depends(get_context)):
    _require_service()
    row = (await ctx.session.execute(
        select(WorkflowDecision).where(
            WorkflowDecision.tenant_id == ctx.tenant_id,
            WorkflowDecision.workflow_id == workflow_id))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No decision recorded for workflow '{workflow_id}'.")
    return _serialize(row)


# --------------------------------------------------------------------------- #
# Delivery outbox — the background reconciler's endpoints
# --------------------------------------------------------------------------- #
@router.post("/v1/internal/decisions/deliveries/claim", tags=["Internal"],
             summary="Reconciler: atomically claim due pending deliveries (with a lease)")
async def claim_deliveries(payload: ClaimIn, ctx: RequestContext = Depends(get_context)):
    _require_service()
    # Atomically lease due, unleased, pending rows and bump their attempt count. FOR UPDATE
    # SKIP LOCKED lets multiple reconciler replicas claim disjoint batches without blocking.
    rows = (await ctx.session.execute(text("""
        UPDATE workflow_decision_outbox o
        SET leased_until = now() + make_interval(secs => :lease),
            attempts = o.attempts + 1,
            claim_token = gen_random_uuid()
        WHERE o.id IN (
            SELECT id FROM workflow_decision_outbox
            WHERE tenant_id = :tid AND status = 'pending' AND next_attempt_at <= now()
              AND (leased_until IS NULL OR leased_until < now())
            ORDER BY next_attempt_at
            LIMIT :lim
            FOR UPDATE SKIP LOCKED)
        RETURNING o.workflow_id, o.decision, o.attempts, o.claim_token
    """), {"lease": payload.lease_seconds, "tid": str(ctx.tenant_id),
           "lim": payload.limit})).mappings().all()
    return {"claimed": [{"workflow_id": r["workflow_id"], "decision": r["decision"],
                         "attempts": r["attempts"], "claim_token": str(r["claim_token"])}
                        for r in rows]}


@router.post("/v1/internal/decisions/{workflow_id}/delivery", tags=["Internal"],
             summary="Reconciler: mark a delivery applied / retry (backoff) / dead")
async def update_delivery(workflow_id: str, payload: DeliveryUpdateIn,
                          ctx: RequestContext = Depends(get_context)):
    _require_service()
    params: dict[str, Any] = {"tid": str(ctx.tenant_id), "wf": workflow_id,
                              "err": payload.error, "backoff": payload.backoff_seconds,
                              "token": payload.claim_token}
    # FENCED + TERMINAL-SAFE: each statement is a fixed literal (all inputs are bound params).
    # The WHERE guard applies only while the row is still pending and the CURRENT claim token
    # matches, so a stalled claimant can't regress an already-applied/dead (terminal) row.
    updated = (await ctx.session.execute(
        text(_DELIVERY_SQL[payload.status]), params)).first()
    if updated is not None:
        return {"workflow_id": workflow_id, "status": payload.status}
    # Nothing updated: the row is gone (404) or it was terminal / re-claimed by a newer lease
    # (stale) → a NO-OP, not a corruption. Report the current state so the caller can move on.
    cur = (await ctx.session.execute(text(
        "SELECT status FROM workflow_decision_outbox WHERE tenant_id=:tid AND workflow_id=:wf"),
        params)).first()
    if cur is None:
        raise NotFoundError(f"No delivery row for workflow '{workflow_id}'.")
    return {"workflow_id": workflow_id, "status": "ignored", "current": cur[0]}


@router.post("/v1/internal/decisions/{workflow_id}/redrive", tags=["Internal"],
             summary="Recover a dead-lettered delivery back to pending (Admin, audited)")
async def redrive_delivery(workflow_id: str, payload: RedriveIn,
                           ctx: RequestContext = Depends(get_context)):
    """Return a ``dead`` delivery to ``pending`` so the reconciler picks it up again — the
    operator recovery path for a decision dead-lettered by a (mis)judged terminal state.

    Redrive re-activates a financial decision, so it is a HUMAN ADMIN action, not a service
    one: it requires a verified Admin identity in the delegated context (svc_workflows is only
    the transport), a MANDATORY recovery reason, and it writes an IMMUTABLE audit event that
    names the admin, the reason, any ticket reference, and — crucially — the PREVIOUS
    dead-letter cause before it is cleared, so the recovery is fully explainable after the fact.
    Clears the lease/token/error and resets attempts. A no-op (404) if there is no dead row."""
    _require_service()
    if ctx.user is None or not ctx.user.is_admin:
        raise ForbiddenError(
            "Redrive re-activates a decision and requires a verified Admin identity.")
    reason = payload.reason.strip()
    if not reason:
        raise ValidationAppError("A non-empty recovery reason is required to redrive a decision.")
    # Capture the PREVIOUS dead-letter cause atomically BEFORE it is cleared: the CTE reads (and
    # locks) the dead row, the UPDATE clears last_error, and RETURNING hands back the pre-update
    # value from the CTE — so the audit event preserves why the delivery died.
    updated = (await ctx.session.execute(text(
        "WITH prev AS ("
        " SELECT id, last_error FROM workflow_decision_outbox"
        " WHERE tenant_id=:tid AND workflow_id=:wf AND status='dead' FOR UPDATE)"
        " UPDATE workflow_decision_outbox o"
        " SET status='pending', next_attempt_at=now(), leased_until=NULL,"
        " claim_token=NULL, last_error=NULL, attempts=0"
        " FROM prev WHERE o.id = prev.id"
        " RETURNING prev.last_error"),
        {"tid": str(ctx.tenant_id), "wf": workflow_id})).first()
    if updated is None:
        raise NotFoundError(f"No dead-lettered delivery for workflow '{workflow_id}'.")
    previous_error = updated[0]
    # Immutable governance evidence: WHO re-activated WHICH decision, WHEN, WHY, under which
    # ticket, and the failure it is recovering from (append-only audit).
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.user.email, action="decision.redrive",
        resource_type="workflow_decision_outbox", resource_id=workflow_id,
        request_id=request_id_ctx.get(),
        changes={"from": "dead", "to": "pending", "by": ctx.user.email,
                 "reason": reason, "ticket": payload.ticket,
                 "previous_error": previous_error}))
    return {"workflow_id": workflow_id, "status": "pending", "by": ctx.user.email}


@router.get("/v1/internal/decisions/deliveries/stats", tags=["Internal"],
            summary="Reconciler: delivery counts + aged-pending gauge (for metrics/alerts)")
async def delivery_stats(ctx: RequestContext = Depends(get_context)):
    _require_service()
    by_status = {r[0]: int(r[1]) for r in (await ctx.session.execute(text(
        "SELECT status, count(*) FROM workflow_decision_outbox WHERE tenant_id = :tid "
        "GROUP BY status"), {"tid": str(ctx.tenant_id)})).all()}
    # "Aged" = pending and overdue for more than 15 minutes → a delivery that keeps failing.
    aged = (await ctx.session.execute(text(
        "SELECT count(*) FROM workflow_decision_outbox WHERE tenant_id = :tid "
        "AND status = 'pending' AND next_attempt_at < now() - interval '15 minutes'"),
        {"tid": str(ctx.tenant_id)})).scalar()
    return {"pending": int(by_status.get("pending", 0)),
            "applied": int(by_status.get("applied", 0)),
            "dead": int(by_status.get("dead", 0)),
            "aged_pending": int(aged or 0)}


@router.get("/v1/internal/tenants", tags=["Internal"],
            summary="Reconciler: active tenant codes to scan for pending deliveries")
async def list_tenant_codes(ctx: RequestContext = Depends(get_context)):
    _require_service()
    codes = (await ctx.session.execute(
        select(Tenant.code).where(Tenant.is_active.is_(True)))).scalars().all()
    return {"tenants": list(codes)}
