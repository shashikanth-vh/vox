"""End-to-end workflow test on Temporal's in-memory (time-skipping) test server.

Skips automatically where the test server can't be downloaded/started (e.g. an offline
sandbox); runs in CI. The activities hit the mock Register via the monkeypatched client.
"""

from __future__ import annotations

import uuid

import pytest

from app import activities
from app.types import InteractionInput
from app.workflows import IngestInteractionWorkflow

pytestmark = pytest.mark.asyncio


async def test_ingest_workflow_end_to_end(mock_register):
    try:
        from temporalio.testing import WorkflowEnvironment
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - environment/download issue → skip, don't fail
        pytest.skip(f"Temporal test server unavailable: {exc}")

    from temporalio.worker import Worker

    async with env:
        task_queue = "test-tq"
        async with Worker(
            env.client, task_queue=task_queue,
            workflows=[IngestInteractionWorkflow],
            activities=[activities.write_interaction, activities.fetch_dossier],
        ):
            result = await env.client.execute_workflow(
                IngestInteractionWorkflow.run,
                InteractionInput(entity_id="e-42", interaction_type="Site Visit", source="VOX"),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            )

    assert result.interaction_id
    assert result.dossier_counts.get("interactions") == 1
