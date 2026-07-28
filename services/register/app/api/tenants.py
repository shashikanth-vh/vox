"""Tenant administration — CRUD for the tenant boundary itself.

Tenants sit **above** tenancy. Unlike every business table, a tenant row is *not*
tenant-scoped — scoping it would be a chicken-and-egg (you'd need a valid tenant to
create the first tenant). So these endpoints:

* are gated by the ``X-API-Key`` alone and do **not** require an ``X-Tenant`` header
  (treat the API key as an admin credential for this router), and
* are the API equivalent of ``python -m app.seed.bootstrap`` — create the tenant that
  every other request's ``X-Tenant`` then refers to.

    POST   /v1/tenants            create a tenant
    GET    /v1/tenants            list tenants
    GET    /v1/tenants/{code}     read one (by code or id)
    PATCH  /v1/tenants/{code}     rename / (de)activate
    DELETE /v1/tenants/{code}     deactivate (soft — business rows keep their tenant_id)

``code`` is the natural key used in the ``X-Tenant`` header and is immutable; a tenant is
never hard-deleted (that would orphan its business rows) — ``DELETE`` deactivates it, and
``PATCH {"is_active": true}`` reactivates. Every change is written to the audit trail and
clears the in-process tenant cache so it takes effect immediately.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import _check_api_key, clear_tenant_cache
from app.db.base import AuditLog
from app.db.session import get_sessionmaker
from app.models.system import Tenant
from app.schemas.base import ORMModel

router = api_router(prefix="/v1/tenants", tags=["Tenants"])


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40,
                      description="Natural key sent in the X-Tenant header (e.g. EVAM).")
    name: str = Field(min_length=1, max_length=200)
    is_active: bool = True


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class TenantRead(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None


# --------------------------------------------------------------------------- #
# auth: API key only, a plain (un-scoped) transactional session
# --------------------------------------------------------------------------- #
def _authorize_tenant_admin(
    x_api_key: str | None, x_admin_key: str | None,
    x_user_email: str | None, x_user_roles: str | None, x_gateway_auth: str | None,
) -> None:
    """Two-factor gate for tenant administration:

    * a **separately scoped admin credential** (``X-Admin-Key`` ∈ ``admin_api_keys``) —
      not the shared data-plane key. When ``admin_api_keys`` is unset (dev), the shared
      key still authenticates, but then a forwarded human identity MUST hold Admin.
    * a **verified platform-Admin identity** whenever an identity is forwarded: the
      gateway secret must match (so the identity can't be spoofed) and the roles must
      include ``Admin``. A non-admin routed through the gateway can never reach here even
      with a valid key.
    """
    import hmac as _hmac

    settings = get_settings()
    admin_keys = settings.admin_api_keys
    if admin_keys:
        if not (x_admin_key and any(_hmac.compare_digest(x_admin_key, k) for k in admin_keys)):
            raise UnauthorizedError(
                "Tenant administration requires a valid X-Admin-Key (admin credential).")
        # With the admin credential configured (production posture), the key is NOT enough:
        # a verified human Admin identity is ALSO required. A key alone can never administer
        # tenants.
        if not x_user_email:
            raise ForbiddenError(
                "Tenant administration requires a verified Admin identity, not just the key.")
    else:
        _check_api_key(x_api_key)  # dev/compat: the shared key authenticates the machine

    # If a human identity is forwarded, it must be a verified Admin.
    if x_user_email:
        secret = settings.gateway_shared_secret
        if secret and not (x_gateway_auth and _hmac.compare_digest(x_gateway_auth, secret)):
            raise ForbiddenError(
                "Identity headers must come via the gateway (X-Gateway-Auth mismatch).")
        roles = {r.strip() for r in (x_user_roles or "").split(",") if r.strip()}
        if "Admin" not in roles:
            raise ForbiddenError(
                f"Tenant administration is Admin-only; '{x_user_email}' is not an Admin.")


async def admin_session(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
    x_user_roles: str | None = Header(default=None, alias="X-User-Roles"),
    x_gateway_auth: str | None = Header(default=None, alias="X-Gateway-Auth"),
) -> AsyncIterator[AsyncSession]:
    """Authenticate a tenant-admin caller and yield a session with commit/rollback.

    Deliberately does NOT resolve or set a tenant — this router manages tenants
    themselves, which are not tenant-scoped.
    """
    _authorize_tenant_admin(x_api_key, x_admin_key, x_user_email, x_user_roles,
                            x_gateway_auth)
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _audit(session: AsyncSession, actor: str, action: str, tenant: Tenant,
                 changes: dict | None = None) -> None:
    # This router runs without a tenant GUC (it manages tenants, which aren't tenant-scoped)
    # — but the audit_log IS RLS-covered, so under FORCE RLS the insert's WITH CHECK needs
    # the GUC bound to the affected tenant. Set it transaction-locally right here.
    from sqlalchemy import text as _text

    await session.execute(
        _text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant.id)})
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor=actor,
            action=action,
            resource_type="tenant",
            resource_id=str(tenant.id),
            request_id=request_id_ctx.get(),
            changes=changes,
        )
    )


async def _find(session: AsyncSession, code_or_id: str) -> Tenant | None:
    """Look a tenant up by its code, or by its UUID if the path looks like one."""
    conds = [Tenant.code == code_or_id]
    with contextlib.suppress(ValueError):
        conds.append(Tenant.id == uuid.UUID(code_or_id))
    for cond in conds:
        row = (await session.execute(select(Tenant).where(cond))).scalar_one_or_none()
        if row is not None:
            return row
    return None


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.post("", response_model=TenantRead, status_code=201, summary="Create tenant")
async def create_tenant(
    payload: TenantCreate,
    session: AsyncSession = Depends(admin_session),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> TenantRead:
    # Prefer the verified Admin identity over the client-controlled X-Actor.
    actor = (x_user_email or x_actor or "api").strip()[:120]
    code = payload.code.strip()
    existing = (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A tenant with code '{code}' already exists.")
    row = Tenant(code=code, name=payload.name.strip(), is_active=payload.is_active,
                 created_by=actor, updated_by=actor)
    session.add(row)
    await session.flush()
    await _audit(session, actor, "create", row)
    await session.refresh(row)  # load server-side defaults (id, timestamps) before serialising
    clear_tenant_cache()  # so the new tenant resolves on the very next request
    return TenantRead.model_validate(row)


@router.get("", response_model=list[TenantRead], summary="List tenants")
async def list_tenants(
    session: AsyncSession = Depends(admin_session),
    active_only: bool = Query(default=False, description="Only return active tenants"),
) -> list[TenantRead]:
    stmt = select(Tenant).order_by(Tenant.code)
    if active_only:
        stmt = stmt.where(Tenant.is_active.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return [TenantRead.model_validate(r) for r in rows]


@router.get("/{code}", response_model=TenantRead, summary="Get tenant")
async def get_tenant(
    code: str,
    session: AsyncSession = Depends(admin_session),
) -> TenantRead:
    row = await _find(session, code)
    if row is None:
        raise NotFoundError(f"No tenant '{code}'.")
    return TenantRead.model_validate(row)


@router.patch("/{code}", response_model=TenantRead, summary="Update tenant")
async def update_tenant(
    code: str,
    payload: TenantUpdate,
    session: AsyncSession = Depends(admin_session),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> TenantRead:
    # Prefer the verified Admin identity over the client-controlled X-Actor.
    actor = (x_user_email or x_actor or "api").strip()[:120]
    row = await _find(session, code)
    if row is None:
        raise NotFoundError(f"No tenant '{code}'.")
    data = payload.model_dump(exclude_unset=True)
    changed = []
    for field in ("name", "is_active"):
        if field in data and getattr(row, field) != data[field]:
            setattr(row, field, data[field].strip() if field == "name" else data[field])
            changed.append(field)
    if changed:
        row.updated_by = actor
        await session.flush()
        await _audit(session, actor, "update", row, changes={"fields": changed})
        await session.refresh(row)  # reload server-updated updated_at
        clear_tenant_cache()  # (de)activation must take effect immediately
    return TenantRead.model_validate(row)


@router.delete("/{code}", response_model=TenantRead, summary="Deactivate tenant")
async def deactivate_tenant(
    code: str,
    session: AsyncSession = Depends(admin_session),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> TenantRead:
    """Soft-deactivate: a tenant is never hard-deleted because its business rows
    reference ``tenant_id``. Sets ``is_active=false``; reactivate via PATCH."""
    actor = (x_user_email or x_actor or "api").strip()[:120]
    row = await _find(session, code)
    if row is None:
        raise NotFoundError(f"No tenant '{code}'.")
    if row.is_active:
        row.is_active = False
        row.updated_by = actor
        await session.flush()
        await _audit(session, actor, "deactivate", row)
        await session.refresh(row)  # reload server-updated updated_at
        clear_tenant_cache()
    return TenantRead.model_validate(row)
