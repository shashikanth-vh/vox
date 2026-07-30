"""A transactional delivery outbox for workflow decisions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29

A decision is recorded durably (single-winner, immutable) BEFORE the orchestrator signals the
workflow. If that signal is lost and no caller retries, the accepted decision would sit
unapplied forever. This table is the OUTBOX that closes that gap: one delivery row is created
IN THE SAME TRANSACTION as the decision, and a background reconciler repeatedly claims pending
rows (with a lease), re-delivers them to the workflow, marks them applied when the run has
converted, and dead-letters ones whose workflow closed without applying / exhausted retries.

Unlike ``workflow_decisions`` (append-only, immutable), THIS table is deliberately mutable —
its whole purpose is to track evolving delivery state — so it carries no immutability trigger.
Its ``status``/``attempts``/``next_attempt_at``/``leased_until`` are the reconciler's workspace.
RLS is fail-closed exactly like every other tenant table.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflow_decision_outbox (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_id     varchar(200) NOT NULL,
            decision        varchar(20)  NOT NULL,
            -- pending → not yet confirmed applied; applied → the run converted with this
            -- outcome; dead → the run closed without applying it or retries were exhausted.
            status          varchar(12)  NOT NULL DEFAULT 'pending',
            attempts        integer      NOT NULL DEFAULT 0,
            next_attempt_at timestamptz  NOT NULL DEFAULT now(),
            leased_until    timestamptz,
            last_error      text,
            applied_at      timestamptz,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz,
            CONSTRAINT workflow_decision_outbox_tenant_wf UNIQUE (tenant_id, workflow_id),
            CONSTRAINT workflow_decision_outbox_status
                CHECK (status IN ('pending', 'applied', 'dead'))
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workflow_decision_outbox_updated_at
        BEFORE UPDATE ON workflow_decision_outbox
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute("CREATE INDEX ix_workflow_decision_outbox_tenant "
               "ON workflow_decision_outbox (tenant_id);")
    # The reconciler's claim query filters on (status, next_attempt_at) — index it.
    op.execute("CREATE INDEX ix_workflow_decision_outbox_due "
               "ON workflow_decision_outbox (tenant_id, status, next_attempt_at);")

    op.execute("ALTER TABLE workflow_decision_outbox ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY workflow_decision_outbox_tenant_isolation ON workflow_decision_outbox
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE workflow_decision_outbox FORCE ROW LEVEL SECURITY;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_decision_outbox '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'workflow_decision_outbox grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_decision_outbox;")
