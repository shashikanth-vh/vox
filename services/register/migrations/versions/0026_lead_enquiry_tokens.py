"""Lead enquiry tokens — the Approve / Reject links PRISM mints for a parked
website enquiry. Only the token's SHA-256 is stored; a token is bound to one
enquiry, one action and the address it was issued to, expires, and is spent by
the first decision.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS lead_enquiry_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            enquiry_id UUID NOT NULL REFERENCES lead_enquiries(id) ON DELETE CASCADE,
            kind VARCHAR(10) NOT NULL,
            recipient VARCHAR(200),
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_lead_enquiry_tokens_enquiry "
               "ON lead_enquiry_tokens (tenant_id, enquiry_id)")
    op.execute("ALTER TABLE lead_enquiry_tokens ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE lead_enquiry_tokens FORCE ROW LEVEL SECURITY;")
    # The tenant setting reads as '' (not NULL) on a connection that once held a
    # transaction-local value, so the policy must treat '' as "no tenant bound" —
    # exactly as 0002/0004/0009/0011 do. 0025's policy did not; repair it here.
    for table in ("lead_enquiry_tokens", "lead_enquiries"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                NULLIF(current_setting('app.current_tenant', true), '') IS NULL
                OR tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            WITH CHECK (
                NULLIF(current_setting('app.current_tenant', true), '') IS NULL
                OR tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lead_enquiry_tokens")
