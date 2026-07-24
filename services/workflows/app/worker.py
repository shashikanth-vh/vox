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
from app.workflows import IngestInteractionWorkflow

log = get_logger("workflows")


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, json_logs=s.log_json and not s.is_local)
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    worker = Worker(
        client,
        task_queue=s.task_queue,
        workflows=[IngestInteractionWorkflow],
        activities=[activities.write_interaction, activities.fetch_dossier],
    )
    log.info("worker_started",
             extra={"task_queue": s.task_queue, "temporal": s.temporal_address})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
