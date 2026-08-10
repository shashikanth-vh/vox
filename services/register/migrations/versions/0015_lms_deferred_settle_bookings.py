"""LMS is deferred: settle the bookings that were waiting for it.

The LOS→LMS seam used to leave a human-recorded disbursement PENDING until an LMS
Management approved it. With the servicing side deferred there is nobody holding that
queue, so every row in it is a line stuck at 'Ready for Disbursement' with the money
already out of the door — the worst of both readings.

This settles them the way the code now does it: the tranche is Booked, the line takes its
actuals and moves to 'Disbursed', and the stage history records that the deferral (not a
person) settled it. Attribution is preserved — ``recorded_by`` still names whoever attested
the disbursement, and ``booked_by`` says plainly that this was the deferral.

NO LOAN ACCOUNT is opened and NO CP/CS CHECKLIST is handed over, matching the deferred
behaviour: those are servicing artefacts, and handing a checklist to a desk that does not
exist is a chase that stops. The conditions stay with the origination desk.

WHEN LMS IS TURNED BACK ON, loan accounts for anything disbursed during the deferral have
to be opened from the booked tranches — they will not appear by themselves. That backfill
is a deliberate, separate step; see docs/LENDING_LOS.md.

Irreversible by design: ``downgrade`` does not un-disburse a loan. Reversing this means
deciding, per line, whether the money really moved — a judgement, not a schema change.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_BY = "system:lms-deferred"


def settle_pending_bookings(conn) -> int:  # noqa: ANN001
    """Book every Pending tranche and move its line to 'Disbursed'. Returns how many.

    A named function rather than inline SQL so it can be exercised against a real
    database with real pending rows — a data migration nobody ran against data is a
    guess, and this one moves loan stages."""
    pending = conn.execute(sa.text(
        "SELECT id, tenant_id, lending_id, tranche_ref, amount, disbursed_on "
        "FROM disbursement_tranches "
        "WHERE booking_status = 'Pending' AND deleted_at IS NULL "
        "ORDER BY created_at")).mappings().all()
    if not pending:
        return 0

    for t in pending:
        conn.execute(sa.text(
            "UPDATE disbursement_tranches SET booking_status = 'Booked', "
            "booked_by = CAST(:by AS text), booked_at = now(), "
            "updated_by = CAST(:by AS text), "
            "booking_note = COALESCE(booking_note, CAST(:note AS text)) "
            "WHERE id = CAST(:id AS uuid)"),
            {"id": t["id"], "by": _BY,
             "note": "Settled automatically: LMS servicing deferred, so the booking "
                     "queue no longer exists. The CP/CS approval upstream is the "
                     "control on this disbursement."})

    # One pass per touched line, AFTER the tranches are booked, so the actuals are the
    # sum of everything booked rather than of one row at a time.
    lines = {(t["tenant_id"], t["lending_id"]) for t in pending}
    for tenant_id, lending_id in sorted(lines):
        total = conn.execute(sa.text(
            "SELECT COALESCE(SUM(amount), 0), MIN(disbursed_on) "
            "FROM disbursement_tranches "
            # lending_id is varchar(64) on this table; lending_tracker.id is a uuid.
            # Each cast has to match ITS column or Postgres refuses the comparison.
            "WHERE tenant_id = CAST(:t AS uuid) AND lending_id = CAST(:l AS varchar) "
            "AND booking_status = 'Booked' AND deleted_at IS NULL"),
            {"t": tenant_id, "l": lending_id}).first()
        booked_total, first_on = float(total[0] or 0), total[1]
        conn.execute(sa.text(
            "UPDATE lending_tracker SET "
            # Every parameter is cast explicitly: inside jsonb_build_object() the
            # server has no column to infer a type from and refuses the statement
            # ("could not determine data type of parameter").
            "  disbursed_amount = CAST(:amt AS numeric), "
            "  disbursement_date = COALESCE(disbursement_date, CAST(:on_ AS date), "
            "                               CURRENT_DATE), "
            "  stage_history = COALESCE(stage_history, '[]'::jsonb) || "
            "                  jsonb_build_array(jsonb_build_object("
            "                    'from', stage, 'to', 'Disbursed', "
            "                    'source', 'lms-deferred-settlement', "
            "                    'by', CAST(:by AS text))), "
            "  stage = 'Disbursed', "
            "  updated_by = CAST(:by AS text) "
            "WHERE tenant_id = CAST(:t AS uuid) AND id = CAST(:l AS uuid) "
            "AND stage <> 'Disbursed'"),
            {"amt": booked_total, "on_": first_on, "by": _BY,
             "t": tenant_id, "l": lending_id})
        # A line already at 'Disbursed' still needs its actuals to include what was
        # only pending until now.
        conn.execute(sa.text(
            "UPDATE lending_tracker SET disbursed_amount = CAST(:amt AS numeric), "
            "                           updated_by = CAST(:by AS text) "
            "WHERE tenant_id = CAST(:t AS uuid) AND id = CAST(:l AS uuid) "
            "AND COALESCE(disbursed_amount, 0) <> CAST(:amt AS numeric)"),
            {"amt": booked_total, "by": _BY, "t": tenant_id, "l": lending_id})

    # The audit trail has to carry this: a stage that moved with no person behind it is
    # exactly what someone will come asking about later.
    for t in pending:
        conn.execute(sa.text(
            "INSERT INTO audit_log (tenant_id, actor, action, resource_type, "
            "                       resource_id, changes) "
            "VALUES (CAST(:t AS uuid), CAST(:by AS text), "
            "        'disbursement.tranche.approve', 'disbursement_tranches', "
            "        CAST(:rid AS varchar), CAST(:ch AS jsonb))"),
            {"t": t["tenant_id"], "by": _BY, "rid": str(t["id"]),
             "ch": json.dumps({
                 "lending_id": str(t["lending_id"]),
                 "tranche_ref": t["tranche_ref"],
                 "amount": float(t["amount"] or 0),
                 "booking_status": "Booked",
                 "reason": "LMS servicing deferred — the booking queue it waited on no "
                           "longer exists; the CP/CS approval upstream is the control "
                           "on this disbursement.",
             })})
    return len(pending)


def upgrade() -> None:
    settle_pending_bookings(op.get_bind())


def downgrade() -> None:
    """Deliberately empty. Un-booking a disbursement would claim money did not move."""
