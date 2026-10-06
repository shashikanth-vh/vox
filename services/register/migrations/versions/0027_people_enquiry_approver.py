"""People — "enquiry approver": who gets the website / WhatsApp enquiry
Approve / Reject e-mail when the sender names nobody. Ticked in the Employees
master; empty = INTAKE_APPROVERS, else every BD Head.

Revision ID: 0027
Revises: 0026
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE people ADD COLUMN IF NOT EXISTS enquiry_approver boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE people DROP COLUMN IF EXISTS enquiry_approver")
