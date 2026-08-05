"""Covenants may defer their schedule to disbursement.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05

``covenants.first_due_on`` becomes nullable: a covenant entered at sanction time often
cannot know its first due date — the reporting obligation starts with the money. A NULL
first due is stamped automatically one cycle after the FIRST confirmed disbursement
tranche (the same event that opens the loan account); until then the covenant simply
does not remind.
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE covenants ALTER COLUMN first_due_on DROP NOT NULL;")


def downgrade() -> None:
    # Backfill any NULLs before restoring the constraint (today keeps the record valid).
    op.execute("UPDATE covenants SET first_due_on = now()::date WHERE first_due_on IS NULL;")
    op.execute("ALTER TABLE covenants ALTER COLUMN first_due_on SET NOT NULL;")
