"""VOX structuring engine choice — the vendor-blind live A/B.

The recorder (or a reviewer re-analysing) picks the structuring engine per
take: "default" (the deployment's standard) or "regional" (the Indic-tuned
alternative). NULL keeps the box default, so nothing changes for anyone who
never touches the picker. The choice rides the row because every later
regenerate must honour it; the exact model that ran lives in the report
metadata.

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        ADD COLUMN IF NOT EXISTS engine varchar(20)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE vox_conversations DROP COLUMN IF EXISTS engine")
