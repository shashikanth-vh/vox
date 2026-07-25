"""CLI: ``python -m app.seed`` — provision a usable Access service.

Creates the default tenant, seeds the access matrix from the spec artifact
(``evam_backend_core.rbac``), and provisions the initial Admin user so the RBAC
chicken-and-egg is solved (user management is Admin-only, so someone must exist).
Idempotent — safe to run repeatedly.
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


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, settings.default_tenant_code, "Evam Finance")
        cells = await seed_matrix(session, tenant_id)
        admin = await ensure_admin_user(session, tenant_id)
        await session.commit()
    log.info("access seed complete: matrix_cells=+%d admin_created=%s", cells, admin)
    print(f"Access seed complete. Matrix cells added: {cells}, admin created: {admin}.")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
