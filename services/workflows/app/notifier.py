"""The notification delivery sweep — the external-channel engine behind increment 7.

The Register holds the durable notification (the in-app inbox row) plus one PENDING
delivery-outbox row per external channel (email / sms / webhook), created transactionally
with the notification. This daemon drives those rows to a terminal state:

    claim (lease + fencing token)
      → attempt the channel send
        → delivered                      success
        → retry (exponential backoff)    transient failure, attempts remain
        → dead                           attempts exhausted → Admin redrive path

Channel transports:
  * email   — SMTP relay (WORKFLOWS_SMTP_HOST/PORT/FROM, optional auth + STARTTLS).
  * sms     — a provider-agnostic HTTP hook (WORKFLOWS_SMS_WEBHOOK_URL): POST
              {to, body, event}. Twilio/MSG91/… adapters terminate behind that URL.
  * webhook — POST the full notification JSON to the delivery's target URL.

Failure discipline (the increment's gate): every send failure is CONTAINED — recorded on
the delivery row with the next attempt scheduled by exponential backoff
(base · 2^(attempts−1), capped), never lost, never crashing the sweep; exhausted attempts
dead-letter LOUDLY (warning log + the stats gauge) and stay recoverable via the audited
Admin redrive endpoint.

Run it:  python -m app.notifier   (same image as the worker; a small separate deployment).

``deliver_one`` and ``sweep_tenant`` take the sender map as a parameter, so tests inject
failing/succeeding fakes and prove the retry/dead-letter machinery without a mail server.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from typing import Any, Callable

import httpx
from evam_backend_core.logging import configure_logging, get_logger
from evam_register_client import AsyncRegisterClient
from evam_register_client.config import RegisterClientConfig

from app.config import get_settings

log = get_logger("notifier")

Sender = Callable[[dict[str, Any]], Any]   # async callable: claim → None (raises on failure)


def _smtp_send(claim: dict[str, Any]) -> None:
    """Blocking SMTP send (runs in a worker thread). Raises on any failure."""
    import smtplib

    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = claim["target"]
    msg["Subject"] = f"[PRISM] {claim.get('title') or claim.get('event')}"
    body = claim.get("body") or claim.get("title") or claim.get("event") or ""
    subject_line = ""
    if claim.get("subject_type"):
        subject_line = f"\n\nSubject: {claim['subject_type']} {claim.get('subject_id') or ''}"
    run_line = f"\nRun: {claim['workflow_id']}" if claim.get("workflow_id") else ""
    msg.set_content(f"{body}{subject_line}{run_line}\n\n— PRISM workflows")
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_s) as smtp:
        if s.smtp_starttls:
            smtp.starttls()
        if s.smtp_username:
            smtp.login(s.smtp_username, s.smtp_password)
        smtp.send_message(msg)


async def send_email(claim: dict[str, Any]) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise RuntimeError("email channel is not configured (WORKFLOWS_SMTP_HOST empty)")
    await asyncio.to_thread(_smtp_send, claim)


async def send_sms(claim: dict[str, Any]) -> None:
    s = get_settings()
    if not s.sms_webhook_url:
        raise RuntimeError("sms channel is not configured (WORKFLOWS_SMS_WEBHOOK_URL empty)")
    async with httpx.AsyncClient(timeout=s.notify_timeout_s) as client:
        r = await client.post(s.sms_webhook_url, json={
            "to": claim["target"],
            "body": f"[PRISM] {claim.get('title') or claim.get('event')}",
            "event": claim.get("event")})
        if r.status_code >= 300:
            raise RuntimeError(f"sms hook returned HTTP {r.status_code}")


async def send_webhook(claim: dict[str, Any]) -> None:
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.notify_timeout_s) as client:
        r = await client.post(claim["target"], json={
            "event": claim.get("event"), "severity": claim.get("severity"),
            "title": claim.get("title"), "body": claim.get("body"),
            "recipient": claim.get("recipient"),
            "subject_type": claim.get("subject_type"),
            "subject_id": claim.get("subject_id"),
            "workflow_id": claim.get("workflow_id")})
        if r.status_code >= 300:
            raise RuntimeError(f"webhook returned HTTP {r.status_code}")


SENDERS: dict[str, Sender] = {"email": send_email, "sms": send_sms,
                              "webhook": send_webhook}


def backoff_seconds(attempts: int, *, base: int, cap: int) -> int:
    """Exponential: base · 2^(attempts−1), capped. attempts is the count ALREADY made."""
    return min(base * (2 ** max(attempts - 1, 0)), cap)


async def deliver_one(claim: dict[str, Any], senders: dict[str, Sender], *,
                      max_attempts: int, backoff_base: int,
                      backoff_cap: int) -> tuple[str, str | None, int]:
    """Attempt ONE claimed delivery. Returns (outcome, error, backoff_seconds):

    * delivered — the channel send succeeded.
    * retry     — it failed but attempts remain; back off exponentially.
    * dead      — attempts are exhausted (or the channel is unknown): dead-letter.
    """
    channel = str(claim.get("channel"))
    attempts = int(claim.get("attempts") or 1)   # the claim already counted this attempt
    sender = senders.get(channel)
    if sender is None:
        return "dead", f"unknown channel {channel!r}", 0
    try:
        await sender(claim)
    except Exception as exc:  # noqa: BLE001 - ANY send failure is contained, never raised
        err = f"{type(exc).__name__}: {exc}"
        if attempts >= max_attempts:
            return "dead", f"attempts exhausted ({attempts}/{max_attempts}); last: {err}", 0
        return "retry", err, backoff_seconds(attempts, base=backoff_base, cap=backoff_cap)
    return "delivered", None, 0


async def sweep_tenant(reg: AsyncRegisterClient, senders: dict[str, Sender], *,
                       batch: int, lease_seconds: int, max_attempts: int,
                       backoff_base: int, backoff_cap: int) -> int:
    """Claim + drive one batch for the tenant ``reg`` is scoped to. Never raises for a
    single delivery: each outcome (including a failed status write-back) is contained so
    one poisoned row cannot stall the queue."""
    claimed = await reg.claim_notification_deliveries(limit=batch,
                                                      lease_seconds=lease_seconds)
    for claim in claimed:
        outcome, err, backoff = await deliver_one(
            claim, senders, max_attempts=max_attempts,
            backoff_base=backoff_base, backoff_cap=backoff_cap)
        if outcome == "dead":
            log.warning("notifier_dead_letter",
                        extra={"delivery_id": claim.get("delivery_id"),
                               "channel": claim.get("channel"), "reason": err})
        try:
            await reg.update_notification_delivery(
                claim["delivery_id"], outcome, claim_token=claim["claim_token"],
                error=err, backoff_seconds=backoff)
        except Exception as exc:  # noqa: BLE001 - lease expiry re-queues it anyway
            log.warning("notifier_status_writeback_failed",
                        extra={"delivery_id": claim.get("delivery_id"), "error": str(exc)})
    return len(claimed)


def _reg_for(tenant: str) -> AsyncRegisterClient:
    s = get_settings()
    return AsyncRegisterClient(config=RegisterClientConfig(
        base_url=s.register_base_url, api_key=s.register_api_key, tenant=tenant,
        actor="notifier"))


async def run_once() -> dict[str, int]:
    """One full sweep across every active tenant. Returns aggregate delivery stats."""
    s = get_settings()
    async with _reg_for(s.register_tenant) as reg0:
        tenants = await reg0.internal_tenants()
    totals = {"processed": 0, "pending": 0, "delivered": 0, "dead": 0, "aged_pending": 0}
    for tenant in tenants or [s.register_tenant]:
        async with _reg_for(tenant) as reg:
            totals["processed"] += await sweep_tenant(
                reg, SENDERS, batch=s.notifier_batch,
                lease_seconds=s.notifier_lease_seconds,
                max_attempts=s.notifier_max_attempts,
                backoff_base=s.notifier_backoff_base_seconds,
                backoff_cap=s.notifier_backoff_cap_seconds)
            stats = await reg.notification_delivery_stats()
            for k in ("pending", "delivered", "dead", "aged_pending"):
                totals[k] += int(stats.get(k, 0))
    if totals["aged_pending"]:
        log.warning("notifier_aged_pending", extra={"aged_pending": totals["aged_pending"]})
    log.info("notifier_sweep", extra=totals)
    return totals


async def main() -> None:  # pragma: no cover - process entrypoint
    s = get_settings()
    configure_logging(s.log_level, json_logs=s.log_json and not s.is_local)
    log.info("notifier_started",
             extra={"interval_s": s.notifier_interval_seconds,
                    "channels": s.notify_channel_list()})
    while True:
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001 - never let one bad sweep kill the daemon
            log.warning("notifier_sweep_failed", extra={"error": str(exc)})
        await asyncio.sleep(s.notifier_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
