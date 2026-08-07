"""Increment ⑤ (Platform Deals matrix) — the lender allocation amount.

A bank's SANCTION is an amount, not just a colour: ₹20 Cr asked, five banks
identified at ₹4 Cr each — the fee book, the allocation summary and the drawer's
"approved ₹16 Cr of ₹20 Cr" arithmetic all hang off the per-lender figure, so it
lives on the lender row itself.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE syndication_lenders "
               "ADD COLUMN IF NOT EXISTS amount_cr numeric(14, 2);")


def downgrade() -> None:
    op.execute("ALTER TABLE syndication_lenders DROP COLUMN IF EXISTS amount_cr;")
