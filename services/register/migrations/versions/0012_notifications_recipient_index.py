"""The inbox poll becomes an index hit.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-06

Every signed-in client polls its unread notifications (the header bell, ~45s) — at a
thousand users that is a steady ~20 req/s against ``notifications`` filtered by
(tenant, recipient, read_at IS NULL). Without an index that filter is a sequential
scan over a table that only ever grows. Partial over live rows; serves the unread
list and the count alike.
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_recipient_unread "
        "ON notifications (tenant_id, recipient, read_at) "
        "WHERE deleted_at IS NULL;")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_recipient_unread;")
