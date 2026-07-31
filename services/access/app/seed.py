"""CLI: ``python -m app.seed`` — the EXPLICIT, VERSIONED seed of the Access service.

Creates the default tenant, seeds MISSING baseline cells of the access matrix from the
approved compiled policy (``evam_backend_core.rbac``, provenance-tagged 'baseline' and
stamped with its policy version + fingerprint), and provisions the initial Admin user so
the RBAC chicken-and-egg is solved. Idempotent; NEVER overwrites a runtime override.

``python -m app.seed --check`` runs the DRIFT REPORT instead: compare the live matrix
against the approved baseline and print the differences — WITHOUT writing anything. Exit
code 0 = in sync, 3 = drift found (so a deployment pipeline can gate on it).

Production posture runs with ``ACCESS_AUTO_SEED=false``: the container start performs the
--check report only, and seeding happens solely through this explicit command.
"""

from __future__ import annotations

import asyncio
import uuid

from evam_backend_core.db.session import dispose_engine, get_sessionmaker, init_engine
from evam_backend_core.logging import configure_logging, get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.matrix import seed_matrix
from app.models import Tenant, User, UserRole

log = get_logger("access.seed")


async def ensure_tenant(session: AsyncSession, code: str, name: str) -> uuid.UUID:
    row = (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()
    if row is None:
        row = Tenant(code=code, name=name)
        session.add(row)
        await session.flush()
    return row.id


async def ensure_admin_user(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    email = f"admin@{get_settings().user_email_domain}"
    existing = (
        await session.execute(
            select(User).where(User.tenant_id == tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    user = User(tenant_id=tenant_id, email=email, full_name="System Administrator",
                short_name="Admin", created_by="seed", updated_by="seed")
    session.add(user)
    await session.flush()
    for role in ("Admin", "Management"):
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role,
                             granted_by="seed", created_by="seed", updated_by="seed"))
    await session.flush()
    return True


async def run() -> int:
    from evam_backend_core.rbac import policy_fingerprint
    from evam_backend_core.rbac_catalog import POLICY_VERSION
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, settings.default_tenant_code, "Evam Finance")
        cells = await seed_matrix(session, tenant_id)
        admin = await ensure_admin_user(session, tenant_id)
        await session.commit()
    log.info("access seed complete: policy=%s fp=%s matrix_cells=+%d admin_created=%s",
             POLICY_VERSION, policy_fingerprint()[:12], cells, admin)
    print(f"Access seed complete (policy {POLICY_VERSION}, fingerprint "
          f"{policy_fingerprint()[:12]}). Matrix cells added: {cells}, admin created: {admin}.")
    await dispose_engine()
    return 0


async def check() -> int:
    """Drift report ONLY — no writes. Exit 0 in sync, 3 on drift."""
    import json

    from app.matrix import drift_report
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await _tenant_id_readonly(session, settings.default_tenant_code)
        report = await drift_report(session, tenant_id)
        await session.rollback()      # belt-and-braces: --check never writes
    print(json.dumps(report, indent=2, default=str))
    await dispose_engine()
    return 0 if report["in_sync"] else 3


async def _tenant_id_readonly(session: AsyncSession, code: str):
    row = (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"tenant '{code}' does not exist — run the seed first")
    return row.id


if __name__ == "__main__":
    import sys
    mode = check if "--check" in sys.argv[1:] else run
    raise SystemExit(asyncio.run(mode()))
