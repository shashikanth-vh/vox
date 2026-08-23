"""VOX proposed lead — the new-lead intent lives on the conversation until approval.

Field feedback: "create new lead" wrote a Lead into the register the moment the
button was tapped — before the report was even reviewed. A discarded or corrected
conversation left a stray lead behind. Now the atlas screen only RECORDS the
intent (company name + chosen RM) on the conversation row, and the approve step
materialises the lead — so the register gains a row exactly when the firm has
signed off on the conversation that justifies it, and never before.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        ADD COLUMN IF NOT EXISTS proposed_lead_company varchar(300),
        ADD COLUMN IF NOT EXISTS proposed_lead_rm varchar(120)
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE vox_conversations
        DROP COLUMN IF EXISTS proposed_lead_company,
        DROP COLUMN IF EXISTS proposed_lead_rm
    """)
