"""FastAPI application factory for the PRISM Register.

Assembled from ``evam_backend_core`` (the shared backend platform) — logging, error
contract, request correlation, transient-retry, DB lifespan and health probes come from
there; this module supplies only the Register's routers and identity.
"""

from __future__ import annotations

from evam_backend_core.app import create_service_app
from fastapi import FastAPI

from app.api.activity import router as activity_router
from app.api.advaya import router as advaya_router
from app.api.advaya_manual import router as advaya_manual_router
from app.api.calendar import router as calendar_router
from app.api.chitti import router as chitti_router
from app.api.panorama import router as panorama_router
from app.api.tracxn import router as tracxn_router
from app.api.prospect_rules import router as prospects_router
from app.api.closure import router as closure_router
from app.api.covenants import router as covenants_router
from app.api.cpcs import router as cpcs_router
from app.api.custom import router as custom_router
from app.api.lead_lookup import router as lead_lookup_router
from app.api.intake import router as intake_router
from app.api.decisions import router as decisions_router
from app.api.documents_lifecycle import router as documents_lifecycle_router
from app.api.evidence import router as evidence_router
from app.api.ews import router as ews_router
from app.api.export import router as export_router
from app.api.export_ledger import router as export_ledger_router
from app.api.followups import router as followups_router
from app.api.handover import router as handover_router
from app.api.imports import router as import_router
from app.api.notifications import router as notifications_router
from app.api.people_sync import router as people_sync_router
from app.api.lms import router as lms_router
from app.api.rbac import router as rbac_router
from app.api.reconciliation import router as reconciliation_router
from app.api.resources import build_resource_router
from app.api.sanction import router as sanction_router
from app.api.series import router as series_router
from app.api.vox import router as vox_router
from app.api.tenants import router as tenants_router
from app.api.tranches import router as tranches_router
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


async def _intake_chase_loop(interval_s: int) -> None:
    """Every ``interval_s`` seconds, run the enquiry chase (reminders, escalations)
    for every active tenant — inside the register, so no extra container."""
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import select, text

    from app.api.intake import sweep_intake
    from app.db.session import get_sessionmaker
    from app.models.system import Tenant

    await asyncio.sleep(60)   # let the service settle first
    while True:
        try:
            sm = get_sessionmaker()
            async with sm() as session:
                tenants = (await session.execute(
                    select(Tenant.id, Tenant.code).where(Tenant.is_active.is_(True)))).all()
            for tid, code in tenants:
                async with sm() as session:
                    await session.execute(text("SELECT set_config('app.current_tenant', :tid, true)"),
                                          {"tid": str(tid)})
                    ctx = SimpleNamespace(session=session, tenant_id=tid, actor="intake:sweep", user=None)
                    out = await sweep_intake(ctx)   # type: ignore[arg-type]
                    await session.commit()
                    if out["reminded"] or out["escalated"] or out["stranded"]:
                        log.info("intake_chase", extra={"tenant": code, **{k: len(v) for k, v in out.items()}})
        except Exception as exc:  # noqa: BLE001 - one bad sweep never kills the service
            log.warning("intake_chase_failed", extra={"error": str(exc)})
        await asyncio.sleep(max(60, interval_s))

DESCRIPTION = """
The **Register** is PRISM's single source of truth — every entity, deal, financial,
contract, touchpoint, signal and behavioural record. It is entity-centric, tenant-aware,
product-aware and versioned.

Auth: send an `X-API-Key` header. Bind a tenant with `X-Tenant` (default `EVAM`).
Optimistic concurrency: pass `If-Match: "<version>"` (or `expected_version`) on writes.
Idempotent creates: pass an `Idempotency-Key` header.
""".strip()


def create_app() -> FastAPI:
    settings = get_settings()
    # Fail closed on an incomplete future-integration configuration (e.g. Advaya enabled with no
    # endpoint) — the acknowledgement path can never be half-enabled.
    settings.validate_startup()
    routers = [
        tenants_router,
        rbac_router,
        export_router,
        export_ledger_router,
        import_router,
        custom_router,
        lead_lookup_router,
        intake_router,
        activity_router,
        decisions_router,
        cpcs_router,
        sanction_router,
        people_sync_router,
        handover_router,
        # The MANUAL Advaya attestation lane is ALWAYS on — it exists precisely for
        # deployments where the real integration is not live yet: an authorised human
        # relays Advaya's offline confirmation on their own identity.
        advaya_manual_router,
        tranches_router,
        lms_router,
        calendar_router,
        documents_lifecycle_router,
        notifications_router,
        covenants_router,
        followups_router,
        ews_router,
        closure_router,
        reconciliation_router,
        evidence_router,
        series_router,
        vox_router,
        # Chitti (chatbot) read-only machine lane — svc_chitti principal only.
        chitti_router,
        # BEFORE the generic resources: /v1/prospects/import|export-xlsx|facets must
        # not be swallowed by the generic /v1/prospects/{obj_id} path parameter.
        prospects_router,
        # Company 360 — one company's whole story, RBAC'd per section.
        panorama_router,
        tracxn_router,
        build_resource_router(),
    ]
    # The DORMANT Advaya acknowledgement path (internal handoff record) is registered ONLY under an
    # enabled Advaya integration (default off). Without it, the acknowledgement endpoint does not
    # exist and 'Disbursement Pending' is unreachable — PRISM stops at 'Disbursed'.
    if settings.advaya_integration_enabled:
        routers.insert(6, advaya_router)
    app = create_service_app(
        settings=settings,
        routers=routers,
        title="PRISM Register",
        version="0.1.0",
        description=DESCRIPTION,
    )

    @app.get("/", tags=["Health"], include_in_schema=False)
    async def root() -> dict:
        return {"service": settings.app_name, "docs": "/docs", "health": "/healthz"}

    # The enquiry chase (reminders, escalations) runs inside the register, hourly,
    # on top of the core lifespan; 0 switches it off (the sweep endpoint remains).
    if int(settings.intake_sweep_interval_s) > 0:
        import asyncio
        from contextlib import asynccontextmanager

        core_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(a):  # noqa: ANN001, ANN202
            async with core_lifespan(a):
                task = asyncio.create_task(_intake_chase_loop(int(settings.intake_sweep_interval_s)))
                try:
                    yield
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        app.router.lifespan_context = lifespan
    return app


app = create_app()
