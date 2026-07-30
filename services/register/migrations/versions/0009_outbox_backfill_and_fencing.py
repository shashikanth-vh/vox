"""Backfill the decision outbox and add a claim/lease fencing token.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-29

Two durability fixes on top of 0008:

1. **Backfill** — 0008 created an EMPTY outbox, so any decision recorded before the outbox
   existed (rounds 15–15f) — the exact orphan condition the reconciler is meant to recover —
   was invisible to it forever. Insert a pending delivery row for EVERY existing decision
   (idempotent via the unique constraint). A separate migration (not baked into 0008) so an
   environment that already ran 0008 still picks this up on upgrade.

2. **Fencing token** — claims get a ``claim_token``; a delivery update must present the token
   of the CURRENT claim, so a stalled claimant whose lease expired (and whose row was re-claimed
   by another replica) can no longer overwrite the newer result. NULL for backfilled/unclaimed
   rows; set fresh on every claim.
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_decision_outbox ADD COLUMN claim_token uuid;")
    # Backfill: one pending delivery per existing decision. ON CONFLICT DO NOTHING makes it
    # idempotent (re-runnable) and harmless where an outbox row already exists.
    op.execute(
        """
        INSERT INTO workflow_decision_outbox
            (tenant_id, workflow_id, decision, status, attempts, next_attempt_at)
        SELECT d.tenant_id, d.workflow_id, d.decision, 'pending', 0, now()
        FROM workflow_decisions d
        ON CONFLICT ON CONSTRAINT workflow_decision_outbox_tenant_wf DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_decision_outbox DROP COLUMN IF EXISTS claim_token;")
