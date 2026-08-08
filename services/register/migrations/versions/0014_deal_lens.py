"""Deals carry the climate lens (Mitigation / Adaptation) at deal level.

The Deals grid has always shown a Lens column, but only LEADS stored the value — a
converted or imported deal showed it blank. The lens is the company's climate
orientation; it belongs on the deal row the desk actually works.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("lens", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "lens")
