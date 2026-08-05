"""The booking carries its disclosure: open CP/CS conditions snapshotted at recording.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-05

The disbursement request travels with the unmet CPs spelled out — the partner accepts
KNOWING. The internal booking gate now makes the same disclosure to the checker:
``disbursement_tranches.conditions_open`` is a point-in-time snapshot of the line's
outstanding CP/CS conditions, stamped when the tranche is RECORDED. The live chase
stays on the checklist (one source of truth); the snapshot answers, forever, "what
was open when this money was recorded and approved?" — even after the conditions
later complete. Frozen like every other tranche fact (guard extended).
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_GUARD_TMPL = """
CREATE OR REPLACE FUNCTION disbursement_tranche_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'disbursement_tranches rows are append-only';
    END IF;
    IF OLD.booking_status IS DISTINCT FROM 'Pending' THEN
        RAISE EXCEPTION
            'disbursement_tranches row % is % — a settled tranche is frozen',
            OLD.id, OLD.booking_status;
    END IF;
    IF NEW.booking_status NOT IN ('Booked', 'Rejected')
       OR NEW.tenant_id        IS DISTINCT FROM OLD.tenant_id
       OR NEW.lending_id       IS DISTINCT FROM OLD.lending_id
       OR NEW.deal_id          IS DISTINCT FROM OLD.deal_id
       OR NEW.tranche_ref      IS DISTINCT FROM OLD.tranche_ref
       OR NEW.amount           IS DISTINCT FROM OLD.amount
       OR NEW.disbursed_on     IS DISTINCT FROM OLD.disbursed_on
       OR NEW.advaya_reference IS DISTINCT FROM OLD.advaya_reference
       OR NEW.note             IS DISTINCT FROM OLD.note
       OR NEW.recorded_by      IS DISTINCT FROM OLD.recorded_by
       OR NEW.created_at       IS DISTINCT FROM OLD.created_at
       OR NEW.created_by       IS DISTINCT FROM OLD.created_by
       OR NEW.deleted_at       IS DISTINCT FROM OLD.deleted_at{extra}
    THEN
        RAISE EXCEPTION
            'disbursement_tranches row % — only a Pending booking may be settled '
            '(Booked/Rejected); the tranche facts are frozen', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_WITH_SNAPSHOT = _GUARD_TMPL.format(
    extra="\n       OR NEW.conditions_open  IS DISTINCT FROM OLD.conditions_open")
_WITHOUT_SNAPSHOT = _GUARD_TMPL.format(extra="")


def upgrade() -> None:
    op.execute("ALTER TABLE disbursement_tranches ADD COLUMN conditions_open jsonb;")
    op.execute(_WITH_SNAPSHOT)


def downgrade() -> None:
    op.execute(_WITHOUT_SNAPSHOT)
    op.execute("ALTER TABLE disbursement_tranches DROP COLUMN IF EXISTS conditions_open;")
