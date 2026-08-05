"""Tranche BOOKINGS: a manually-recorded disbursement waits for the LMS Authorizer.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-05

The maker/checker gate at the LOS→LMS handoff: a HUMAN-recorded tranche (the manual
attestation lane, or the LMS recorder for later phases) lands as a PENDING BOOKING —
the money's actuals, the stage move and the loan account wait for the LMS Authorizer's
approval. The machine lane (the real Advaya integration, service keys) still books
directly: its confirmation IS the partner's system speaking.

``disbursement_tranches`` gains the booking lifecycle (Pending → Booked | Rejected)
and the append-only trigger is NARROWED the way 0006 narrowed cp_cs_checklists: the
tranche FACTS (ref, amount, dates, provenance) stay frozen forever; the only legal
update settles a *Pending* booking exactly once. Deletes stay impossible.
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# The narrowed guard: DELETE never; UPDATE only to settle a Pending booking, and only
# the booking fields (+ the standard bookkeeping columns) may move.
_NARROW = """
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
       OR NEW.deleted_at       IS DISTINCT FROM OLD.deleted_at
    THEN
        RAISE EXCEPTION
            'disbursement_tranches row % — only a Pending booking may be settled '
            '(Booked/Rejected); the tranche facts are frozen', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_ORIGINAL = """
CREATE OR REPLACE FUNCTION disbursement_tranche_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'disbursement_tranches rows are append-only';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # Existing rows are machine-confirmed facts — they were Booked the day they landed.
    op.execute("""
        ALTER TABLE disbursement_tranches
            ADD COLUMN booking_status varchar(20) NOT NULL DEFAULT 'Booked',
            ADD COLUMN booked_by      varchar(120),
            ADD COLUMN booked_at      timestamptz,
            ADD COLUMN booking_note   text;
    """)
    op.execute(_NARROW)
    # 0001 granted SELECT, INSERT only (fully append-only then); settling a booking is
    # an UPDATE the app role must now be allowed to attempt — the trigger keeps it narrow.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'register_app') THEN "
        "EXECUTE 'GRANT UPDATE ON disbursement_tranches TO register_app'; "
        "END IF; "
        "EXCEPTION WHEN insufficient_privilege THEN "
        "RAISE NOTICE 'disbursement_tranches grant to register_app skipped.'; "
        "END $$;")


def downgrade() -> None:
    op.execute(_ORIGINAL)
    op.execute("""
        ALTER TABLE disbursement_tranches
            DROP COLUMN IF EXISTS booking_note,
            DROP COLUMN IF EXISTS booked_at,
            DROP COLUMN IF EXISTS booked_by,
            DROP COLUMN IF EXISTS booking_status;
    """)
