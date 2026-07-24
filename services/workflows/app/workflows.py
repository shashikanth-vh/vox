"""Durable workflows. The workflow code is deterministic and side-effect-free — all I/O
happens in activities. Temporal persists every step, so a crash resumes exactly where it
left off.

``IngestInteractionWorkflow`` is the reference: record a field interaction against an
entity, then read back the entity's dossier. It shows the platform pattern — orchestration
here, Register I/O in activities via the SDK — and the safety story: Temporal's automatic
activity retries combined with a stable, workflow-derived idempotency key give an
**exactly-once effect** on the Register.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app import activities
    from app.types import IngestResult, InteractionInput

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@workflow.defn
class IngestInteractionWorkflow:
    @workflow.run
    async def run(self, inp: InteractionInput) -> IngestResult:
        # Stable key from the workflow id → activity retries (or a whole-workflow retry)
        # can never create a duplicate interaction in the Register.
        idem = f"wf:{workflow.info().workflow_id}"

        created = await workflow.execute_activity(
            activities.write_interaction,
            args=[inp, idem],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_RETRY,
        )
        dossier = await workflow.execute_activity(
            activities.fetch_dossier,
            created["entity_id"],
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_RETRY,
        )
        return IngestResult(
            interaction_id=created["id"],
            dossier_counts=dossier.get("counts", {}),
        )
