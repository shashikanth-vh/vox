"""LMS owns its conditions: the checklist HANDS OVER at account opening.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-05

``loan_account_conditions`` — the servicing side's OWN register of a loan's CP/CS
conditions. At the booking approval that OPENS the account, the register copies the
line's final checklist — completed and uncompleted items alike — into these rows, and
ownership transfers: the LOS checklist freezes into a decision record (its cs-progress
lane refuses transferred lines), while the live chase — receipts, expiry, reminders —
runs here, with no dependency on LOS. Same trailing columns, updated_at trigger and
fail-closed tenant RLS as the other LMS tables (0004).
"""
from __future__ import annotations

import os

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE loan_account_conditions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            lending_id     varchar(64)  NOT NULL,
            account_id     uuid         NOT NULL REFERENCES loan_accounts(id)
                                        ON DELETE CASCADE,
            key            varchar(80)  NOT NULL,
            label          varchar(1000) NOT NULL,
            condition_type varchar(8)   NOT NULL DEFAULT 'CS',
            required       boolean      NOT NULL DEFAULT true,
            status         varchar(20)  NOT NULL DEFAULT 'Pending',
            reason         text,
            expiry_date    date,
            evidence_ref   varchar(300),
            source_version integer,
            completed_on   date,
            completed_by   varchar(120),
            note           text,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz
        );
    """)
    op.execute("""
        CREATE TRIGGER trg_loan_account_conditions_updated_at
        BEFORE UPDATE ON loan_account_conditions
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)
    op.execute("""
        CREATE UNIQUE INDEX loan_account_conditions_tenant_key
        ON loan_account_conditions (tenant_id, lending_id, key)
        WHERE deleted_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX ix_loan_account_conditions_lending
        ON loan_account_conditions (tenant_id, lending_id);
    """)
    force = (os.getenv("REGISTER_ENFORCE_RLS") or "").strip().lower() in {
        "1", "true", "yes", "on"}
    op.execute("ALTER TABLE loan_account_conditions ENABLE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY loan_account_conditions_tenant_isolation ON loan_account_conditions
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
    """)
    if force:
        op.execute("ALTER TABLE loan_account_conditions FORCE ROW LEVEL SECURITY;")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN "
        "EXECUTE 'GRANT SELECT, INSERT, UPDATE ON loan_account_conditions TO register_app'; "
        "END IF; "
        "EXCEPTION WHEN insufficient_privilege THEN "
        "RAISE NOTICE 'loan_account_conditions grant to register_app skipped.'; "
        "END $$;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS loan_account_conditions CASCADE;")
