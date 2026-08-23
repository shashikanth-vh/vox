"""VOX conversations — the firm's shared memory (Build Specification, Section 12).

Four tables and two hard disciplines enforced in the database itself:

* ``vox_consent_records`` is INSERT-ONLY — a trigger refuses UPDATE and DELETE.
  Evidence you can quietly edit is not evidence (D5).
* ``vox_conversation_edits`` is APPEND-ONLY — same trigger discipline. The audit
  trail cannot be rewritten by the code paths it audits.

The verbatim transcript gets a full-text GIN index (the Memory search) and the
structured report a JSONB GIN index (field-level filters), exactly as the spec's
DDL draws them.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS vox_consent_records (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        version integer NOT NULL DEFAULT 1,
        conversation_id uuid,
        user_email varchar(200) NOT NULL,
        user_name varchar(200),
        certified_at timestamptz NOT NULL DEFAULT now(),
        certification_text text NOT NULL,
        device_meta jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        created_by varchar(120),
        updated_by varchar(120),
        deleted_at timestamptz
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS vox_conversations (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        version integer NOT NULL DEFAULT 1,
        recorder_email varchar(200) NOT NULL,
        recorder_name varchar(200),
        entity_id uuid REFERENCES entities(id) ON DELETE SET NULL,
        lead_id uuid REFERENCES leads(id) ON DELETE SET NULL,
        deal_id uuid REFERENCES deals(id) ON DELETE SET NULL,
        interaction_id uuid,
        entity_candidates jsonb,
        recording_mode varchar(20) NOT NULL
            CHECK (recording_mode IN ('post_meeting','live')),
        capture_id varchar(120),
        audio_ref varchar(512),
        audio_deleted_at timestamptz,
        duration_seconds integer,
        latitude double precision,
        longitude double precision,
        raw_transcript text,
        transcript_segments jsonb,
        structured_report jsonb,
        sector varchar(60),
        subsector varchar(120),
        meeting_date date,
        language_detected varchar(40),
        status varchar(30) NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','uploading','processing','ready','submitted',
                              'processing_failed','failed_permanently')),
        processing_stage varchar(40),
        processing_error text,
        retry_count integer NOT NULL DEFAULT 0,
        consent_id uuid REFERENCES vox_consent_records(id) ON DELETE SET NULL,
        prompt_version varchar(20),
        registry_version varchar(20),
        erased_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        created_by varchar(120),
        updated_by varchar(120),
        deleted_at timestamptz,
        CONSTRAINT vox_conversations_tenant_capture UNIQUE (tenant_id, capture_id)
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS vox_conversation_use_cases (
        conversation_id uuid NOT NULL
            REFERENCES vox_conversations(id) ON DELETE CASCADE,
        use_case varchar(30) NOT NULL,
        PRIMARY KEY (conversation_id, use_case)
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS vox_conversation_edits (
        id bigserial PRIMARY KEY,
        tenant_id uuid NOT NULL,
        conversation_id uuid NOT NULL
            REFERENCES vox_conversations(id) ON DELETE CASCADE,
        editor_email varchar(200) NOT NULL,
        editor_name varchar(200),
        field_path varchar(200) NOT NULL,
        old_value jsonb,
        new_value jsonb,
        edited_at timestamptz NOT NULL DEFAULT now()
    );
    """)

    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_conversations_entity_time "
               "ON vox_conversations (tenant_id, entity_id, meeting_date);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_conversations_recorder "
               "ON vox_conversations (tenant_id, recorder_email);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_conversations_status "
               "ON vox_conversations (tenant_id, status);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_conversations_report_gin "
               "ON vox_conversations USING gin (structured_report);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_conversations_transcript_fts "
               "ON vox_conversations USING gin "
               "(to_tsvector('english', coalesce(raw_transcript,'')));")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vox_edits_conversation "
               "ON vox_conversation_edits (conversation_id, edited_at);")

    # Immutability, enforced where it cannot be argued with.
    op.execute("""
    CREATE OR REPLACE FUNCTION vox_refuse_mutation() RETURNS trigger AS $$
    BEGIN
        -- The edit trail holds field values, so an AUTHORISED ERASURE must reach it.
        -- That one flow announces itself with a transaction-local GUC; consent
        -- records yield to nothing.
        IF TG_TABLE_NAME = 'vox_conversation_edits'
           AND current_setting('app.vox_erasure', true) = 'on'
           AND TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION '% is %: rows are written once and never changed',
            TG_TABLE_NAME,
            CASE TG_TABLE_NAME WHEN 'vox_consent_records' THEN 'immutable (D5)'
                               ELSE 'append-only' END
            USING ERRCODE = 'raise_exception';
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("DROP TRIGGER IF EXISTS vox_consent_immutable ON vox_consent_records;")
    op.execute("CREATE TRIGGER vox_consent_immutable "
               "BEFORE UPDATE OR DELETE ON vox_consent_records "
               "FOR EACH ROW EXECUTE FUNCTION vox_refuse_mutation();")
    op.execute("DROP TRIGGER IF EXISTS vox_edits_append_only ON vox_conversation_edits;")
    op.execute("CREATE TRIGGER vox_edits_append_only "
               "BEFORE UPDATE OR DELETE ON vox_conversation_edits "
               "FOR EACH ROW EXECUTE FUNCTION vox_refuse_mutation();")

    # RLS, matching the register's existing posture: rows visible only inside the
    # tenant the session is bound to (D2's "all authenticated staff" is WITHIN the
    # tenant — cross-tenant stays sealed).
    for table in ("vox_conversations", "vox_consent_records", "vox_conversation_edits"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                current_setting('app.current_tenant', true) IS NULL
                OR tenant_id = current_setting('app.current_tenant', true)::uuid
            )
            WITH CHECK (
                current_setting('app.current_tenant', true) IS NULL
                OR tenant_id = current_setting('app.current_tenant', true)::uuid
            );
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vox_conversation_edits;")
    op.execute("DROP TABLE IF EXISTS vox_conversation_use_cases;")
    op.execute("DROP TABLE IF EXISTS vox_conversations;")
    op.execute("DROP TABLE IF EXISTS vox_consent_records;")
    op.execute("DROP FUNCTION IF EXISTS vox_refuse_mutation();")
