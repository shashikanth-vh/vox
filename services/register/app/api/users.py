"""User management & RBAC endpoints — the ATLAS RBAC spec (v3.1) as an API.

Flows implemented exactly per the spec:
* **Employees governance** — `/v1/users` CRUD (+ activate/deactivate), e-mail domain
  enforced (SSO integrity), `reports_to` mandatory for ICs. Admin/Management only.
* **Role stacking** — grant/revoke catalogue roles; effective permission = highest role.
* **Assignments** — Credit Head assigns Deal Analysts to Lending/Syn/AM lines; Syn Head
  assigns Syn RMs; AM Head assigns AM RMs; BD Head reassigns leads. Assignment grants
  write on that line until unassigned; two assignees can co-exist on a line.
* **Request → approve/reject** — non-approvers raise a stage/status change request;
  Admin / Management / the relevant vertical Head decides; approval APPLIES the change
  (with history + audit via the standard repository).
* **/v1/me** — the caller's effective views, operations and assignments; ATLAS renders
  its menus and buttons straight from this.
* **/v1/authz/check** — evaluate any (operation, subject) for the calling user.

Authority checks always apply when a user context is present; without one, behaviour
follows REGISTER_ENFORCE_RBAC (compatibility mode for machine-to-machine callers).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import select

from app import authz
from app.authz.matrix import PRIMARY_ASSIGNMENT_ROLE, ROLES
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models.users import ChangeRequest, LineAssignment, User, UserRole
from app.repositories.crud import CRUDRepository
from app.repositories.subjects import SUBJECTS, load_subject
from app.schemas import users as s
from app.schemas.users import AssignmentRead, ChangeRequestRead, MeRead, UserRead

router = api_router()

_user_repo = CRUDRepository(User, searchable=["email", "full_name", "short_name"],
                            filterable=["is_active", "email"])
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


def _validate_email(email: str) -> str:
    email = email.strip().lower()
    domain = get_settings().user_email_domain
    if "@" not in email or not email.endswith(f"@{domain}"):
        raise ValidationAppError(f"User e-mail must be an @{domain} address (SSO integrity).")
    return email


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValidationAppError(f"Unknown role '{role}'. One of: {', '.join(ROLES)}.")
    return role


async def _roles_of(ctx: RequestContext, user_id: uuid.UUID) -> list[str]:
    rows = (
        await ctx.session.execute(
            select(UserRole.role).where(
                UserRole.tenant_id == ctx.tenant_id,
                UserRole.user_id == user_id,
                UserRole.deleted_at.is_(None),
            ).order_by(UserRole.role)
        )
    ).scalars().all()
    return list(rows)


async def _user_read(ctx: RequestContext, obj: User) -> UserRead:
    data = UserRead.model_validate(obj)
    data.roles = await _roles_of(ctx, obj.id)
    return data


# --------------------------------------------------------------------------- #
# Users (Employees governance table) — Admin / Management only
# --------------------------------------------------------------------------- #
@router.post("/v1/users", response_model=UserRead, status_code=201, tags=["Users & RBAC"],
             summary="Add a user (employee) — optionally with initial roles")
async def create_user(payload: s.UserCreate, ctx: RequestContext = Depends(get_context)) -> Any:
    authz.enforce_operation(ctx.user, "add_employee_assign_role")
    data = payload.model_dump(exclude_unset=False)
    roles = data.pop("roles", None) or []
    data["email"] = _validate_email(data["email"])
    for r in roles:
        _validate_role(r)
    obj = await _user_repo.create(ctx.session, ctx.tenant_id, ctx.actor, data)
    for r in dict.fromkeys(roles):  # de-dupe, keep order
        ctx.session.add(UserRole(tenant_id=ctx.tenant_id, user_id=obj.id, role=r,
                                 granted_by=ctx.actor, created_by=ctx.actor,
                                 updated_by=ctx.actor))
    await ctx.session.flush()
    return await _user_read(ctx, obj)


@router.get("/v1/users", response_model=list[UserRead], tags=["Users & RBAC"],
            summary="List users (team directory is readable by every role)")
async def list_users(ctx: RequestContext = Depends(get_context),
                     q: str | None = Query(default=None),
                     include_inactive: bool = Query(default=False)) -> Any:
    conds = [User.tenant_id == ctx.tenant_id, User.deleted_at.is_(None)]
    if not include_inactive:
        conds.append(User.is_active.is_(True))
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_

        conds.append(or_(User.email.ilike(like), User.full_name.ilike(like)))
    rows = (
        await ctx.session.execute(select(User).where(*conds).order_by(User.full_name))
    ).scalars().all()
    return [await _user_read(ctx, u) for u in rows]


@router.get("/v1/users/{user_id}", response_model=UserRead, tags=["Users & RBAC"],
            summary="Get one user with their stacked roles")
async def get_user(user_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> Any:
    obj = await _user_repo.get(ctx.session, ctx.tenant_id, user_id)
    return await _user_read(ctx, obj)


@router.patch("/v1/users/{user_id}", response_model=UserRead, tags=["Users & RBAC"],
              summary="Edit a user (governance fields) — Admin / Management")
async def update_user(user_id: uuid.UUID, payload: s.UserUpdate,
                      ctx: RequestContext = Depends(get_context)) -> Any:
    authz.enforce_operation(ctx.user, "edit_employee")
    data = payload.model_dump(exclude_unset=True)
    expected = data.pop("expected_version", None)
    obj = await _user_repo.update(ctx.session, ctx.tenant_id, user_id, ctx.actor, data,
                                  expected_version=expected)
    return await _user_read(ctx, obj)


@router.post("/v1/users/{user_id}/roles", response_model=UserRead, status_code=201,
             tags=["Users & RBAC"], summary="Grant a role (role stacking)")
async def grant_role(user_id: uuid.UUID, payload: s.RoleGrant,
                     ctx: RequestContext = Depends(get_context)) -> Any:
    authz.enforce_operation(ctx.user, "add_employee_assign_role")
    role = _validate_role(payload.role)
    obj = await _user_repo.get(ctx.session, ctx.tenant_id, user_id)
    if role in await _roles_of(ctx, user_id):
        raise ConflictError(f"User already holds role '{role}'.")
    ctx.session.add(UserRole(tenant_id=ctx.tenant_id, user_id=user_id, role=role,
                             granted_by=ctx.actor, created_by=ctx.actor, updated_by=ctx.actor))
    await ctx.session.flush()
    return await _user_read(ctx, obj)


@router.delete("/v1/users/{user_id}/roles/{role}", response_model=UserRead,
               tags=["Users & RBAC"], summary="Revoke a role")
async def revoke_role(user_id: uuid.UUID, role: str,
                      ctx: RequestContext = Depends(get_context)) -> Any:
    authz.enforce_operation(ctx.user, "add_employee_assign_role")
    obj = await _user_repo.get(ctx.session, ctx.tenant_id, user_id)
    row = (
        await ctx.session.execute(
            select(UserRole).where(UserRole.tenant_id == ctx.tenant_id,
                                   UserRole.user_id == user_id, UserRole.role == role,
                                   UserRole.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"User does not hold role '{role}'.")
    row.deleted_at = datetime.now(UTC)
    row.updated_by = ctx.actor
    await ctx.session.flush()
    return await _user_read(ctx, obj)


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
        authz.enforce_operation(None, "add_employee_assign_role")  # honours enforce_rbac

    # Subject + assignee must exist.
    if await load_subject(ctx.session, ctx.tenant_id, stype, data["subject_id"]) is None:
        raise NotFoundError(f"{stype} '{data['subject_id']}' not found.")
    await _user_repo.get(ctx.session, ctx.tenant_id, data["user_id"])

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
    subject = await load_subject(ctx.session, ctx.tenant_id, stype, data["subject_id"])
    if subject is None:
        raise NotFoundError(f"{stype} '{data['subject_id']}' not found.")
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
# /v1/me + /v1/authz/check — what the caller may see and do
# --------------------------------------------------------------------------- #
@router.get("/v1/me", response_model=MeRead, tags=["Users & RBAC"],
            summary="The calling user's effective permissions (renders the ATLAS menu)")
async def me(ctx: RequestContext = Depends(get_context)) -> Any:
    user = _require_user(ctx)
    assignments = await authz.engine.active_assignments(ctx.session, ctx.tenant_id, user.id)
    return MeRead(
        id=user.id, email=user.email, full_name=user.full_name,
        roles=sorted(user.roles),
        views=authz.effective_views(user),
        operations=authz.effective_operations(user),
        assignments=[AssignmentRead.model_validate(a) for a in assignments],
    )


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
        on_line = await authz.engine.is_assigned(
            ctx.session, ctx.tenant_id, user.id, subject_type, subject_id)
        allowed = on_line
    return {"operation": operation, "allowed": allowed, "access": scope,
            "on_line": on_line,
            "primary_owner_default": PRIMARY_ASSIGNMENT_ROLE.get(subject_type or "", None)}
