"""People — role holds every role the person has, comma-joined ("Deal Analyst,
Management, Syn RM"), which the 30-character column refused once a person held
three. Widen it to 120.

Revision ID: 0029
Revises: 0028
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE people ALTER COLUMN role TYPE varchar(120)")


def downgrade() -> None:
    # Values longer than 30 would be cut; leave the column wide.
    pass
