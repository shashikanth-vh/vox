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
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.matrix import (
    APPROVER_FOR_SUBJECT,
    ASSIGNMENT_AUTHORITY,
    OPERATIONS,
    VIEW_ACCESS,
    Access,
)
from app.core.config import get_settings
from app.core.errors import ForbiddenError
from app.models.users import LineAssignment


@dataclass
class UserContext:
    """The acting user, resolved once per request when ``X-User-Email`` is sent."""

    id: uuid.UUID
    email: str
    full_name: str
    roles: set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles

    @property
    def display(self) -> str:
        return self.email


def user_context_from_headers(
    email: str, roles_header: str | None, user_id_header: str | None
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
    return UserContext(id=uid, email=email, full_name=email.split("@")[0], roles=roles)


def _stacked(matrix_row: dict[str, Access], roles: set[str]) -> Access:
    """Role stacking: the highest access across all held roles."""
    return max((matrix_row.get(r, Access.NONE) for r in roles), default=Access.NONE)


def effective_views(user: UserContext) -> dict[str, str]:
    """View → access name, after stacking. What ATLAS renders the menu from."""
    return {view: _stacked(row, user.roles).name for view, row in VIEW_ACCESS.items()}


def effective_operations(user: UserContext) -> dict[str, str]:
    """Operation → access name, after stacking."""
    return {op: _stacked(row, user.roles).name for op, row in OPERATIONS.items()}


def operation_access(user: UserContext, operation: str) -> Access:
    row = OPERATIONS.get(operation)
    if row is None:
        raise ValueError(f"Unknown operation '{operation}'.")
    return _stacked(row, user.roles)


def enforce_operation(user: UserContext | None, operation: str) -> Access:
    """Gate an endpoint on an operation from the matrix.

    Returns the granted access level (FULL vs SCOPED — callers use it to narrow rows).
    No user context → allowed in compatibility mode, 403 when RBAC is enforced.
    """
    if user is None:
        if get_settings().enforce_rbac:
            raise ForbiddenError(
                f"Operation '{operation}' requires a user context (X-User-Email)."
            )
        return Access.FULL  # machine-to-machine / dev mode: API key already vetted
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
        return await is_assigned(session, tenant_id, user.id, subject_type, subject_id)
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
