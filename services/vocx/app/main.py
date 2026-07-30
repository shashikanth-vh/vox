"""PRISM VocX — voice-based field touchpoint capture (formerly "VOX").

A captured touchpoint (transcript, GPS, attendees, structured key intel) is written to
the Register as an interaction with ``source: "VocX"`` through the platform SDK. The
capture id doubles as the idempotency key, so a flaky uplink retrying the same upload
can never create a duplicate touchpoint — Temporal-grade exactly-once effect without a
workflow.

Stateless and individually deployable: own Dockerfile, Compose service, Helm subchart.
"""

from __future__ import annotations

import hmac
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_register_client import AsyncRegisterClient
from evam_register_client.errors import RegisterError
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

log = get_logger("vocx")

SUBJECT_TYPES = {"Lead", "Deal", "Entity", "Counterparty", "Lending", "Syndication",
                 "AssetMonetisation"}


class TouchpointIn(BaseModel):
    """One captured field touchpoint. Speech-to-text happens upstream — VocX receives
    the transcript and the intel already structured by the capture app."""

    model_config = ConfigDict(extra="forbid")

    # What the touchpoint is about. EITHER a resolved subject (ATLAS refType/refId) —
    # the direct fast path — OR just the company name: with an orchestrator configured,
    # VocX starts a durable VoxTouchpointWorkflow that resolves the company canonically
    # and creates the entity + lead when they don't exist yet.
    subject_type: str | None = Field(default=None, max_length=30)
    subject_id: uuid.UUID | None = None
    company_name: str | None = Field(default=None, max_length=300)
    interaction_type: str = Field(default="In-Person Meeting", max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: str | None = None            # ISO timestamp; defaults to now server-side
    # Who captured it.
    performed_by: str | None = Field(default=None, max_length=120)
    contact_name: str | None = Field(default=None, max_length=200)
    # The capture itself.
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    transcript: str | None = None
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    next_action: str | None = None
    next_action_date: str | None = None
    next_meeting_date: str | None = None
    # Workflow-path extras: the recording's storage URI and the owning RM.
    audio_ref: str | None = Field(default=None, max_length=500)
    assigned_rm: str | None = Field(default=None, max_length=120)
    assigned_rm_id: str | None = None
    # Hints used only when the workflow has to CREATE the company.
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)
    # Stable id of the recording/upload — becomes the idempotency key (and, on the
    # workflow path, the business workflow id vox-{capture_id}).
    capture_id: str | None = Field(default=None, max_length=180)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.register = AsyncRegisterClient(
            base_url=settings.register_base_url,
            api_key=settings.register_api_key,
            tenant=settings.register_tenant,
            actor="vocx",
        )
        app.state.http = httpx.AsyncClient(timeout=60.0)
        log.info("vocx_started", extra={"register": settings.register_base_url,
                                        "orchestrator": settings.orchestrator_url or None})
        yield
        await app.state.register.aclose()
        await app.state.http.aclose()

    app = FastAPI(title="PRISM VocX", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name}

    # VOX — the vendored voice pipeline (STT → extraction → resolve → gate → commit).
    # Backend-only: JSON endpoints under /v1/*, reached through the edge as
    # /vocx/v1/*. The existing /v1/touchpoints flow below is untouched.
    if settings.vocx_pipeline_enabled:
        from app.vocx.mount import build_vocx_router

        app.include_router(build_vocx_router(settings))

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        return {"status": "ready", "service": settings.app_name}

    def _front_door_denied(request: Request) -> ORJSONResponse | None:
        """VocX's own front door: when VOCX_API_KEYS is set, the caller MUST present the
        gateway-injected key. So VocX is not an open endpoint any pod can call directly."""
        keys = [k.strip() for k in settings.api_keys.split(",") if k.strip()]
        if not keys:
            return None  # dev / open (rely on a NetworkPolicy)
        provided = request.headers.get("X-API-Key", "")
        if any(hmac.compare_digest(provided, k) for k in keys):
            return None
        return ORJSONResponse(status_code=401, content={"error": {
            "type": "unauthorized", "title": "Unauthorized",
            "detail": "Missing or invalid X-API-Key."}})

    def _verify_context(request: Request):  # noqa: ANN202
        """The VERIFIED caller context the gateway minted FOR THIS HOP, or None.

        Beyond the signature, the token's BINDING is enforced: it must be bound to THIS
        route (POST /v1/touchpoints) and its signed tenant must equal the request's
        X-Tenant. This blocks replay of a token minted for another route or another tenant —
        without it, a holder of the VocX key could re-mint a tenant-A token for tenant B."""
        if not settings.internal_signing_secret:
            return None
        raw = request.headers.get("X-Internal-Context")
        if not raw:
            return None
        from evam_backend_core.internal_token import (
            InternalTokenError,
            verify_internal_context,
        )
        try:
            ic = verify_internal_context(
                raw, verify_key=settings.internal_signing_secret,
                algorithms=(settings.internal_signing_algorithm,))
        except InternalTokenError as exc:
            log.warning("vocx_context_verify_failed", extra={"error": str(exc)})
            return None
        req_tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        # REQUIRE the binding claims to be PRESENT and EXACT — an UNBOUND token (method/path
        # absent) is rejected, not accepted. Without this, a valid signed token with no route
        # binding could be replayed onto this route / another tenant.
        if ic.method != request.method:
            log.warning("vocx_context_method", extra={"tok": ic.method})
            return None
        if ic.path != request.url.path:
            log.warning("vocx_context_path", extra={"tok": ic.path})
            return None
        if not ic.tenant or ic.tenant != req_tenant:
            log.warning("vocx_context_tenant", extra={"tok": ic.tenant, "req": req_tenant})
            return None
        return ic

    def _delegation_required() -> bool:
        """Production posture: a user-triggered capture must carry a valid delegated
        identity. Implied whenever the signed channel is configured."""
        return settings.require_delegation or bool(settings.internal_signing_secret)

    def _delegated_register(request: Request):  # noqa: ANN202
        """A per-request Register client that PROPAGATES the caller's identity: VocX verifies
        the gateway's context and RE-MINTS one bound to the Register interaction write, so the
        HUMAN's live grant — not VocX's service key — is the authorization identity. Returns
        None when there is no verifiable caller context (dev / pure machine)."""
        ic = _verify_context(request)
        if ic is None:
            return None
        from evam_backend_core.internal_token import mint_internal_context
        from evam_register_client.config import RegisterClientConfig
        tenant = ic.tenant or settings.register_tenant   # the VERIFIED tenant, not the header
        minted = mint_internal_context(
            signing_key=settings.internal_signing_secret,
            algorithm=settings.internal_signing_algorithm,
            ttl_seconds=settings.internal_token_ttl_seconds,
            tenant=tenant, email=ic.email, user_id=ic.user_id, roles=list(ic.roles),
            report_ids=list(ic.report_ids), report_emails=list(ic.report_emails),
            effective_views=ic.effective_views, effective_operations=ic.effective_operations,
            matrix_version=ic.matrix_version, decision=ic.decision,
            method="POST", path="/v1/interactions")
        cfg = RegisterClientConfig(
            base_url=settings.register_base_url, api_key=settings.register_api_key,
            tenant=tenant, actor="vocx", extra_headers={"X-Internal-Context": minted})
        return AsyncRegisterClient(config=cfg)

    @app.post("/v1/touchpoints", status_code=201, tags=["Touchpoints"],
              summary="Capture a field touchpoint → an interaction in the Register")
    async def capture_touchpoint(payload: TouchpointIn, request: Request) -> ORJSONResponse:
        # VocX's own front door (the gateway-injected key) — not an open endpoint.
        if (denied := _front_door_denied(request)) is not None:
            return denied
        # A user-triggered capture MUST carry a valid delegated identity in production:
        # verify it up front and FAIL CLOSED rather than silently writing as VocX's key.
        ctx = _verify_context(request)
        if _delegation_required() and ctx is None:
            return ORJSONResponse(status_code=403, content={"error": {
                "type": "forbidden", "title": "Delegation required",
                "detail": "This capture must carry a verified caller identity "
                          "(signed X-Internal-Context from the gateway), bound to this "
                          "route and tenant."}})
        # The tenant is the VERIFIED one from the token (not the caller-controlled X-Tenant)
        # whenever a context is present — so a replayed token can't be re-scoped to another
        # tenant. _verify_context already asserted ic.tenant == X-Tenant.
        tenant = ctx.tenant if ctx is not None else request.headers.get(
            "X-Tenant", settings.register_tenant)

        # -- Workflow path: company-centric capture (new or unresolved company) -------
        if payload.subject_id is None:
            if not payload.company_name:
                return ORJSONResponse(status_code=422, content={"error": {
                    "type": "validation_error", "title": "Validation failed",
                    "detail": "Provide subject_type+subject_id, or company_name."}})
            if not settings.orchestrator_url:
                return ORJSONResponse(status_code=422, content={"error": {
                    "type": "validation_error", "title": "Validation failed",
                    "detail": "company_name capture needs the workflow plane: set "
                              "VOCX_ORCHESTRATOR_URL (see services/workflows)."}})
            body = payload.model_dump(exclude_none=True,
                                      exclude={"subject_type", "subject_id"})
            body.setdefault("capture_id", uuid.uuid4().hex)
            # Forward the TENANT and the DELEGATED identity so the workflow runs in the
            # caller's tenant and its Register writes are authorized as the human — not just
            # under VocX/svc_workflows keys. (Same X-Internal-Context the gateway minted for
            # this hop; the orchestrator verifies it and threads it into the workflow.)
            headers = {"X-Tenant": tenant}
            if settings.orchestrator_api_key:
                headers["X-API-Key"] = settings.orchestrator_api_key
            # RE-MINT a token BOUND to the orchestrator's route for THIS hop (the token the
            # gateway minted is bound to VocX's own /v1/touchpoints and must not be replayed
            # onward). The orchestrator enforces this binding.
            if ctx is not None and settings.internal_signing_secret:
                from evam_backend_core.internal_token import mint_internal_context
                headers["X-Internal-Context"] = mint_internal_context(
                    signing_key=settings.internal_signing_secret,
                    algorithm=settings.internal_signing_algorithm,
                    ttl_seconds=settings.internal_token_ttl_seconds,
                    tenant=tenant, email=ctx.email, user_id=ctx.user_id,
                    roles=list(ctx.roles), report_ids=list(ctx.report_ids),
                    report_emails=list(ctx.report_emails),
                    effective_views=ctx.effective_views,
                    effective_operations=ctx.effective_operations,
                    matrix_version=ctx.matrix_version, decision=ctx.decision,
                    method="POST", path="/v1/workflows/vox-touchpoints")
            try:
                resp = await request.app.state.http.post(
                    f"{settings.orchestrator_url.rstrip('/')}/v1/workflows/vox-touchpoints",
                    json=body, params={"wait": "true"}, headers=headers)
            except httpx.HTTPError as exc:
                return ORJSONResponse(status_code=502, content={"error": {
                    "type": "orchestrator_error", "title": "Orchestrator unreachable",
                    "detail": str(exc)}})
            return ORJSONResponse(status_code=resp.status_code if resp.status_code >= 400
                                  else 201, content=resp.json())

        # -- Direct fast path: the subject is already resolved ------------------------
        if payload.subject_type not in SUBJECT_TYPES:
            return ORJSONResponse(status_code=422, content={"error": {
                "type": "validation_error", "title": "Validation failed",
                "detail": f"Unknown subject_type '{payload.subject_type}'. "
                          f"One of: {', '.join(sorted(SUBJECT_TYPES))}."}})
        data = payload.model_dump(exclude_none=True, exclude={"capture_id"})
        subject_type = data.pop("subject_type")
        subject_id = str(data.pop("subject_id"))
        interaction_type = data.pop("interaction_type")
        # Exactly-once: the capture id keys the write; a retried upload replays.
        idem = f"vocx:{payload.capture_id}" if payload.capture_id else None
        # Propagate the human's identity to the Register when possible, so the write is
        # authorized as the CALLER (not VocX's service key). Falls back to the shared
        # service-key client (dev / pure machine call).
        delegated = _delegated_register(request)
        reg = delegated or request.app.state.register
        try:
            interaction = await reg.log_interaction(
                subject_type, subject_id, interaction_type,
                source="VocX", idempotency_key=idem,
                **data,
            )
        except RegisterError as exc:
            return ORJSONResponse(status_code=exc.status or 502, content={"error": {
                "type": "register_error", "title": "Register rejected the touchpoint",
                "detail": str(exc)}})
        finally:
            if delegated is not None:
                await delegated.aclose()
        return ORJSONResponse(status_code=201, content={
            "interaction_id": interaction["id"],
            "entity_id": interaction.get("entity_id"),
            "deal_id": interaction.get("deal_id"),
            "source": interaction.get("source"),
        })

    return app


app = create_app()
