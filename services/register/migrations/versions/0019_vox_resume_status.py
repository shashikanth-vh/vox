"""VOX re-analysis of approved records — the round-trip home.

An approved conversation whose transcript the desk corrects must rebuild its
report and COME BACK approved: ``resume_status`` remembers where the row
belongs while it passes through the pipeline again, and the pipeline write
returns it there when the fresh report lands.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        ADD COLUMN IF NOT EXISTS resume_status varchar(32)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE vox_conversations DROP COLUMN IF EXISTS resume_status")
