"""Authoritative, immutable Advaya-handoff record — so the disbursement acknowledgement cannot be
manually manufactured.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-29

Round M added the ``advaya_acknowledgement`` evidence kind and the ``Disbursed`` gate, but the
Register verified only that a digest + workflow/run were supplied — so Admin/Management could invent
provenance and satisfy the gate without Advaya actually accepting the handoff. This migration adds
``advaya_handoffs``: the single-winner, IMMUTABLE record of an Advaya handoff OUTCOME (accepted /
rejected), keyed by an idempotency ``handoff_key`` (one per tenant + lending line), carrying the
payload digest, Advaya's acknowledgement id, and the run that performed it. The evidence attach then
VERIFIES ``advaya_acknowledgement`` against an ``Accepted`` handoff for the same subject with the
matching payload hash — exactly as committee/sanction evidence is verified against a committee
decision. Tenant-scoped, fail-closed RLS, UPDATE/DELETE-blocked.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE advaya_handoffs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            handoff_key       varchar(200) NOT NULL,
            lending_id        varchar(64)  NOT NULL,
            payload_sha256    varchar(64)  NOT NULL,
            status            varchar(20)  NOT NULL,
            acknowledgement_id varchar(200),
            workflow_id       varchar(200),
            run_id            varchar(200),
            note              text,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz,
            CONSTRAINT advaya_handoffs_status CHECK (status IN ('Accepted', 'Rejected')),
            CONSTRAINT advaya_handoffs_tenant_key UNIQUE (tenant_id, handoff_key)
        );
        """
    )
    op.execute("CREATE INDEX ix_advaya_handoffs_lending "
               "ON advaya_handoffs (tenant_id, lending_id);")
    # Immutable: an accepted handoff cannot be silently altered/removed to (un)justify a disbursement.
    op.execute(
        """
        CREATE TRIGGER trg_advaya_handoffs_immutable
        BEFORE UPDATE OR DELETE ON advaya_handoffs
        FOR EACH ROW EXECUTE FUNCTION governance_evidence_immutable();
        """
    )
    op.execute("ALTER TABLE advaya_handoffs ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY advaya_handoffs_tenant_isolation ON advaya_handoffs
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE advaya_handoffs FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT ON advaya_handoffs TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'advaya_handoffs grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS advaya_handoffs;")
