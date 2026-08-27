"""LMS · Servicing is DEFERRED — the book ends at 'Disbursed'.

The LOS→LMS seam left every human-recorded disbursement PENDING until an LMS Management
settled it. With no servicing desk that queue has nobody in it, so a line would sit at
'Ready for Disbursement' with the money already gone — the worst of both readings.

Deferred (the default, ``REGISTER_LMS_ENABLED=false``):

* recording IS the disbursement — actuals and stage in one step, in the recorder's name;
* NO loan account and NO condition handover. Opening the account is also what hands the
  CP/CS checklist to the servicing desk, and a checklist handed to nobody is a chase that
  stops. The conditions stay with the origination desk, which keeps working them;
* later tranches are recorded the same way, on the same screen — there is nowhere else.

The gate itself is not deleted, only switched off: test_booking_gate.py runs the whole
maker/checker spec with the flag on, so the day servicing goes live it is still described.
"""

from __future__ import annotations

import pytest

from tests.test_booking_gate import AUTHORIZER, _accepted_manual_line
from tests.test_handover import ADMIN, CREDIT_HEAD

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _lms_off(monkeypatch):
    """The shipped default. Explicit here because test_booking_gate turns it on."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "lms_enabled", False)


async def test_recording_the_disbursement_is_the_disbursement(client):
    """One step: no Pending, no queue, no second pair of eyes to wait for."""
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-7001",
                                  "amount_cr": 5.0, "disbursed_on": "2026-08-03"})
    assert dis.status_code == 201, dis.text
    assert dis.json()["tranche"]["booking_status"] == "Booked"

    line = (await client.get(f"/v1/lending/{lid}")).json()
    # The fixture checklist's CS half was settled at approval, so the settlement
    # closes the book with both halves done (the simple line, CS-first ordering).
    assert line["stage"] == "CP/CS Completed"
    assert float(line["disbursed_amount"]) == 5.0

    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    assert sched["total_disbursed"] == 5.0 and sched["total_pending"] == 0.0


async def test_nothing_is_left_waiting_in_a_queue_nobody_holds(client):
    """The failure this exists to prevent: a booking queued for a role that is not
    staffed is a disbursement that never lands."""
    lid = await _accepted_manual_line(client)
    await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                      json={"event": "disbursed", "reference": "UTR-7002",
                            "amount_cr": 2.0})
    q = (await client.get("/v1/bookings/pending", headers=AUTHORIZER)).json()
    assert not [i for i in q["items"] if i["lending_id"] == lid]


async def test_the_cp_cs_checklist_stays_with_the_origination_desk(client):
    """No loan account, so no handover: CP/CS carry on where they are being worked.
    Handing the checklist to a desk that does not exist is how a chase goes quiet."""
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
                            json={"event": "disbursed", "reference": "UTR-7003",
                                  "amount_cr": 3.0})
    assert dis.status_code == 201, dis.text

    # No servicing account was opened...
    assert (await client.get(f"/v1/lending/{lid}/loan-account",
                             headers=ADMIN)).status_code == 404
    # ...and the open condition is still on the LOS checklist, being chased.
    lists = (await client.get("/v1/internal/cpcs-checklists",
                              params={"lending_id": lid}, headers=ADMIN)).json()
    latest = sorted(lists, key=lambda c: c["checklist_version"] or 0)[-1]
    keys = {i["key"]: i for i in (latest.get("items") or [])}
    assert keys["cs-noc"]["status"] == "Pending", latest
    assert keys["cs-noc"]["condition_type"] == "CS"


async def test_a_later_tranche_is_recorded_on_the_same_screen(client):
    """There is no servicing book to send the desk to for T2, so the same lane takes
    it and the actuals accumulate."""
    lid = await _accepted_manual_line(client)
    first = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                              json={"event": "disbursed", "reference": "UTR-7010",
                                    "amount_cr": 4.0})
    assert first.status_code == 201, first.text
    second = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                               json={"event": "disbursed", "reference": "UTR-7011",
                                     "amount_cr": 2.0})
    assert second.status_code == 201, second.text
    assert second.json()["tranche"]["booking_status"] == "Booked"

    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert float(line["disbursed_amount"]) == 6.0
    # CS settled at approval + the money in — the book ends at the closing milestone.
    assert line["stage"] == "CP/CS Completed"


async def test_the_ceiling_still_holds(client):
    """Dropping the queue drops a control; it must not drop the one that stops an
    over-disbursement."""
    lid = await _accepted_manual_line(client)
    ok = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                           json={"event": "disbursed", "reference": "UTR-7020",
                                 "amount_cr": 7.0})
    assert ok.status_code == 201, ok.text
    over = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                             json={"event": "disbursed", "reference": "UTR-7021",
                                   "amount_cr": 5.0})
    assert over.status_code == 422, over.text
    assert "exceed" in over.text


async def test_the_attestation_still_names_a_person(client):
    """No approver does not mean no attribution — the recorder is on the row, and the
    reference they cited is what makes it evidence rather than an assertion."""
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-7030",
                                  "amount_cr": 1.5})
    assert dis.status_code == 201, dis.text
    assert dis.json()["recorded_by"] == CREDIT_HEAD["X-User-Email"]
    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    row = next(i for i in sched["items"] if i["tranche_ref"] == "UTR-7030")
    assert row["recorded_by"] == CREDIT_HEAD["X-User-Email"]


async def test_the_migration_settles_what_was_left_in_the_queue(client, monkeypatch):
    """Migration 0015, against real pending rows.

    The deferral has to clear the queue it retires. A booking left Pending after the
    queue disappears is money out of the door on a line that still reads 'Ready for
    Disbursement' — so the migration books it, moves the line, and says in the audit
    trail that the deferral (not a person) did it."""
    import importlib

    from sqlalchemy import text

    from app.core.config import get_settings
    from app.db.session import get_sessionmaker

    # Record one the OLD way — the gate on, so it lands Pending like the rows already
    # sitting on a deployed system.
    monkeypatch.setattr(get_settings(), "lms_enabled", True)
    lid = await _accepted_manual_line(client)
    dis = await client.post(f"/v1/lending/{lid}/advaya-events", headers=CREDIT_HEAD,
                            json={"event": "disbursed", "reference": "UTR-OLD-1",
                                  "amount_cr": 4.0, "disbursed_on": "2026-08-01"})
    assert dis.status_code == 201, dis.text
    assert dis.json()["tranche"]["booking_status"] == "Pending"
    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Ready for Disbursement"      # stuck, exactly as described

    mig = importlib.import_module(
        "migrations.versions.0015_lms_deferred_settle_bookings"
        .replace("migrations.versions.0015", "migrations.versions.0015"))
    sm = get_sessionmaker()
    async with sm() as s:
        settled = await s.run_sync(lambda sync_conn: mig.settle_pending_bookings(sync_conn))
        await s.commit()
    assert settled >= 1

    line = (await client.get(f"/v1/lending/{lid}")).json()
    assert line["stage"] == "Disbursed"
    assert float(line["disbursed_amount"]) == 4.0
    assert line["disbursement_date"] == "2026-08-01"

    sched = (await client.get(f"/v1/lending/{lid}/tranches", headers=ADMIN)).json()
    row = next(i for i in sched["items"] if i["tranche_ref"] == "UTR-OLD-1")
    assert row["booking_status"] == "Booked"
    assert row["booked_by"] == "system:lms-deferred"
    assert row["recorded_by"] == CREDIT_HEAD["X-User-Email"]   # attribution survives

    # It is in the audit trail, and it is idempotent — a second run finds nothing.
    async with sm() as s:
        await s.execute(text("SET LOCAL app.tenant_id = '00000000-0000-0000-0000-000000000000'"))
        again = await s.run_sync(lambda c: mig.settle_pending_bookings(c))
        await s.commit()
    assert again == 0
