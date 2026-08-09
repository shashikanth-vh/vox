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
from app.api.closure import router as closure_router
from app.api.covenants import router as covenants_router
from app.api.cpcs import router as cpcs_router
from app.api.custom import router as custom_router
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
from app.api.tenants import router as tenants_router
from app.api.tranches import router as tranches_router
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

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

    return app


app = create_app()
