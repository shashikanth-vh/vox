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

# Every tenant table + the audit log — migration 0005's set plus every tenant table a
# later migration added (0011+ create their policies in-migration; the FORCE posture is
# converged here so a REGISTER_ENFORCE_RLS flip takes effect on the next deploy).
TENANT_TABLES = [
    "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
    "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
    "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
    "documents", "document_checklist", "line_assignments", "change_requests",
    "tenant_settings", "idempotency_keys", "audit_log", "workflow_decisions",
    "workflow_decision_outbox", "import_reconciliation_items",
    "governance_evidence", "governance_evidence_status", "advaya_handoffs",
    "cp_cs_checklists",
    "advaya_handover_packages", "disbursement_tranches",
    "calendar_events", "notifications", "notification_deliveries",
    "covenants", "ews_cases",
]

# Append-only tables: rows are write-once, so the runtime role must NOT hold UPDATE/DELETE
# (a DB trigger also blocks mutation — see migration 0007). The generic ALL-TABLES grant
# re-adds those privileges, so they are revoked again after every provisioning run.
APPEND_ONLY_TABLES = ["workflow_decisions"]


async def apply() -> None:
    settings = get_settings()
    init_engine(settings)
    engine = get_engine()
    enforce = settings.enforce_rls
    app_password = os.getenv("REGISTER_APP_PASSWORD", "").strip()

    # PROVISIONING IS FAIL-CLOSED WHEN INTENDED. If REGISTER_APP_PASSWORD is set, the deploy
    # INTENDS the register_app login to exist and be usable — any failure to create the
    # role, grant it, or set its login MUST fail the migrate Job (so a broken runtime login
    # is never masked by a "successful" migration). Only the pure best-effort case (no
    # password → operator will provision the login later) tolerates insufficient privilege.
    async with engine.begin() as conn:
        role_sql = [
            ("create", "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE "
                       "rolname = 'register_app') THEN CREATE ROLE register_app NOLOGIN; "
                       "END IF; END $$;"),
            ("usage", "GRANT USAGE ON SCHEMA public TO register_app"),
            ("dml", "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    "IN SCHEMA public TO register_app"),
            ("seq", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO register_app"),
            ("defdml", "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                       "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO register_app"),
            ("defseq", "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                       "GRANT USAGE, SELECT ON SEQUENCES TO register_app"),
        ]
        if app_password:
            # Fail-closed: run each statement plainly; a privilege error propagates and
            # fails the Job. Then set the LOGIN + password so the runtime role is usable.
            for _label, stmt in role_sql:
                await conn.execute(text(stmt))
            # Converge the runtime role to a SAFE shape — never assume a pre-existing
            # register_app is harmless (a SUPERUSER or BYPASSRLS role silently defeats RLS).
            # Changing these attributes requires superuser; when the migrate owner is a
            # non-superuser CREATEROLE role it can't, but a role WE created is already
            # NOSUPERUSER NOBYPASSRLS by default — so this is best-effort (a failure to
            # harden a pre-existing mis-created role is logged, not fatal).
            try:
                async with conn.begin_nested():  # savepoint: a failure here isn't fatal
                    await conn.exec_driver_sql(
                        "ALTER ROLE register_app NOSUPERUSER NOBYPASSRLS")
            except Exception as exc:  # noqa: BLE001 - needs superuser; default shape is safe
                log.warning("rls_role_harden_skipped", extra={"error": str(exc)})
            # FAIL CLOSED: whether or not the harden ran, VERIFY the runtime role is safe.
            # A SUPERUSER or BYPASSRLS register_app silently defeats row-level security, so a
            # role we could not fix (pre-existing + insufficient privilege) FAILS the Job
            # rather than deploying an RLS bypass.
            attrs = (await conn.exec_driver_sql(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = 'register_app'")).first()
            if attrs is None or attrs[0] or attrs[1]:
                raise RuntimeError(
                    "register_app must be NOSUPERUSER NOBYPASSRLS for RLS to bind "
                    f"(got rolsuper={attrs and attrs[0]}, rolbypassrls={attrs and attrs[1]}). "
                    "Refusing to deploy an RLS bypass.")
            # PostgreSQL utility DDL (ALTER ROLE … PASSWORD) does NOT accept a bind
            # parameter for the password literal, so we escape it and inline it via the raw
            # driver (exec_driver_sql — no SQLAlchemy ':' param parsing, which would also
            # choke on a colon in the secret). Doubling single quotes is sufficient under
            # standard_conforming_strings (the modern default).
            escaped = app_password.replace("'", "''")
            await conn.exec_driver_sql(
                f"ALTER ROLE register_app WITH LOGIN PASSWORD '{escaped}'")
            log.info("rls_register_app_login_provisioned")
        else:
            # Best-effort: no password given (dev / operator provisions the login later).
            # Swallow a privilege error so a non-CREATEROLE dev DB still migrates.
            try:
                await conn.execute(text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app')
                        THEN CREATE ROLE register_app NOLOGIN; END IF;
                        EXECUTE 'ALTER ROLE register_app NOSUPERUSER NOBYPASSRLS';
                        EXECUTE 'GRANT USAGE ON SCHEMA public TO register_app';
                        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                                'IN SCHEMA public TO register_app';
                        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES '
                                'IN SCHEMA public TO register_app';
                        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO register_app';
                        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                                'GRANT USAGE, SELECT ON SEQUENCES TO register_app';
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE NOTICE 'register_app role/grants skipped (insufficient privilege).';
                    END $$;
                """))
            except Exception as exc:  # noqa: BLE001 - dev best-effort only
                log.warning("rls_role_setup_skipped", extra={"error": str(exc)})

        # 2. converge FORCE to match the flag, every run.
        verb = "FORCE" if enforce else "NO FORCE"
        for tbl in TENANT_TABLES:
            await conn.execute(text(f"ALTER TABLE {tbl} {verb} ROW LEVEL SECURITY"))
        log.info("rls_force_converged", extra={"enforce_rls": enforce})

        # 3. least-privilege exceptions. The generic "GRANT … ON ALL TABLES" above re-grants
        # UPDATE/DELETE on the APPEND-ONLY tables (whose rows must never change), so REVOKE
        # them again here — every run, so the claim stays true after a re-provision. (The
        # 0007 DB trigger is the hard stop; this keeps the grant matrix honest too.)
        #
        # FAIL CLOSED when the deploy INTENDS a runtime login (REGISTER_APP_PASSWORD set): the
        # REVOKE must succeed and be VERIFIED, or the Job fails — a false least-privilege claim
        # is not shipped. Without the password (dev) it stays best-effort (role may not exist).
        for tbl in APPEND_ONLY_TABLES:
            if app_password:
                await conn.execute(text(f"REVOKE UPDATE, DELETE ON {tbl} FROM register_app"))
            else:
                try:
                    async with conn.begin_nested():   # savepoint: role may not exist (dev)
                        await conn.execute(text(
                            f"REVOKE UPDATE, DELETE ON {tbl} FROM register_app"))
                except Exception as exc:  # noqa: BLE001 - best-effort; trigger still enforces
                    log.warning("rls_revoke_append_only_skipped",
                                extra={"table": tbl, "error": str(exc)})
        if app_password:
            # VERIFY: register_app must hold NEITHER UPDATE nor DELETE on any append-only table.
            for tbl in APPEND_ONLY_TABLES:
                bad = (await conn.execute(text(
                    "SELECT has_table_privilege('register_app', :t, 'UPDATE') OR "
                    "has_table_privilege('register_app', :t, 'DELETE')"),
                    {"t": tbl})).scalar()
                if bad:
                    raise RuntimeError(
                        f"register_app still holds UPDATE/DELETE on append-only '{tbl}' after "
                        "revoke. Refusing to deploy a false least-privilege posture.")
        log.info("rls_append_only_least_privilege_applied",
                 extra={"tables": APPEND_ONLY_TABLES, "verified": bool(app_password)})


def main() -> None:  # pragma: no cover - entrypoint
    asyncio.run(apply())


if __name__ == "__main__":  # pragma: no cover
    main()
