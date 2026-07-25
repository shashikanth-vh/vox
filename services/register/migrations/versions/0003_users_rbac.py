"""user management & RBAC — users, roles, line assignments, change requests

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-25

Implements the ATLAS RBAC spec (v3.1): the Employees governance table (users), role
stacking (user_roles), the assignment-driven permission primitive (line_assignments),
and the request → approve/reject stage-change flow (change_requests).
Hand-written to match 0001/0002 conventions (shared trailing columns, updated_at
trigger, tenant indexes, row-level-security policies).
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
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
    op.execute(f"CREATE INDEX ix_{name}_tenant_active ON {name} (tenant_id) WHERE deleted_at IS NULL;")


_RLS_TABLES = ["users", "user_roles", "line_assignments", "change_requests"]


def upgrade() -> None:
    # --- users (the Employees governance table) ----------------------------
    _table("users", """
        email varchar(200) NOT NULL,
        full_name varchar(200) NOT NULL,
        short_name varchar(60),
        is_active boolean NOT NULL DEFAULT true,
        reports_to uuid REFERENCES users(id) ON DELETE SET NULL,
        person_id uuid REFERENCES people(id) ON DELETE SET NULL,
        phone varchar(30),
        notes text,
        meta jsonb,
        CONSTRAINT users_tenant_email UNIQUE (tenant_id, email)
    """)
    op.execute("CREATE INDEX ix_users_tenant_is_active ON users (tenant_id, is_active);")

    # --- user_roles (role stacking) ----------------------------------------
    _table("user_roles", """
        user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role varchar(30) NOT NULL,
        granted_by varchar(200),
        CONSTRAINT user_roles_unique UNIQUE (tenant_id, user_id, role)
    """)
    op.execute("CREATE INDEX ix_user_roles_user ON user_roles (tenant_id, user_id);")

    # --- line_assignments (assignment-driven permission) --------------------
    _table("line_assignments", """
        user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        subject_type varchar(30) NOT NULL,
        subject_id uuid NOT NULL,
        assignment_role varchar(30) NOT NULL,
        assigned_by varchar(200),
        ended_at timestamptz,
        ended_by varchar(200),
        note text
    """)
    op.execute("CREATE INDEX ix_assign_subject ON line_assignments (tenant_id, subject_type, subject_id);")
    op.execute("CREATE INDEX ix_assign_user_active ON line_assignments (tenant_id, user_id, ended_at);")
    # One ACTIVE assignment per (user, line, capacity) — history rows keep ended_at.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_assign_active
        ON line_assignments (tenant_id, subject_type, subject_id, user_id, assignment_role)
        WHERE ended_at IS NULL AND deleted_at IS NULL;
        """
    )

    # --- change_requests (request → approve/reject flow) --------------------
    _table("change_requests", """
        subject_type varchar(30) NOT NULL,
        subject_id uuid NOT NULL,
        field varchar(60) NOT NULL,
        from_value varchar(120),
        to_value varchar(120) NOT NULL,
        note text,
        requested_by varchar(200) NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'Pending',
        decided_by varchar(200),
        decided_at timestamptz,
        decision_note text
    """)
    op.execute("CREATE INDEX ix_chreq_status ON change_requests (tenant_id, status);")
    op.execute("CREATE INDEX ix_chreq_subject ON change_requests (tenant_id, subject_type, subject_id);")

    _apply_row_level_security()


def _apply_row_level_security() -> None:
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
    op.execute("DROP TABLE IF EXISTS change_requests CASCADE;")
    op.execute("DROP TABLE IF EXISTS line_assignments CASCADE;")
    op.execute("DROP TABLE IF EXISTS user_roles CASCADE;")
    op.execute("DROP TABLE IF EXISTS users CASCADE;")
