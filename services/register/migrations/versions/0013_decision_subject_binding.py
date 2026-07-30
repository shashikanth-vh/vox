"""Bind workflow decisions to a subject + outcome, and make governance evidence reference them.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-29

Round K recorded governance provenance (workflow_id / run_id / decision_ref) but only checked the
strings were non-empty — so a committee/sanction evidence row could cite invented provenance. This
migration makes provenance VERIFIABLE:

* ``workflow_decisions`` gains ``subject_type`` / ``subject_id`` / ``run_id`` so a decision (already
  single-winner and server-provenanced) can be a Credit Committee decision bound to a specific Deal
  and run — not only a lead conversion.
* A partial UNIQUE index on ``governance_evidence (tenant_id, decision_ref, evidence_kind)`` (where
  ``decision_ref`` is set) enforces the authoritative one-to-one relationship: a single decision can
  back at most one committee-approval and one sanction-letter evidence row — no duplicate manufacture.
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_decisions "
               "ADD COLUMN IF NOT EXISTS subject_type varchar(40);")
    op.execute("ALTER TABLE workflow_decisions "
               "ADD COLUMN IF NOT EXISTS subject_id varchar(64);")
    op.execute("ALTER TABLE workflow_decisions "
               "ADD COLUMN IF NOT EXISTS run_id varchar(200);")
    # One authoritative decision backs at most one evidence row of each kind.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_governance_evidence_decision "
        "ON governance_evidence (tenant_id, decision_ref, evidence_kind) "
        "WHERE decision_ref IS NOT NULL;")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_governance_evidence_decision;")
    for col in ("subject_type", "subject_id", "run_id"):
        op.execute(f"ALTER TABLE workflow_decisions DROP COLUMN IF EXISTS {col};")
