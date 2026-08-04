"""Unique names must not survive their row's soft-delete.

Revision ID: 0003
Revises: 0002

``people``, ``entities`` and ``counterparties`` all soft-delete (``deleted_at``), but
their natural-key constraints — full name, entity code, counterparty name — were plain
UNIQUEs over every row, deleted or not. So removing an employee kept their full name
reserved forever: hiring a second "Arun Menon" after the first left the firm failed on
``people_tenant_full_name`` with nothing visible on the roster to explain it. Same trap
for a re-registered entity code or bank name.

Each constraint becomes a partial unique INDEX over the LIVE rows only
(``WHERE deleted_at IS NULL``), keeping its name so any code or operator who knows the
old constraint name still recognises the error. Deleted rows keep their data (history
stays attributable); they just stop reserving the name.

No ON CONFLICT anywhere targets these three constraints (the seeders dedupe in logic),
so the swap is behaviour-preserving for every writer except the case being fixed.
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_SWAPS = (
    ("people", "people_tenant_full_name", "(tenant_id, full_name)"),
    ("entities", "entities_tenant_code", "(tenant_id, code)"),
    ("counterparties", "counterparties_tenant_name", "(tenant_id, name)"),
)


def upgrade() -> None:
    for table, name, cols in _SWAPS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} {cols} "
            "WHERE deleted_at IS NULL"
        )


def downgrade() -> None:
    # Best-effort: restoring the full-width constraint fails outright if a live row now
    # legitimately reuses a soft-deleted row's name — that data was legal under 0003 and
    # the operator must resolve it by hand before downgrading.
    for table, name, cols in _SWAPS:
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE {cols}")
