"""CLI: ``python -m app.seed`` — the EXPLICIT, VERSIONED seed of the Access service.

Creates the default tenant, seeds MISSING baseline cells of the access matrix from the
approved compiled policy (``evam_backend_core.rbac``, provenance-tagged 'baseline' and
stamped with its policy version + fingerprint), and provisions the initial Admin user so
the RBAC chicken-and-egg is solved. Idempotent; NEVER overwrites a runtime override.

``python -m app.seed --check`` runs the DRIFT REPORT instead: compare the live matrix
against the approved baseline and print the differences — WITHOUT writing anything. Exit
code 0 = in sync, 3 = drift found (so a deployment pipeline can gate on it).

``python -m app.seed --if-empty`` is the FIRST-BOOT bootstrap: seed only when the
database holds no tenants at all (an empty Access DB is a bricked platform — nothing can
authenticate against nothing); any non-empty database is NEVER written — it gets the
drift report, exactly like --check.

Production posture runs with ``ACCESS_AUTO_SEED=false``: the container start performs the
--if-empty bootstrap (first boot fills the baseline; every later start is report-only),
and all subsequent grants happen through the governed Access APIs.
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


def _default_admins() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(email, full name, short name, roles) of every DEFAULT user: the system account
    (Admin + Management) plus the deployment's own operators
    (ACCESS_EXTRA_ADMIN_EMAILS — the Admin role). An entry names its display name
    after a colon ("tech@evamfinance.com:TechAdmin"); without one, the name derives
    from the mailbox."""
    settings = get_settings()
    out: list[tuple[str, str, str, tuple[str, ...]]] = [
        (f"admin@{settings.user_email_domain}", "System Administrator", "Admin",
         ("Admin", "Management"))]
    for entry in settings.extra_admin_emails:
        email, _, name = entry.partition(":")
        email = email.strip()
        name = name.strip() or email.split("@", 1)[0].replace(".", " ").title()
        out.append((email, name, name.split()[0], ("Admin",)))
    return out


async def ensure_admin_user(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Provision every default user that is missing — idempotent, and it NEVER touches
    a user that already exists (their name and grants stay whatever governance made
    them). Returns how many users were created."""
    created = 0
    for email, full_name, short_name, roles in _default_admins():
        existing = (
            await session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        user = User(tenant_id=tenant_id, email=email, full_name=full_name,
                    short_name=short_name, created_by="seed", updated_by="seed")
        session.add(user)
        await session.flush()
        for role in roles:
            session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role=role,
                                 granted_by="seed", created_by="seed", updated_by="seed"))
        created += 1
    await session.flush()
    return created


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
        admins = await ensure_admin_user(session, tenant_id)
        await session.commit()
    log.info("access seed complete: policy=%s fp=%s matrix_cells=+%d admins_created=%d",
             POLICY_VERSION, policy_fingerprint()[:12], cells, admins)
    print(f"Access seed complete (policy {POLICY_VERSION}, fingerprint "
          f"{policy_fingerprint()[:12]}). Matrix cells added: {cells}, "
          f"admin users created: {admins}.")
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


async def bootstrap_if_empty() -> int:
    """First-boot bootstrap. An EMPTY Access database (no tenants at all) is a bricked
    platform, not a hardened one — nothing can authenticate against nothing — so it is
    seeded with the baseline (tenant + matrix + admin) in ANY posture. A database with
    any tenant row is NEVER matrix-written on start: it gets the drift report, and every
    later grant goes through the governed APIs. The one additive exception is the
    DEFAULT ADMIN USER LIST (admin@<domain> + ACCESS_EXTRA_ADMIN_EMAILS): a user added
    to that deployment configuration is provisioned on the next start — idempotently,
    never modifying anyone who already exists — so the platform's own operators don't
    need a live Admin session to be born."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        empty = (await session.execute(select(Tenant.id).limit(1))).first() is None
        await session.rollback()
    if empty:
        await dispose_engine()
        print("Access database is EMPTY — first-boot bootstrap "
              "(tenant + baseline matrix + admin user).")
        return await run()
    async with sm() as session:
        row = (await session.execute(
            select(Tenant).where(Tenant.code == settings.default_tenant_code)
        )).scalar_one_or_none()
        if row is not None:
            admins = await ensure_admin_user(session, row.id)
            # The shipped visibility layer (matrix.VISIBILITY_READ) applies on every
            # start — the second additive exception beside the admin list. Without
            # this, the layer reached only fresh installs: a long-running production
            # database kept its SCOPED cells and every widened role kept seeing
            # nothing, exactly what the first deployment demonstrated.
            from app.matrix import apply_visibility

            vis = await apply_visibility(session, row.id)
            await session.commit()
            if admins:
                log.info("default admin users provisioned on start: +%d", admins)
                print(f"Default admin users provisioned: {admins}.")
            if vis:
                log.info("visibility layer applied on start: %d cell(s)", len(vis))
                print(f"Visibility layer applied: {len(vis)} cell(s) -> READ.")
    await dispose_engine()
    return await check()


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv[1:]:
        mode = check
    elif "--if-empty" in sys.argv[1:]:
        mode = bootstrap_if_empty
    else:
        mode = run
    raise SystemExit(asyncio.run(mode()))
