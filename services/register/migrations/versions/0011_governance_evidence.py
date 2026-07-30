"""Immutable governance-evidence store for evidence-based lifecycle gates.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-29

Ordered transitions and mandatory fields prove *sequence* and *shape*, but not that the real-world
governance work actually happened before a sensitive stage. This migration adds ``governance_evidence``
— an APPEND-ONLY record that a milestone occurred (a Credit Committee approval, a sanction letter, an
executed facility agreement, a completed document set): its kind, an external reference, an optional
integrity digest, and who/when.

The shared policy engine (:data:`evam_backend_core.policy.EVIDENCE_FOR_STAGE`) refuses a gated
transition until every evidence kind that stage requires is present here — for humans and services
alike. So the store must be TRUSTWORTHY: it is tenant-scoped with fail-closed RLS, and a trigger
rejects UPDATE and DELETE so an attached evidence row can never be silently altered or removed to
retro-justify (or un-justify) a transition. Corrections are made by appending a new row, never by
mutating history.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE governance_evidence (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type   varchar(40)  NOT NULL,
            subject_id     uuid         NOT NULL,
            evidence_kind  varchar(60)  NOT NULL,
            reference      varchar(500) NOT NULL,
            sha256         varchar(64),
            note           text,
            recorded_by    varchar(120),
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz
        );
        """
    )
    op.execute("CREATE INDEX ix_governance_evidence_subject "
               "ON governance_evidence (tenant_id, subject_type, subject_id, evidence_kind);")

    # WRITE-ONCE immutability: an evidence row may be inserted but never updated or deleted, so it
    # cannot be silently altered after it has justified (or been relied on to justify) a transition.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_evidence_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'governance_evidence is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'raise_exception';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_governance_evidence_immutable
        BEFORE UPDATE OR DELETE ON governance_evidence
        FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
        """
    )

    op.execute("ALTER TABLE governance_evidence ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY governance_evidence_tenant_isolation ON governance_evidence
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE governance_evidence FORCE ROW LEVEL SECURITY;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                -- INSERT + SELECT only: the app never needs UPDATE/DELETE, and the trigger blocks
                -- them anyway, so the grant matches the intent (append + read).
                EXECUTE 'GRANT SELECT, INSERT ON governance_evidence TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'governance_evidence grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_governance_evidence_immutable ON governance_evidence;")
    op.execute("DROP FUNCTION IF EXISTS governance_evidence_immutable();")
    op.execute("DROP TABLE IF EXISTS governance_evidence;")
