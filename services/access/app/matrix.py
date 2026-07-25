"""The access matrix as data — seed, compile, edit (with guardrails), version.

The spec artifact (``evam_backend_core.rbac``) is the SEED and the schema of truth for
what exists; the ``access_grants`` table is the LIVE matrix Admins may edit at runtime.
Guardrail cells are immutable even to Admins so a mis-click can never disable the spec's
hard rules (irreversible delete, the Admin-only audit surfaces).
"""

from __future__ import annotations

import uuid

from evam_backend_core.errors import ForbiddenError, ValidationAppError
from evam_backend_core.rbac import OPERATIONS, ROLES, VIEW_ACCESS, Access
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessGrant, MatrixVersion

ACCESS_LEVELS = {a.name for a in Access}

# Cells that may never be edited, even by Admin (kind, item). The spec's hard rules.
IMMUTABLE_ITEMS: set[tuple[str, str]] = {
    ("operation", "delete_row"),       # IRREVERSIBLE — Admin ONLY
    ("operation", "backup_restore"),   # restore can wipe the book — Admin ONLY
    ("view", "audit"),                 # Admin-only by design (v2.1)
    ("view", "activity_log"),          # Admin-only by design (v2.1)
}


async def seed_matrix(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert any missing cells from the spec artifact. Idempotent; returns rows added."""
    existing = set(
        (
            await session.execute(
                select(AccessGrant.kind, AccessGrant.item, AccessGrant.role).where(
                    AccessGrant.tenant_id == tenant_id
                )
            )
        ).all()
    )
    added = 0
    for kind, matrix in (("view", VIEW_ACCESS), ("operation", OPERATIONS)):
        for item, row in matrix.items():
            for role, access in row.items():
                if (kind, item, role) in existing:
                    continue
                session.add(AccessGrant(tenant_id=tenant_id, kind=kind, item=item,
                                        role=role, access=access.name,
                                        created_by="seed", updated_by="seed"))
                added += 1
    ver = (
        await session.execute(select(MatrixVersion).where(MatrixVersion.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if ver is None:
        session.add(MatrixVersion(tenant_id=tenant_id, version=1))
    await session.flush()
    return added


async def compiled_matrix(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[str, dict[str, dict[str, str]]], int]:
    """The live matrix as {kind: {item: {role: ACCESS}}} plus its version."""
    rows = (
        await session.execute(select(AccessGrant).where(
            AccessGrant.tenant_id == tenant_id, AccessGrant.deleted_at.is_(None)
        ))
    ).scalars().all()
    out: dict[str, dict[str, dict[str, str]]] = {"view": {}, "operation": {}}
    for g in rows:
        out.setdefault(g.kind, {}).setdefault(g.item, {})[g.role] = g.access
    ver = (
        await session.execute(select(MatrixVersion.version).where(
            MatrixVersion.tenant_id == tenant_id
        ))
    ).scalar_one_or_none()
    return out, int(ver or 0)


def stacked(row: dict[str, str], roles: set[str]) -> str:
    """Role stacking on data cells: the highest access across held roles."""
    best = Access.NONE
    for r in roles:
        level = Access[row.get(r, "NONE")]
        if level > best:
            best = level
    return best.name


async def set_grant(
    session: AsyncSession, tenant_id: uuid.UUID, actor: str,
    *, kind: str, item: str, role: str, access: str,
) -> None:
    """Edit one matrix cell (Admin-only at the API layer). Guardrails + validation here."""
    if kind not in ("view", "operation"):
        raise ValidationAppError("kind must be 'view' or 'operation'.")
    if role not in ROLES:
        raise ValidationAppError(f"Unknown role '{role}'. One of: {', '.join(ROLES)}.")
    if access not in ACCESS_LEVELS:
        raise ValidationAppError(f"Unknown access '{access}'. One of: {', '.join(sorted(ACCESS_LEVELS))}.")
    known = VIEW_ACCESS if kind == "view" else OPERATIONS
    if item not in known:
        raise ValidationAppError(f"Unknown {kind} '{item}'.")
    if (kind, item) in IMMUTABLE_ITEMS:
        raise ForbiddenError(
            f"'{item}' is a guardrail cell — immutable even to Admin (spec hard rule)."
        )

    row = (
        await session.execute(select(AccessGrant).where(
            AccessGrant.tenant_id == tenant_id, AccessGrant.kind == kind,
            AccessGrant.item == item, AccessGrant.role == role,
        ))
    ).scalar_one_or_none()
    if row is None:
        session.add(AccessGrant(tenant_id=tenant_id, kind=kind, item=item, role=role,
                                access=access, created_by=actor, updated_by=actor))
    else:
        row.access = access
        row.updated_by = actor
        row.deleted_at = None
    await session.execute(
        update(MatrixVersion).where(MatrixVersion.tenant_id == tenant_id)
        .values(version=MatrixVersion.version + 1)
    )
    await session.flush()
