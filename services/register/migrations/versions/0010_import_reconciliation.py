"""Persist import reconciliation as an operational object + give every product line stage history.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-29

A governed historical import may RETAIN a row whose terminal stage is missing its mandatory data
(``retain_incomplete=true``). Reporting that only in the HTTP response and the audit event is not
enough — once the response is lost, the record looks operationally complete to every list API,
ATLAS, export and workflow. This migration makes reconciliation a durable object:

* ``import_reconciliation_items`` — one row per retained-incomplete import row, carrying the batch
  id / checksum lineage, the missing fields, the ORIGINAL imported values, an owner, and an
  open/closed ``status`` an Admin resolves. Mutable, tenant-scoped, fail-closed RLS.
* ``reconciliation_status`` on each tracker (lending / deals / syndication / asset monetisation) —
  set to ``Required`` on a retained-incomplete row, so normal "operationally complete" reads can
  exclude it (``reconciliation_status IS NULL``).
* ``deals.stage_history`` — Deal gains the same append-only history the other trackers have, so the
  unified import-history helper can record Deal stage changes too.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def upgrade() -> None:
    # Deal and Asset Monetisation gain an append-only history (parity with the other trackers).
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS stage_history jsonb;")
    op.execute("ALTER TABLE asset_monetisation ADD COLUMN IF NOT EXISTS status_history jsonb;")
    # A lightweight per-record flag so operational reads can exclude unreconciled imports.
    for table in ("lending_tracker", "deals", "syndication_tracker", "asset_monetisation"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                   "reconciliation_status varchar(20);")

    op.execute(
        """
        CREATE TABLE import_reconciliation_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            import_batch_id  varchar(80)  NOT NULL,
            checksum         varchar(80),
            subject_type     varchar(40)  NOT NULL,
            subject_id       uuid,
            sheet            varchar(80),
            company          varchar(200),
            stage_field      varchar(40),
            stage_value      varchar(80),
            missing_fields   jsonb        NOT NULL DEFAULT '[]'::jsonb,
            original_values  jsonb,
            status           varchar(20)  NOT NULL DEFAULT 'Required',
            owner            varchar(120),
            resolution_note  text,
            resolved_by      varchar(120),
            resolved_at      timestamptz,
            tenant_id      uuid        NOT NULL,
            version        integer     NOT NULL DEFAULT 1,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            created_by     varchar(120),
            updated_by     varchar(120),
            deleted_at     timestamptz,
            CONSTRAINT import_reconciliation_status
                CHECK (status IN ('Required', 'Resolved', 'Waived'))
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_import_reconciliation_items_updated_at
        BEFORE UPDATE ON import_reconciliation_items
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute("CREATE INDEX ix_import_reconciliation_tenant "
               "ON import_reconciliation_items (tenant_id, status);")
    op.execute("CREATE INDEX ix_import_reconciliation_batch "
               "ON import_reconciliation_items (tenant_id, import_batch_id);")

    op.execute("ALTER TABLE import_reconciliation_items ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY import_reconciliation_items_tenant_isolation ON import_reconciliation_items
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);
        """
    )
    if _truthy(os.getenv("REGISTER_ENFORCE_RLS")):
        op.execute("ALTER TABLE import_reconciliation_items FORCE ROW LEVEL SECURITY;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON import_reconciliation_items '
                        'TO register_app';
            END IF;
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE NOTICE 'import_reconciliation_items grant to register_app skipped.';
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS import_reconciliation_items;")
    for table in ("lending_tracker", "deals", "syndication_tracker", "asset_monetisation"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS reconciliation_status;")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS stage_history;")
    op.execute("ALTER TABLE asset_monetisation DROP COLUMN IF EXISTS status_history;")
