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
    user: UserContext | None = None
    # The gateway's binary decision for this request ("FULL" | "SCOPED"), trusted under
    # the same rules as the identity headers. None = no gateway decision (direct call) —
    # the Register then falls back to its own code-matrix check.
    authz_decision: str | None = None


def _check_api_key(provided: str | None) -> None:
    settings = get_settings()
    if not settings.require_api_key:
        return
    if not provided:
        raise UnauthorizedError("Missing X-API-Key header.")
    # Constant-time comparison against every configured key (generic + named service keys).
    for key in settings.all_api_keys():
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
    x_on_behalf_of: str | None = Header(default=None, alias="X-On-Behalf-Of"),
    x_user_roles: str | None = Header(default=None, alias="X-User-Roles"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_gateway_auth: str | None = Header(default=None, alias="X-Gateway-Auth"),
    x_authz_decision: str | None = Header(default=None, alias="X-Authz-Decision"),
    x_user_report_ids: str | None = Header(default=None, alias="X-User-Report-Ids"),
    x_user_reports: str | None = Header(default=None, alias="X-User-Reports"),
    x_internal_context: str | None = Header(default=None, alias="X-Internal-Context"),
) -> AsyncIterator[RequestContext]:
    """FastAPI dependency: authenticate, resolve tenant, open a scoped transaction.

    Yields a :class:`RequestContext` carrying a single transactional session already
    scoped to the caller's tenant. Commit-on-success / rollback-on-error is handled here
    so handlers never manage transactions by hand.
    """
    settings = get_settings()
    _check_api_key(x_api_key)

    tenant_code = (x_tenant or settings.default_tenant_code).strip()
    # X-Actor is client-controlled and is only honoured for an UNauthenticated dev call.
    # A named service key stamps the service as the actor; an authenticated user overrides
    # it with their verified e-mail (below), so a caller can never masquerade as a person.
    service = settings.service_for_key(x_api_key)
    actor = service or (x_actor or "api").strip()[:120]
    from app.authz.engine import service_ctx
    service_ctx.set(service)

    sm = get_sessionmaker()
    async with sm() as session:
        try:
            tenant_id = await _resolve_tenant_id(session, tenant_code)
            # Bind the tenant to the session for RLS (transaction-local).
            await session.execute(
                text("SELECT set_config('app.current_tenant', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            # Acting user from gateway-forwarded identity headers. Identity FACTS live in
            # the Access service; the Gateway resolves + forwards them. Trust rules:
            #   * gateway secret configured → headers accepted only with a matching
            #     X-Gateway-Auth (spoofing-proof inside the perimeter);
            #   * no secret configured (dev/local) → headers trusted as sent.
            user = None
            decision = None
            # Preferred: a SIGNED internal context. Verifying its signature IS the proof it
            # came from the gateway — no shared secret to leak, and the caller's identity +
            # LIVE effective permissions come straight from the token (so the Register
            # enforces exactly what Access resolved, never a stale compiled copy).
            if x_internal_context and settings.internal_signing_secret:
                from evam_backend_core.internal_token import (
                    InternalTokenError,
                    verify_internal_context,
                )

                from app.authz.engine import user_context_from_internal
                from app.core.errors import ForbiddenError

                try:
                    ic = verify_internal_context(
                        x_internal_context,
                        verify_key=settings.internal_signing_secret,
                        algorithms=(settings.internal_signing_algorithm,))
                except InternalTokenError as exc:
                    raise ForbiddenError(f"Invalid internal context: {exc}") from exc
                # A token minted for one tenant cannot be replayed against another.
                if ic.tenant and ic.tenant != tenant_code:
                    raise ForbiddenError(
                        "Internal context tenant does not match the request tenant.")
                # …nor against a different route: the token is bound to the method + path
                # it was minted for, so it cannot be replayed across routes within its TTL.
                req_path = request.url.path
                if ic.method and ic.method != request.method:
                    raise ForbiddenError("Internal context method mismatch.")
                if ic.path and ic.path != req_path:
                    raise ForbiddenError("Internal context path mismatch.")
                user = user_context_from_internal(ic)
                if ic.decision in ("FULL", "SCOPED"):
                    decision = ic.decision
                actor = user.email[:120]
            elif x_user_email and not settings.internal_signing_secret:
                # LEGACY header propagation — trusted ONLY when the signed context is not
                # configured. Once signing is on, the signed token is the sole identity
                # path and plaintext X-User-* headers are ignored (no downgrade).
                from app.authz.engine import user_context_from_headers
                from app.core.errors import ForbiddenError

                secret = settings.gateway_shared_secret
                if secret and not (x_gateway_auth and hmac.compare_digest(x_gateway_auth, secret)):
                    raise ForbiddenError(
                        "Identity headers must come via the gateway (X-Gateway-Auth mismatch)."
                    )
                user = user_context_from_headers(x_user_email, x_user_roles, x_user_id,
                                                 x_user_report_ids, x_user_reports)
                if x_authz_decision in ("FULL", "SCOPED"):
                    decision = x_authz_decision
                # Authenticated writes are stamped with the VERIFIED user identity —
                # X-Actor is client-controlled and must never masquerade as a person.
                # (Own-book scope matches rows on created_by == user e-mail.)
                actor = user.email[:120]
            # ATTRIBUTION for a service acting FOR a person.
            #
            # VocX writes with its own service key: it holds the authority, but the lead
            # and the company it creates were dictated by an RM who has already been
            # verified at VocX's own front door. Stamped with the service, those rows
            # land in nobody's own book (scope rule 3, created_by = the user's e-mail) —
            # so the RM who recorded the capture could not see what they had just filed,
            # while an Admin could. That is the opposite of the intended default.
            #
            # This header changes WHO THE ROW BELONGS TO, never what the caller may do:
            # `user` stays None, so authorisation remains the service's, least-privilege
            # and unchanged. Honoured only for a NAMED service key (a generic key or a
            # human request cannot use it), and ignored outright once a verified user
            # identity is present — a person's own e-mail always wins over a claim.
            if service and user is None and x_on_behalf_of:
                claimed = x_on_behalf_of.strip()[:120]
                if "@" in claimed:
                    actor = claimed
            tenant_ctx.set(tenant_code)
            actor_ctx.set(actor)
            request.state.tenant_id = tenant_id
            request.state.actor = actor
            yield RequestContext(session, tenant_id, tenant_code, actor, user=user,
                                 authz_decision=decision)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
