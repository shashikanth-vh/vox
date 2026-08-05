"""LMS increment ⑥ — the tranche BOOKING GATE at the LOS→LMS seam.

A human-recorded disbursement (manual attestation in LOS, or the LMS recorder for
later phases) lands as a PENDING BOOKING: no actuals, no stage move, no loan account.
The LMS Management settles it — approval runs the one settlement block (actuals +
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
AUTHORIZER = {"X-User-Email": "authz@evamfinance.com", "X-User-Roles": "LMS Management"}
# v3.7 renamed "LMS Authorizer" → "LMS Management"; the OLD string must keep resolving
# (ROLE_ALIASES) so a grant stored before the rename never silently loses access.
LEGACY_AUTHORIZER = {"X-User-Email": "authz.legacy@evamfinance.com",
                     "X-User-Roles": "LMS Authorizer"}


async def _accepted_manual_line(client) -> str:  # noqa: ANN001
    """A line whose handover Advaya accepted through the MANUAL lane — the state from
    which a human records disbursement tranches."""
    lid = await _submitted_line(client)
    acc = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "accepted", "reference": "ADV-LTR/1"})
    assert acc.status_code == 201, acc.text
    return lid


async def test_the_renamed_role_still_answers_to_its_old_name(client):
    """A booking settles under the PRE-RENAME role string: the alias resolves it to
    LMS Management, so nothing granted earlier breaks. New grants validate against
    the current catalogue only (Access refuses 'LMS Authorizer' on creation)."""
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9500",
                                  "amount_cr": 2.0})
    assert dis.status_code == 201, dis.text
    tid = dis.json()["tranche"]["id"]
    ok = await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                           json={"action": "approve"}, headers=LEGACY_AUTHORIZER)
    assert ok.status_code == 200, ok.text
    assert ok.json()["booking_status"] == "Booked"


async def test_lms_roles_read_the_line_whole_book_but_cannot_edit_it(client):
    """v3.6: the servicing pair's lending VIEW stops at READ. They see the whole book
    (no assignment scoping), but the origination row itself refuses their writes —
    their verbs are the servicing ones (ledger, bookings, covenants, classification),
    and the LOS screen being read-only is now matched by the API."""
    lid = await _accepted_manual_line(client)
    for who in (OPERATOR, AUTHORIZER):
        seen = await client.get(f"/v1/lending/{lid}", headers=who)
        assert seen.status_code == 200, seen.text
        edit = await client.patch(f"/v1/lending/{lid}",
                                  json={"remarks": "servicing note"}, headers=who)
        assert edit.status_code == 403, edit.text


async def test_booking_gate_pending_four_eyes_approve(client):
    """Record (maker) → Pending in the queue → the recorder cannot settle it →
    the LMS Management approves → account opens, stage moves, actuals land."""
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


async def test_the_booking_snapshots_the_open_conditions_and_keeps_them(client):
    """The disclosure travels with the recording: the tranche carries the line's open
    CP/CS conditions as of that moment. The live chase then clears on the checklist —
    the booking's snapshot does NOT: it stays what the checker saw and accepted."""
    lid = await _accepted_manual_line(client)
    lists = (await client.get("/v1/internal/cpcs-checklists",
                              params={"lending_id": lid}, headers=ADMIN)).json()
    approved = sorted([c for c in lists if c["status"] == "Approved"],
                      key=lambda c: c["checklist_version"] or 0)[-1]
    # A CS obligation joins the approved record, still open when the money moves.
    add = await client.post(
        f"/v1/internal/cpcs-checklists/{approved['id']}/cs-progress",
        json={"items": [{"key": "cs-insurance", "label": "Insurance endorsement",
                         "status": "Pending"}]}, headers=ADMIN)
    assert add.status_code == 200, add.text

    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9300",
                                  "amount_cr": 4.0})
    assert dis.status_code == 201, dis.text
    t = dis.json()["tranche"]
    assert any(c["key"] == "cs-insurance" for c in t["conditions_open"]), t

    # The document arrives — the LIVE chase clears on the checklist…
    done = await client.post(
        f"/v1/internal/cpcs-checklists/{approved['id']}/cs-progress",
        json={"items": [{"key": "cs-insurance", "status": "Completed",
                         "evidence_ref": "doc/endt-9"}]}, headers=ADMIN)
    assert done.status_code == 200, done.text

    # …but the booking still says what was open when it was recorded, and the
    # approval carries that context into the permanent record.
    ok = await client.post(f"/v1/lending/{lid}/tranches/{t['id']}/book",
                           json={"action": "approve"}, headers=AUTHORIZER)
    assert ok.status_code == 200, ok.text
    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    mine = next(i for i in sched["items"] if i["tranche_ref"] == "UTR-9300")
    assert any(c["key"] == "cs-insurance" for c in mine["conditions_open"])


