"""Notifications — the durable in-app inbox + the external-channel delivery outbox.

The write path is the workflow plane (``svc_workflows``); humans read their own inbox.

    POST /v1/internal/notifications                      create (idempotent by dedupe_key)
    GET  /v1/notifications                               my inbox (recipient-scoped)
    POST /v1/notifications/{id}/read                     mark read (recipient or Admin)
    POST /v1/internal/notifications/deliveries/claim     notifier: claim due deliveries
    POST /v1/internal/notifications/deliveries/{id}      notifier: delivered / retry / dead
    POST /v1/internal/notifications/deliveries/{id}/redrive   Admin: dead → pending (audited)
    GET  /v1/internal/notifications/deliveries/stats     delivery counts (for metrics)

Semantics (same machinery the decision outbox proved out):

* **The in-app row IS the delivery** — creating the notification is the durable,
  always-works channel every deployment has. External channels (email / sms / webhook)
  each get an outbox row created in the SAME transaction, so an accepted notification
  can never lose its delivery intent.
* **Idempotent** — ``dedupe_key`` (UNIQUE per tenant) makes creation replay-safe: a
  Temporal activity retry returns the original row instead of double-notifying.
* **Claim/lease/fence** — the notifier sweep claims due pending deliveries with a lease
  and a fencing token; a stalled claimant whose lease expired can't overwrite a row a
  newer claim owns, and terminal rows (delivered / dead) never regress.
* **Dead-letter, recoverable** — retries exhaust into ``dead``; an Admin redrive (with a
  mandatory reason, audited) returns it to ``pending``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.authz.engine import service_ctx
from app.core.errors import ForbiddenError, NotFoundError, ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.notifications import Notification, NotificationDelivery

router = api_router()

# Only the workflow plane writes notifications (the notifier sweep runs on its key too).
_ALLOWED_SERVICES = {"svc_workflows"}

_CHANNELS = {"email", "sms", "webhook"}

# Fixed delivery-update statements — single constant literals (bound params only). The
# WHERE guard fences on the current claim token and requires status='pending', so a
# stalled claimant can't regress a terminal row (same shape as the decision outbox).
_DELIVERY_SQL = {
    "delivered": "UPDATE notification_deliveries SET status='delivered',"
                 " delivered_at=now(), leased_until=NULL, claim_token=NULL,"
                 " last_error=NULL"
                 " WHERE tenant_id = :tid AND id = CAST(:did AS uuid)"
                 " AND status = 'pending' AND claim_token = CAST(:token AS uuid)"
                 " RETURNING id",
    "dead": "UPDATE notification_deliveries SET status='dead', leased_until=NULL,"
            " claim_token=NULL, last_error=:err"
            " WHERE tenant_id = :tid AND id = CAST(:did AS uuid)"
            " AND status = 'pending' AND claim_token = CAST(:token AS uuid)"
            " RETURNING id",
    "retry": "UPDATE notification_deliveries SET status='pending', leased_until=NULL,"
             " claim_token=NULL, last_error=:err,"
             " next_attempt_at = now() + make_interval(secs => :backoff)"
             " WHERE tenant_id = :tid AND id = CAST(:did AS uuid)"
             " AND status = 'pending' AND claim_token = CAST(:token AS uuid)"
             " RETURNING id",
}


class NotificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=3, max_length=200)          # e-mail
    recipient_role: str | None = Field(default=None, max_length=60)
    event: str = Field(min_length=1, max_length=120)              # machine key
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    title: str = Field(min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=10000)
    subject_type: str | None = Field(default=None, max_length=40)
    subject_id: str | None = Field(default=None, max_length=64)
    workflow_id: str | None = Field(default=None, max_length=200)
    # Idempotency anchor — a replay with the same key returns the original notification.
    dedupe_key: str | None = Field(default=None, max_length=240)
    # External channels to fan out to (in-app is implicit — this row is it).
    # email → recipient; sms → sms_to (required); webhook → webhook_url (required).
    channels: list[str] = Field(default_factory=list)
    sms_to: str | None = Field(default=None, max_length=32)
    webhook_url: str | None = Field(default=None, max_length=300)
    meta: dict[str, Any] | None = None


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=200)
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class DeliveryUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(delivered|retry|dead)$")
    claim_token: str = Field(min_length=1)
    error: str | None = Field(default=None, max_length=4000)
    backoff_seconds: int = Field(default=60, ge=0, le=86400)


class RedriveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


def _require_service() -> None:
    if service_ctx.get() not in _ALLOWED_SERVICES:
        raise ForbiddenError(
            "Notifications are written by the workflow service principal only.")


def _serialize(row: Notification,
               deliveries: list[NotificationDelivery] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(row.id), "recipient": row.recipient,
        "recipient_role": row.recipient_role, "event": row.event,
        "severity": row.severity, "title": row.title, "body": row.body,
        "subject_type": row.subject_type, "subject_id": row.subject_id,
        "workflow_id": row.workflow_id, "dedupe_key": row.dedupe_key,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "meta": row.meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if deliveries is not None:
        out["deliveries"] = [_serialize_delivery(d) for d in deliveries]
    return out


def _serialize_delivery(d: NotificationDelivery) -> dict[str, Any]:
    return {"id": str(d.id), "notification_id": str(d.notification_id),
            "channel": d.channel, "target": d.target, "status": d.status,
            "attempts": d.attempts, "last_error": d.last_error,
            "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at else None,
            "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None}


async def _deliveries_for(ctx: RequestContext,
                          notification_id: uuid.UUID) -> list[NotificationDelivery]:
    return list((await ctx.session.execute(
        select(NotificationDelivery).where(
            NotificationDelivery.tenant_id == ctx.tenant_id,
            NotificationDelivery.notification_id == notification_id)
        .order_by(NotificationDelivery.channel))).scalars())


@router.post("/v1/internal/notifications", tags=["Internal"], status_code=201,
             summary="Create a notification (idempotent) + its channel delivery rows")
async def create_notification(payload: NotificationIn,
                              ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service()
    channels = sorted(set(payload.channels))
    unknown = [c for c in channels if c not in _CHANNELS]
    if unknown:
        raise ValidationAppError(
            f"Unknown channel(s) {unknown}; supported: {sorted(_CHANNELS)}.")
    # Resolve each channel's target UP FRONT, so a mis-specified fan-out is refused as a
    # whole rather than half-created.
    targets: dict[str, str] = {}
    for ch in channels:
        if ch == "email":
            targets[ch] = payload.recipient
        elif ch == "sms":
            if not payload.sms_to:
                raise ValidationAppError("channel 'sms' requires sms_to.")
            targets[ch] = payload.sms_to
        elif ch == "webhook":
            if not payload.webhook_url:
                raise ValidationAppError("channel 'webhook' requires webhook_url.")
            targets[ch] = payload.webhook_url

    values = {
        "tenant_id": ctx.tenant_id, "recipient": payload.recipient,
        "recipient_role": payload.recipient_role, "event": payload.event,
        "severity": payload.severity, "title": payload.title, "body": payload.body,
        "subject_type": payload.subject_type, "subject_id": payload.subject_id,
        "workflow_id": payload.workflow_id, "dedupe_key": payload.dedupe_key,
        "meta": payload.meta, "created_by": ctx.actor,
    }
    if payload.dedupe_key:
        # Idempotent single-winner on (tenant, dedupe_key): a replay returns the original.
        stmt = (pg_insert(Notification).values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"],
                                        index_where=text("dedupe_key IS NOT NULL"))
                .returning(Notification.id))
        won = (await ctx.session.execute(stmt)).scalar_one_or_none()
        if won is None:
            existing = (await ctx.session.execute(
                select(Notification).where(
                    Notification.tenant_id == ctx.tenant_id,
                    Notification.dedupe_key == payload.dedupe_key))).scalar_one()
            return _serialize(existing, await _deliveries_for(ctx, existing.id))
        nid = won
    else:
        nid = (await ctx.session.execute(
            pg_insert(Notification).values(**values)
            .returning(Notification.id))).scalar_one()

    # TRANSACTIONAL OUTBOX: the channel rows are created with the notification — an
    # accepted notification can never lose its delivery intent.
    for ch in channels:
        await ctx.session.execute(
            pg_insert(NotificationDelivery)
            .values(tenant_id=ctx.tenant_id, notification_id=nid, channel=ch,
                    target=targets[ch], status="pending", created_by=ctx.actor)
            .on_conflict_do_nothing(constraint="notification_deliveries_unique"))
    row = (await ctx.session.execute(
        select(Notification).where(Notification.id == nid))).scalar_one()
    return _serialize(row, await _deliveries_for(ctx, nid))


# --------------------------------------------------------------------------- #
# The human inbox
# --------------------------------------------------------------------------- #
@router.get("/v1/notifications", tags=["Notifications"],
            summary="My notifications (in-app inbox)")
async def my_notifications(ctx: RequestContext = Depends(get_context),
                           unread_only: bool = Query(default=False),
                           limit: int = Query(default=50, ge=1, le=200),
                           recipient: str | None = Query(
                               default=None,
                               description="Admin only: view another inbox")) -> dict[str, Any]:
    if ctx.user is None or not ctx.user.email:
        raise ForbiddenError("The notification inbox requires a signed-in user.")
    who = ctx.user.email
    if recipient and recipient != ctx.user.email:
        if not ctx.user.is_admin:
            raise ForbiddenError("Only an Admin may read another user's inbox.")
        who = recipient
    conds = [Notification.tenant_id == ctx.tenant_id, Notification.recipient == who,
             Notification.deleted_at.is_(None)]
    if unread_only:
        conds.append(Notification.read_at.is_(None))
    rows = list((await ctx.session.execute(
        select(Notification).where(*conds)
        .order_by(Notification.created_at.desc()).limit(limit))).scalars())
    unread = (await ctx.session.execute(text(
        "SELECT count(*) FROM notifications WHERE tenant_id = :tid AND recipient = :who "
        "AND read_at IS NULL AND deleted_at IS NULL"),
        {"tid": str(ctx.tenant_id), "who": who})).scalar()
    return {"items": [_serialize(r) for r in rows], "unread": int(unread or 0)}


@router.post("/v1/notifications/{notification_id}/read", tags=["Notifications"],
             summary="Mark a notification read (recipient or Admin)")
async def mark_read(notification_id: uuid.UUID,
                    ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    if ctx.user is None or not ctx.user.email:
        raise ForbiddenError("Marking a notification read requires a signed-in user.")
    row = (await ctx.session.execute(
        select(Notification).where(
            Notification.tenant_id == ctx.tenant_id,
            Notification.id == notification_id,
            Notification.deleted_at.is_(None)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No notification '{notification_id}'.")
    if row.recipient != ctx.user.email and not ctx.user.is_admin:
        raise ForbiddenError("Only the recipient (or an Admin) may mark this read.")
    if row.read_at is None:
        await ctx.session.execute(text(
            "UPDATE notifications SET read_at = now() "
            "WHERE id = CAST(:nid AS uuid) AND read_at IS NULL"),
            {"nid": str(notification_id)})
        await ctx.session.refresh(row)
    return _serialize(row)


# --------------------------------------------------------------------------- #
# Delivery outbox — the notifier sweep's endpoints
# --------------------------------------------------------------------------- #
@router.post("/v1/internal/notifications/deliveries/claim", tags=["Internal"],
             summary="Notifier: atomically claim due pending deliveries (with a lease)")
async def claim_deliveries(payload: ClaimIn,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service()
    # Lease due, unleased, pending rows and bump the attempt count. FOR UPDATE SKIP LOCKED
    # lets multiple notifier replicas claim disjoint batches without blocking. The claim
    # carries everything needed to render + send, so the sweep makes no read-back call.
    rows = (await ctx.session.execute(text("""
        UPDATE notification_deliveries d
        SET leased_until = now() + make_interval(secs => :lease),
            attempts = d.attempts + 1,
            claim_token = gen_random_uuid()
        FROM notifications n
        WHERE d.id IN (
            SELECT id FROM notification_deliveries
            WHERE tenant_id = :tid AND status = 'pending' AND next_attempt_at <= now()
              AND (leased_until IS NULL OR leased_until < now())
            ORDER BY next_attempt_at
            LIMIT :lim
            FOR UPDATE SKIP LOCKED)
          AND n.id = d.notification_id
        RETURNING d.id, d.channel, d.target, d.attempts, d.claim_token,
                  n.event, n.severity, n.title, n.body, n.recipient,
                  n.subject_type, n.subject_id, n.workflow_id
    """), {"lease": payload.lease_seconds, "tid": str(ctx.tenant_id),
           "lim": payload.limit})).mappings().all()
    return {"claimed": [
        {"delivery_id": str(r["id"]), "channel": r["channel"], "target": r["target"],
         "attempts": r["attempts"], "claim_token": str(r["claim_token"]),
         "event": r["event"], "severity": r["severity"], "title": r["title"],
         "body": r["body"], "recipient": r["recipient"],
         "subject_type": r["subject_type"], "subject_id": r["subject_id"],
         "workflow_id": r["workflow_id"]}
        for r in rows]}


@router.post("/v1/internal/notifications/deliveries/{delivery_id}", tags=["Internal"],
             summary="Notifier: mark a delivery delivered / retry (backoff) / dead")
async def update_delivery(delivery_id: uuid.UUID, payload: DeliveryUpdateIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service()
    params: dict[str, Any] = {"tid": str(ctx.tenant_id), "did": str(delivery_id),
                              "err": payload.error, "backoff": payload.backoff_seconds,
                              "token": payload.claim_token}
    updated = (await ctx.session.execute(
        text(_DELIVERY_SQL[payload.status]), params)).first()
    if updated is not None:
        return {"delivery_id": str(delivery_id), "status": payload.status}
    cur = (await ctx.session.execute(text(
        "SELECT status FROM notification_deliveries "
        "WHERE tenant_id = :tid AND id = CAST(:did AS uuid)"), params)).first()
    if cur is None:
        raise NotFoundError(f"No delivery '{delivery_id}'.")
    # Terminal / re-claimed by a newer lease → a NO-OP, not a corruption.
    return {"delivery_id": str(delivery_id), "status": "ignored", "current": cur[0]}


@router.post("/v1/internal/notifications/deliveries/{delivery_id}/redrive",
             tags=["Internal"],
             summary="Recover a dead-lettered delivery back to pending (Admin, audited)")
async def redrive_delivery(delivery_id: uuid.UUID, payload: RedriveIn,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service()
    if ctx.user is None or not ctx.user.is_admin:
        raise ForbiddenError(
            "Redriving a notification delivery requires a verified Admin identity.")
    reason = payload.reason.strip()
    if not reason:
        raise ValidationAppError("A non-empty recovery reason is required.")
    # Capture the previous dead-letter cause atomically BEFORE it is cleared (audit).
    updated = (await ctx.session.execute(text(
        "WITH prev AS ("
        " SELECT id, last_error FROM notification_deliveries"
        " WHERE tenant_id = :tid AND id = CAST(:did AS uuid) AND status='dead'"
        " FOR UPDATE)"
        " UPDATE notification_deliveries d"
        " SET status='pending', next_attempt_at=now(), leased_until=NULL,"
        " claim_token=NULL, last_error=NULL, attempts=0"
        " FROM prev WHERE d.id = prev.id"
        " RETURNING prev.last_error"),
        {"tid": str(ctx.tenant_id), "did": str(delivery_id)})).first()
    if updated is None:
        raise NotFoundError(f"No dead-lettered delivery '{delivery_id}'.")
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.user.email, action="notification.redrive",
        resource_type="notification_deliveries", resource_id=str(delivery_id),
        request_id=request_id_ctx.get(),
        changes={"from": "dead", "to": "pending", "by": ctx.user.email,
                 "reason": reason, "previous_error": updated[0]}))
    return {"delivery_id": str(delivery_id), "status": "pending", "by": ctx.user.email}


@router.get("/v1/internal/notifications/deliveries/stats", tags=["Internal"],
            summary="Notifier: delivery counts + aged-pending gauge (for metrics/alerts)")
async def delivery_stats(ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    _require_service()
    by_status = {r[0]: int(r[1]) for r in (await ctx.session.execute(text(
        "SELECT status, count(*) FROM notification_deliveries WHERE tenant_id = :tid "
        "GROUP BY status"), {"tid": str(ctx.tenant_id)})).all()}
    aged = (await ctx.session.execute(text(
        "SELECT count(*) FROM notification_deliveries WHERE tenant_id = :tid "
        "AND status = 'pending' AND next_attempt_at < now() - interval '15 minutes'"),
        {"tid": str(ctx.tenant_id)})).scalar()
    return {"pending": int(by_status.get("pending", 0)),
            "delivered": int(by_status.get("delivered", 0)),
            "dead": int(by_status.get("dead", 0)),
            "aged_pending": int(aged or 0)}
