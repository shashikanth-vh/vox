"""Access service endpoints — governance (Admin-only writes), resolve, me.

* ``/v1/users`` (+ roles)  — the Employees governance table. Writes are Admin-only.
* ``/v1/access``           — the live matrix; ``PATCH`` edits one cell (Admin-only,
                             guardrails enforced, every change bumps the matrix version).
* ``/v1/resolve``          — user → roles + effective matrices + version. The gateway
                             calls this on cache miss / version change — never per request.
* ``/v1/me``               — the calling user's own resolution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from evam_backend_core.errors import ConflictError, NotFoundError, ValidationAppError
from evam_backend_core.rbac import ROLES
from evam_backend_core.router import api_router
from fastapi import Depends, Query
from sqlalchemy import or_, select

from app import matrix as mx
from app.config import get_settings
from app.models import User, UserRole
from app.schemas import (
    GrantUpdate,
    ResolveRead,
    RoleGrant,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.security import RequestContext, get_context, require_admin, require_governance

router = api_router()


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


async def _get_user(ctx: RequestContext, user_id: uuid.UUID) -> User:
    obj = (
        await ctx.session.execute(
            select(User).where(User.id == user_id, User.tenant_id == ctx.tenant_id,
                               User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if obj is None:
        raise NotFoundError(f"user '{user_id}' not found.")
    return obj


async def _user_read(ctx: RequestContext, obj: User) -> UserRead:
    data = UserRead.model_validate(obj)
    data.roles = await _roles_of(ctx, obj.id)
    return data


# --------------------------------------------------------------------------- #
# Users (Employees governance) — writes Admin-only
# --------------------------------------------------------------------------- #
@router.post("/v1/users", response_model=UserRead, status_code=201, tags=["Users"],
             summary="Add a user (employee) — optionally with initial roles")
async def create_user(payload: UserCreate, ctx: RequestContext = Depends(get_context)) -> Any:
    require_governance(ctx, "add user")
    data = payload.model_dump(exclude_unset=False)
    roles = data.pop("roles", None) or []
    data["email"] = _validate_email(data["email"])
    for r in roles:
        _validate_role(r)
    obj = User(tenant_id=ctx.tenant_id, created_by=ctx.actor, updated_by=ctx.actor, **data)
    ctx.session.add(obj)
    await ctx.session.flush()
    for r in dict.fromkeys(roles):
        ctx.session.add(UserRole(tenant_id=ctx.tenant_id, user_id=obj.id, role=r,
                                 granted_by=ctx.actor, created_by=ctx.actor,
                                 updated_by=ctx.actor))
    mx.audit(ctx.session, ctx.tenant_id, ctx.actor, "user.create", item=obj.email,
             detail={"roles": list(dict.fromkeys(roles))})
    await ctx.session.flush()
    await ctx.session.refresh(obj)
    return await _user_read(ctx, obj)


@router.get("/v1/users", response_model=list[UserRead], tags=["Users"],
            summary="List users (readable by every role — the team directory)")
async def list_users(ctx: RequestContext = Depends(get_context),
                     q: str | None = Query(default=None),
                     include_inactive: bool = Query(default=False)) -> Any:
    conds = [User.tenant_id == ctx.tenant_id, User.deleted_at.is_(None)]
    if not include_inactive:
        conds.append(User.is_active.is_(True))
    if q:
        like = f"%{q}%"
        conds.append(or_(User.email.ilike(like), User.full_name.ilike(like)))
    rows = (
        await ctx.session.execute(select(User).where(*conds).order_by(User.full_name))
    ).scalars().all()
    return [await _user_read(ctx, u) for u in rows]


@router.get("/v1/users/{user_id}", response_model=UserRead, tags=["Users"],
            summary="Get one user with stacked roles")
async def get_user(user_id: uuid.UUID, ctx: RequestContext = Depends(get_context)) -> Any:
    return await _user_read(ctx, await _get_user(ctx, user_id))


@router.patch("/v1/users/{user_id}", response_model=UserRead, tags=["Users"],
              summary="Edit a user — Admin-only")
async def update_user(user_id: uuid.UUID, payload: UserUpdate,
                      ctx: RequestContext = Depends(get_context)) -> Any:
    require_governance(ctx, "edit user")
    obj = await _get_user(ctx, user_id)
    was_active = obj.is_active
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_by = ctx.actor
    if obj.is_active != was_active:
        # (De)activation is a revocation event: bump the epoch so previously issued
        # signed contexts fail sensitive-operation revalidation immediately.
        obj.permissions_epoch += 1
        mx.audit(ctx.session, ctx.tenant_id, ctx.actor,
                 "user.deactivate" if was_active else "user.reactivate", item=obj.email,
                 detail={"epoch": obj.permissions_epoch})
    await ctx.session.flush()
    await ctx.session.refresh(obj)
    return await _user_read(ctx, obj)


@router.post("/v1/users/{user_id}/roles", response_model=UserRead, status_code=201,
             tags=["Users"], summary="Grant a role (role stacking) — Admin-only")
async def grant_role(user_id: uuid.UUID, payload: RoleGrant,
                     ctx: RequestContext = Depends(get_context)) -> Any:
    require_governance(ctx, "grant role")
    role = _validate_role(payload.role)
    obj = await _get_user(ctx, user_id)
    # Revocation is a SOFT delete, but user_roles_unique covers every row — deleted
    # included. Inserting blind therefore 409s on any role this user EVER held: a
    # revoked role could never be granted back (the desk hit exactly this restoring a
    # deactivated admin). The grant must look for the buried row and restore it — the
    # audit trail keeps both the old revocation and this fresh grant.
    existing = (
        await ctx.session.execute(
            select(UserRole).where(UserRole.tenant_id == ctx.tenant_id,
                                   UserRole.user_id == user_id, UserRole.role == role)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.deleted_at is None:
        raise ConflictError(f"User already holds role '{role}'.")
    if existing is not None:
        existing.deleted_at = None
        existing.granted_by = ctx.actor
        existing.updated_by = ctx.actor
    else:
        ctx.session.add(UserRole(tenant_id=ctx.tenant_id, user_id=user_id, role=role,
                                 granted_by=ctx.actor, created_by=ctx.actor,
                                 updated_by=ctx.actor))
    obj.permissions_epoch += 1
    mx.audit(ctx.session, ctx.tenant_id, ctx.actor, "role.grant", item=obj.email,
             detail={"role": role, "epoch": obj.permissions_epoch,
                     **({"regrant": True} if existing is not None else {})})
    await ctx.session.flush()
    await ctx.session.refresh(obj)   # the epoch UPDATE touched onupdate columns
    return await _user_read(ctx, obj)


@router.delete("/v1/users/{user_id}/roles/{role}", response_model=UserRead, tags=["Users"],
               summary="Revoke a role — Admin-only")
async def revoke_role(user_id: uuid.UUID, role: str,
                      ctx: RequestContext = Depends(get_context)) -> Any:
    require_governance(ctx, "revoke role")
    obj = await _get_user(ctx, user_id)
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
    obj.permissions_epoch += 1
    mx.audit(ctx.session, ctx.tenant_id, ctx.actor, "role.revoke", item=obj.email,
             detail={"role": role, "epoch": obj.permissions_epoch})
    await ctx.session.flush()
    await ctx.session.refresh(obj)   # the epoch UPDATE touched onupdate columns
    return await _user_read(ctx, obj)


# --------------------------------------------------------------------------- #
# The access matrix — live data, Admin-editable, guardrails
# --------------------------------------------------------------------------- #
@router.get("/v1/access", tags=["Access Matrix"],
            summary="The live access matrix (views + operations) and its version")
async def get_access_matrix(ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    matrix, version = await mx.compiled_matrix(ctx.session, ctx.tenant_id)
    return {"version": version, "views": matrix["view"], "operations": matrix["operation"]}


@router.patch("/v1/access", tags=["Access Matrix"], status_code=200,
              summary="Edit one matrix cell — Admin-only; guardrail cells refuse")
async def patch_access_matrix(payload: GrantUpdate,
                              ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    require_admin(ctx, "edit access matrix")
    await mx.set_grant(ctx.session, ctx.tenant_id, ctx.actor,
                       kind=payload.kind, item=payload.item, role=payload.role,
                       access=payload.access)
    matrix, version = await mx.compiled_matrix(ctx.session, ctx.tenant_id)
    return {"version": version, "changed": payload.model_dump()}


# --------------------------------------------------------------------------- #
# Resolve + me — what the gateway caches
# --------------------------------------------------------------------------- #
async def _resolve(ctx: RequestContext, email: str) -> ResolveRead:
    user = (
        await ctx.session.execute(
            select(User).where(User.tenant_id == ctx.tenant_id,
                               User.email == email.strip().lower(),
                               User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise NotFoundError(f"user '{email}' not found or inactive.")
    roles = set(await _roles_of(ctx, user.id))
    matrix, version = await mx.compiled_matrix(ctx.session, ctx.tenant_id)
    views = {item: mx.stacked(row, roles) for item, row in matrix["view"].items()}
    operations = {item: mx.stacked(row, roles) for item, row in matrix["operation"].items()}

    # The user's reporting tree (transitive): the basis of a Head's TEAM scope in the
    # Register. Resolved here — the one service that knows reports_to — and forwarded
    # by the gateway as X-User-Report-Ids / X-User-Reports.
    all_rows = (
        await ctx.session.execute(
            select(User.id, User.email, User.reports_to).where(
                User.tenant_id == ctx.tenant_id, User.deleted_at.is_(None),
                User.is_active.is_(True))
        )
    ).all()
    children: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for uid, uemail, boss in all_rows:
        if boss is not None:
            children.setdefault(boss, []).append((uid, uemail))
    reports: list[dict] = []
    frontier = [user.id]
    seen = {user.id}
    while frontier:
        nxt: list[uuid.UUID] = []
        for boss in frontier:
            for uid, uemail in children.get(boss, []):
                if uid not in seen:
                    seen.add(uid)
                    reports.append({"id": uid, "email": uemail})
                    nxt.append(uid)
        frontier = nxt

    return ResolveRead(id=user.id, email=user.email, full_name=user.full_name,
                       is_active=user.is_active, roles=sorted(roles),
                       views=views, operations=operations, version=version,
                       epoch=user.permissions_epoch, reports=reports)


@router.get("/v1/resolve", response_model=ResolveRead, tags=["Resolve"],
            summary="User → roles + effective matrices + version (the gateway's cache fill)")
async def resolve(email: str = Query(...),
                  ctx: RequestContext = Depends(get_context)) -> Any:
    return await _resolve(ctx, email)


@router.get("/v1/access/drift", tags=["Access Matrix"],
            summary="Compare the live matrix against the approved compiled baseline — "
                    "report only, Admin-only, no writes")
async def access_drift(ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    require_admin(ctx, "read access drift")
    return await mx.drift_report(ctx.session, ctx.tenant_id)


@router.get("/v1/access/version", tags=["Resolve"],
            summary="Just the matrix version (cheap cache-validity poll)")
async def matrix_version(ctx: RequestContext = Depends(get_context)) -> dict[str, int]:
    _, version = await mx.compiled_matrix(ctx.session, ctx.tenant_id)
    return {"version": version}


@router.get("/v1/me", response_model=ResolveRead, tags=["Resolve"],
            summary="The calling user's own identity + effective matrices")
async def me(ctx: RequestContext = Depends(get_context)) -> Any:
    if ctx.user is None:
        from evam_backend_core.errors import ForbiddenError

        raise ForbiddenError("This endpoint requires a user context (X-User-Email).")
    return await _resolve(ctx, ctx.user.email)
