"""Number series — instrument numbers come from a register, not a keyboard.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-06

``number_series`` backs the auto-numbered references (first user: the credit note sent
to committee, ``CN/<company>/<yyyymm>-<seq>``). One row per series; the mint endpoint
advances ``last_value`` with an atomic upsert, so concurrent sends can never draw the
same number. The (tenant_id, series_key) UNIQUE constraint is the upsert's arbiter —
a real constraint, not a partial index, because series rows are never soft-deleted.
Same trailing columns, updated_at trigger and fail-closed tenant RLS as the LMS tables.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE number_series (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            series_key varchar(200) NOT NULL,
            last_value integer      NOT NULL DEFAULT 0,
            tenant_id  uuid         NOT NULL,
            version    integer      NOT NULL DEFAULT 1,
            created_at timestamptz  NOT NULL DEFAULT now(),
            updated_at timestamptz  NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120),
            deleted_at timestamptz,
            CONSTRAINT number_series_tenant_key UNIQUE (tenant_id, series_key)
        );
    """)
    op.execute("""
        CREATE TRIGGER trg_number_series_updated_at
        BEFORE UPDATE ON number_series
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)
    force = (os.getenv("REGISTER_ENFORCE_RLS") or "").strip().lower() in {
        "1", "true", "yes", "on"}
    op.execute("ALTER TABLE number_series ENABLE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY number_series_tenant_isolation ON number_series
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
    """)
    if force:
        op.execute("ALTER TABLE number_series FORCE ROW LEVEL SECURITY;")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN "
        "EXECUTE 'GRANT SELECT, INSERT, UPDATE ON number_series TO register_app'; "
        "END IF; "
        "EXCEPTION WHEN insufficient_privilege THEN "
        "RAISE NOTICE 'number_series grant to register_app skipped.'; "
        "END $$;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS number_series CASCADE;")
