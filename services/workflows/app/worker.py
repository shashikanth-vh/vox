"""Worker entrypoint — connects to Temporal and runs the workflows + activities.

    python -m app.worker
"""

from __future__ import annotations

import asyncio

from evam_backend_core.logging import configure_logging, get_logger
from temporalio.client import Client
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

from app import activities
from app.codec import build_data_converter
from app.config import get_settings
from app.workflows import (
    AdvayaHandoffWorkflow,
    AssetMonetisationWorkflow,
    SanctionExpiryMonitorWorkflow,
    SyndicationMandateWorkflow,
    CovenantMonitorWorkflow,
    CpcsChecklistWorkflow,
    DealStructuringWorkflow,
    DocumentCollectionWorkflow,
    DocumentExpiryMonitorWorkflow,
    EwsCaseWorkflow,
    IngestInteractionWorkflow,
    LeadConversionWorkflow,
    LeadQualificationWorkflow,
    VoxTouchpointWorkflow,
)

log = get_logger("workflows")


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, json_logs=s.log_json and not s.is_local)
    # SDK metrics: with a bind address set, the worker exposes a Prometheus scrape endpoint
    # (task/activity latencies, failures, slot usage, cache) — the operational dashboard's
    # data source. Off by default.
    runtime = None
    if s.metrics_bind_address:
        runtime = Runtime(telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=s.metrics_bind_address)))
    client = await Client.connect(
        s.temporal_address, namespace=s.temporal_namespace,
        data_converter=build_data_converter(s.payload_encryption_key),
        runtime=runtime)
    # Worker build identity: stamping runs with the build that processed them is the basis
    # for safe worker upgrades; full versioned task routing additionally needs server-side
    # rules (see README).
    versioning_kwargs: dict = {}
    if s.worker_build_id:
        versioning_kwargs = {"build_id": s.worker_build_id,
                             "use_worker_versioning": s.use_worker_versioning}
    worker = Worker(
        client,
        task_queue=s.task_queue,
        **versioning_kwargs,
        workflows=[IngestInteractionWorkflow, VoxTouchpointWorkflow,
                   LeadConversionWorkflow, LeadQualificationWorkflow,
                   DealStructuringWorkflow, DocumentCollectionWorkflow,
                   AdvayaHandoffWorkflow, CpcsChecklistWorkflow,
                   SanctionExpiryMonitorWorkflow, SyndicationMandateWorkflow,
                   AssetMonetisationWorkflow, DocumentExpiryMonitorWorkflow,
                   CovenantMonitorWorkflow, EwsCaseWorkflow],
        activities=[
            activities.write_interaction, activities.fetch_dossier,
            # VOX touchpoint set
            activities.resolve_entity, activities.create_entity,
            activities.find_active_lead, activities.find_active_leads,
            activities.resolve_entity_candidates, activities.create_lead,
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
            activities.verify_facility_decisions,  # per-facility committee outcomes
            activities.verify_syndication_decision,
            activities.verify_am_decision,
            activities.find_lines_for_deal,       # a deal's product lines (sanction fan-out)
            activities.update_fields,             # plain data update, no lifecycle change
            activities.prepare_cpcs_checklist,   # authoritative CP/CS checklist (maker)
            activities.create_handover_package,   # durable handover package + stage advance
            activities.record_advaya_handoff,   # future-Advaya hook (not on the handover path)
            # Release-1 foundation: run-control verification + operational events.
            activities.verify_control,
            activities.emit_operational_event,
            # Increment 7: first-class calendar events + the document-expiry sweep.
            activities.create_calendar_event,
            activities.sweep_document_expiry,
            # Increment 8: the covenant sweep + EWS SLA plumbing.
            activities.sweep_covenants,
            activities.auto_escalate_ews_case,
        ],
    )
    log.info("worker_started",
             extra={"task_queue": s.task_queue, "temporal": s.temporal_address})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
