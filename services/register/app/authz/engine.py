"""RBAC evaluation — role stacking, scope, assignments, approvals, enforcement.

Design decisions (from the spec):
* **Role stacking**: a user holds N roles; for any view/operation the HIGHER access wins
  (``max()`` over ``Access``).
* **Assignment-driven permission**: being assigned to a line grants role-appropriate
  write on that line, regardless of the user's vertical, until unassigned.
* **Ownership by default**: an unassigned line belongs to its vertical Head.
* **Backwards-compatible enforcement**: a request carries a user via ``X-User-Email``.
  When present, checks apply. When absent, behaviour depends on
  ``REGISTER_ENFORCE_RBAC`` — off (default) keeps machine-to-machine/API-key flows
  working exactly as before; on requires a user for gated operations.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.matrix import (
    APPROVER_FOR_SUBJECT,
    ASSIGNMENT_AUTHORITY,
    OPERATIONS,
    SERVICE_GRANTS,
    SERVICE_READ_GRANTS,
    VIEW_ACCESS,
    Access,
)
from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.models.users import LineAssignment

# The named service principal for an identity-less (machine) request, resolved from the
# API key in get_context. None = generic key (legacy compatibility). Set per request; read
# by enforce_operation so no call site needs to thread it through.
service_ctx: ContextVar[str | None] = ContextVar("service_ctx", default=None)


@dataclass
class UserContext:
    """The acting user, resolved once per request when ``X-User-Email`` is sent."""

    id: uuid.UUID
    email: str
    full_name: str
    roles: set[str] = field(default_factory=set)
    # The user's reporting team (transitive), resolved by the Access service and
    # forwarded by the gateway — the basis of a Head's team scope.
    report_ids: list[uuid.UUID] = field(default_factory=list)
    report_emails: list[str] = field(default_factory=list)
    # The caller's LIVE effective access, by view / operation name, when it arrived in a
    # signed internal context. When present it is AUTHORITATIVE — the Register enforces it
    # instead of re-deriving from the compiled static matrix, so a live Access-matrix edit
    # takes effect immediately and the two services can never disagree. Empty = derive from
    # the static matrix (dev / legacy header propagation).
    effective_operations: dict[str, Access] = field(default_factory=dict)
    effective_views: dict[str, Access] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles

    @property
    def display(self) -> str:
        return self.email


def user_context_from_headers(
    email: str, roles_header: str | None, user_id_header: str | None,
    report_ids_header: str | None = None, report_emails_header: str | None = None,
) -> UserContext:
    """Build the acting user from gateway-forwarded (or dev-trusted) identity headers.

    Identity FACTS live in the Access service; the Gateway resolves them there (cached)
    and forwards email + roles + id. A stable UUID is derived from the e-mail when no id
    header is present (dev/direct calls), so scoped checks still key consistently.
    """
    email = email.strip().lower()
    roles = {r.strip() for r in (roles_header or "").split(",") if r.strip()}
    if user_id_header:
        uid = uuid.UUID(user_id_header)
    else:
        uid = uuid.uuid5(uuid.NAMESPACE_URL, f"prism-user:{email}")
    report_ids = [uuid.UUID(x.strip()) for x in (report_ids_header or "").split(",")
                  if x.strip()]
    report_emails = [x.strip().lower() for x in (report_emails_header or "").split(",")
                     if x.strip()]
    return UserContext(id=uid, email=email, full_name=email.split("@")[0], roles=roles,
                       report_ids=report_ids, report_emails=report_emails)


def _to_access(name: str) -> Access:
    try:
        return Access[name]
    except KeyError:
        return Access.NONE


def user_context_from_internal(ic) -> UserContext:  # noqa: ANN001
    """Build the acting user from a VERIFIED signed internal context (the production
    channel). Identity, roles AND the live effective matrices all come from the token, so
    nothing here is client-assertable."""
    uid = (uuid.UUID(ic.user_id) if _looks_like_uuid(ic.user_id)
           else uuid.uuid5(uuid.NAMESPACE_URL, f"prism-user:{ic.email.strip().lower()}"))
    return UserContext(
        id=uid,
        email=ic.email.strip().lower(),
        full_name=ic.email.split("@")[0],
        roles=set(ic.roles),
        report_ids=[uuid.UUID(x) for x in ic.report_ids if _looks_like_uuid(x)],
        report_emails=[x.strip().lower() for x in ic.report_emails if x.strip()],
        effective_operations={k: _to_access(v) for k, v in ic.effective_operations.items()},
        effective_views={k: _to_access(v) for k, v in ic.effective_views.items()},
    )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _stacked(matrix_row: dict[str, Access], roles: set[str]) -> Access:
    """Role stacking: the highest access across all held roles."""
    return max((matrix_row.get(r, Access.NONE) for r in roles), default=Access.NONE)


def enforce_service_read(resource: str | None = None,
                         user: UserContext | None = None) -> None:
    """Gate a READ for a machine (service) caller.

    Cases:
      * **DELEGATED read** — the request carries a signed USER context (``user`` is set).
        The service acts on a human's behalf; that user's view/row scope governs the read
        downstream, so the gate PASSES regardless of the service's own read grants. This is
        what lets ``svc_atlas`` (a pure BFF with no own-key grants) serve legitimate reads.
      * **generic / unnamed key** (service is None, no user) — dev-compat, passes ONLY when
        RBAC is not enforced. Under ``enforce_rbac`` (production) an unnamed key gets NO
        blanket read: it FAILS CLOSED (a leaked generic key must not read tenant-wide,
        request deleted rows, or pivot tenants via X-Tenant). It must forward a user context.
      * **OWN-KEY read** (named service, no user) — restricted to the resources on the
        service's READ allowlist. Having a *write* grant does not imply tenant-wide read of
        every table: ``svc_pulse`` may read its intelligence context, not deals.
    """
    if user is not None:
        return  # delegated — the user's scope governs
    service = service_ctx.get()
    if service is None:
        if get_settings().enforce_rbac:
            raise ForbiddenError(
                "An unnamed API key may not read the data plane without a user context "
                "(RBAC enforced). Route through the gateway with a signed identity.")
        return  # dev / compatibility
    allowed = SERVICE_READ_GRANTS.get(service, set())
    if resource is None or resource not in allowed:
        raise ForbiddenError(
            f"Service '{service}' may not read '{resource or 'this resource'}' on its own "
            "key; it must forward a user context or use its own capability endpoints.")


def view_access(user: UserContext, view: str) -> Access:
    """A view's access for this user, preferring the LIVE effective grant from a signed
    context; falls back to the compiled static matrix (dev / legacy)."""
    if user.effective_views:
        return user.effective_views.get(view, Access.NONE)
    return _stacked(VIEW_ACCESS.get(view, {}), user.roles)


def effective_views(user: UserContext) -> dict[str, str]:
    """View → access name, after stacking. What ATLAS renders the menu from."""
    return {view: _stacked(row, user.roles).name for view, row in VIEW_ACCESS.items()}


def effective_operations(user: UserContext) -> dict[str, str]:
    """Operation → access name, after stacking."""
    return {op: _stacked(row, user.roles).name for op, row in OPERATIONS.items()}


def operation_access(user: UserContext, operation: str) -> Access:
    # Prefer the LIVE effective grant from a signed internal context — so an Admin's live
    # matrix edit in Access is enforced here immediately, and the Register can never grant
    # more than Access currently allows. Fall back to the compiled matrix (dev / legacy).
    if user.effective_operations:
        return user.effective_operations.get(operation, Access.NONE)
    row = OPERATIONS.get(operation)
    if row is None:
        raise ValueError(f"Unknown operation '{operation}'.")
    return _stacked(row, user.roles)


def enforce_operation(user: UserContext | None, operation: str) -> Access:
    """Gate an endpoint on an operation from the matrix.

    Returns the granted access level (FULL vs SCOPED — callers use it to narrow rows).
    Machine callers (no user) are resolved against their SERVICE PRINCIPAL:
      * a NAMED service key (svc_pulse / svc_vox / svc_workflows / svc_atlas) may perform
        ONLY the operations on its allowlist — least privilege, regardless of enforce_rbac;
      * a generic/unnamed key keeps the legacy behaviour (compat off, 403 when enforced).
    """
    if user is None:
        service = service_ctx.get()
        if service is not None:
            allowed = SERVICE_GRANTS.get(service, set())
            if operation in allowed:
                return Access.FULL
            raise ForbiddenError(
                f"Service '{service}' is not permitted to perform '{operation}'.")
        if get_settings().enforce_rbac:
            raise ForbiddenError(
                f"Operation '{operation}' requires a user context (X-User-Email)."
            )
        return Access.FULL  # generic machine key / dev mode: API key already vetted
    granted = operation_access(user, operation)
    if granted is Access.NONE:
        raise ForbiddenError(
            f"Role(s) {sorted(user.roles) or ['<none>']} may not perform '{operation}'."
        )
    return granted


async def active_assignments(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[LineAssignment]:
    rows = (
        await session.execute(
            select(LineAssignment).where(
                LineAssignment.tenant_id == tenant_id,
                LineAssignment.user_id == user_id,
                LineAssignment.ended_at.is_(None),
                LineAssignment.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def is_assigned(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID,
    subject_type: str, subject_id: uuid.UUID,
) -> bool:
    row = (
        await session.execute(
            select(LineAssignment.id).where(
                LineAssignment.tenant_id == tenant_id,
                LineAssignment.user_id == user_id,
                LineAssignment.subject_type == subject_type,
                LineAssignment.subject_id == subject_id,
                LineAssignment.ended_at.is_(None),
                LineAssignment.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def can_write_line(
    session: AsyncSession, tenant_id: uuid.UUID, user: UserContext,
    subject_type: str, subject_id: uuid.UUID,
) -> bool:
    """Write follows the vertical: FULL access writes anywhere; SCOPED writes only on
    lines the user is assigned to (the assignment-driven primitive)."""
    view = {"Lending": "lending", "Syndication": "syndication",
            "AssetMonetisation": "asset_monetisation", "Lead": "leads",
            "Deal": "deals"}.get(subject_type)
    if view is None:
        return False
    granted = _stacked(VIEW_ACCESS[view], user.roles)
    if granted is Access.FULL:
        return True
    if granted is Access.SCOPED:
        # The full scope rule (assignment / team / vertical-Head default ownership of
        # an unassigned line) lives in the central evaluator.
        from app.authz import scope as scope_mod
        from app.core.security import RequestContext

        ctx = RequestContext(session, tenant_id, "", "", user=user)
        user_scope = await scope_mod.build_scope(ctx, user)
        return await scope_mod.can_write_row(ctx, user_scope, subject_type, subject_id)
    return False


def can_assign(user: UserContext, subject_type: str, assignment_role: str) -> bool:
    """Who may place this assignment role on this line (Credit Head owns the analyst
    pool; each vertical Head assigns their own RM; Mgmt/Admin override)."""
    allowed = ASSIGNMENT_AUTHORITY.get((subject_type, assignment_role))
    if allowed is None:
        return user.roles & {"Admin", "Management"} != set()
    return bool(user.roles & allowed)


def can_approve(user: UserContext, subject_type: str) -> bool:
    """Approval routing: Admin, Management, and the relevant vertical Head."""
    allowed = APPROVER_FOR_SUBJECT.get(subject_type, {"Admin", "Management"})
    return bool(user.roles & allowed)
