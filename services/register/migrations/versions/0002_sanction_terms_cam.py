"""Sanction terms + the CAM workbench record.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04

Lending increment 1+2 (docs/LENDING_WORKFLOW_DESIGN.md):

* ``sanction_terms`` — the STRUCTURED sanction: one row per lending line, captured once
  at committee approval and seeding four registers (the CP/CS checklist, covenant rows,
  and — later — the LMS account). Amount, rate, tenor, EMI, day-count and the CP/CS/
  covenant item lists as entered; the seeded artefacts keep their own lifecycles.
* ``cam_reports`` — one row per CAM VERSION (the CpcsChecklist maker-checker shape):
  Draft → Submitted → Approved | Returned | Rejected, preparer/decider identities, the
  engine (provider:model) that drafted it, its inputs (source document ids + the prompt
  doc), the current draft text, and the Data Register document it was finalised to.
* ``cam_turns`` — the rework transcript per report: what the analyst asked, what the
  engine answered. The audit answer to "why does the CAM say this?".

All three carry the standard trailing columns, updated_at trigger, and the fail-closed
tenant-isolation RLS policy the rest of the register runs under.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_COMMON = """
    tenant_id      uuid        NOT NULL,
    version        integer     NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    created_by     varchar(120),
    updated_by     varchar(120),
    deleted_at     timestamptz
"""

_TABLES = ["sanction_terms", "cam_reports", "cam_turns"]


def _table(name: str, columns: str) -> None:
    op.execute(f"""
        CREATE TABLE {name} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            {columns},
            {_COMMON}
        );
    """)
    op.execute(f"""
        CREATE TRIGGER trg_{name}_updated_at BEFORE UPDATE ON {name}
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)


def upgrade() -> None:
    _table("sanction_terms", """
        lending_id       varchar(64)  NOT NULL,
        deal_id          varchar(64),
        amount_cr        numeric(14,2),
        rate_kind        varchar(10)  NOT NULL DEFAULT 'Fixed',
        rate_pct         numeric(7,4),
        spread_pct       numeric(7,4),
        tenor_months     integer,
        emi_amount       numeric(16,2),
        repayment_start  date,
        day_count        varchar(8)   NOT NULL DEFAULT '365',
        penal_rate_pct   numeric(7,4),
        moratorium_months integer     NOT NULL DEFAULT 0,
        schedule_kind    varchar(10)  NOT NULL DEFAULT 'EMI',
        -- The item lists AS ENTERED (the seeded artefacts keep their own lifecycles):
        -- cp_items / cs_items: [{key,label,required,note}]; covenants: covenant defs.
        cp_items         jsonb,
        cs_items         jsonb,
        covenants        jsonb,
        -- What the one save seeded, for traceability: checklist id + covenant ids.
        seeded_checklist_id varchar(64),
        seeded_covenant_ids jsonb,
        note             text
    """)
    op.execute("""
        CREATE UNIQUE INDEX sanction_terms_tenant_lending
        ON sanction_terms (tenant_id, lending_id) WHERE deleted_at IS NULL;
    """)

    _table("cam_reports", """
        lending_id      varchar(64)  NOT NULL,
        deal_id         varchar(64),
        report_version  integer      NOT NULL DEFAULT 1,
        status          varchar(12)  NOT NULL DEFAULT 'Draft',
        engine          varchar(120),
        source_doc_ids  jsonb,
        prompt_doc_id   varchar(64),
        draft_md        text,
        document_id     varchar(64),
        prepared_by     varchar(120),
        prepared_by_id  varchar(64),
        decided_by      varchar(120),
        decided_by_id   varchar(64),
        decision_note   text,
        note            text
    """)
    op.execute("""
        ALTER TABLE cam_reports ADD CONSTRAINT cam_reports_tenant_lending_version
        UNIQUE (tenant_id, lending_id, report_version);
    """)

    _table("cam_turns", """
        report_id  uuid         NOT NULL REFERENCES cam_reports(id) ON DELETE CASCADE,
        turn_no    integer      NOT NULL,
        role       varchar(10)  NOT NULL,
        content    text         NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_cam_turns_report ON cam_turns (tenant_id, report_id, turn_no);
    """)

    # The same fail-closed tenant isolation as every other business table (0001 §RLS).
    force = (os.getenv("REGISTER_ENFORCE_RLS") or "").strip().lower() in {
        "1", "true", "yes", "on"}
    for tbl in _TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
        """)
        if force:
            op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    for tbl in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
