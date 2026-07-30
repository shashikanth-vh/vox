"""Make governance evidence AUTHORITATIVE: provenance binding + an append-only validity status.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-29

Round J landed immutable evidence, but validity was decided only by (subject + kind) — so a
mistakenly or fraudulently attached row could never be invalidated, and the row carried no link to
the authoritative workflow/decision that produced it. This migration adds:

* Provenance columns on ``governance_evidence`` — ``workflow_id`` / ``run_id`` / ``decision_ref``
  (the authoritative Temporal run + decision record a GOVERNANCE evidence row must cite) and
  ``supersedes_id`` (a corrected row points at the one it replaces) and ``effective_date``.
* ``governance_evidence_status`` — an APPEND-ONLY status ledger. A row here marks an evidence
  record ``Revoked`` / ``Invalidated`` / ``Superseded`` with a reason + actor. The evidence row
  itself stays immutable (history is preserved); the policy loader treats a record as currently
  valid ONLY when it has no terminal status event and is not superseded — so a bad attachment can
  be neutralised without ever mutating or deleting the original.

Both tables are tenant-scoped with fail-closed RLS; the status ledger is itself append-only
(UPDATE/DELETE blocked by trigger), so a revocation cannot later be quietly walked back.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    op.execute("ALTER TABLE governance_evidence "
               "ADD COLUMN IF NOT EXISTS workflow_id varchar(200);")
    op.execute("ALTER TABLE governance_evidence "
               "ADD COLUMN IF NOT EXISTS run_id varchar(200);")
    op.execute("ALTER TABLE governance_evidence "
               "ADD COLUMN IF NOT EXISTS decision_ref varchar(200);")
    op.execute("ALTER TABLE governance_evidence "
               "ADD COLUMN IF NOT EXISTS supersedes_id uuid;")
    op.execute("ALTER TABLE governance_evidence "
               "ADD COLUMN IF NOT EXISTS effective_date timestamptz;")

    op.execute(
        """
        CREATE TABLE governance_evidence_status (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            evidence_id  uuid         NOT NULL REFERENCES governance_evidence(id),
            status       varchar(20)  NOT NULL,
            reason       text         NOT NULL,
            actor        varchar(120),
            tenant_id    uuid        NOT NULL,
            version      integer     NOT NULL DEFAULT 1,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            created_by   varchar(120),
            updated_by   varchar(120),
            deleted_at   timestamptz,
            CONSTRAINT governance_evidence_status_value
                CHECK (status IN ('Revoked', 'Invalidated', 'Superseded'))
        );
        """
    )
    op.execute("CREATE INDEX ix_governance_evidence_status_evidence "
               "ON governance_evidence_status (tenant_id, evidence_id);")

    # The status ledger is itself append-only — a revocation cannot later be silently reversed.
    op.execute(
        """
        CREATE TRIGGER trg_governance_evidence_status_immutable
        BEFORE UPDATE OR DELETE ON governance_evidence_status
        FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
        """
    )

    op.execute("ALTER TABLE governance_evidence_status ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY governance_evidence_status_tenant_isolation ON governance_evidence_status
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE governance_evidence_status FORCE ROW LEVEL SECURITY;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT ON governance_evidence_status TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'governance_evidence_status grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS governance_evidence_status;")
    for col in ("workflow_id", "run_id", "decision_ref", "supersedes_id", "effective_date"):
        op.execute(f"ALTER TABLE governance_evidence DROP COLUMN IF EXISTS {col};")
