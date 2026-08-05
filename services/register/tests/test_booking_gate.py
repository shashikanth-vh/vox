"""LMS increment ⑥ — the tranche BOOKING GATE at the LOS→LMS seam.

A human-recorded disbursement (manual attestation in LOS, or the LMS recorder for
later phases) lands as a PENDING BOOKING: no actuals, no stage move, no loan account.
The LMS Authorizer settles it — approval runs the one settlement block (actuals +
stage + account + covenant stamping) in its own transaction; rejection needs the
reason and frees the headroom. Four-eyes: the recorder can never settle their own
booking. The machine lane (service keys) still books directly — test_increment4
covers that unchanged.
"""

from __future__ import annotations

import pytest

from tests.test_advaya_manual import _submitted_line
from tests.test_handover import ADMIN, CREDIT_HEAD

pytestmark = pytest.mark.asyncio

OPERATOR = {"X-User-Email": "ops@evamfinance.com", "X-User-Roles": "LMS Operator"}
AUTHORIZER = {"X-User-Email": "authz@evamfinance.com", "X-User-Roles": "LMS Authorizer"}


async def _accepted_manual_line(client) -> str:  # noqa: ANN001
    """A line whose handover Advaya accepted through the MANUAL lane — the state from
    which a human records disbursement tranches."""
    lid = await _submitted_line(client)
    acc = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "accepted", "reference": "ADV-LTR/1"})
    assert acc.status_code == 201, acc.text
    return lid


async def test_booking_gate_pending_four_eyes_approve(client):
    """Record (maker) → Pending in the queue → the recorder cannot settle it →
    the LMS Authorizer approves → account opens, stage moves, actuals land."""
    lid = await _accepted_manual_line(client)
    # Sanction terms so the account header has a rate when it opens.
    t = await client.post("/v1/internal/sanction-terms", json={
        "lending_id": lid, "amount_cr": 8.0, "rate_kind": "Fixed", "rate_pct": 14.0,
        "day_count": "365"}, headers=ADMIN)
    assert t.status_code == 201, t.text

    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9001",
                                  "amount_cr": 5.0, "disbursed_on": "2026-08-03"})
    assert dis.status_code == 201, dis.text
    tid = dis.json()["tranche"]["id"]
    assert dis.json()["tranche"]["booking_status"] == "Pending"

    # Nothing moved: no account, stage held, schedule shows the pending slice.
    assert (await client.get(f"/v1/lending/{lid}/loan-account",
                             headers=ADMIN)).status_code == 404
    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    assert sched["total_disbursed"] == 0.0 and sched["total_pending"] == 5.0
    assert sched["remaining"] == 3.0
    assert sched["items"][0]["booking_status"] == "Pending"

    # The queue shows it, whole-book, with the line context attached.
    q = (await client.get("/v1/bookings/pending", headers=AUTHORIZER)).json()
    assert q["count"] >= 1
    mine = next(i for i in q["items"] if i["id"] == tid)
    assert mine["stage"] == "Ready for Disbursement" and mine["entity_id"]

    # FOUR-EYES: the credit head recorded it — even holding the authorize authority,
    # they cannot settle their own booking.
    own = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                            json={"action": "approve"}, headers=CREDIT_HEAD)
    assert own.status_code == 422 and "four-eyes" in own.text.lower()

    ok = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                           json={"action": "approve", "note": "UTR verified."},
                           headers=AUTHORIZER)
    assert ok.status_code == 200, ok.text
    assert ok.json()["booking_status"] == "Booked"

    # The settlement ran: stage, actuals, and the loan account with its ledger row.
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Disbursed" and float(line["disbursed_amount"]) == 5.0
    body = (await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)).json()
    assert body["account"]["amount"] == 5.0 and body["account"]["rate_pct"] == 14.0
    assert body["entries"][0]["entry_type"] == "Disbursement"

    # A settled booking is frozen — the database itself refuses a second settlement.
    again = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                              json={"action": "reject", "note": "x"},
                              headers=AUTHORIZER)
    assert again.status_code == 409


async def test_booking_rejection_needs_reason_and_frees_headroom(client):
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9100",
                                  "amount_cr": 8.0})
    assert dis.status_code == 201, dis.text
    tid = dis.json()["tranche"]["id"]

    # While the full-ceiling recording is pending, a second recording is refused.
    full = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                             json={"event": "disbursed", "reference": "UTR-9101",
                                   "amount_cr": 1.0})
    assert full.status_code == 422 and "exceed" in full.text.lower()

    # A rejection without the reason is refused; with it, the row settles 'Rejected'.
    bare = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                             json={"action": "reject"}, headers=AUTHORIZER)
    assert bare.status_code == 422 and "reason" in bare.text.lower()
    rej = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                            json={"action": "reject",
                                  "note": "Amount does not match the UTR."},
                            headers=AUTHORIZER)
    assert rej.status_code == 200 and rej.json()["booking_status"] == "Rejected"

    # Nothing moved, the headroom is free again, and the corrected figure lands fresh.
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Ready for Disbursement"
    redo = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                             json={"event": "disbursed", "reference": "UTR-9102",
                                   "amount_cr": 5.0, "disbursed_on": "2026-08-04"})
    assert redo.status_code == 201, redo.text
    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    # The rejected recording keeps its row (no number); the fresh one is T1.
    by_ref = {i["tranche_ref"]: i for i in sched["items"]}
    assert by_ref["UTR-9100"]["tranche_no"] is None
    assert by_ref["UTR-9102"]["tranche_no"] == "T1"
    assert sched["total_pending"] == 5.0


async def test_later_tranches_are_recorded_in_lms_by_the_operator(client):
    """T1 through LOS books the account; T2 is recorded by the LMS OPERATOR directly
    on the servicing side — same pending gate, same authorizer."""
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9200",
                                  "amount_cr": 5.0, "disbursed_on": "2026-08-01"})
    assert dis.status_code == 201, dis.text
    t1 = dis.json()["tranche"]["id"]
    assert (await client.post(f"/v1/lending/{lid}/tranches/{t1}/book",
                              json={"action": "approve"},
                              headers=AUTHORIZER)).status_code == 200

    # The OPERATOR records T2 in LMS · Servicing — it lands Pending.
    rec = await client.post(f"/v1/lending/{lid}/tranches",
                            json={"tranche_ref": "UTR-9201", "amount": 3.0,
                                  "disbursed_on": "2026-08-05"}, headers=OPERATOR)
    assert rec.status_code == 201, rec.text
    t2 = rec.json()
    assert t2["booking_status"] == "Pending"
    assert t2["recorded_by"] == "ops@evamfinance.com"
    # The account has NOT grown yet.
    body = (await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)).json()
    assert body["account"]["amount"] == 5.0

    ok = await client.post(f"/v1/lending/{lid}/tranches/{t2['id']}/book",
                           json={"action": "approve"}, headers=AUTHORIZER)
    assert ok.status_code == 200, ok.text
    body = (await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)).json()
    assert body["account"]["amount"] == 8.0
    assert body["entries"][1]["particulars"] == "Loan Disbursement (T2)"
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert float(line["disbursed_amount"]) == 8.0
    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    assert sched["fully_disbursed"] is True and sched["total_pending"] == 0.0

    # The queue is empty again for this line.
    q = (await client.get("/v1/bookings/pending", headers=OPERATOR)).json()
    assert all(i["lending_id"] != lid for i in q["items"])
