"""Carry the committee/sanction references on the authoritative decision record.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-29

The Deal-Structuring workflow must derive the committee outcome, approver, note AND the
committee-minute / sanction-letter references ONLY from the durable decision record (never from the
untrusted Temporal signal). This migration adds those two reference columns to
``workflow_decisions`` so the record is self-contained and ``verify_committee_decision`` can return
everything the evidence step needs.
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_decisions "
               "ADD COLUMN IF NOT EXISTS committee_reference varchar(500);")
    op.execute("ALTER TABLE workflow_decisions "
               "ADD COLUMN IF NOT EXISTS sanction_letter_reference varchar(500);")


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_decisions "
               "DROP COLUMN IF EXISTS committee_reference;")
    op.execute("ALTER TABLE workflow_decisions "
               "DROP COLUMN IF EXISTS sanction_letter_reference;")
