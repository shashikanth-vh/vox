"""Authentication, tenant resolution and the per-request context.

The Register keeps auth deliberately light (per the brief — user management lives
upstream in the platform's doors): a shared ``X-API-Key`` gates access, and every
request is bound to a tenant (default: Evam) via the ``X-Tenant`` header. That tenant
id is set as a PostgreSQL session GUC so row-level-security policies can enforce
isolation at the database, not just in application code.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Header, Request

if TYPE_CHECKING:
    from app.authz.engine import UserContext
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.logging import actor_ctx, tenant_ctx
from app.db.session import get_sessionmaker
from app.models.system import Tenant

# Cache of tenant code → id. Tenants change rarely; this avoids a lookup per request.
_tenant_cache: dict[str, uuid.UUID] = {}


@dataclass
class RequestContext:
    session: AsyncSession
    tenant_id: uuid.UUID
    tenant_code: str
    actor: str
    # The acting user (resolved from X-User-Email), when the caller identifies one.
    # None = machine-to-machine call carrying only the API key — RBAC checks then follow
    # settings.enforce_rbac (off: compatibility mode; on: gated operations 403).
    user: "UserContext | None" = None


def _check_api_key(provided: str | None) -> None:
    settings = get_settings()
    if not settings.require_api_key:
        return
    if not provided:
        raise UnauthorizedError("Missing X-API-Key header.")
    # Constant-time comparison against every configured key.
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


async def get_context(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_tenant: str | None = Header(default=None, alias="X-Tenant"),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> AsyncIterator[RequestContext]:
    """FastAPI dependency: authenticate, resolve tenant, open a scoped transaction.

    Yields a :class:`RequestContext` carrying a single transactional session already
    scoped to the caller's tenant. Commit-on-success / rollback-on-error is handled here
    so handlers never manage transactions by hand.
    """
    settings = get_settings()
    _check_api_key(x_api_key)

    tenant_code = (x_tenant or settings.default_tenant_code).strip()
    actor = (x_actor or "api").strip()[:120]

    sm = get_sessionmaker()
    async with sm() as session:
        try:
            tenant_id = await _resolve_tenant_id(session, tenant_code)
            # Bind the tenant to the session for RLS (transaction-local).
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            # Resolve the acting user when the caller identifies one (RBAC context).
            user = None
            if x_user_email:
                from app.authz.engine import load_user_context

                user = await load_user_context(session, tenant_id, x_user_email)
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
