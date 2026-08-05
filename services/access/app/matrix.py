"""The access matrix as data — seed, compile, edit (with guardrails), version.

The spec artifact (``evam_backend_core.rbac``) is the SEED and the schema of truth for
what exists; the ``access_grants`` table is the LIVE matrix Admins may edit at runtime.
Guardrail cells are immutable even to Admins so a mis-click can never disable the spec's
hard rules (irreversible delete, the Admin-only audit surfaces).
"""

from __future__ import annotations

import uuid

from evam_backend_core.errors import ForbiddenError, ValidationAppError
from evam_backend_core.rbac import OPERATIONS, ROLES, VIEW_ACCESS, Access, policy_fingerprint
from evam_backend_core.rbac_catalog import POLICY_VERSION
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessAudit, AccessGrant, MatrixVersion

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
                                        role=role, access=access.name, origin="baseline",
                                        created_by="seed", updated_by="seed"))
                added += 1
    ver = (
        await session.execute(select(MatrixVersion).where(MatrixVersion.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if ver is None:
        session.add(MatrixVersion(tenant_id=tenant_id, version=1))
    if added:
        audit(session, tenant_id, "seed", "matrix.seed", item=None,
              detail={"cells_added": added, "fingerprint": policy_fingerprint()})
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
    """Role stacking on data cells: the highest access across held roles. Held roles
    pass through the rename table first (v3.7: "LMS Authorizer" → "LMS Management")
    so rows granted under a role's old name keep resolving."""
    from evam_backend_core.rbac_catalog import canonical_roles

    best = Access.NONE
    for r in canonical_roles(roles):
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
        before = None
        session.add(AccessGrant(tenant_id=tenant_id, kind=kind, item=item, role=role,
                                access=access, origin="override",
                                created_by=actor, updated_by=actor))
    else:
        before = row.access
        row.access = access
        row.origin = "override"
        row.updated_by = actor
        row.deleted_at = None
    audit(session, tenant_id, actor, "matrix.edit", item=f"{kind}:{item}:{role}",
          detail={"from": before, "to": access})
    await session.execute(
        update(MatrixVersion).where(MatrixVersion.tenant_id == tenant_id)
        .values(version=MatrixVersion.version + 1)
    )
    await session.flush()


def audit(session: AsyncSession, tenant_id: uuid.UUID, actor: str, action: str,
          *, item: str | None, detail: dict | None = None) -> None:
    """Append one immutable governance-audit event, stamped with the policy version."""
    session.add(AccessAudit(tenant_id=tenant_id, actor=actor, action=action, item=item,
                            detail=detail, policy_version=POLICY_VERSION))


async def drift_report(session: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Compare the LIVE matrix against the approved compiled baseline — REPORT ONLY, no
    writes. Deployment runs this (``python -m app.seed --check``) to prove the database
    still reflects the approved ATLAS version plus known overrides."""
    live, version = await compiled_matrix(session, tenant_id)
    rows = (
        await session.execute(select(AccessGrant).where(
            AccessGrant.tenant_id == tenant_id, AccessGrant.deleted_at.is_(None)))
    ).scalars().all()
    origin_of = {(g.kind, g.item, g.role): g.origin for g in rows}
    missing: list[dict] = []
    differing: list[dict] = []
    unknown: list[dict] = []
    for kind, matrix in (("view", VIEW_ACCESS), ("operation", OPERATIONS)):
        for item, row in matrix.items():
            for role, access in row.items():
                got = live.get(kind, {}).get(item, {}).get(role)
                if got is None:
                    missing.append({"kind": kind, "item": item, "role": role,
                                    "baseline": access.name})
                elif got != access.name:
                    differing.append({"kind": kind, "item": item, "role": role,
                                      "baseline": access.name, "live": got,
                                      "origin": origin_of.get((kind, item, role), "?")})
    for kind, items in live.items():
        known: dict = VIEW_ACCESS if kind == "view" else OPERATIONS
        for item, live_row in items.items():
            for role in live_row:
                if item not in known or role not in known.get(item, {}):
                    unknown.append({"kind": kind, "item": item, "role": role})
    return {"policy_version": POLICY_VERSION, "fingerprint": policy_fingerprint(),
            "matrix_version": version,
            "in_sync": not (missing or differing or unknown),
            "missing_baseline_cells": missing,
            "differing_cells": differing,
            "unknown_cells": unknown}
