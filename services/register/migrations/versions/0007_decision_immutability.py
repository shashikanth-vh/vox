"""Make workflow_decisions immutable at the database level.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28

A recorded decision is meant to be write-once: the single-winner row is the authority for a
conversion's outcome, approver and note, so nothing — not even the application role — should
be able to UPDATE or DELETE it after the fact. 0006 created the table but ``register_app``
inherited full DML (via 0005's ALTER DEFAULT PRIVILEGES). This migration:

1. REVOKEs UPDATE and DELETE on the table from ``register_app`` (best-effort), leaving only
   SELECT + INSERT; and
2. installs a trigger that RAISES on any UPDATE or DELETE — enforcing immutability even for
   the table owner / a superuser, independent of grants.

A brand-new attempt is a NEW row under a fresh (retry ``-r2``) workflow id, so immutability
never blocks a legitimate retry.
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Least privilege: the app role may read and append, never mutate or remove.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'REVOKE UPDATE, DELETE ON workflow_decisions FROM register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'REVOKE on workflow_decisions skipped (insufficient privilege).';
        END
        $$;
        """
    )
    # 2. Hard immutability: block UPDATE/DELETE at the row level, for everyone.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION workflow_decisions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'workflow_decisions is append-only; % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workflow_decisions_immutable
        BEFORE UPDATE OR DELETE ON workflow_decisions
        FOR EACH ROW EXECUTE FUNCTION workflow_decisions_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_workflow_decisions_immutable ON workflow_decisions;")
    op.execute("DROP FUNCTION IF EXISTS workflow_decisions_immutable();")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT UPDATE, DELETE ON workflow_decisions TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'GRANT on workflow_decisions skipped (insufficient privilege).';
        END
        $$;
        """
    )
