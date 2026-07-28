"""The Orchestrator API — the operational front door of the workflow plane.

This is what was missing between "workflows exist" and "workflows run": an HTTP service
that STARTS workflows with **stable business workflow ids**, delivers **signals**
(approve/reject) and answers **status** queries. VocX, ATLAS, the gateway — anything that
can POST JSON — can now trigger durable work without a Temporal client or CLI.

Identity of a run = its business id:
    VOX capture        →  vox-{capture_id}
    lead conversion    →  leadconv-{lead_id}
Starting the same id twice attaches to the existing run instead of duplicating it —
idempotent starts on top of the Register-level idempotency the activities already carry.

Run it:  python -m app.api   (same image as the worker; a second container/deployment).
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.oidc import OidcError, OidcVerifier, bearer_token
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field
import httpx
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from app.config import get_settings
from app.types import LeadConversionInput, VoxTouchpoint
from app.workflows import LeadConversionWorkflow, VoxTouchpointWorkflow

log = get_logger("orchestrator")

# Who may decide which workflow. leadconv is a lead→deal conversion — a BD decision.
_APPROVER_ROLES: dict[str, set[str]] = {
    "leadconv": {"BD Head", "Management", "Admin"},
}


class VoxTouchpointIn(BaseModel):
    """The HTTP shape of a VOX capture — mirrors ``types.VoxTouchpoint``."""

    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(max_length=180)   # required here: it IS the workflow id
    company_name: str | None = Field(default=None, max_length=300)
    entity_id: str | None = None
    interaction_type: str = Field(default="In-Person Meeting", max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: str | None = None
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    transcript: str | None = None
    audio_ref: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    performed_by: str | None = Field(default=None, max_length=120)
    assigned_rm: str | None = Field(default=None, max_length=120)
    assigned_rm_id: str | None = None
    next_action: str | None = None
    next_action_date: str | None = None
    next_meeting_date: str | None = None
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)


class LeadConversionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str
    requested_by: str = Field(max_length=200)
    is_lending: bool = False
    is_syndication: bool = False
    is_asset_mon: bool = False
    product_type: str | None = Field(default=None, max_length=60)
    amount_cr: float | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    note: str | None = None
    approval_timeout_hours: int = Field(default=24 * 7, ge=1, le=24 * 90)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    note: str | None = None


def _problem(status: int, title: str, detail: str) -> ORJSONResponse:
    return ORJSONResponse(status_code=status, content={"error": {
        "type": title.lower().replace(" ", "_"), "title": title, "detail": detail}})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        # One Temporal client per process; connecting lazily on first request would hide
        # a bad TEMPORAL_ADDRESS until traffic arrives — fail loud at startup instead.
        app.state.temporal = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace)
        app.state.http = httpx.AsyncClient(timeout=10.0)
        app.state.oidc = (
            OidcVerifier(settings.oidc_issuer, settings.oidc_audience or None,
                         app.state.http, email_claim=settings.oidc_email_claim)
            if settings.oidc_issuer else None)
        log.info("orchestrator_started", extra={"temporal": settings.temporal_address,
                                                "task_queue": settings.task_queue})
        yield
        await app.state.http.aclose()

    app = FastAPI(title="PRISM Orchestrator", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")

    def denied(provided: str | None) -> ORJSONResponse | None:
        keys = settings.api_key_list()
        if not keys:
            return None
        if provided and any(hmac.compare_digest(provided, k) for k in keys):
            return None
        return _problem(401, "Unauthorized", "Missing or invalid X-API-Key.")

    async def start(request: Request, workflow_cls: Any, arg: Any,
                    workflow_id: str, *, restart_if_closed: bool = False) -> WorkflowHandle:
        """Idempotent start: if the business id is already RUNNING, attach to it. When
        ``restart_if_closed`` and the prior run has CLOSED (rejected/timed-out/failed),
        start a fresh attempt under ``{id}#{n}`` so a conversion can be retried cleanly
        without colliding with the terminal history."""
        client: Client = request.app.state.temporal
        try:
            return await client.start_workflow(
                workflow_cls.run, arg, id=workflow_id, task_queue=settings.task_queue)
        except TemporalError as exc:
            if "already started" not in str(exc).lower():
                raise
            handle = client.get_workflow_handle(workflow_id)
            if not restart_if_closed:
                return handle
            desc = await handle.describe()
            if desc.status == WorkflowExecutionStatus.RUNNING:
                return handle
            # Prior attempt is terminal → new attempt id.
            n = 2
            while True:
                try:
                    return await client.start_workflow(
                        workflow_cls.run, arg, id=f"{workflow_id}#{n}",
                        task_queue=settings.task_queue)
                except TemporalError as exc2:
                    if "already started" not in str(exc2).lower():
                        raise
                    h = client.get_workflow_handle(f"{workflow_id}#{n}")
                    if (await h.describe()).status == WorkflowExecutionStatus.RUNNING:
                        return h
                    n += 1

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": "prism-orchestrator"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Any:
        try:
            await request.app.state.temporal.service_client.check_health()
        except Exception as exc:  # noqa: BLE001
            return _problem(503, "Not ready", f"Temporal unreachable: {exc}")
        return {"status": "ready", "service": "prism-orchestrator"}

    @app.post("/v1/workflows/vox-touchpoints", status_code=202, tags=["Workflows"],
              summary="Start (or attach to) a VOX touchpoint workflow")
    async def start_vox(payload: VoxTouchpointIn, request: Request,
                        wait: bool = Query(default=False,
                                           description="Block until the run completes "
                                                       "and return its result"),
                        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                        ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        if not payload.company_name and not payload.entity_id:
            return _problem(422, "Validation failed",
                            "Provide company_name or entity_id.")
        wf_id = f"vox-{payload.capture_id}"
        handle = await start(request, VoxTouchpointWorkflow,
                             VoxTouchpoint(**payload.model_dump()), wf_id)
        if wait:
            result = await handle.result()
            return ORJSONResponse(status_code=200,
                                  content={"workflow_id": wf_id, "result": result})
        return ORJSONResponse(status_code=202, content={
            "workflow_id": wf_id, "status": "started",
            "status_url": f"/v1/workflows/{wf_id}"})

    @app.post("/v1/workflows/lead-conversions", status_code=202, tags=["Workflows"],
              summary="Request a lead→deal conversion (waits for approve/reject)")
    async def start_conversion(payload: LeadConversionIn, request: Request,
                               x_api_key: str | None = Header(default=None,
                                                              alias="X-API-Key"),
                               ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        wf_id = f"leadconv-{payload.lead_id}"
        handle = await start(request, LeadConversionWorkflow,
                             LeadConversionInput(**payload.model_dump()), wf_id,
                             restart_if_closed=True)
        wf_id = handle.id  # may be the #n retry id if a prior attempt had closed
        return ORJSONResponse(status_code=202, content={
            "workflow_id": wf_id, "status": "pending approval",
            "approve_url": f"/v1/workflows/{wf_id}/approve",
            "reject_url": f"/v1/workflows/{wf_id}/reject",
            "status_url": f"/v1/workflows/{wf_id}"})

    async def _decider(request: Request, workflow_id: str,
                       payload: DecisionIn) -> tuple[str, ORJSONResponse | None]:
        """The trustworthy decider identity + a role check. With OIDC configured the
        e-mail comes from the verified token (never the caller-supplied 'by'), and the
        Access service confirms an approver role for the workflow's vertical."""
        verifier: OidcVerifier | None = request.app.state.oidc
        decided_by = payload.by
        if verifier is not None:
            token = bearer_token(request.headers.get("Authorization"))
            if not token:
                return "", _problem(401, "Unauthorized", "Bearer token required.")
            try:
                ident = await verifier.verify(token)
            except OidcError as exc:
                return "", _problem(401, "Unauthorized", f"Invalid token: {exc}")
            decided_by = ident.email
        # Role check (needs the Access service): the decider must hold an approver role.
        prefix = workflow_id.split("-", 1)[0]
        needed = _APPROVER_ROLES.get(prefix)
        if needed and settings.access_url:
            try:
                resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": decided_by},
                    headers={"X-API-Key": settings.access_api_key})
                roles = set(resp.json().get("roles", [])) if resp.status_code == 200 else set()
            except httpx.HTTPError as exc:
                return "", _problem(502, "Upstream unavailable", f"Access: {exc}")
            if not (roles & needed):
                return "", _problem(
                    403, "Forbidden",
                    f"'{decided_by}' lacks an approver role {sorted(needed)} for {prefix}.")
        return decided_by, None

    async def _signal(request: Request, workflow_id: str, name: str,
                      payload: DecisionIn, decided_by: str) -> Any:
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(name, args=[decided_by, payload.note])
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        return {"workflow_id": workflow_id, "signalled": name, "by": decided_by}

    @app.post("/v1/workflows/{workflow_id}/approve", tags=["Workflows"],
              summary="Approve a pending human-in-the-loop workflow")
    async def approve(workflow_id: str, payload: DecisionIn, request: Request,
                      x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                      ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        decided_by, err = await _decider(request, workflow_id, payload)
        if err is not None:
            return err
        return await _signal(request, workflow_id, "approve", payload, decided_by)

    @app.post("/v1/workflows/{workflow_id}/reject", tags=["Workflows"],
              summary="Reject a pending human-in-the-loop workflow")
    async def reject(workflow_id: str, payload: DecisionIn, request: Request,
                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        decided_by, err = await _decider(request, workflow_id, payload)
        if err is not None:
            return err
        return await _signal(request, workflow_id, "reject", payload, decided_by)

    @app.get("/v1/workflows/{workflow_id}", tags=["Workflows"],
             summary="A run's live status (execution state + in-workflow stage)")
    async def describe(workflow_id: str, request: Request,
                       x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                       ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        out: dict[str, Any] = {
            "workflow_id": workflow_id,
            "run_id": desc.run_id,
            "status": desc.status.name if desc.status else "UNKNOWN",
            "workflow_type": desc.workflow_type,
            "started_at": desc.start_time.isoformat() if desc.start_time else None,
            "closed_at": desc.close_time.isoformat() if desc.close_time else None,
        }
        # The in-workflow stage query only answers while the run is open.
        if desc.status == WorkflowExecutionStatus.RUNNING:
            try:
                out["stage"] = await handle.query("status")
            except (RPCError, TemporalError):
                pass
        elif desc.status == WorkflowExecutionStatus.COMPLETED:
            out["result"] = await handle.result()
        return out

    return app


app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    s = get_settings()
    uvicorn.run("app.api:app", host=s.api_host, port=s.api_port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
