"""The decision-delivery reconciler.

A background daemon that closes the "accepted but never delivered" gap: the orchestrator records
a decision durably (transactional outbox) BEFORE it signals the workflow, so if that signal is
lost and no caller retries, the decision would sit unapplied forever. This daemon repeatedly
claims pending deliveries (leased), re-delivers them to the workflow, marks them ``applied`` once
the run has converted, and ``dead``-letters ones whose workflow closed without applying or whose
attempts are exhausted. It scans every active tenant and logs delivery metrics each cycle.

Run it:  python -m app.reconciler   (same image as the worker; a small separate deployment).

The two core functions are pure enough to unit-test with fakes:
  * ``deliver_one``     — decide a single decision's delivery outcome from the workflow's state.
  * ``reconcile_tenant`` — claim + drive a batch for one tenant.
"""

from __future__ import annotations

import asyncio
from typing import Any

from evam_backend_core.logging import configure_logging, get_logger
from evam_register_client import AsyncRegisterClient
from evam_register_client.config import RegisterClientConfig
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings

log = get_logger("reconciler")

_SIGNAL = {"Approved": "approve", "Rejected": "reject"}


async def deliver_one(handle: Any, workflow_id: str, decision: str) -> tuple[str, str | None]:
    """Decide the delivery outcome for ONE decision from the workflow's ACTUAL state:

    * missing run                → ``dead`` (nothing to deliver to).
    * RUNNING                    → re-signal it, then ``retry`` (next cycle confirms it applied).
    * COMPLETED with this outcome → ``applied``.
    * COMPLETED / closed otherwise → ``dead`` (closed without applying this decision).

    Re-signalling is safe: the worker derives the authoritative outcome from the persisted
    decision record, and the run ignores a duplicate signal once it has decided."""
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return "dead", "workflow not found"
        raise   # transient → let the caller leave it leased; it retries next cycle
    status = desc.status
    if status == WorkflowExecutionStatus.RUNNING:
        # Re-deliver. by/note are irrelevant (the worker uses the persisted record); token and
        # decision_ref are vestigial for the same reason.
        await handle.signal(_SIGNAL.get(decision, "approve"),
                            args=["reconciler", None, "", ""])
        return "retry", "re-signalled a running workflow"
    if status == WorkflowExecutionStatus.COMPLETED:
        # DEAD-LETTER ONLY on an AUTHORITATIVE result. A transient result-read failure
        # (RPCError / transport) PROPAGATES so the caller keeps it retryable — we never turn a
        # network blip on a successfully-completed run into a permanent false dead-letter.
        result = await handle.result()
        if isinstance(result, dict) and result.get("status") == decision:
            return "applied", None
        return "dead", "workflow completed with a different outcome"
    return "dead", f"workflow closed ({status.name if status else 'UNKNOWN'})"


async def reconcile_tenant(client: Any, reg: AsyncRegisterClient, *, batch: int,
                           lease_seconds: int, backoff_seconds: int) -> int:
    """Claim + drive a batch of pending deliveries for the tenant ``reg`` is scoped to.
    Returns how many deliveries were processed.

    An accepted decision is NEVER dead-lettered merely because the workflow has stayed running
    or a transient error keeps recurring — those RETRY indefinitely (a transient infra outage
    must not decide a financial outcome). Dead-lettering happens ONLY on an authoritative
    terminal mismatch (deliver_one → ``dead``): a missing run, or a run that completed/closed
    with a different outcome. Aged-pending is surfaced via metrics + the redrive endpoint."""
    claimed = await reg.claim_deliveries(limit=batch, lease_seconds=lease_seconds)
    for item in claimed:
        wf, decision, token = item["workflow_id"], item["decision"], item["claim_token"]
        try:
            outcome, err = await deliver_one(client.get_workflow_handle(wf), wf, decision)
        except Exception as exc:  # noqa: BLE001 - transient describe/result/signal → retry later
            await reg.update_delivery(wf, "retry", claim_token=token, error=str(exc),
                                      backoff_seconds=backoff_seconds)
            continue
        await reg.update_delivery(wf, outcome, claim_token=token, error=err,
                                  backoff_seconds=backoff_seconds)
        if outcome == "dead":
            log.warning("reconciler_dead_letter", extra={"workflow_id": wf, "reason": err})
    return len(claimed)


def _reg_for(tenant: str) -> AsyncRegisterClient:
    s = get_settings()
    return AsyncRegisterClient(config=RegisterClientConfig(
        base_url=s.register_base_url, api_key=s.register_api_key, tenant=tenant,
        actor="reconciler"))


async def run_once(client: Client) -> dict[str, int]:
    """One full sweep across every active tenant. Returns aggregate delivery stats."""
    s = get_settings()
    async with _reg_for(s.register_tenant) as reg0:
        tenants = await reg0.internal_tenants()
    totals = {"processed": 0, "pending": 0, "applied": 0, "dead": 0, "aged_pending": 0}
    for tenant in tenants or [s.register_tenant]:
        async with _reg_for(tenant) as reg:
            totals["processed"] += await reconcile_tenant(
                client, reg, batch=s.reconciler_batch, lease_seconds=s.reconciler_lease_seconds,
                backoff_seconds=s.reconciler_backoff_seconds)
            stats = await reg.delivery_stats()
            for k in ("pending", "applied", "dead", "aged_pending"):
                totals[k] += int(stats.get(k, 0))
    if totals["aged_pending"]:
        log.warning("reconciler_aged_pending", extra={"aged_pending": totals["aged_pending"]})
    log.info("reconciler_sweep", extra=totals)
    return totals


async def main() -> None:  # pragma: no cover - process entrypoint
    s = get_settings()
    configure_logging(s.log_level, json_logs=s.log_json and not s.is_local)
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    log.info("reconciler_started", extra={"interval_s": s.reconciler_interval_seconds})
    while True:
        try:
            await run_once(client)
        except Exception as exc:  # noqa: BLE001 - never let one bad sweep kill the daemon
            log.warning("reconciler_sweep_failed", extra={"error": str(exc)})
        await asyncio.sleep(s.reconciler_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
