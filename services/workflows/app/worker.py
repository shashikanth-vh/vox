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
    AdvayaHandoffWorkflow,
    CpcsChecklistWorkflow,
    DealStructuringWorkflow,
    DocumentCollectionWorkflow,
    IngestInteractionWorkflow,
    LeadConversionWorkflow,
    LeadQualificationWorkflow,
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
                   LeadConversionWorkflow, LeadQualificationWorkflow,
                   DealStructuringWorkflow, DocumentCollectionWorkflow,
                   AdvayaHandoffWorkflow, CpcsChecklistWorkflow],
        activities=[
            activities.write_interaction, activities.fetch_dossier,
            # VOX touchpoint set
            activities.resolve_entity, activities.create_entity,
            activities.find_active_lead, activities.create_lead,
            activities.update_lead_touch, activities.log_touchpoint,
            activities.assign_lead_owner,
            # Lead-conversion set. verify_decision MUST be registered: the
            # LeadConversionWorkflow calls it for every approve/reject, so a worker that omits
            # it would fail the run with "activity is not registered" and no decision could
            # ever complete.
            activities.get_lead, activities.verify_decision,
            activities.convert_lead_txn,
            activities.create_deal, activities.create_line,
            activities.mark_lead_converted, activities.mark_lead_note,
            activities.soft_delete_row,
            # Business-lifecycle set (qualification / structuring / document collection / Advaya).
            activities.attach_evidence, activities.advance_stage, activities.get_resource,
            activities.verify_committee_decision,
            activities.find_lines_for_deal,       # a deal's product lines (sanction fan-out)
            activities.prepare_cpcs_checklist,   # authoritative CP/CS checklist (maker)
            activities.create_handover_package,   # durable handover package + stage advance
            activities.record_advaya_handoff,   # future-Advaya hook (not on the handover path)
        ],
    )
    log.info("worker_started",
             extra={"task_queue": s.task_queue, "temporal": s.temporal_address})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
