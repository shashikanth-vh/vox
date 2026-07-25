"""Access-service auth: API key, tenant, and the acting user (for Admin-only writes)."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from evam_backend_core.db.session import get_sessionmaker
from evam_backend_core.errors import ForbiddenError, UnauthorizedError
from evam_backend_core.logging import actor_ctx, tenant_ctx
from fastapi import Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Tenant, User, UserRole

_tenant_cache: dict[str, uuid.UUID] = {}


@dataclass
class ActingUser:
    id: uuid.UUID
    email: str
    full_name: str
    roles: set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles


@dataclass
class RequestContext:
    session: AsyncSession
    tenant_id: uuid.UUID
    tenant_code: str
    actor: str
    user: ActingUser | None = None


def _check_api_key(provided: str | None) -> None:
    settings = get_settings()
    if not settings.require_api_key:
        return
    if not provided:
        raise UnauthorizedError("Missing X-API-Key header.")
    for key in settings.api_keys:
        if hmac.compare_digest(provided, key):
            return
    raise UnauthorizedError("Invalid API key.")


async def _resolve_tenant_id(session: AsyncSession, code: str) -> uuid.UUID:
    if code in _tenant_cache:
        return _tenant_cache[code]
    row = (
        await session.execute(select(Tenant).where(Tenant.code == code, Tenant.is_active.is_(True)))
    ).scalar_one_or_none()
    if row is None:
        raise ForbiddenError(f"Unknown or inactive tenant '{code}'.")
    _tenant_cache[code] = row.id
    return row.id


def clear_tenant_cache() -> None:
    _tenant_cache.clear()


async def load_acting_user(
    session: AsyncSession, tenant_id: uuid.UUID, email: str
) -> ActingUser:
    user = (
        await session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == email.strip().lower(),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise ForbiddenError(f"Unknown or inactive user '{email}'.")
    roles = (
        await session.execute(
            select(UserRole.role).where(
                UserRole.tenant_id == tenant_id,
                UserRole.user_id == user.id,
                UserRole.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return ActingUser(id=user.id, email=user.email, full_name=user.full_name, roles=set(roles))


def require_admin(ctx: RequestContext, action: str) -> None:
    """Governance writes are Admin-only (the user's requirement). Compatibility mode:
    no user context → allowed unless ACCESS_ENFORCE_RBAC is on."""
    if ctx.user is None:
        if get_settings().enforce_rbac:
            raise ForbiddenError(f"'{action}' requires an Admin user context (X-User-Email).")
        return
    if not ctx.user.is_admin:
        raise ForbiddenError(f"'{action}' is Admin-only; role(s) {sorted(ctx.user.roles)} denied.")


async def get_context(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_tenant: str | None = Header(default=None, alias="X-Tenant"),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> AsyncIterator[RequestContext]:
    settings = get_settings()
    _check_api_key(x_api_key)
    tenant_code = (x_tenant or settings.default_tenant_code).strip()
    actor = (x_actor or "api").strip()[:120]

    sm = get_sessionmaker()
    async with sm() as session:
        try:
            tenant_id = await _resolve_tenant_id(session, tenant_code)
            user = None
            if x_user_email:
                user = await load_acting_user(session, tenant_id, x_user_email)
                if actor == "api":
                    actor = user.email[:120]
            tenant_ctx.set(tenant_code)
            actor_ctx.set(actor)
            request.state.tenant_id = tenant_id
            request.state.actor = actor
            yield RequestContext(session, tenant_id, tenant_code, actor, user=user)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
