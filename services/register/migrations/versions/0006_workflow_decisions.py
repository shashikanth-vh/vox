"""A dedicated, single-winner workflow-decision resource.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28

The lead-conversion approval was previously recorded as an ordinary ``interactions`` row,
which the security review flagged on three counts:

* **No single-winner guarantee.** Approve and Reject wrote different idempotency keys, so a
  concurrent Approve and Reject could BOTH persist and both be acknowledged — the outcome
  then depended on Temporal signal-delivery order, and an opposite decision could even be
  written after the workflow had completed.
* **Weak authority.** A general interaction's ``interaction_type`` / ``source`` / permission
  metadata are client-supplied fields — not trustworthy authorization evidence.
* **Over-broad reads.** Reading decisions via ``/v1/interactions`` meant the workflow service
  could read every tenant interaction (meeting notes, transcripts) just to fetch a decision.

This table fixes the first at the database level with a **UNIQUE (tenant_id, workflow_id)**
constraint: the FIRST decision for a workflow wins atomically; replaying the SAME decision
returns the original row; the OPPOSITE decision is rejected (409). Its own endpoints
(``/v1/internal/decisions``) are restricted to the workflow service principal and set
provenance server-side from the verified approver context — never client fields.

RLS is applied fail-closed exactly like every other tenant table (and FORCEd in production).
"""
from __future__ import annotations

import os

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflow_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_id     varchar(200) NOT NULL,
            lead_id         varchar(64),
            decision        varchar(20)  NOT NULL,
            decided_by      varchar(200) NOT NULL,
            decided_by_id   varchar(64),
            roles           jsonb NOT NULL DEFAULT '[]'::jsonb,
            operations      jsonb NOT NULL DEFAULT '{}'::jsonb,
            views           jsonb NOT NULL DEFAULT '{}'::jsonb,
            note            text,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz,
            -- The single-winner guarantee: ONE decision per workflow, per tenant. A second,
            -- DIFFERENT decision hits this constraint; the app turns that into a 409.
            CONSTRAINT workflow_decisions_tenant_wf UNIQUE (tenant_id, workflow_id)
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workflow_decisions_updated_at BEFORE UPDATE ON workflow_decisions
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute("CREATE INDEX ix_workflow_decisions_tenant ON workflow_decisions (tenant_id);")

    # Fail-CLOSED RLS, identical to every other tenant table (0005): an unset GUC denies.
    op.execute("ALTER TABLE workflow_decisions ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY workflow_decisions_tenant_isolation ON workflow_decisions
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE workflow_decisions FORCE ROW LEVEL SECURITY;")

    # The non-owner app role gets DML (0005 set ALTER DEFAULT PRIVILEGES, but grant explicitly
    # so an existing register_app picks up this new table immediately). Best-effort.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_decisions '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'workflow_decisions grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_decisions;")
