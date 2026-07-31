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

import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from typing import Any

from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.oidc import (
    OidcError,
    TokenVerifier,
    bearer_token,
    build_verifier,
)
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field
import httpx
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from app.config import get_settings
from app.types import (
    AdvayaHandoffInput,
    CallerContext,
    CpcsChecklistInput,
    DealStructuringInput,
    DocumentCollectionInput,
    LeadConversionInput,
    LeadQualificationInput,
    VoxTouchpoint,
)
from app.workflows import (
    AdvayaHandoffWorkflow,
    CpcsChecklistWorkflow,
    DealStructuringWorkflow,
    DocumentCollectionWorkflow,
    LeadConversionWorkflow,
    LeadQualificationWorkflow,
    VoxTouchpointWorkflow,
)

log = get_logger("orchestrator")

# Who may decide which workflow, keyed by the workflow-id PREFIX. leadconv is a lead→deal
# conversion (a BD decision); struct is the Credit Committee's sanction decision on a structured
# deal (credit authority only).
_APPROVER_ROLES: dict[str, set[str]] = {
    "leadconv": {"BD Head", "Management", "Admin"},
    "struct": {"Credit Head", "Management", "Admin"},
    # Handing a facility OVER to Advaya is a money-movement authorization — senior credit authority.
    "handover": {"Credit Head", "Management", "Admin"},
    # Approving a CP/CS checklist (the checker) — senior credit authority.
    "cpcs": {"Credit Head", "Management", "Admin"},
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
    # Access user ids for the auto-created product-line assignments (verified server-side).
    # Without these, RM/analyst line assignments are never created on conversion.
    rm_id: str | None = None
    analyst_id: str | None = None
    note: str | None = None
    approval_timeout_hours: int = Field(default=24 * 7, ge=1, le=24 * 90)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    note: str | None = None


class LeadQualificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: str
    qualified_by: str = Field(max_length=200)
    qualification_reference: str = Field(default="", max_length=500)
    qualification_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    passed: bool = True
    reason: str | None = Field(default=None, max_length=2000)


class DealStructuringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deal_id: str
    requested_by: str = Field(max_length=200)
    product_type: str | None = Field(default=None, max_length=60)
    rm: str | None = Field(default=None, max_length=120)
    credit_note_reference: str = Field(default="", max_length=500)
    decision_timeout_hours: int = Field(default=24 * 14, ge=1, le=24 * 90)


class CommitteeDecisionIn(BaseModel):
    """The Credit Committee's recorded decision on a structured deal, delivered through the
    orchestrator (fresh-authorized + durably persisted BEFORE the workflow is signalled)."""

    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    approved: bool
    committee_reference: str = Field(default="", max_length=500)
    sanction_letter_reference: str = Field(default="", max_length=500)
    note: str | None = Field(default=None, max_length=2000)


class DocumentCollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str = Field(pattern="^(Deal|Lending)$")
    subject_id: str
    requested_by: str = Field(max_length=200)
    required_documents: list[str] = Field(default_factory=list, max_length=100)
    collection_timeout_hours: int = Field(default=24 * 30, ge=1, le=24 * 120)


class HandoverDocRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class AdvayaHandoverIn(BaseModel):
    """MAKER prepares the Advaya handover of a Lending line that is 'Ready for Disbursement'.
    Requires senior credit authority (Credit Head / Management / Admin). The workflow prepares the
    durable handover package (authoritative amounts + package digest generated server-side); a
    DIFFERENT checker must then approve it to advance the line to 'Disbursed'."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str
    requested_by: str = Field(max_length=200)          # the maker (authenticated)
    executed_document_refs: list[HandoverDocRef] = Field(min_length=1, max_length=100)
    cpcs_checklist_version: int | None = Field(default=None, ge=1)
    delivery_method: str = Field(min_length=1, max_length=60)
    recipient: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class CpcsItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=300)
    condition_type: str = Field(pattern="^(CP|CS)$")
    required: bool = True
    status: str = Field(default="Pending", pattern="^(Pending|Completed|Waived|Deferred as CS)$")
    reason: str | None = Field(default=None, max_length=1000)
    expiry_date: str | None = None
    evidence_ref: str | None = Field(default=None, max_length=300)
    note: str | None = None


class CpcsChecklistIn(BaseModel):
    """MAKER prepares the CP/CS checklist for a Lending line. A DIFFERENT checker then approves it."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str
    requested_by: str = Field(max_length=200)          # the maker (authenticated)
    items: list[CpcsItemIn] = Field(min_length=1)
    deal_id: str | None = Field(default=None, max_length=64)
    checklist_version: int = Field(default=1, ge=1)
    note: str | None = Field(default=None, max_length=2000)


class CpcsApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(max_length=200)


class AdvayaHandoverApproveIn(BaseModel):
    """CHECKER approves a prepared handover. The checker is the AUTHENTICATED caller (resolved from
    the verified identity), and must be a different person than the maker (enforced by the
    Register). ``approved_by`` is only a dev fallback when no OIDC identity is configured."""

    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(max_length=200)


class DocumentReceivedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    reference: str = Field(default="", max_length=500)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")


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
            build_verifier(
                app.state.http, issuer=settings.oidc_issuer,
                audience=settings.oidc_audience or None,
                issuers_spec=settings.oidc_issuers,
                email_claim=settings.oidc_email_claim,
                allowed_domains=settings.oidc_allowed_domains.split(",")))
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
                    workflow_id: str, *, restart_if_closed: bool = False,
                    memo: dict | None = None) -> WorkflowHandle:
        """Idempotent start: if the business id is already RUNNING, attach to it. When
        ``restart_if_closed`` and the prior run has CLOSED (rejected/timed-out/failed),
        start a fresh attempt under a **URL-safe** ``{id}-r{n}`` suffix so a conversion can be
        retried cleanly without colliding with the terminal history. (``#`` was NOT URL-safe:
        browsers/clients treat everything after it as a fragment, so the generated approval and
        decision-lookup URLs silently dropped the suffix and addressed the wrong workflow.)
        ``memo`` records the initiator + tenant for subject-level status scoping."""
        client: Client = request.app.state.temporal
        try:
            return await client.start_workflow(
                workflow_cls.run, arg, id=workflow_id, task_queue=settings.task_queue,
                memo=memo)
        except TemporalError as exc:
            if "already started" not in str(exc).lower():
                raise
            handle = client.get_workflow_handle(workflow_id)
            if not restart_if_closed:
                return handle
            desc = await handle.describe()
            if desc.status == WorkflowExecutionStatus.RUNNING:
                return handle
            # Prior attempt is terminal → new attempt id (URL-safe suffix).
            n = 2
            while True:
                retry_id = f"{workflow_id}-r{n}"
                try:
                    return await client.start_workflow(
                        workflow_cls.run, arg, id=retry_id,
                        task_queue=settings.task_queue, memo=memo)
                except TemporalError as exc2:
                    if "already started" not in str(exc2).lower():
                        raise
                    h = client.get_workflow_handle(retry_id)
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
        caller, verified = _caller_context(request)
        # FAIL CLOSED: with signing configured, a workflow that will WRITE (create entity /
        # lead / interaction) must carry a verified, route-bound delegated identity — never
        # start it to run under the service key's authority.
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"vox-{_tenant_slug(caller.tenant)}-{payload.capture_id}"
        memo = {"initiator": (caller.email or ""), "tenant": caller.tenant}
        handle = await start(request, VoxTouchpointWorkflow,
                             VoxTouchpoint(caller=caller, **payload.model_dump()), wf_id,
                             memo=memo)
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
        # The requester is bound to the VERIFIED identity when OIDC is on (and mandatory
        # under require_auth) — a conversion can never be requested under a spoofed name.
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        payload = payload.model_copy(update={"requested_by": requested_by})
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "a conversion.")
        wf_id = f"leadconv-{_tenant_slug(caller.tenant)}-{payload.lead_id}"
        # Record the INITIATOR + tenant in the workflow memo so status/result can be scoped
        # to the initiator or an approver — not any same-tenant caller. The real lead_id is
        # carried here too, so a decision NEVER derives it from a (retry-suffixed) workflow id.
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lead_id": payload.lead_id}
        handle = await start(request, LeadConversionWorkflow,
                             LeadConversionInput(caller=caller, **payload.model_dump()),
                             wf_id, restart_if_closed=True, memo=memo)
        wf_id = handle.id  # may be the #n retry id if a prior attempt had closed
        return ORJSONResponse(status_code=202, content={
            "workflow_id": wf_id, "status": "pending approval",
            "approve_url": f"/v1/workflows/{wf_id}/approve",
            "reject_url": f"/v1/workflows/{wf_id}/reject",
            "status_url": f"/v1/workflows/{wf_id}"})

    def _tenant_slug(tenant: str) -> str:
        """A workflow-id-safe, COLLISION-FREE tenant slug, so a tenant-B run can never
        collide with (or be reached as) a tenant-A run. A readable alnum prefix PLUS a hash
        of the full code disambiguates codes that share an alnum form (``A-B`` vs ``AB``)."""
        t = (tenant or settings.register_tenant).strip()
        alnum = re.sub(r"[^A-Za-z0-9]", "", t) or "T"
        return f"{alnum}{hashlib.sha256(t.encode()).hexdigest()[:10]}"

    def _auth_enforced() -> bool:
        """Production identity posture: any of OIDC / require_auth / signed context on."""
        return bool(settings.oidc_issuer or settings.require_auth
                    or settings.internal_signing_secret)

    def _wf_tenant_denied(request: Request, workflow_id: str) -> ORJSONResponse | None:
        """A workflow may be approved / rejected / read ONLY within its own tenant: the
        request's X-Tenant must reproduce the tenant slug embedded in the business id
        (``{prefix}-{tenantSlug}-{business_id}``). A LEGACY id with no embedded slug FAILS
        CLOSED under the production identity posture (it can't be tenant-verified), and is
        allowed only in dev."""
        parts = workflow_id.split("-", 2)
        if len(parts) < 3:
            if _auth_enforced():
                return _problem(403, "Forbidden",
                                "This workflow id predates tenant binding and cannot be "
                                "tenant-verified; refused.")
            return None
        want = _tenant_slug(request.headers.get("X-Tenant") or settings.register_tenant)
        if parts[1] != want:
            return _problem(403, "Forbidden",
                            "This workflow belongs to a different tenant.")
        return None

    def _caller_context(request: Request,
                        requested_by: str = "") -> tuple[CallerContext, bool]:
        """The TENANT + human identity a workflow acts for, and whether a VERIFIED,
        route-bound delegated identity was present. The tenant comes from the request; the
        identity + live grant come from the gateway's SIGNED context when present, verified
        AND bound to this route + tenant. The ``verified`` flag lets the caller FAIL CLOSED in
        production rather than starting a workflow that would run under the service key."""
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        cc = CallerContext(tenant=tenant, email=requested_by or "")
        raw = request.headers.get("X-Internal-Context")
        if not (settings.internal_signing_secret and raw):
            return cc, False
        from evam_backend_core.internal_token import (
            InternalTokenError,
            verify_internal_context,
        )
        try:
            ic = verify_internal_context(
                raw, verify_key=settings.internal_signing_secret,
                algorithms=(settings.internal_signing_algorithm,))
        except InternalTokenError as exc:
            log.warning("orchestrator_context_verify_failed", extra={"error": str(exc)})
            return cc, False
        # REQUIRE the route binding to be present AND exact: a token with no method/path, or
        # one minted for another route (e.g. VocX's /v1/touchpoints) or another tenant, does
        # NOT delegate. The previous hop MUST mint a token bound to THIS route + tenant.
        bound_ok = (bool(ic.method) and ic.method == request.method
                    and bool(ic.path) and ic.path == request.url.path
                    and bool(ic.tenant) and ic.tenant == tenant)
        if not bound_ok:
            log.warning("orchestrator_context_binding_rejected",
                        extra={"tok_method": ic.method, "tok_path": ic.path,
                               "tok_tenant": ic.tenant, "req_method": request.method,
                               "req_path": request.url.path, "req_tenant": tenant})
            return cc, False
        return CallerContext(
            tenant=ic.tenant, email=ic.email or requested_by or "",
            user_id=ic.user_id, roles=list(ic.roles),
            report_ids=list(ic.report_ids), report_emails=list(ic.report_emails),
            effective_views=ic.effective_views,
            effective_operations=ic.effective_operations, decision=ic.decision), True

    async def _verified_email(request: Request,
                              fallback: str) -> tuple[str, ORJSONResponse | None]:
        """The caller's trustworthy identity. With OIDC configured it is the e-mail from
        the VERIFIED bearer token — never a caller-supplied string. With no OIDC and
        ``require_auth`` on, the request is REFUSED rather than trusting the fallback (so a
        production orchestrator can never approve on an unauthenticated say-so). Only in
        dev (require_auth off, no OIDC) does the supplied ``fallback`` stand in."""
        verifier: TokenVerifier | None = request.app.state.oidc
        if verifier is None:
            if settings.require_auth:
                return "", _problem(
                    401, "Unauthorized",
                    "This orchestrator requires a verified identity; set "
                    "WORKFLOWS_OIDC_ISSUER (or WORKFLOWS_OIDC_ISSUERS for several IdPs).")
            return fallback, None
        token = bearer_token(request.headers.get("Authorization"))
        if not token:
            return "", _problem(401, "Unauthorized", "Bearer token required.")
        try:
            ident = await verifier.verify(token)
        except OidcError as exc:
            return "", _problem(401, "Unauthorized", f"Invalid token: {exc}")
        return ident.email, None

    def _mint_approval(workflow_id: str, decision: str, decided_by: str,
                       approver: CallerContext | None) -> str:
        """A SIGNED, workflow-AND-decision-BOUND approval record. This is the anti-bypass
        primitive: the worker requires (in production) a token minted HERE — bound to THIS
        workflow id AND to THIS decision (``Approved`` / ``Rejected``, carried in the
        immutable ``operation`` claim) — before it will honour the signal. A direct Temporal
        signal cannot forge it (no signing secret), and an approve token cannot be replayed as
        a reject (or vice-versa) because the decision is signed into the token."""
        if not settings.internal_signing_secret:
            return ""
        from evam_backend_core.internal_token import mint_internal_context
        a = approver or CallerContext(tenant=settings.register_tenant, email=decided_by)
        return mint_internal_context(
            signing_key=settings.internal_signing_secret,
            algorithm=settings.internal_signing_algorithm,
            ttl_seconds=max(settings.internal_token_ttl_seconds, 600),
            tenant=a.tenant, email=a.email or decided_by,
            user_id=a.user_id or decided_by, roles=list(a.roles),
            effective_views=a.effective_views,
            effective_operations=a.effective_operations, decision="FULL",
            method="APPROVE", path=f"/approval/{workflow_id}", operation=decision)

    async def _decider(request: Request, workflow_id: str, decision: str, payload: DecisionIn
                       ) -> tuple[str, CallerContext | None, str, ORJSONResponse | None]:
        """The trustworthy decider identity, its resolved approver context, a FRESH role check,
        and a SIGNED approval token bound to this workflow AND this ``decision``
        (Approved/Rejected). The approver's identity + live grant are resolved AT DECISION TIME
        (a role revoked mid-wait is caught now) via Access, scoped to the workflow's tenant.
        The token is the worker's FRESH-path proof; the durable decision record (persisted
        synchronously, below) is the authority when the token has since expired."""
        # Authenticate first (401), THEN authorize the decision to the workflow's tenant (403).
        decided_by, err = await _verified_email(request, payload.by)
        if err is not None:
            return "", None, "", err
        if (denied := _wf_tenant_denied(request, workflow_id)) is not None:
            return "", None, "", denied
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        prefix = workflow_id.split("-", 1)[0]
        needed = _APPROVER_ROLES.get(prefix)
        approver: CallerContext | None = None
        if settings.access_url:
            try:
                resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": decided_by},
                    headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
            except httpx.HTTPError as exc:
                return "", None, "", _problem(502, "Upstream unavailable", f"Access: {exc}")
            body = resp.json() if resp.status_code == 200 else {}
            roles = set(body.get("roles", []))
            # FRESH authority check: a role revoked mid-wait is caught here, now.
            if needed and not (roles & needed):
                return "", None, "", _problem(
                    403, "Forbidden",
                    f"'{decided_by}' lacks an approver role {sorted(needed)} for {prefix}.")
            approver = CallerContext(
                tenant=tenant, email=decided_by, user_id=str(body.get("id") or decided_by),
                roles=list(roles), effective_views=body.get("views", {}),
                effective_operations=body.get("operations", {}), decision="FULL")
        token = _mint_approval(workflow_id, decision, decided_by, approver)
        return decided_by, approver, token, None

    async def _lending_lines_for_deal(request: Request, deal_id: str,
                                      caller: CallerContext | None, who: str) -> list[str]:
        """Ids of the lending lines belonging to ``deal_id``, read as the deciding human.

        Used to record a subject-bound committee decision per line (see the committee-decision
        handler). Never raises: an unreachable Register or an empty list just means no lending
        line is sanctioned by this decision.
        """
        headers = {"X-Tenant": request.headers.get("X-Tenant", settings.register_tenant),
                   "X-API-Key": settings.register_api_key}
        path = "/v1/lending"
        if settings.internal_signing_secret and caller is not None and caller.email:
            from evam_backend_core.internal_token import mint_internal_context

            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=settings.internal_token_ttl_seconds,
                tenant=headers["X-Tenant"], email=caller.email,
                user_id=caller.user_id or caller.email, roles=list(caller.roles),
                report_ids=list(caller.report_ids), report_emails=list(caller.report_emails),
                effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="GET", path=path)
        else:
            headers["X-User-Email"] = who
            if caller is not None and caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.get(
                f"{settings.register_base_url.rstrip('/')}{path}",
                params={"deal_id": deal_id, "limit": 50}, headers=headers)
            if rr.status_code >= 300:
                # Silence here means the deal sanctions and its facility does NOT — the exact
                # divergence an operator must be told about, so this is an ERROR, not a warning.
                log.error("lending_lines_lookup_failed",
                          extra={"deal_id": deal_id, "status": rr.status_code,
                                 "impact": "lending line(s) will NOT be sanctioned by this "
                                           "decision; re-send the committee decision once the "
                                           "Register is reachable"})
                return []
            return [str(r["id"]) for r in (rr.json().get("items") or []) if r.get("id")]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.error("lending_lines_lookup_error",
                      extra={"deal_id": deal_id, "error": str(exc),
                             "impact": "lending line(s) will NOT be sanctioned by this decision"})
            return []

    async def _persist_decision(request: Request, workflow_id: str, decision: str,
                                decided_by: str, note: str | None,
                                approver: CallerContext | None, lead_id: str | None,
                                extra: dict | None = None
                                ) -> tuple[dict | None, ORJSONResponse | None]:
        """Record the decision on the dedicated SINGLE-WINNER decision resource SYNCHRONOUSLY —
        BEFORE the API acknowledges and before the signal is delivered — so the outcome is
        durable at ACCEPTANCE time and the FIRST decision atomically wins:

        * the Register enforces one decision per (tenant, workflow_id) with a UNIQUE
          constraint, so a concurrent Approve+Reject can never both persist;
        * replaying the SAME decision returns the original record (idempotent);
        * the OPPOSITE decision returns 409 — surfaced to the caller as 409, and NOT signalled;
        * provenance is set server-side from the delegated approver context we mint here — never
          a client field.

        ``lead_id`` is the REAL lead id (from the workflow memo), never derived from a
        retry-suffixed workflow id. Returns ``(record, error)`` — the record is the AUTHORITATIVE
        one the Register holds (the first approver's, on an idempotent replay), so the caller
        reports the true approver, not the latest caller. In dev (no signing) returns
        ``(None, None)`` and the worker trusts the signal."""
        if not settings.internal_signing_secret:
            return None, None
        tenant = (approver.tenant if approver and approver.tenant
                  else (request.headers.get("X-Tenant") or settings.register_tenant).strip())
        a = approver or CallerContext(tenant=tenant, email=decided_by)
        # Mint a delegated approver context bound to THIS write, so the Register records the
        # decision AS the verified human (server-controlled provenance), scoped to the tenant.
        from evam_backend_core.internal_token import mint_internal_context
        ctx_token = mint_internal_context(
            signing_key=settings.internal_signing_secret,
            algorithm=settings.internal_signing_algorithm,
            ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
            tenant=tenant, email=a.email or decided_by, user_id=a.user_id or decided_by,
            roles=list(a.roles), effective_views=a.effective_views,
            effective_operations=a.effective_operations, decision="FULL",
            method="POST", path="/v1/internal/decisions")
        body = {"workflow_id": workflow_id, "decision": decision, "note": note}
        if lead_id:
            body["lead_id"] = lead_id
        if extra:
            body.update({k: v for k, v in extra.items() if v is not None})
        try:
            resp = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}/v1/internal/decisions",
                json={k: v for k, v in body.items() if v is not None},
                headers={"X-API-Key": settings.register_api_key, "X-Tenant": tenant,
                         "X-Internal-Context": ctx_token})
        except httpx.HTTPError as exc:
            return None, _problem(502, "Upstream unavailable",
                                  f"Could not durably record the decision (Register: {exc}).")
        if resp.status_code == 409:
            return None, _problem(409, "Conflict",
                                  "A different decision has already been recorded for this "
                                  "workflow; it cannot be changed.")
        if resp.status_code >= 300:
            return None, _problem(502, "Upstream error",
                                  f"Register refused the decision record ({resp.status_code}).")
        return resp.json(), None

    async def _has_approver_role(request: Request, workflow_id: str, who: str,
                                 tenant: str) -> bool:
        """Whether ``who`` holds an approver role for this workflow's vertical (via Access)."""
        prefix = workflow_id.split("-", 1)[0]
        needed = _APPROVER_ROLES.get(prefix)
        if not (needed and settings.access_url and who):
            return False
        try:
            resp = await request.app.state.http.get(
                f"{settings.access_url.rstrip('/')}/v1/resolve",
                params={"email": who},
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
        except httpx.HTTPError:
            return False
        roles = set(resp.json().get("roles", [])) if resp.status_code == 200 else set()
        return bool(roles & needed)

    async def _authorised_for(prefix: str, request: Request, who: str, tenant: str) -> bool:
        """Whether ``who`` holds an authorising role for ``prefix`` (fresh via Access when configured,
        else the forwarded roles header in dev). Used to gate a workflow START by authority."""
        needed = _APPROVER_ROLES.get(prefix, set())
        if not needed:
            return False
        if settings.access_url and who:
            try:
                resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": who},
                    headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
            except httpx.HTTPError:
                return False
            roles = set(resp.json().get("roles", [])) if resp.status_code == 200 else set()
            return bool(roles & needed)
        raw = request.headers.get("X-User-Roles") or ""
        return bool({r.strip() for r in raw.split(",") if r.strip()} & needed)

    async def _status_scope_denied(request: Request, workflow_id: str, desc: Any,
                                   who: str) -> ORJSONResponse | None:
        """Only the INITIATOR (workflow memo) or an approver-role holder may read a run."""
        # ``memo`` on a Temporal description is an ASYNC accessor, not a dict — read it via
        # memo_value, or the initiator is never recognised and a legitimate requester is 403'd.
        initiator = str(await desc.memo_value("initiator", "") or "").strip().lower()
        if who and who.strip().lower() == initiator:
            return None
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        if await _has_approver_role(request, workflow_id, who, tenant):
            return None
        return _problem(403, "Forbidden",
                        "You may not read this workflow's status (not its initiator or an "
                        "approver).")

    async def _reconcile_closed(handle: WorkflowHandle, desc: Any, workflow_id: str,
                                decision: str) -> Any:
        """Reconcile a decision against a workflow that is no longer RUNNING.

        Two cases the caller must be able to retry safely:

        * **Already applied (idempotent retry).** The signal was delivered and the run
          COMPLETED with THIS decision's outcome, but the caller lost the response and retried.
          Return the AUTHORITATIVE completed result (200) — never a misleading 409.
        * **Closed without applying this decision.** The run timed out / failed / completed
          with a different outcome. Return 409 with guidance to start a fresh attempt (which
          gets a new ``-r2`` id); the persisted decision row remains for reconciliation."""
        status = desc.status
        if status == WorkflowExecutionStatus.COMPLETED:
            try:
                result = await handle.result()
            except (RPCError, TemporalError):
                result = None
            if isinstance(result, dict) and result.get("status") == decision:
                return ORJSONResponse(status_code=200, content={
                    "workflow_id": workflow_id, "status": "already_applied",
                    "result": result})
            return _problem(409, "Conflict",
                            f"Workflow '{workflow_id}' already completed with a different "
                            "outcome; start a new attempt.")
        return _problem(409, "Conflict",
                        f"Workflow '{workflow_id}' is closed "
                        f"({status.name if status else 'UNKNOWN'}) and did not apply this "
                        "decision; start a new attempt.")

    async def _decide(request: Request, workflow_id: str, name: str, decision: str,
                      payload: DecisionIn) -> Any:
        """Shared approve/reject path — authenticate + fresh-authorize, confirm the workflow is
        RUNNING, DURABLY record the decision, then deliver it — and RECONCILE idempotently
        against a closed workflow so retries are safe:

        * The RUNNING check before persistence guards against writing a decision for a
          nonexistent / already-closed workflow (which a later run reusing the id could
          consume).
        * If the run closes in the tiny window between that check and the signal, or a prior
          delivery already applied it and the caller is retrying, ``_reconcile_closed`` returns
          the authoritative applied result (or a clear 409) instead of a misleading error."""
        decided_by, approver, token, err = await _decider(
            request, workflow_id, decision, payload)
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        # Not pending → reconcile (idempotent retry of an applied decision, or a clear 409).
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return await _reconcile_closed(handle, desc, workflow_id, decision)
        # The REAL lead id comes from the memo (an ASYNC accessor, not a dict), never a
        # retry-suffixed workflow id.
        lead_id = await desc.memo_value("lead_id", None)
        record, perr = await _persist_decision(
            request, workflow_id, decision, decided_by, payload.note, approver, lead_id)
        if perr is not None:
            return perr   # not acknowledged unless durably recorded first
        # Report the AUTHORITATIVE approver from the persisted record (the FIRST approver on an
        # idempotent replay) — not the latest caller — so the API response matches the run + DB.
        authoritative_by = (record.get("decided_by") if record else "") or decided_by
        decision_ref = str(record.get("id") or "") if record else ""
        try:
            await handle.signal(
                name, args=[authoritative_by, payload.note, token, decision_ref])
        except RPCError:
            # A signal RPCError may mean the run CLOSED — or just a TRANSIENT Temporal/network
            # blip while the run is still fine. Do NOT guess from the error code: re-describe
            # and act on the ACTUAL state. Still RUNNING → the decision is persisted but
            # undelivered → 503 "retry delivery" (a retry re-signals safely; the run ignores a
            # duplicate once decided). Closed → reconcile (already-applied vs a real conflict).
            try:
                desc2 = await handle.describe()
            except (RPCError, TemporalError):
                return _problem(503, "Delivery failed",
                                "Decision persisted; Temporal is unreachable — retry delivery.")
            if desc2.status == WorkflowExecutionStatus.RUNNING:
                return _problem(503, "Delivery failed",
                                "Decision persisted but signal delivery failed transiently; "
                                "retry delivery.")
            return await _reconcile_closed(handle, desc2, workflow_id, decision)
        out: dict[str, Any] = {"workflow_id": workflow_id, "signalled": name,
                               "by": authoritative_by}
        if record:
            out["decision"] = record.get("decision")
            out["note"] = record.get("note")
        return out

    @app.post("/v1/workflows/{workflow_id}/approve", tags=["Workflows"],
              summary="Approve a pending human-in-the-loop workflow")
    async def approve(workflow_id: str, payload: DecisionIn, request: Request,
                      x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                      ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide(request, workflow_id, "approve", "Approved", payload)

    @app.post("/v1/workflows/{workflow_id}/reject", tags=["Workflows"],
              summary="Reject a pending human-in-the-loop workflow")
    async def reject(workflow_id: str, payload: DecisionIn, request: Request,
                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide(request, workflow_id, "reject", "Rejected", payload)

    async def _start_business(request: Request, x_api_key: str | None, requested_by_raw: str,
                              workflow_cls: Any, arg_cls: Any, id_prefix: str, id_suffix: str,
                              extra_memo: dict, **arg_fields: Any) -> Any:
        """Shared start path for the business-lifecycle workflows (qualification / structuring /
        document collection): API-key gate → verified initiator → fail-closed delegated identity →
        idempotent start under a tenant-bound business id."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, requested_by_raw)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"{id_prefix}-{_tenant_slug(caller.tenant)}-{id_suffix}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                **extra_memo}
        handle = await start(request, workflow_cls, arg_cls(caller=caller, **arg_fields), wf_id,
                             restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "started",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/lead-qualifications", status_code=202, tags=["Workflows"],
              summary="Start a lead-qualification workflow")
    async def start_qualification(payload: LeadQualificationIn, request: Request,
                                  x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                  ) -> Any:
        return await _start_business(
            request, x_api_key, payload.qualified_by, LeadQualificationWorkflow,
            LeadQualificationInput, "qual", payload.lead_id, {"lead_id": payload.lead_id},
            lead_id=payload.lead_id, qualified_by=payload.qualified_by,
            qualification_reference=payload.qualification_reference,
            qualification_sha256=payload.qualification_sha256, passed=payload.passed,
            reason=payload.reason)

    @app.post("/v1/workflows/deal-structurings", status_code=202, tags=["Workflows"],
              summary="Start a deal-structuring workflow (awaits the committee decision)")
    async def start_structuring(payload: DealStructuringIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        return await _start_business(
            request, x_api_key, payload.requested_by, DealStructuringWorkflow,
            DealStructuringInput, "struct", payload.deal_id,
            {"deal_id": payload.deal_id, "subject_type": "Deal"},
            deal_id=payload.deal_id, requested_by=payload.requested_by,
            product_type=payload.product_type, rm=payload.rm,
            credit_note_reference=payload.credit_note_reference,
            decision_timeout_hours=payload.decision_timeout_hours)

    @app.post("/v1/workflows/document-collections", status_code=202, tags=["Workflows"],
              summary="Start a document-collection workflow (awaits document signals)")
    async def start_documents(payload: DocumentCollectionIn, request: Request,
                              x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                              ) -> Any:
        return await _start_business(
            request, x_api_key, payload.requested_by, DocumentCollectionWorkflow,
            DocumentCollectionInput, "docs", payload.subject_id,
            {"subject_id": payload.subject_id, "subject_type": payload.subject_type},
            subject_type=payload.subject_type, subject_id=payload.subject_id,
            requested_by=payload.requested_by, required_documents=payload.required_documents,
            collection_timeout_hours=payload.collection_timeout_hours)

    @app.post("/v1/workflows/advaya-handover", status_code=202, tags=["Workflows"],
              summary="MAKER prepares the Advaya handover (senior credit authority)")
    async def start_advaya_handover(payload: AdvayaHandoverIn, request: Request,
                                    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                    ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        # AUTHORITY — handing over to Advaya is a money-movement authorization (Credit Head /
        # Management / Admin), checked fresh via Access (or the forwarded roles in dev).
        if not await _authorised_for("handover", request, requested_by, caller.tenant):
            return _problem(403, "Forbidden",
                            "Handing a facility over to Advaya requires Credit Head / Management / "
                            "Admin authority.")
        wf_id = f"handover-{_tenant_slug(caller.tenant)}-{payload.lending_id}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lending_id": payload.lending_id}
        handle = await start(
            request, AdvayaHandoffWorkflow,
            AdvayaHandoffInput(
                caller=caller, lending_id=payload.lending_id, requested_by=requested_by,
                executed_document_refs=[d.model_dump() for d in payload.executed_document_refs],
                cpcs_checklist_version=payload.cpcs_checklist_version,
                delivery_method=payload.delivery_method, recipient=payload.recipient,
                note=payload.note),
            wf_id, restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "prepared",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/advaya-handover/{lending_id}/approve", tags=["Workflows"],
              summary="CHECKER approves a prepared Advaya handover (different person)")
    async def approve_advaya_handover(lending_id: str, payload: AdvayaHandoverApproveIn,
                                      request: Request,
                                      x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                      ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        approved_by, err = await _verified_email(request, payload.approved_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, approved_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to approve.")
        if not await _authorised_for("handover", request, approved_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Approving an Advaya handover requires Credit Head / Management / "
                            "Admin authority.")
        # Approve at the Register, AS the verified checker (server-minted delegated context). The
        # Register enforces checker != maker and advances the stage transactionally.
        tenant = checker.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            path = f"/v1/internal/handover-packages/{lending_id}/approve"
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=checker.email or approved_by,
                user_id=checker.user_id or approved_by, roles=list(checker.roles),
                effective_views=checker.effective_views,
                effective_operations=checker.effective_operations, decision="FULL",
                method="POST", path=path)
        else:
            headers["X-User-Email"] = approved_by
            if checker.user_id:
                headers["X-User-Id"] = checker.user_id
            if checker.roles:
                headers["X-User-Roles"] = ",".join(checker.roles)
        try:
            reg_resp = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}"
                f"/v1/internal/handover-packages/{lending_id}/approve",
                json={}, headers=headers)
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable",
                            f"Could not approve the handover (Register: {exc}).")
        if reg_resp.status_code >= 300:
            ct = reg_resp.headers.get("content-type", "")
            detail = (reg_resp.json().get("error", {}).get("detail")
                      if ct.startswith("application/json") else reg_resp.text)
            return _problem(reg_resp.status_code if reg_resp.status_code < 500 else 502,
                            "Handover approval refused", str(detail))
        return ORJSONResponse(status_code=200, content=reg_resp.json())

    async def _register_post_as(request: Request, path: str, who: str, caller: CallerContext,
                                body: dict) -> Any:
        """POST to the Register AS the verified human (server-minted delegated context in prod, or
        forwarded identity headers in dev). Returns the passthrough response or a _problem()."""
        tenant = caller.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=caller.email or who, user_id=caller.user_id or who,
                roles=list(caller.roles), effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="POST", path=path)
        else:
            headers["X-User-Email"] = who
            if caller.user_id:
                headers["X-User-Id"] = caller.user_id
            if caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable", f"Register unreachable: {exc}")
        if rr.status_code >= 300:
            ct = rr.headers.get("content-type", "")
            detail = (rr.json().get("error", {}).get("detail")
                      if ct.startswith("application/json") else rr.text)
            return _problem(rr.status_code if rr.status_code < 500 else 502,
                            "Register refused the request", str(detail))
        return ORJSONResponse(status_code=200, content=rr.json())

    @app.post("/v1/workflows/cpcs-checklists", status_code=202, tags=["Workflows"],
              summary="MAKER prepares the CP/CS checklist")
    async def start_cpcs_checklist(payload: CpcsChecklistIn, request: Request,
                                   x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                   ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"cpcs-{_tenant_slug(caller.tenant)}-{payload.lending_id}-v{payload.checklist_version}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lending_id": payload.lending_id}
        handle = await start(
            request, CpcsChecklistWorkflow,
            CpcsChecklistInput(
                caller=caller, lending_id=payload.lending_id, requested_by=requested_by,
                items=[i.model_dump(mode="json") for i in payload.items], deal_id=payload.deal_id,
                checklist_version=payload.checklist_version, note=payload.note),
            wf_id, restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "prepared",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/cpcs-checklists/{checklist_id}/approve", tags=["Workflows"],
              summary="CHECKER approves the CP/CS checklist (different person, senior authority)")
    async def approve_cpcs_checklist(checklist_id: str, payload: CpcsApproveIn, request: Request,
                                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        approved_by, err = await _verified_email(request, payload.approved_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, approved_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to approve.")
        if not await _authorised_for("cpcs", request, approved_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Approving a CP/CS checklist requires Credit Head / Management / Admin "
                            "authority.")
        return await _register_post_as(
            request, f"/v1/internal/cpcs-checklists/{checklist_id}/approve", approved_by, checker, {})

    @app.post("/v1/workflows/{workflow_id}/committee-decision", tags=["Workflows"],
              summary="Record the Credit Committee decision (durable) and signal the workflow")
    async def committee_decision(workflow_id: str, payload: CommitteeDecisionIn, request: Request,
                                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                 ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        outcome = "Approved" if payload.approved else "Rejected"
        # FRESH authority check (committee roles) via Access, bound to the workflow's tenant.
        decided_by, approver, _token, err = await _decider(
            request, workflow_id, outcome, DecisionIn(by=payload.by, note=payload.note))
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return await _reconcile_closed(handle, desc, workflow_id, outcome)
        deal_id = await desc.memo_value("deal_id", None)
        if not deal_id:
            return _problem(409, "Conflict", "This workflow has no bound deal to decide on.")
        # DURABLY record the committee decision (single-winner, subject-bound, provenance server-set)
        # BEFORE signalling — so the evidence gate can VERIFY the sanction against it, and a raw
        # signal alone can never manufacture a committee outcome.
        record, perr = await _persist_decision(
            request, workflow_id, outcome, decided_by, payload.note, approver, None,
            extra={"kind": "committee", "subject_type": "Deal", "subject_id": deal_id,
                   "run_id": desc.run_id,
                   "committee_reference": payload.committee_reference or workflow_id,
                   "sanction_letter_reference": payload.sanction_letter_reference})
        if perr is not None:
            return perr
        # A committee decision sanctions the DEAL *and* its lending facility. Evidence
        # verification binds a decision to its subject (Register: _verify_committee_decision
        # rejects "a different subject"), so a Deal-scoped decision cannot authorise the
        # Lending-scoped evidence the lending line's own Sanctioned gate requires. Record a
        # SUBJECT-BOUND decision per lending line here — with THIS human's committee authority,
        # which the workflow (a service principal) could never supply — keyed
        # "{workflow_id}:lending:{lending_id}" so it stays single-winner per line. The workflow
        # then cites that key when filing the line's evidence. Best-effort: a line that cannot
        # be recorded simply is not sanctioned; the deal outcome still stands.
        for line in await _lending_lines_for_deal(request, deal_id, approver, decided_by):
            await _persist_decision(
                request, f"{workflow_id}:lending:{line}", outcome, decided_by, payload.note,
                approver, None,
                extra={"kind": "committee", "subject_type": "Lending", "subject_id": line,
                       "run_id": desc.run_id,
                       "committee_reference": payload.committee_reference or workflow_id,
                       "sanction_letter_reference": payload.sanction_letter_reference})
        authoritative_by = (record.get("decided_by") if record else "") or decided_by
        # The signal is only a WAKE-UP: the workflow re-reads the authoritative decision record and
        # derives the outcome/approver/note/references from it — nothing here is trusted by the run.
        try:
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
        except RPCError:
            try:
                desc2 = await handle.describe()
            except (RPCError, TemporalError):
                return _problem(503, "Delivery failed",
                                "Decision persisted; Temporal is unreachable — retry delivery.")
            if desc2.status == WorkflowExecutionStatus.RUNNING:
                return _problem(503, "Delivery failed",
                                "Decision persisted but signal delivery failed transiently; "
                                "retry delivery.")
            return await _reconcile_closed(handle, desc2, workflow_id, outcome)
        return {"workflow_id": workflow_id, "decision": outcome, "by": authoritative_by}

    @app.post("/v1/workflows/{workflow_id}/document-received", tags=["Workflows"],
              summary="Signal that a required document was received")
    async def document_received(workflow_id: str, payload: DocumentReceivedIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if (denied_t := _wf_tenant_denied(request, workflow_id)) is not None:
            return denied_t
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        # Only the initiator or an approver-role holder may feed documents into the run.
        if (scope_err := await _status_scope_denied(request, workflow_id, desc, who)) is not None:
            return scope_err
        try:
            await handle.signal(DocumentCollectionWorkflow.document_received,
                                args=[payload.name, payload.reference, payload.sha256 or ""])
        except (RPCError, TemporalError) as exc:
            return _problem(503, "Delivery failed", f"Signal delivery failed: {exc}")
        return {"workflow_id": workflow_id, "document_received": payload.name}

    @app.get("/v1/workflows/{workflow_id}", tags=["Workflows"],
             summary="A run's live status (execution state + in-workflow stage)")
    async def describe(workflow_id: str, request: Request,
                       x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                       ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        # A run's status/result requires a VERIFIED identity (not just the shared
        # orchestrator key) and is readable ONLY within its own tenant. (Subject/assignment
        # level scoping is a further refinement tracked separately.)
        _who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if _auth_enforced() and not _who:
            return _problem(401, "Unauthorized",
                            "A verified identity is required to read a workflow's status.")
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        # SUBJECT SCOPE: under the production posture, only the INITIATOR (recorded in the
        # workflow memo at start) or an APPROVER-role holder for this vertical may read the
        # run — not any same-tenant caller who merely knows the id.
        if _auth_enforced():
            scope_err = await _status_scope_denied(request, workflow_id, desc, _who)
            if scope_err is not None:
                return scope_err
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
