"""RBAC v3.7: the role "LMS Authorizer" is renamed "LMS Management".

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05

Stored grants move to the new name; the runtime additionally resolves the old
string through the rename table (evam_backend_core ROLE_ALIASES), so a signed
context minted before this migration keeps its access. New grants validate
against the current catalogue only.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE user_roles SET role = 'LMS Management' "
        "WHERE role = 'LMS Authorizer' AND deleted_at IS NULL;")


def downgrade() -> None:
    op.execute(
        "UPDATE user_roles SET role = 'LMS Authorizer' "
        "WHERE role = 'LMS Management' AND deleted_at IS NULL;")
