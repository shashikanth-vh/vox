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


async def test_a_deferred_cp_completes_its_lifecycle_through_cs_progress(client):
    """A CP 'Deferred as CS' (senior authority, reason + expiry) is an OBLIGATION, not
    a frozen decision: when its document finally arrives, CS progress retires it — the
    item converts to CS (provenance kept) and the chase reminder goes quiet."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [
                  {"key": "cp1", "label": "Security created", "condition_type": "CP",
                   "status": "Completed"},
                  {"key": "cp2", "label": "Insurance policy assigned",
                   "condition_type": "CP", "status": "Deferred as CS",
                   "reason": "Insurer needs 30 days; disbursement approved without.",
                   "expiry_date": "2026-09-04"}]},
        headers=CREDIT_HEAD)
    assert chk.status_code == 201, chk.text
    cid = chk.json()["id"]
    assert (await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve",
                              headers=ADMIN)).status_code == 200

    # The deferred item is still being CHASED on the approved checklist.
    fu = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    mine = next(i for i in fu["items"]
                if i["kind"] == "cs-followup" and i["lending_id"] == lid)
    assert "Insurance policy assigned" in mine["outstanding"]

    # The policy arrives → CS progress retires the deferred CP, in place.
    got = await client.post(
        f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
        json={"items": [{"key": "cp2", "status": "Completed",
                         "evidence_ref": "doc/insurance-endt-1"}]},
        headers=ADMIN)
    assert got.status_code == 200, got.text
    by_key = {i["key"]: i for i in got.json()["items"]}
    assert by_key["cp2"]["status"] == "Completed"
    assert by_key["cp2"]["condition_type"] == "CS"      # the conversion made literal
    assert by_key["cp2"]["deferred_from"] == "CP"       # provenance kept
    assert by_key["cp1"]["status"] == "Completed"       # the decided CP half untouched

    # The chase for this line is over.
    fu = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    assert not any(i["kind"] == "cs-followup" and i["lending_id"] == lid
                   for i in fu["items"])

    # A decided (non-deferred) CP still cannot be reopened through this lane.
    cp = await client.post(f"/v1/internal/cpcs-checklists/{cid}/cs-progress",
                           json={"items": [{"key": "cp1", "status": "Pending"}]},
                           headers=ADMIN)
    assert cp.status_code == 422 and "CP" in cp.text


async def test_follow_ups_scope_to_a_persons_own_book(client):
    """``scope_email``: an IC's reminders are their book, not the tenant's — the item
    stays when they PREPARED the checklist or the line names them as RM/analyst;
    an unrelated IC sees nothing (Today says "on your book" and must mean it)."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence",
                                   "rm": "Chasey"})).json()["id"]
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": lid, "status": "Completed",
              "items": [{"key": "cs1", "label": "End-use certificate",
                         "condition_type": "CS", "status": "Pending"}]},
        headers=ADMIN)
    assert chk.status_code == 201, chk.text
    assert (await client.post(f"/v1/internal/cpcs-checklists/{chk.json()['id']}/approve",
                              headers=CREDIT_HEAD)).status_code == 200

    def mine(body):  # noqa: ANN001
        return [i for i in body["items"]
                if i["kind"] == "cs-followup" and i["lending_id"] == lid]

    # Unscoped (heads, servicing desk): the item is there.
    assert mine((await client.get("/v1/internal/follow-ups", headers=ADMIN)).json())
    # The PREPARER keeps their own chase.
    assert mine((await client.get(
        "/v1/internal/follow-ups",
        params={"scope_email": "admin@evamfinance.com"}, headers=ADMIN)).json())
    # An unrelated IC sees nothing of it.
    assert not mine((await client.get(
        "/v1/internal/follow-ups",
        params={"scope_email": "stranger@evamfinance.com"}, headers=ADMIN)).json())
    # The line's RM (resolved through the people roster) sees it.
    p = await client.post("/v1/people", json={
        "name": "Chasey", "full_name": "Chasey Bookman", "role": "RM",
        "email": "chasey@evamfinance.com"}, headers=ADMIN)
    assert p.status_code == 201, p.text
    assert mine((await client.get(
        "/v1/internal/follow-ups",
        params={"scope_email": "chasey@evamfinance.com"}, headers=ADMIN)).json())
