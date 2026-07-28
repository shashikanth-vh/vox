"""RBAC endpoints that live NEXT TO THE DATA — assignments, change requests, authz check.

Three-service architecture: identity + roles + the admin-editable access matrix live in
the **Access service**; the **Gateway** decides binary access from cached facts and
forwards verified identity headers. This module keeps the flows whose semantics are
inseparable from the business rows:

* **Assignments** — Credit Head assigns Deal Analysts to Lending/Syn/AM lines; Syn Head
  assigns Syn RMs; AM Head assigns AM RMs. Assignment grants write on that line until
  unassigned; two assignees can co-exist on a line.
* **Request → approve/reject** — approval APPLIES the stage/status change atomically
  with history + audit via the standard repository.
* **/v1/authz/check** — evaluate an operation (optionally against a line) for the
  calling user — the scoped half the gateway cannot answer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, Header, Query
from sqlalchemy import select

from app import authz
from app.authz.matrix import PRIMARY_ASSIGNMENT_ROLE
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models.system import IdempotencyKey
from app.models.users import ChangeRequest, LineAssignment
from app.repositories.crud import CRUDRepository
from app.repositories.subjects import SUBJECTS, load_subject
from app.schemas import rbac as s
from app.schemas.rbac import AssignmentRead, ChangeRequestRead

router = api_router()

_assign_repo = CRUDRepository(LineAssignment,
                              filterable=["user_id", "subject_type", "subject_id",
                                          "assignment_role"])
_request_repo = CRUDRepository(ChangeRequest,
                               filterable=["status", "subject_type", "subject_id",
                                           "requested_by"])

# Which field a change request may target per line, and the tracker model behind it.
_REQUESTABLE_FIELDS: dict[str, set[str]] = {
    "Lending": {"stage"},
    "Syndication": {"status"},
    "AssetMonetisation": {"status"},
    "Lead": {"status"},
    "Deal": {"stage"},
}

_ASSIGNMENT_ROLES = {"BDRM", "Deal Analyst", "Syn RM", "AM RM"}


def _require_user(ctx: RequestContext) -> authz.UserContext:
    if ctx.user is None:
        raise ForbiddenError("This endpoint requires a user context (X-User-Email).")
    return ctx.user


# --------------------------------------------------------------------------- #
# Assignments — the assignment-driven permission primitive
# --------------------------------------------------------------------------- #
@router.post("/v1/assignments", response_model=AssignmentRead, status_code=201,
             tags=["Users & RBAC"],
             summary="Assign a user to a line (grants write on that line)")
async def create_assignment(payload: s.AssignmentCreate,
                            ctx: RequestContext = Depends(get_context)) -> Any:
    data = payload.model_dump(exclude_unset=False)
    stype, arole = data["subject_type"], data["assignment_role"]
    if stype not in SUBJECTS:
        raise ValidationAppError(f"Unknown subject_type '{stype}'. One of: {', '.join(SUBJECTS)}.")
    if arole not in _ASSIGNMENT_ROLES:
        raise ValidationAppError(
            f"Unknown assignment_role '{arole}'. One of: {', '.join(sorted(_ASSIGNMENT_ROLES))}.")
    # Authority: Credit Head owns the analyst pool; each Head assigns their own RM;
    # Management/Admin override. (Compatibility mode applies without a user context.)
    if ctx.user is not None and not authz.engine.can_assign(ctx.user, stype, arole):
        raise ForbiddenError(
            f"Role(s) {sorted(ctx.user.roles)} may not assign a {arole} to a {stype} line.")
    elif ctx.user is None:
        # A machine caller (vetted API key, e.g. the VOX workflow) may create ONLY the
        # line's PRIMARY-OWNER assignment — a BDRM on their own new lead. That is the
        # auto-own semantics, not arbitrary reassignment, so it does not need the
        # add_employee_assign_role operation.
        from app.authz.matrix import PRIMARY_ASSIGNMENT_ROLE

        # ONLY the VOX case: a BDRM primary-owner assignment on a LEAD. Anything else
        # from a machine caller still needs the operation (honours enforce_rbac).
        vox_primary = (stype == "Lead" and arole == PRIMARY_ASSIGNMENT_ROLE.get("Lead"))
        if not vox_primary:
            authz.enforce_operation(None, "add_employee_assign_role")

    # Subject must exist (the assignee is an Access-service user — validated upstream).
    if await load_subject(ctx.session, ctx.tenant_id, stype, data["subject_id"]) is None:
        raise NotFoundError(f"{stype} '{data['subject_id']}' not found.")

    data["assigned_by"] = ctx.actor
    obj = await _assign_repo.create(ctx.session, ctx.tenant_id, ctx.actor, data)
    return AssignmentRead.model_validate(obj)


@router.get("/v1/assignments", response_model=list[AssignmentRead], tags=["Users & RBAC"],
            summary="List assignments (active by default)")
async def list_assignments(ctx: RequestContext = Depends(get_context),
                           user_id: uuid.UUID | None = Query(default=None),
                           subject_type: str | None = Query(default=None),
                           subject_id: uuid.UUID | None = Query(default=None),
                           include_ended: bool = Query(default=False)) -> Any:
    conds = [LineAssignment.tenant_id == ctx.tenant_id, LineAssignment.deleted_at.is_(None)]
    if not include_ended:
        conds.append(LineAssignment.ended_at.is_(None))
    if user_id:
        conds.append(LineAssignment.user_id == user_id)
    if subject_type:
        conds.append(LineAssignment.subject_type == subject_type)
    if subject_id:
        conds.append(LineAssignment.subject_id == subject_id)
    # Scope: only a FULL 'employees' viewer (Admin/Management) sees the whole tenant's
    # assignments. Everyone else sees only assignments for themselves and their reports —
    # the list is no longer a tenant-wide directory of who-is-on-what.
    if ctx.user is not None:
        from app.authz.engine import _stacked
        from app.authz.matrix import VIEW_ACCESS

        if _stacked(VIEW_ACCESS["employees"], ctx.user.roles) is not authz.Access.FULL:
            conds.append(LineAssignment.user_id.in_([ctx.user.id, *ctx.user.report_ids]))
    rows = (
        await ctx.session.execute(
            select(LineAssignment).where(*conds).order_by(LineAssignment.created_at.desc())
        )
    ).scalars().all()
    return [AssignmentRead.model_validate(r) for r in rows]


@router.post("/v1/assignments/{assignment_id}/end", response_model=AssignmentRead,
             tags=["Users & RBAC"], summary="End an assignment (revokes line write)")
async def end_assignment(assignment_id: uuid.UUID,
                         ctx: RequestContext = Depends(get_context)) -> Any:
    obj = await _assign_repo.get(ctx.session, ctx.tenant_id, assignment_id)
    if obj.ended_at is not None:
        raise ConflictError("Assignment is already ended.")
    if ctx.user is not None and not authz.engine.can_assign(
        ctx.user, obj.subject_type, obj.assignment_role
    ):
        raise ForbiddenError(
            f"Role(s) {sorted(ctx.user.roles)} may not end a "
            f"{obj.assignment_role} assignment on a {obj.subject_type} line.")
    if ctx.user is None:
        # A machine caller ending an assignment must clear the assignment operation
        # (honours enforce_rbac) — it was previously unchecked.
        authz.enforce_operation(None, "add_employee_assign_role")
    obj.ended_at = datetime.now(UTC)
    obj.ended_by = ctx.actor
    obj.updated_by = ctx.actor
    await ctx.session.flush()
    await ctx.session.refresh(obj)  # updated_at is trigger-maintained server-side
    return AssignmentRead.model_validate(obj)


# --------------------------------------------------------------------------- #
# Change requests — request → approve/reject, approval applies the change
# --------------------------------------------------------------------------- #
@router.post("/v1/requests", response_model=ChangeRequestRead, status_code=201,
             tags=["Users & RBAC"], summary="Request a stage/status change")
async def create_change_request(payload: s.ChangeRequestCreate,
                                ctx: RequestContext = Depends(get_context)) -> Any:
    authz.enforce_operation(ctx.user, "request_stage_change")
    data = payload.model_dump(exclude_unset=False)
    stype, field = data["subject_type"], data["field"]
    allowed_fields = _REQUESTABLE_FIELDS.get(stype)
    if allowed_fields is None:
        raise ValidationAppError(
            f"Unknown subject_type '{stype}'. One of: {', '.join(_REQUESTABLE_FIELDS)}.")
    if field not in allowed_fields:
        raise ValidationAppError(
            f"Field '{field}' is not requestable on {stype} (allowed: {', '.join(allowed_fields)}).")
    # A lead conversion is not a mere status change — it must create the deal + product
    # lines atomically. Route it to /convert, never through the request→approve flow.
    if stype == "Lead" and field == "status" and data.get("to_value") == "Converted":
        raise ValidationAppError(
            "Converting a lead goes through POST /v1/leads/{id}/convert, not a change request.")
    subject = await load_subject(ctx.session, ctx.tenant_id, stype, data["subject_id"])
    if subject is None:
        raise NotFoundError(f"{stype} '{data['subject_id']}' not found.")
    # A SCOPED requester may only raise requests on lines/companies in their scope.
    if ctx.user is not None:
        granted = authz.engine.operation_access(ctx.user, "request_stage_change")
        if granted is authz.Access.SCOPED:
            from app.authz import scope as scope_mod

            user_scope = await scope_mod.build_scope(ctx, ctx.user)
            in_scope = await scope_mod.row_in_scope(ctx, user_scope, stype, subject)
            if not in_scope:
                entity_id = getattr(subject, "entity_id", None)
                in_scope = entity_id is not None and await scope_mod.entity_in_scope(
                    ctx, user_scope, entity_id)
            if not in_scope:
                raise ForbiddenError(
                    f"Scoped access: this {stype} is not in your scope to request on.")
    data["from_value"] = getattr(subject, field, None)
    data["requested_by"] = ctx.user.email if ctx.user else ctx.actor
    data["status"] = "Pending"
    obj = await _request_repo.create(ctx.session, ctx.tenant_id, ctx.actor, data)
    return ChangeRequestRead.model_validate(obj)


@router.get("/v1/requests", response_model=list[ChangeRequestRead], tags=["Users & RBAC"],
            summary="List change requests")
async def list_change_requests(ctx: RequestContext = Depends(get_context),
                               status: str | None = Query(default="Pending"),
                               subject_id: uuid.UUID | None = Query(default=None)) -> Any:
    conds = [ChangeRequest.tenant_id == ctx.tenant_id, ChangeRequest.deleted_at.is_(None)]
    if status:
        conds.append(ChangeRequest.status == status)
    if subject_id:
        conds.append(ChangeRequest.subject_id == subject_id)
    # Scope: Admin / Management / a vertical Head (the approvers) see the whole queue.
    # An IC sees only the requests they raised — not the tenant-wide request log.
    if ctx.user is not None:
        approver_roles = {"Admin", "Management", "BD Head", "Credit Head",
                          "Syn Head", "AM Head"}
        if not (ctx.user.roles & approver_roles):
            conds.append(ChangeRequest.requested_by == ctx.user.email)
    rows = (
        await ctx.session.execute(
            select(ChangeRequest).where(*conds).order_by(ChangeRequest.created_at.desc())
        )
    ).scalars().all()
    return [ChangeRequestRead.model_validate(r) for r in rows]


async def _decide(ctx: RequestContext, request_id: uuid.UUID, approve: bool,
                  note: str | None) -> ChangeRequest:
    req = await _request_repo.get(ctx.session, ctx.tenant_id, request_id)
    if req.status != "Pending":
        raise ConflictError(f"Request is already {req.status.lower()}.")
    # Approval routing: Admin / Management / the relevant vertical Head.
    if ctx.user is not None:
        if not authz.can_approve(ctx.user, req.subject_type):
            from app.core.errors import ForbiddenError

            raise ForbiddenError(
                f"Role(s) {sorted(ctx.user.roles)} may not decide requests on a "
                f"{req.subject_type} line.")
    else:
        authz.enforce_operation(None, "approve_stage_change")  # honours enforce_rbac

    req.status = "Approved" if approve else "Rejected"
    req.decided_by = ctx.user.email if ctx.user else ctx.actor
    req.decided_at = datetime.now(UTC)
    req.decision_note = note
    req.updated_by = ctx.actor

    if approve:
        # Transition/row-lock policy: if the requested target value is a LOCKED state
        # (a Converted lead, a Disbursed lending line), only a role the lock names may put
        # it there — the approval path must not bypass the same lock a direct edit obeys.
        from app.authz.matrix import ROW_LOCKS

        lock = ROW_LOCKS.get(req.subject_type)
        if lock is not None:
            field_name, locking_values, allowed_roles = lock
            if (req.field == field_name and req.to_value in locking_values
                    and ctx.user is not None and not (ctx.user.roles & allowed_roles)):
                raise ForbiddenError(
                    f"Approving this would move {req.subject_type}.{field_name} to "
                    f"{req.to_value!r}, a locked state only {sorted(allowed_roles)} may set.")
        # Apply the change through the standard repository so history auto-appends and
        # the audit trail records it (stage auto-stamping = the existing history hook).
        model = SUBJECTS[req.subject_type]
        repo: CRUDRepository = CRUDRepository(model)
        await repo.update(ctx.session, ctx.tenant_id, req.subject_id, ctx.actor,
                          {req.field: req.to_value})
    await ctx.session.flush()
    await ctx.session.refresh(req)  # updated_at is trigger-maintained server-side
    return req


@router.post("/v1/requests/{request_id}/approve", response_model=ChangeRequestRead,
             tags=["Users & RBAC"], summary="Approve a request (applies the change)")
async def approve_request(request_id: uuid.UUID, payload: s.ChangeRequestDecision,
                          ctx: RequestContext = Depends(get_context)) -> Any:
    req = await _decide(ctx, request_id, approve=True, note=payload.note)
    return ChangeRequestRead.model_validate(req)


@router.post("/v1/requests/{request_id}/reject", response_model=ChangeRequestRead,
             tags=["Users & RBAC"], summary="Reject a request")
async def reject_request(request_id: uuid.UUID, payload: s.ChangeRequestDecision,
                         ctx: RequestContext = Depends(get_context)) -> Any:
    req = await _decide(ctx, request_id, approve=False, note=payload.note)
    return ChangeRequestRead.model_validate(req)


# --------------------------------------------------------------------------- #
# /v1/authz/check — the scoped half of an access decision
# --------------------------------------------------------------------------- #
@router.get("/v1/authz/check", tags=["Users & RBAC"],
            summary="Can the calling user perform an operation (optionally on a line)?")
async def authz_check(
    ctx: RequestContext = Depends(get_context),
    operation: str = Query(...),
    subject_type: str | None = Query(default=None),
    subject_id: uuid.UUID | None = Query(default=None),
) -> dict[str, Any]:
    user = _require_user(ctx)
    try:
        granted = authz.engine.operation_access(user, operation)
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc
    allowed = granted is not authz.Access.NONE
    scope = granted.name
    on_line: bool | None = None
    if allowed and subject_type and subject_id and granted is authz.Access.SCOPED:
        # Answer from the CENTRAL evaluator — same scope as list/GET/write, so this
        # endpoint can never disagree with actual enforcement.
        from app.authz import scope as scope_mod

        user_scope = await scope_mod.build_scope(ctx, user)
        on_line = await scope_mod.can_write_row(ctx, user_scope, subject_type, subject_id)
        allowed = on_line
    return {"operation": operation, "allowed": allowed, "access": scope,
            "on_line": on_line,
            "primary_owner_default": PRIMARY_ASSIGNMENT_ROLE.get(subject_type or "", None)}


# --------------------------------------------------------------------------- #
# Lead → deal conversion — ONE transaction (deal + product lines + lead Converted)
# --------------------------------------------------------------------------- #
@router.post("/v1/leads/{lead_id}/convert", response_model=s.LeadConvertResult,
             tags=["Users & RBAC"],
             summary="Convert a lead to a deal atomically (deal + lines + lead marked "
                     "Converted, all-or-nothing)")
async def convert_lead(lead_id: uuid.UUID, payload: s.LeadConvertRequest,
                       ctx: RequestContext = Depends(get_context),
                       idempotency_key: str | None = Header(
                           default=None, alias="Idempotency-Key")) -> Any:
    """The transactional alternative to workflow-side compensation: the whole conversion
    happens in THIS request's transaction, so a failure anywhere rolls the entire thing
    back — no orphan deal or half-created product lines can ever survive. The Orchestrator
    calls this on approval instead of creating rows step-by-step and undoing on error.

    Authorization: ``push_lead_to_deals`` (BDRM+ per the matrix), AND — for a SCOPED
    caller — EXACT write access to this Lead (assignment / own-book / vertical default),
    not merely visibility of its company. Only an OPEN lead may be converted. The
    Idempotency-Key (which the SDK sends) is honoured so a retry returns the first result
    instead of creating a second deal.
    """
    from app.models import AssetMonetisation, Deal, LendingTracker, SyndicationTracker
    from app.repositories.crud import CRUDRepository as _Repo

    # Idempotent replay: a retried conversion (same key) returns the original outcome.
    if idempotency_key:
        prior = (
            await ctx.session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == ctx.tenant_id,
                    IdempotencyKey.key == idempotency_key))
        ).scalar_one_or_none()
        if prior is not None:
            return prior.response_body

    # Human callers are gated on push_lead_to_deals. Machine callers (vetted API key: the
    # workflow, AFTER the orchestrator verified the approver's identity + role) honour
    # enforce_rbac like every other operation.
    granted = authz.enforce_operation(ctx.user, "push_lead_to_deals")
    # Lock the lead row FOR UPDATE so two concurrent conversions serialise: the second
    # blocks until the first commits, then sees status=Converted and 409s — no duplicate
    # deal. (Belt-and-braces with the Idempotency-Key replay above for retries.)
    from app.models import Lead as _Lead

    lead = (
        await ctx.session.execute(
            select(_Lead).where(_Lead.id == lead_id, _Lead.tenant_id == ctx.tenant_id,
                                _Lead.deleted_at.is_(None)).with_for_update())
    ).scalar_one_or_none()
    if lead is None:
        raise NotFoundError(f"Lead '{lead_id}' not found.")
    # Re-check the idempotency key now that we hold the lock: a same-key request that raced
    # us past the pre-check replays the first result instead of 409-ing.
    if idempotency_key:
        prior = (
            await ctx.session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == ctx.tenant_id,
                    IdempotencyKey.key == idempotency_key))
        ).scalar_one_or_none()
        if prior is not None:
            return prior.response_body
    entity_id = getattr(lead, "entity_id", None)
    if entity_id is None:
        raise ValidationAppError("Lead has no entity_id — link it to a company first.")
    # Only an OPEN lead converts: a Converted / Lost / Dropped / Rejected lead is closed.
    if lead.status in {"Converted", "Lost", "Dropped", "Rejected", "Closed"}:
        raise ConflictError(f"Lead is {lead.status}; only an open lead can be converted.")

    # SCOPED human caller: require EXACT write access to the lead line (assignment / own
    # book / vertical-Head default ownership) — company visibility alone is not enough to
    # push someone else's lead into a deal.
    if ctx.user is not None and granted is authz.Access.SCOPED:
        from app.authz import scope as scope_mod

        user_scope = await scope_mod.build_scope(ctx, ctx.user)
        if not await scope_mod.can_write_row(ctx, user_scope, "Lead", lead_id):
            raise ForbiddenError(
                "Scoped access: this lead is not assigned to you (or your team), so you "
                "may not convert it.")

    deal = await _Repo(Deal).create(ctx.session, ctx.tenant_id, ctx.actor, {
        "entity_id": str(entity_id),
        "product_type": payload.product_type,
        "is_lending": payload.is_lending,
        "is_syndication": payload.is_syndication,
        "is_asset_mon": payload.is_asset_mon,
        "rm": payload.rm, "analyst": payload.analyst,
        "stage": "Data Awaited", "source": "RM",
        "source_detail": f"Converted from lead {lead_id}", "remarks": payload.note,
    })
    line_ids: dict[str, Any] = {}
    row: Any
    if payload.is_lending:
        row = await _Repo(LendingTracker).create(ctx.session, ctx.tenant_id, ctx.actor, {
            "entity_id": str(entity_id), "deal_id": str(deal.id),
            "amount_cr": payload.amount_cr, "rm": payload.rm, "analyst": payload.analyst,
            "stage": "Data Awaited"})
        line_ids["lending_id"] = row.id
    if payload.is_syndication:
        row = await _Repo(SyndicationTracker).create(ctx.session, ctx.tenant_id, ctx.actor, {
            "entity_id": str(entity_id), "deal_id": str(deal.id),
            "amount_cr": payload.amount_cr, "rm": payload.rm, "analyst": payload.analyst,
            "status": "Deal Sourced"})
        line_ids["syndication_id"] = row.id
    if payload.is_asset_mon:
        row = await _Repo(AssetMonetisation).create(ctx.session, ctx.tenant_id, ctx.actor, {
            "entity_id": str(entity_id), "deal_id": str(deal.id), "status": "Teaser Prepared"})
        line_ids["asset_mon_id"] = row.id

    await _Repo(SUBJECTS["Lead"]).update(ctx.session, ctx.tenant_id, lead_id, ctx.actor, {
        "status": "Converted", "converted_deal_id": str(deal.id),
        "conv": f"Converted by {ctx.actor}"})

    result = s.LeadConvertResult(lead_id=lead_id, deal_id=deal.id, **line_ids)
    if idempotency_key:
        await ctx.session.flush()
        ctx.session.add(IdempotencyKey(
            tenant_id=ctx.tenant_id, key=idempotency_key,
            request_hash="", method="POST",
            path=f"/v1/leads/{lead_id}/convert", status_code=200,
            response_body=result.model_dump(mode="json"),
            expires_at=datetime.now(UTC) + timedelta(
                hours=get_settings().idempotency_ttl_hours)))
    return result
