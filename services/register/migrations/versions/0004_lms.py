"""LMS core: loan accounts + the ledger (lending increment ④).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05

* ``loan_accounts`` — opened automatically on the FIRST Advaya-confirmed disbursement
  tranche: account number (per-tenant sequence), borrower, facility, disbursement date,
  principal (cumulative tranches), rate/tenor/EMI/day-count copied from the sanction
  terms, and the servicing classification (Standard/SMA/…, overdue position,
  provisioning).
* ``loan_ledger_entries`` — the statement: Date | Particulars | Debit | Credit |
  Balance, numbered per account. Interest rows are computed via the accrue endpoint.

Standard trailing columns, updated_at trigger, fail-closed tenant RLS — same as 0002.
"""
from __future__ import annotations

import os

from alembic import op

revision = "0004"
down_revision = "0003"
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

_TABLES = ["loan_accounts", "loan_ledger_entries"]


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
    _table("loan_accounts", """
        lending_id        varchar(64)  NOT NULL,
        deal_id           varchar(64),
        entity_id         varchar(64),
        account_no        integer      NOT NULL,
        borrower          varchar(300),
        facility_type     varchar(80),
        disbursed_on      date,
        amount            numeric(16,2),
        rate_kind         varchar(10)  NOT NULL DEFAULT 'Fixed',
        rate_pct          numeric(7,4),
        tenor_months      integer,
        emi_amount        numeric(16,2),
        repayment_start   date,
        day_count         varchar(8)   NOT NULL DEFAULT '365',
        status            varchar(16)  NOT NULL DEFAULT 'Standard',
        overdue_position  varchar(120) NOT NULL DEFAULT 'Nil',
        provisioning_amount numeric(16,2),
        closed_on         date,
        note              text
    """)
    op.execute("""
        CREATE UNIQUE INDEX loan_accounts_tenant_lending
        ON loan_accounts (tenant_id, lending_id) WHERE deleted_at IS NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX loan_accounts_tenant_no
        ON loan_accounts (tenant_id, account_no) WHERE deleted_at IS NULL;
    """)

    _table("loan_ledger_entries", """
        account_id  uuid         NOT NULL REFERENCES loan_accounts(id) ON DELETE CASCADE,
        entry_no    integer      NOT NULL,
        entry_date  date         NOT NULL,
        particulars varchar(300) NOT NULL,
        entry_type  varchar(16)  NOT NULL,
        debit       numeric(16,2),
        credit      numeric(16,2),
        balance     numeric(16,2) NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_loan_ledger_account
        ON loan_ledger_entries (tenant_id, account_id, entry_no);
    """)

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
