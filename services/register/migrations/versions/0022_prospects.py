"""The prospect universe — curated market lists as a first-class master.

One table. Identity is CIN-anchored (live-unique per tenant where present);
the JSONB tag lists carry which curated verticals a company appears on and
the lists' own sub-sector labels, GIN-indexed for the grid's chip filters.
lead_ids / entity_id record every promotion into the pipeline, so "where did
this client come from?" stays answerable.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS prospects (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        version integer NOT NULL DEFAULT 1,
        prospect_no varchar(20),
        name varchar(300) NOT NULL,
        name_key varchar(300),
        domain varchar(200),
        cin varchar(40),
        overview text,
        verticals jsonb,
        sub_sectors jsonb,
        emails jsonb,
        phones jsonb,
        founded_year integer,
        state varchar(60),
        city varchar(120),
        country varchar(60),
        revenue_cr numeric(14,4),
        net_profit_cr numeric(14,4),
        ebitda_cr numeric(14,4),
        total_funding_cr numeric(14,4),
        latest_funding_cr numeric(14,4),
        latest_valuation_cr numeric(14,4),
        latest_funded_on date,
        status varchar(20) NOT NULL DEFAULT 'uncontacted',
        remarks text,
        source varchar(300),
        import_batch varchar(64),
        lead_ids jsonb,
        entity_id uuid,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        created_by varchar(120),
        updated_by varchar(120),
        deleted_at timestamptz
    );
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS prospects_tenant_no
        ON prospects (tenant_id, prospect_no) WHERE deleted_at IS NULL;
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS prospects_tenant_cin
        ON prospects (tenant_id, cin)
        WHERE deleted_at IS NULL AND cin IS NOT NULL;
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_name ON prospects (name);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_tenant_name_key
        ON prospects (tenant_id, name_key);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_tenant_status
        ON prospects (tenant_id, status);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_entity_id ON prospects (entity_id);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_verticals
        ON prospects USING gin (verticals);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_prospects_sub_sectors
        ON prospects USING gin (sub_sectors);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prospects;")
