"""Lead enquiries — the chase: when the reminder went out and when the expired
enquiry was escalated to the BD Head with fresh links.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE lead_enquiries ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ")
    op.execute("ALTER TABLE lead_enquiries ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE lead_enquiries DROP COLUMN IF EXISTS reminded_at")
    op.execute("ALTER TABLE lead_enquiries DROP COLUMN IF EXISTS escalated_at")
