"""Lead enquiries — what the website form and the WhatsApp bot sent, and what
the register made of each (the lead it became, or the deal / lead it was
logged on, or rejected). One row per enquiry number: redelivery is idempotent.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS lead_enquiries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            enquiry_no VARCHAR(40) NOT NULL,
            channel VARCHAR(20) NOT NULL DEFAULT 'website',
            status VARCHAR(20) NOT NULL,
            intent VARCHAR(20),
            company_name VARCHAR(300),
            contact_name VARCHAR(200),
            approved_by VARCHAR(200),
            approved_at TIMESTAMPTZ,
            submitted_at TIMESTAMPTZ,
            payload JSONB,
            outcome VARCHAR(30) NOT NULL,
            lead_id UUID,
            deal_id UUID,
            interaction_id UUID,
            rm VARCHAR(120),
            note TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT lead_enquiries_tenant_no UNIQUE (tenant_id, enquiry_no)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_lead_enquiries_tenant_received "
               "ON lead_enquiries (tenant_id, received_at)")
    # Tenant isolation, the register's standing posture for every table.
    op.execute("ALTER TABLE lead_enquiries ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE lead_enquiries FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY lead_enquiries_tenant_isolation ON lead_enquiries
        USING (
            current_setting('app.current_tenant', true) IS NULL
            OR tenant_id = current_setting('app.current_tenant', true)::uuid
        )
        WITH CHECK (
            current_setting('app.current_tenant', true) IS NULL
            OR tenant_id = current_setting('app.current_tenant', true)::uuid
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lead_enquiries")
