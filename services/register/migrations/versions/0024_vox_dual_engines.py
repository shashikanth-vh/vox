"""VOX: both structuring engines run on every take; the reviewer approves one.

The take keeps TWO reports — the primary engine's in structured_report (as
before) and the other engine's in structured_report_alt — plus which one the
reviewer chose (chosen_engine) and which one was finally approved
(approved_engine). approved_engine over time is the scoreboard that decides
which model the firm keeps paying for. alt_error records a second engine
that failed, so the screen can say why only one reading exists.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        ADD COLUMN IF NOT EXISTS structured_report_alt jsonb,
        ADD COLUMN IF NOT EXISTS engine_alt varchar(20),
        ADD COLUMN IF NOT EXISTS alt_error text,
        ADD COLUMN IF NOT EXISTS chosen_engine varchar(20),
        ADD COLUMN IF NOT EXISTS approved_engine varchar(20)
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS ix_vox_conversations_approved_engine
        ON vox_conversations (tenant_id, approved_engine)
        WHERE approved_engine IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_vox_conversations_approved_engine")
    op.execute("""
    ALTER TABLE vox_conversations
        DROP COLUMN IF EXISTS structured_report_alt,
        DROP COLUMN IF EXISTS engine_alt,
        DROP COLUMN IF EXISTS alt_error,
        DROP COLUMN IF EXISTS chosen_engine,
        DROP COLUMN IF EXISTS approved_engine
    """)