async def test_the_checklist_hands_over_to_lms_at_account_opening(client):
    """The LOS→LMS handover: when the booking approval opens the account, the COMPLETE
    checklist (completed and open items alike) becomes the account's own register.
    The checklist freezes into a decision record; the servicing OPERATOR retires
    obligations on the LMS copy; the follow-up feed reads the LMS copy; new
    obligations join the account directly — no dependency back on LOS."""
    lid = await _accepted_manual_line(client)
    lists = (await client.get("/v1/internal/cpcs-checklists",
                              params={"lending_id": lid}, headers=ADMIN)).json()
    approved = sorted([c for c in lists if c["status"] == "Approved"],
                      key=lambda c: c["checklist_version"] or 0)[-1]
    add = await client.post(
        f"/v1/internal/cpcs-checklists/{approved['id']}/cs-progress",
        json={"items": [{"key": "cs-noc", "label": "NOC from existing lender",
                         "status": "Pending"}]}, headers=ADMIN)
    assert add.status_code == 200, add.text

    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-9400",
                                  "amount_cr": 3.0})
    assert dis.status_code == 201, dis.text
    tid = dis.json()["tranche"]["id"]
    assert (await client.post(f"/v1/lending/{lid}/tranches/{tid}/book",
                              json={"action": "approve"},
                              headers=AUTHORIZER)).status_code == 200

    # The account's register holds the COMPLETE handover — decided CPs included.
    reg = await client.get(f"/v1/lending/{lid}/loan-account/conditions",
                           headers=OPERATOR)
    assert reg.status_code == 200, reg.text
    body = reg.json()
    by_key = {c["key"]: c for c in body["items"]}
    assert by_key["cs-noc"]["status"] == "Pending"
    assert by_key["cp1"]["status"] == "Completed"        # history travels too
    assert body["open"] == 1

    # The checklist is FROZEN now — CS progress points at the servicing book.
    frozen = await client.post(
        f"/v1/internal/cpcs-checklists/{approved['id']}/cs-progress",
        json={"items": [{"key": "cs-noc", "status": "Completed"}]}, headers=ADMIN)
    assert frozen.status_code == 409 and "servicing book" in frozen.text

    # The follow-up feed reads the LMS register for this line.
    fu = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    mine = [i for i in fu["items"]
            if i["kind"] == "cs-followup" and i["lending_id"] == lid]
    assert len(mine) == 1 and mine[0]["outstanding"] == ["NOC from existing lender"]

    # The OPERATOR retires the obligation on the LMS's own record — once.
    rec = await client.post(
        f"/v1/lending/{lid}/loan-account/conditions/cs-noc/receive",
        json={"evidence_ref": "doc/noc-1"}, headers=OPERATOR)
    assert rec.status_code == 200 and rec.json()["status"] == "Completed"
    assert rec.json()["completed_by"] == "ops@evamfinance.com"
    dup = await client.post(
        f"/v1/lending/{lid}/loan-account/conditions/cs-noc/receive",
        json={}, headers=OPERATOR)
    assert dup.status_code == 409
    fu = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    assert not any(i["kind"] == "cs-followup" and i["lending_id"] == lid
                   for i in fu["items"])

    # A later obligation joins the ACCOUNT's register and the chase resumes there.
    addc = await client.post(
        f"/v1/lending/{lid}/loan-account/conditions",
        json={"key": "insurance-renewal", "label": "Insurance renewal",
              "expiry_date": "2026-09-30"}, headers=OPERATOR)
    assert addc.status_code == 201, addc.text
    fu = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    mine = [i for i in fu["items"]
            if i["kind"] == "cs-followup" and i["lending_id"] == lid]
    assert len(mine) == 1 and mine[0]["outstanding"] == ["Insurance renewal"]
    assert mine[0].get("source") == "lms"


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
