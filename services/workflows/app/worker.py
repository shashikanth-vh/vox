"""Worker entrypoint — connects to Temporal and runs the workflows + activities.

    python -m app.worker
"""

from __future__ import annotations

import asyncio

from evam_backend_core.logging import configure_logging, get_logger
from temporalio.client import Client
from temporalio.worker import Worker

from app import activities
from app.config import get_settings
from app.workflows import (
    IngestInteractionWorkflow,
    LeadConversionWorkflow,
    VoxTouchpointWorkflow,
)

log = get_logger("workflows")


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, json_logs=s.log_json and not s.is_local)
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[IngestInteractionWorkflow, VoxTouchpointWorkflow,
                   LeadConversionWorkflow],
        activities=[
            activities.write_interaction, activities.fetch_dossier,
            # VOX touchpoint set
            activities.resolve_entity, activities.create_entity,
            activities.find_active_lead, activities.create_lead,
            activities.update_lead_touch, activities.log_touchpoint,
            activities.assign_lead_owner,
            # Lead-conversion set
            activities.get_lead, activities.create_deal, activities.create_line,
            activities.mark_lead_converted, activities.mark_lead_note,
            activities.soft_delete_row,
        ],
    )
    log.info("worker_started",
             extra={"task_queue": s.task_queue, "temporal": s.temporal_address})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
