"""Company identity travels with the lead; Tracxn answers get a larder.

Leads gain the optional identity fields the desk collects piecemeal (CIN,
city, address, state, country) — filled as and when known, and copied onto a
client master the lead BIRTHS (an existing master is never overwritten). The
entity master gains the city / country / address it lacked. And tracxn_cache
holds the market feed's answers per (tenant, CIN, endpoint), because Tracxn
bills per call and a company's filed financials do not change by the hour.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE leads
            ADD COLUMN IF NOT EXISTS cin VARCHAR(40),
            ADD COLUMN IF NOT EXISTS city VARCHAR(120),
            ADD COLUMN IF NOT EXISTS address TEXT,
            ADD COLUMN IF NOT EXISTS state VARCHAR(60),
            ADD COLUMN IF NOT EXISTS country VARCHAR(60)
    """)
    op.execute("""
        ALTER TABLE entities
            ADD COLUMN IF NOT EXISTS city VARCHAR(120),
            ADD COLUMN IF NOT EXISTS country VARCHAR(60),
            ADD COLUMN IF NOT EXISTS address TEXT
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS tracxn_cache (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            cin VARCHAR(40) NOT NULL,
            endpoint VARCHAR(80) NOT NULL,
            payload JSONB,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT tracxn_cache_key UNIQUE (tenant_id, cin, endpoint)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tracxn_cache_tenant_cin "
        "ON tracxn_cache (tenant_id, cin)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tracxn_cache")
    op.execute("ALTER TABLE entities DROP COLUMN IF EXISTS city, "
               "DROP COLUMN IF EXISTS country, DROP COLUMN IF EXISTS address")
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS cin, "
               "DROP COLUMN IF EXISTS city, DROP COLUMN IF EXISTS address, "
               "DROP COLUMN IF EXISTS state, DROP COLUMN IF EXISTS country")
