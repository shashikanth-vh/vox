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
# FIRM-WIDE VISIBILITY (deployment policy, 2026-08): every desk SEES every record —
# reading only. These view cells are held at READ by the seed on every start, so the
# policy ships with the build and an `upgrade` applies it to a running database with
# no operator step. Three deliberate properties:
#
#   * The WRITE gate is untouched. can_write_line evaluates ownership against the
#     transcribed spec matrix, not these cells — a desk reads the whole book and still
#     edits only its own rows. (That is also why the spec matrix itself is NOT edited:
#     baking READ into it would revoke every RM's write on their own lines.)
#   * An Admin override still wins. The seed refreshes only rows whose origin is
#     'baseline'; a cell an Admin has PATCHed (origin='override') is never touched.
#   * Today/Dashboard stay scoped (personal work queues) and the two guardrail views
#     stay Admin-only — neither appears here.
VISIBILITY_READ: tuple[tuple[str, str], ...] = tuple(
    (item, role)
    for item, roles in {
        "deals": ("BDRM", "Credit Head", "Deal Analyst",
                  "Syn Head", "Syn RM", "AM Head", "AM RM"),
        "leads": ("BDRM", "Credit Head", "Deal Analyst",
                  "Syn Head", "Syn RM", "AM Head", "AM RM"),
        "lending": ("BDRM", "Deal Analyst"),
        "syndication": ("BDRM", "Credit Head", "Deal Analyst", "Syn RM"),
        "asset_monetisation": ("BDRM", "Credit Head", "Deal Analyst", "AM RM"),
        # The two directories every grid JOINS its names from. Without them a widened
        # desk reads the book but not who it belongs to: lending rows rendered with
        # blank Group Code / Company for exactly the roles the layer had just widened,
        # because the entity lookup stayed scoped to their own (empty) book.
        "clients": ("BDRM", "Syn Head", "Syn RM", "AM Head", "AM RM"),
        "fi_master": ("Credit Head", "Deal Analyst", "Syn RM"),
    }.items()
    for role in roles
)

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
    # The firm-wide visibility layer, applied to this run's freshly inserted cells the
    # same way every later start applies it to a long-running database.
    await apply_visibility(session, tenant_id)
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


async def apply_visibility(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Hold the VISIBILITY_READ view cells at READ — the layer that ships with the
    build. Runs on EVERY service start (the one matrix-writing exception to the
    non-empty-database-is-report-only rule, alongside the default admin list), because
    a policy that only fresh installs receive is not a shipped policy: the first
    deployment of this layer proved that when a long-running production database kept
    its SCOPED cells and every widened role kept seeing nothing. origin='baseline'
    only — a cell an Admin has overridden stays exactly as the Admin left it; a cell
    missing entirely (a vocabulary the old seed never knew) is inserted."""
    refreshed: list[str] = []
    await session.flush()   # the maker runs autoflush=False — make pending inserts visible
    vis_rows = (
        await session.execute(select(AccessGrant).where(
            AccessGrant.tenant_id == tenant_id, AccessGrant.kind == "view"))
    ).scalars().all()
    by_key = {(g.item, g.role): g for g in vis_rows}
    for item, role in VISIBILITY_READ:
        row = by_key.get((item, role))
        if row is None:
            session.add(AccessGrant(tenant_id=tenant_id, kind="view", item=item,
                                    role=role, access="READ", origin="baseline",
                                    created_by="seed", updated_by="seed"))
            refreshed.append(f"{item}:{role}:missing->READ")
            continue
        if row.origin != "baseline" or row.access == "READ":
            continue
        refreshed.append(f"{item}:{role}:{row.access}->READ")
        row.access = "READ"
        row.updated_by = "seed"
        row.deleted_at = None
    if refreshed:
        await session.execute(
            update(MatrixVersion).where(MatrixVersion.tenant_id == tenant_id)
            .values(version=MatrixVersion.version + 1)
        )
        audit(session, tenant_id, "seed", "matrix.visibility", item=None,
              detail={"cells": refreshed})
    await session.flush()
    return refreshed


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
                    # The shipped visibility layer holds these exact cells at READ —
                    # that is the deployment's policy, not drift.
                    if (kind == "view" and (item, role) in VISIBILITY_READ
                            and got == "READ"):
                        continue
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
