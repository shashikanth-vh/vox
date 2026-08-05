"""CS progress may be recorded on an APPROVED checklist.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05

Conditions Subsequent are collected for months after the CP approval — each received
document is a PROGRESS update, not a new governance decision, so it must not need a
fresh checklist version and a checker round-trip. The freeze trigger is narrowed: an
Approved checklist's DECISION FIELDS (status, version, preparer, approver) stay frozen,
while its ``items`` may be updated — the API layer restricts those updates to CS items
and audits every change. Rejected and Returned rows stay fully frozen; deletes stay
impossible.
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_NARROW = """
CREATE OR REPLACE FUNCTION cp_cs_checklist_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cp_cs_checklists rows are append-only and cannot be deleted';
    END IF;
    IF OLD.status IN ('Rejected', 'Returned') THEN
        RAISE EXCEPTION 'cp_cs_checklists row % is % and is frozen', OLD.id, OLD.status;
    END IF;
    IF OLD.status = 'Approved' AND (
           NEW.status            IS DISTINCT FROM OLD.status
        OR NEW.checklist_version IS DISTINCT FROM OLD.checklist_version
        OR NEW.prepared_by       IS DISTINCT FROM OLD.prepared_by
        OR NEW.prepared_by_id    IS DISTINCT FROM OLD.prepared_by_id
        OR NEW.approved_by       IS DISTINCT FROM OLD.approved_by
        OR NEW.approved_by_id    IS DISTINCT FROM OLD.approved_by_id
    ) THEN
        RAISE EXCEPTION
            'cp_cs_checklists row % is Approved — its decision fields are frozen', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_ORIGINAL = """
CREATE OR REPLACE FUNCTION cp_cs_checklist_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'cp_cs_checklists rows are append-only and cannot be deleted';
    END IF;
    IF OLD.status IN ('Approved', 'Rejected', 'Returned') THEN
        RAISE EXCEPTION 'cp_cs_checklists row % is % and is frozen', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_NARROW)


def downgrade() -> None:
    op.execute(_ORIGINAL)
