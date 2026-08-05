"""The ledger learns rupees: money columns widen from 2 to 7 decimals (₹ Cr units).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-05

The servicing desk's real ledger works in absolute rupees — an interest row is
₹57,535, an EMI is ₹4,47,608. PRISM stores amounts in ₹ Cr, and at Numeric(…,2)
that is 1-lakh granularity: real interest rows rounded to noise. Seven decimals of
a crore is exactly one rupee (1e-7 Cr = ₹1), so every figure the Excel holds fits
losslessly. Pure widening — no data changes.
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_COLS = [
    ("loan_ledger_entries", "debit"),
    ("loan_ledger_entries", "credit"),
    ("loan_ledger_entries", "balance"),
    ("loan_accounts", "amount"),
    ("loan_accounts", "emi_amount"),
    ("loan_accounts", "provisioning_amount"),
    ("sanction_terms", "emi_amount"),
]


def upgrade() -> None:
    for table, col in _COLS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE numeric(20,7);")


def downgrade() -> None:
    for table, col in _COLS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE numeric(16,2);")
