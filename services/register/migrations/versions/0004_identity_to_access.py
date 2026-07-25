"""identity moves to the Access service — drop users & user_roles

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-25

The three-service RBAC architecture: users, roles and the (now admin-editable) access
matrix live in the dedicated Access service, in its own database. The Register keeps
what must sit next to the data — ``line_assignments`` (scoped enforcement) and
``change_requests`` (approval applies the change atomically) — and receives identity
per-request via gateway-forwarded headers.
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # line_assignments.user_id / change_requests reference Access-service users now —
    # drop the local identity tables (their FKs go with them via CASCADE).
    op.execute("DROP TABLE IF EXISTS user_roles CASCADE;")
    op.execute("DROP TABLE IF EXISTS users CASCADE;")


def downgrade() -> None:
    # Recreate the 0003 identity tables (empty) so a rollback leaves a valid schema.
    common = """
        tenant_id      uuid        NOT NULL,
        version        integer     NOT NULL DEFAULT 1,
        created_at     timestamptz NOT NULL DEFAULT now(),
        updated_at     timestamptz NOT NULL DEFAULT now(),
        created_by     varchar(120),
        updated_by     varchar(120),
        deleted_at     timestamptz
    """
    op.execute(f"""
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email varchar(200) NOT NULL,
            full_name varchar(200) NOT NULL,
            short_name varchar(60),
            is_active boolean NOT NULL DEFAULT true,
            reports_to uuid REFERENCES users(id) ON DELETE SET NULL,
            person_id uuid REFERENCES people(id) ON DELETE SET NULL,
            phone varchar(30),
            notes text,
            meta jsonb,
            {common},
            CONSTRAINT users_tenant_email UNIQUE (tenant_id, email)
        );
    """)
    op.execute(f"""
        CREATE TABLE user_roles (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role varchar(30) NOT NULL,
            granted_by varchar(200),
            {common},
            CONSTRAINT user_roles_unique UNIQUE (tenant_id, user_id, role)
        );
    """)
