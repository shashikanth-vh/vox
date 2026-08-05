"""CS progress: receipt is recorded on the APPROVED checklist — no new version.

The CP half is a governance decision (maker-checker, frozen once decided). The CS half
is a months-long collection: each received document updates the approved row's CS items
in place, the chase reminders shrink, and no checker round-trip is asked for. CP items
and the approval's decision fields stay untouchable.
"""

from __future__ import annotations

import pytest

from tests.test_handover import ADMIN, CREDIT_HEAD, _entity

pytestmark = pytest.mark.asyncio


async def test_cs_receipt_updates_the_approved_row_and_cp_stays_frozen(client):
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [
                  {"key": "cp1", "label": "Security created", "condition_type": "CP",
                   "status": "Completed"},
                  {"key": "cs1", "label": "End-use certificate", "condition_type": "CS",
                   "status": "Pending"},
                  {"key": "cs2", "label": "Insurance endorsement", "condition_type": "CS",
                   "status": "Pending"}]},
        headers=ADMIN)
    assert chk.status_code == 201, chk.text
    cid = chk.json()["id"]

    # Progress on an UNDECIDED checklist is refused — the approved row is the record.
    early = await client.post(f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
                              json={"items": [{"key": "cs1", "status": "Completed"}]},
                              headers=ADMIN)
    assert early.status_code == 409, early.text

    assert (await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve",
                              headers=CREDIT_HEAD)).status_code == 200

    # A document arrived: cs1 completes with its evidence — in place, no new version.
    got = await client.post(
        f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
        json={"items": [{"key": "cs1", "status": "Completed",
                         "evidence_ref": "doc/end-use-1"}]},
        headers=ADMIN)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "Approved" and body["checklist_version"] == chk.json()["checklist_version"]
    by_key = {i["key"]: i for i in body["items"]}
    assert by_key["cs1"]["status"] == "Completed"
    assert by_key["cs1"]["evidence_ref"] == "doc/end-use-1"
    assert by_key["cs2"]["status"] == "Pending"          # still being chased
    assert by_key["cp1"]["status"] == "Completed"        # untouched

    # The CP half does not change through this lane.
    cp = await client.post(f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
                           json={"items": [{"key": "cp1", "status": "Pending"}]},
                           headers=ADMIN)
    assert cp.status_code == 422 and "CP" in cp.text

    # A CS obligation discovered later JOINS the record.
    extra = await client.post(
        f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
        json={"items": [{"key": "cs3", "label": "Board undertaking",
                         "status": "Pending"}]},
        headers=ADMIN)
    assert extra.status_code == 200, extra.text
    assert any(i["key"] == "cs3" and i["condition_type"] == "CS"
               for i in extra.json()["items"])
