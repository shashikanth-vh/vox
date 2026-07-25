"""documents catalog + checklist template (ATLAS "Data Register")

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25

Adds the two tables behind ATLAS's Data Register:

* ``document_checklist`` — the per-tenant checklist *template* (sections + required slots).
* ``documents``          — the *catalog*: one row per document on file, storing a
                           reference to the bytes (``storage_uri`` in object storage) plus
                           metadata; small files may be kept inline (``inline_content``).

Hand-written to match 0001's conventions (shared trailing columns, updated_at trigger,
tenant indexes and row-level-security policies).
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Same trailing columns every business table carries (see 0001).
COMMON = """
    tenant_id      uuid        NOT NULL,
    version        integer     NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    created_by     varchar(120),
    updated_by     varchar(120),
    deleted_at     timestamptz
"""


def _table(name: str, columns: str) -> None:
    op.execute(
        f"""
        CREATE TABLE {name} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            {columns},
            {COMMON}
        );
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {name}
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute(f"CREATE INDEX ix_{name}_tenant ON {name} (tenant_id);")
    op.execute(f"CREATE INDEX ix_{name}_tenant_active ON {name} (tenant_id) WHERE deleted_at IS NULL;")


_RLS_TABLES = ["document_checklist", "documents"]


def upgrade() -> None:
    # --- checklist template ----------------------------------------------
    _table("document_checklist", """
        applies_to varchar(30) NOT NULL DEFAULT '*',
        section varchar(80) NOT NULL,
        section_order integer NOT NULL DEFAULT 0,
        slot_key varchar(60) NOT NULL,
        label varchar(200) NOT NULL,
        is_required boolean NOT NULL DEFAULT false,
        sort_order integer NOT NULL DEFAULT 0,
        is_active boolean NOT NULL DEFAULT true,
        hint text,
        CONSTRAINT document_checklist_unique UNIQUE (tenant_id, applies_to, slot_key)
    """)
    op.execute("CREATE INDEX ix_doc_checklist_applies ON document_checklist (tenant_id, applies_to);")

    # --- documents catalog -----------------------------------------------
    _table("documents", """
        subject_type varchar(30) NOT NULL,
        subject_id uuid NOT NULL,
        entity_id uuid REFERENCES entities(id) ON DELETE CASCADE,
        deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
        section varchar(80),
        slot_key varchar(60),
        doc_type varchar(120),
        title varchar(300) NOT NULL,
        is_required boolean NOT NULL DEFAULT false,
        status varchar(40) NOT NULL DEFAULT 'On File',
        storage_backend varchar(20),
        storage_uri text,
        content_type varchar(120),
        size_bytes bigint,
        checksum varchar(64),
        original_filename varchar(300),
        inline_content bytea,
        uploaded_by varchar(120),
        uploaded_at timestamptz,
        notes text,
        meta jsonb
    """)
    op.execute("CREATE INDEX ix_documents_subject ON documents (tenant_id, subject_type, subject_id);")
    op.execute("CREATE INDEX ix_documents_entity ON documents (tenant_id, entity_id);")
    op.execute("CREATE INDEX ix_documents_slot ON documents (tenant_id, subject_type, subject_id, slot_key);")
    op.execute("CREATE INDEX ix_documents_entity_fk ON documents (entity_id);")

    _apply_row_level_security()


def _apply_row_level_security() -> None:
    """Same tenant-isolation policy as 0001, keyed off the ``app.current_tenant`` GUC."""
    for tbl in _RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (
                current_setting('app.current_tenant', true) IS NULL
                OR tenant_id = current_setting('app.current_tenant', true)::uuid
            )
            WITH CHECK (
                current_setting('app.current_tenant', true) IS NULL
                OR tenant_id = current_setting('app.current_tenant', true)::uuid
            );
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS documents CASCADE;")
    op.execute("DROP TABLE IF EXISTS document_checklist CASCADE;")
