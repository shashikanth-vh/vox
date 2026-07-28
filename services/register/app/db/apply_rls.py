"""Converge the RLS posture on every deploy — run by the migrate step as the DB OWNER.

Alembic migrations run once; flipping ``REGISTER_ENFORCE_RLS`` later would never re-run
migration 0005. This module is run by the entrypoint AFTER ``alembic upgrade head`` and is
idempotent, so the database always ends in the desired state:

* **register_app** — the non-owner runtime role. Created if missing; when
  ``REGISTER_APP_PASSWORD`` is set it is given ``LOGIN`` + that password (self-bootstrapping,
  no manual step), plus the DML grants. Without the env it stays ``NOLOGIN`` (operator sets
  the login later).
* **FORCE ROW LEVEL SECURITY** — applied to every tenant table when
  ``REGISTER_ENFORCE_RLS`` is truthy, removed (``NO FORCE``) when it is not — every run, so a
  flag change takes effect on the next deploy.

Run:  ``python -m app.db.apply_rls``  (the entrypoint's ``migrate`` step does this).
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_engine, init_engine

log = get_logger(__name__)

# Every tenant table + the audit log — must match migration 0005.
TENANT_TABLES = [
    "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
    "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
    "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
    "documents", "document_checklist", "line_assignments", "change_requests",
    "tenant_settings", "idempotency_keys", "audit_log",
]


async def apply() -> None:
    settings = get_settings()
    init_engine(settings)
    engine = get_engine()
    enforce = settings.enforce_rls
    app_password = os.getenv("REGISTER_APP_PASSWORD", "").strip()

    async with engine.begin() as conn:
        # 1. the runtime role — best-effort (needs CREATEROLE; skip with a notice if not).
        try:
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                        CREATE ROLE register_app NOLOGIN;
                    END IF;
                    EXECUTE 'GRANT USAGE ON SCHEMA public TO register_app';
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                            'IN SCHEMA public TO register_app';
                    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO register_app';
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO register_app';
                    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                            'GRANT USAGE, SELECT ON SEQUENCES TO register_app';
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'register_app role/grants skipped (insufficient privilege).';
                END $$;
            """))
            if app_password:
                await conn.execute(
                    text("ALTER ROLE register_app WITH LOGIN PASSWORD :pw"),
                    {"pw": app_password})
                log.info("rls_register_app_login_set")
        except Exception as exc:  # noqa: BLE001 - never fail the deploy on role setup
            log.warning("rls_role_setup_skipped", extra={"error": str(exc)})

        # 2. converge FORCE to match the flag, every run.
        verb = "FORCE" if enforce else "NO FORCE"
        for tbl in TENANT_TABLES:
            await conn.execute(text(f"ALTER TABLE {tbl} {verb} ROW LEVEL SECURITY"))
        log.info("rls_force_converged", extra={"enforce_rls": enforce})


def main() -> None:  # pragma: no cover - entrypoint
    asyncio.run(apply())


if __name__ == "__main__":  # pragma: no cover
    main()
