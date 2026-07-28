"""Durable workflows. The workflow code is deterministic and side-effect-free — all I/O
happens in activities. Temporal persists every step, so a crash resumes exactly where it
left off.

Three workflows:

* ``IngestInteractionWorkflow`` — the original minimal reference (kept for teaching).
* ``VoxTouchpointWorkflow``     — the genuine end-to-end VOX capture: resolve the company
  by canonical name → create the entity + lead when missing / update the active lead when
  present → log the full-fidelity interaction (transcript, audio ref, RMs, GPS,
  follow-up) → return everything it did. Workflow id = ``vox-{capture_id}``, so a retried
  upload replays the SAME workflow and the SAME Register writes — exactly-once end to end.
* ``LeadConversionWorkflow``    — human-in-the-loop: waits for an ``approve``/``reject``
  SIGNAL (with a timeout), exposes a ``status`` QUERY for dashboards, and on approval
  applies the conversion (deal + product lines + lead marked Converted).

The safety story everywhere: Temporal's automatic retries combined with stable,
workflow-derived idempotency keys give an **exactly-once effect** on the Register.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app import activities
    from app.types import (
        IngestResult,
        InteractionInput,
        LeadConversionInput,
        LeadConversionResult,
        VoxResult,
        VoxTouchpoint,
    )

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)
_IO: dict[str, Any] = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": _RETRY}


@workflow.defn
class IngestInteractionWorkflow:
    """The minimal reference — see VoxTouchpointWorkflow for the operational one."""

    @workflow.run
    async def run(self, inp: InteractionInput) -> IngestResult:
        # Stable key from the workflow id → activity retries (or a whole-workflow retry)
        # can never create a duplicate interaction in the Register.
        idem = f"wf:{workflow.info().workflow_id}"

        created = await workflow.execute_activity(
            activities.write_interaction, args=[inp, idem], **_IO)
        dossier = await workflow.execute_activity(
            activities.fetch_dossier, created["entity_id"],
            start_to_close_timeout=timedelta(seconds=15), retry_policy=_RETRY)
        return IngestResult(
            interaction_id=created["id"],
            dossier_counts=dossier.get("counts", {}),
        )


@workflow.defn
class VoxTouchpointWorkflow:
    """Field capture → Register, handling both approved VOX scenarios:

    new company      → create entity → create lead → log interaction → follow-up
    existing company → resolve by canonical name → update/link active lead (create one
                       if none) → log interaction → follow-up
    """

    def __init__(self) -> None:
        self._stage = "starting"

    @workflow.query
    def status(self) -> str:
        """Live progress for dashboards/debugging: which step the run is on."""
        return self._stage

    @workflow.run
    async def run(self, tp: VoxTouchpoint) -> VoxResult:
        wf_id = workflow.info().workflow_id

        # -- 1. company: explicit id, canonical-name match, or create ---------
        entity_created = False
        entity_id = tp.entity_id
        if not entity_id:
            if not tp.company_name:
                raise ApplicationError(
                    "VoxTouchpoint needs company_name or entity_id", non_retryable=True)
            self._stage = "resolving company"
            entity = await workflow.execute_activity(
                activities.resolve_entity, tp.company_name, **_IO)
            if entity is None:
                self._stage = "creating company"
                entity = await workflow.execute_activity(
                    activities.create_entity, args=[tp, f"wf:{wf_id}:entity"], **_IO)
                entity_created = True
            entity_id = entity["id"]
        assert entity_id is not None  # narrowed: explicit id, matched, or just created

        # -- 2. lead: link the active one, or open one ------------------------
        self._stage = "linking lead"
        lead_created = False
        lead: dict[str, Any] | None = await workflow.execute_activity(
            activities.find_active_lead, entity_id, **_IO)
        if lead is None:
            lead = await workflow.execute_activity(
                activities.create_lead, args=[tp, entity_id, f"wf:{wf_id}:lead"], **_IO)
            lead_created = True
            # Assign the actual BDRM as the lead's owner (a real LineAssignment), so the
            # RM's scoped lists/reads/writes cover it — not just the rm name string.
            if tp.assigned_rm_id and lead is not None:
                await workflow.execute_activity(
                    activities.assign_lead_owner,
                    args=[lead["id"], tp.assigned_rm_id], **_IO)
        else:
            lead = await workflow.execute_activity(
                activities.update_lead_touch, args=[lead["id"], tp], **_IO)
        assert lead is not None  # narrowed: found-and-updated, or just created

        # -- 3. the interaction itself (full fidelity, exactly-once) ----------
        self._stage = "logging interaction"
        interaction = await workflow.execute_activity(
            activities.log_touchpoint,
            args=[tp, entity_id, lead["id"], f"wf:{wf_id}:interaction"], **_IO)

        self._stage = "done"
        follow_up = {}
        if tp.next_action or tp.next_meeting_date:
            follow_up = {"next_action": tp.next_action,
                         "next_action_date": tp.next_action_date,
                         "next_meeting_date": tp.next_meeting_date,
                         "calendar": "pending" if tp.next_meeting_date else None}
        return VoxResult(
            workflow_id=wf_id,
            entity_id=entity_id,
            entity_created=entity_created,
            lead_id=lead["id"],
            lead_created=lead_created,
            interaction_id=interaction["id"],
            follow_up={k: v for k, v in follow_up.items() if v is not None},
        )


@workflow.defn
class LeadConversionWorkflow:
    """Request → human decision → applied conversion. The human-in-the-loop pattern:

    * ``approve(by, note)`` / ``reject(by, note)`` — SIGNALS a Head sends (via the
      orchestrator API or the Temporal UI).
    * ``status()`` — a QUERY dashboards poll ("Pending" / "Approved" / ...).
    * No decision within ``approval_timeout_hours`` → TimedOut (recorded on the lead).
    """

    def __init__(self) -> None:
        self._decision: str | None = None      # None until a signal (or timeout)
        self._decided_by: str | None = None
        self._note: str | None = None
        self._stage = "Pending"

    @workflow.signal
    def approve(self, by: str, note: str | None = None) -> None:
        if self._decision is None:             # first decision wins
            self._decision, self._decided_by, self._note = "Approved", by, note

    @workflow.signal
    def reject(self, by: str, note: str | None = None) -> None:
        if self._decision is None:
            self._decision, self._decided_by, self._note = "Rejected", by, note

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: LeadConversionInput) -> LeadConversionResult:
        wf_id = workflow.info().workflow_id
        lead = await workflow.execute_activity(activities.get_lead, inp.lead_id, **_IO)
        entity_id = lead.get("entity_id")
        if not entity_id:
            raise ApplicationError(
                f"Lead {inp.lead_id} has no entity_id — link it to a company first.",
                non_retryable=True)

        # Wait durably for a human. The workflow can sleep here for days and survive
        # worker restarts — that is the whole point of Temporal.
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(hours=inp.approval_timeout_hours))
        except asyncio.TimeoutError:
            pass
        if self._decision is None:
            self._stage = "TimedOut"
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id, f"Conversion request by {inp.requested_by} timed out "
                                   f"(workflow {wf_id})."], **_IO)
            return LeadConversionResult(workflow_id=wf_id, lead_id=inp.lead_id,
                                        status="TimedOut")

        if self._decision == "Rejected":
            self._stage = "Rejected"
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id, f"Conversion rejected by {self._decided_by}: "
                                   f"{self._note or 'no note'} (workflow {wf_id})."], **_IO)
            return LeadConversionResult(workflow_id=wf_id, lead_id=inp.lead_id,
                                        status="Rejected", decided_by=self._decided_by,
                                        decision_note=self._note)

        # Approved → apply in ONE transactional Register call. All-or-nothing on the
        # server: no orphan deal/lines can survive a mid-apply failure, so the workflow
        # needs no compensation. Idempotent on the workflow id → a retry is safe.
        self._stage = "Applying"
        applied = await workflow.execute_activity(
            activities.convert_lead_txn, args=[inp, f"wf:{wf_id}:convert"], **_IO)
        line_ids = {
            "lending": applied.get("lending_id"),
            "syndication": applied.get("syndication_id"),
            "asset-monetisation": applied.get("asset_mon_id"),
        }
        deal = {"id": applied["deal_id"]}

        self._stage = "Approved"
        return LeadConversionResult(
            workflow_id=wf_id, lead_id=inp.lead_id, status="Approved",
            decided_by=self._decided_by, decision_note=self._note,
            deal_id=deal["id"], lending_id=line_ids["lending"],
            syndication_id=line_ids["syndication"],
            asset_mon_id=line_ids["asset-monetisation"],
        )
