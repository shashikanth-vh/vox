"""Revocation epoch, grant provenance and the authorization audit trail.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

Release-one authority model: users carry a REVOCATION EPOCH (bumped on any role change /
(de)activation, compared by sensitive-operation revalidation); every matrix cell carries
its PROVENANCE ('baseline' from the approved compiled policy vs 'override' edited at
runtime); and every governance change lands in an append-only access_audit event stamped
with the policy version.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN permissions_epoch bigint NOT NULL DEFAULT 0;")
    op.execute(
        "ALTER TABLE access_grants ADD COLUMN origin varchar(20) NOT NULL DEFAULT 'baseline';")
    op.execute(
        """
        CREATE TABLE access_audit (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      uuid         NOT NULL,
            actor          varchar(200) NOT NULL,
            action         varchar(60)  NOT NULL,
            item           varchar(200),
            detail         jsonb,
            policy_version varchar(20),
            created_at     timestamptz  NOT NULL DEFAULT now(),
            updated_at     timestamptz  NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120)
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_access_audit_tenant_time ON access_audit (tenant_id, created_at);")
    # Append-only: governance history must be tamper-evident even to the app role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION access_audit_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'access_audit is append-only';
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER access_audit_no_update BEFORE UPDATE OR DELETE ON access_audit
        FOR EACH ROW EXECUTE FUNCTION access_audit_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS access_audit_no_update ON access_audit;")
    op.execute("DROP FUNCTION IF EXISTS access_audit_immutable();")
    op.execute("DROP TABLE IF EXISTS access_audit;")
    op.execute("ALTER TABLE access_grants DROP COLUMN IF EXISTS origin;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS permissions_epoch;")
