"""Access service schema — tenants, users, roles, access grants, matrix version

Revision ID: 0001
Revises:
Create Date: 2026-07-25

The identity & authorization facts for PRISM: the Employees governance table, role
stacking, and the access matrix as admin-editable data. Tenancy is enforced in the
application layer (every query is tenant-scoped); this service holds no business data.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


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


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE TABLE tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            code varchar(40) NOT NULL UNIQUE,
            name varchar(200) NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120)
        );
        """
    )

    _table("users", """
        email varchar(200) NOT NULL,
        full_name varchar(200) NOT NULL,
        short_name varchar(60),
        is_active boolean NOT NULL DEFAULT true,
        reports_to uuid REFERENCES users(id) ON DELETE SET NULL,
        phone varchar(30),
        notes text,
        meta jsonb,
        CONSTRAINT users_tenant_email UNIQUE (tenant_id, email)
    """)
    op.execute("CREATE INDEX ix_users_tenant_active ON users (tenant_id, is_active);")

    _table("user_roles", """
        user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role varchar(30) NOT NULL,
        granted_by varchar(200),
        CONSTRAINT user_roles_unique UNIQUE (tenant_id, user_id, role)
    """)
    op.execute("CREATE INDEX ix_user_roles_user ON user_roles (tenant_id, user_id);")

    _table("access_grants", """
        kind varchar(12) NOT NULL,
        item varchar(60) NOT NULL,
        role varchar(30) NOT NULL,
        access varchar(12) NOT NULL,
        CONSTRAINT access_grants_cell UNIQUE (tenant_id, kind, item, role)
    """)
    op.execute("CREATE INDEX ix_access_grants_kind ON access_grants (tenant_id, kind);")

    op.execute(
        """
        CREATE TABLE matrix_versions (
            tenant_id uuid PRIMARY KEY,
            version bigint NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(120),
            updated_by varchar(120)
        );
        """
    )


def downgrade() -> None:
    for tbl in ("matrix_versions", "access_grants", "user_roles", "users", "tenants"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE;")
