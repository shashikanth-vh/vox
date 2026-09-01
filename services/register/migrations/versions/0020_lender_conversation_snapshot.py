"""The chase list shows the words, not just the clocks.

``chased_date`` and ``response_date`` told the desk WHEN each side last spoke;
the text itself lived only in the interactions timeline, and the single ``note``
column was overwritten by whichever write came last — a chase, a reply, or a
hand-typed remark, indistinguishable on the row. Two dedicated columns carry the
last chase note and the last lender reply alongside their dates, and ``note``
goes back to being the remark it always claimed to be.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("syndication_lenders", sa.Column("last_chase_note", sa.Text(), nullable=True))
    op.add_column("syndication_lenders", sa.Column("last_reply_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("syndication_lenders", "last_reply_note")
    op.drop_column("syndication_lenders", "last_chase_note")
