"""Row-level security becomes a real, fail-CLOSED boundary.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28

The 0001 policies were fail-OPEN in two ways the security audit flagged:

* they permitted access when ``app.current_tenant`` was unset
  (``current_setting(...) IS NULL OR tenant_id = ...``) — a forgotten GUC leaked the
  whole table instead of denying; and
* only ``ENABLE ROW LEVEL SECURITY`` was applied, which the **table owner bypasses** —
  and the application connected as the owner, so RLS never actually constrained it.

This migration:

1. Recreates every tenant table's policy **fail-closed** — no NULL escape, so a missing
   tenant context denies all rows (``tenant_id = current_setting(...)::uuid`` alone;
   ``current_setting(name, true)`` yields NULL when unset → the comparison is NULL →
   the row is filtered out).
2. Extends coverage to the tenant-bearing tables 0001 missed: ``documents``,
   ``document_checklist``, ``line_assignments``, ``change_requests``,
   ``tenant_settings``, ``idempotency_keys`` (and a NULL-tolerant policy for the
   append-only ``audit_log`` whose tenant_id is nullable for system events).
3. Creates a **non-owner application role** (``register_app``) with only DML — RLS is
   always enforced for a non-owner, so the app should connect as this role in
   production (set REGISTER_DB_USER=register_app after giving it a password).
4. When ``REGISTER_ENFORCE_RLS`` is truthy at migration time, additionally applies
   ``FORCE ROW LEVEL SECURITY`` so RLS binds even a superuser/owner connection.

Dev/tests keep connecting as the owner without FORCE, so the owner still bypasses RLS
locally and the suite is unaffected; production flips REGISTER_ENFORCE_RLS + connects as
``register_app`` to get a hard database boundary.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Every table that carries tenant_id and holds business/enforcement data.
_TENANT_TABLES = [
    "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
    "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
    "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
    "documents", "document_checklist", "line_assignments", "change_requests",
    "tenant_settings", "idempotency_keys",
]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    force = _truthy(os.getenv("REGISTER_ENFORCE_RLS"))

    for tbl in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        # Drop any prior policy (0001 created *_tenant_isolation on a subset).
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl};")
        # Fail-CLOSED: no NULL escape. Unset GUC → current_setting(...) is NULL →
        # tenant_id = NULL is NULL (never true) → zero rows / rejected writes.
        op.execute(f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """)
        if force:
            op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")

    # audit_log is append-only and its tenant_id is nullable (system-level events). Isolate
    # tenant-scoped rows but still allow the NULL-tenant system rows through.
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;")
    op.execute("""
        CREATE POLICY audit_log_tenant_isolation ON audit_log
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.current_tenant', true)::uuid
        )
        WITH CHECK (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.current_tenant', true)::uuid
        );
    """)
    if force:
        op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;")

    # A non-owner application role: RLS is always enforced for it (no owner bypass).
    # Created NOLOGIN — operators grant it a password + LOGIN and point
    # REGISTER_DB_USER at it. Best-effort + idempotent: if the migration role lacks
    # CREATEROLE (managed Postgres, CI), the whole block is skipped with a NOTICE rather
    # than failing the migration — operators then create register_app by hand per the
    # deploy docs. New tables/sequences inherit the grants via ALTER DEFAULT PRIVILEGES.
    op.execute("""
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
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'register_app role/grants skipped (insufficient privilege).';
        END
        $$;
    """)


def downgrade() -> None:
    # Restore the 0001 fail-open policies on the original subset; drop the extras.
    original = {
        "entities", "people", "counterparties", "deals", "leads", "lending_tracker",
        "syndication_tracker", "syndication_lenders", "asset_monetisation", "financials",
        "contracts_assets", "interactions", "external_intelligence", "monitoring_reporting",
    }
    op.execute("DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY;")
    for tbl in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl};")
        if tbl in original:
            op.execute(f"""
                CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                USING (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                )
                WITH CHECK (
                    current_setting('app.current_tenant', true) IS NULL
                    OR tenant_id = current_setting('app.current_tenant', true)::uuid
                );
            """)
        else:
            op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY;")
