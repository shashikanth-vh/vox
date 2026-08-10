"""'CP/CS Completed' means BOTH halves are done.

The CP approval settles the CP half and mints the evidence that unblocks disbursement.
It used to also stamp the line 'CP/CS Completed', which claimed the conditions
subsequent were satisfied while the desk was still chasing them — sometimes for months.

The rule this pins: before disbursement, a line reaches 'CP/CS Completed' only when
nothing is open on either half; a line whose CS are still live disburses on the evidence
and its terminal is 'Disbursed'.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from tests.test_maker_actions import _Http, _app, _get

pytestmark = pytest.mark.asyncio

LID = "22222222-2222-2222-2222-222222222222"


class _Register(_Http):
    """Serves the line, the checklist approval, the minted evidence — and RECORDS every
    PATCH, which is where the stage move would show up."""

    def __init__(self, row: dict, items: list[dict]) -> None:
        super().__init__(row)
        self.items = items
        self.patches: list[dict] = []

    async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
        u = str(url)
        if "/approve" in u:
            return httpx.Response(200, request=httpx.Request("POST", u), json={
                "id": "c1", "lending_id": LID, "checklist_version": 1,
                "status": "Approved", "items": self.items})
        if "/v1/evidence" in u:
            return httpx.Response(201, request=httpx.Request("POST", u), json={"id": "e1"})
        return httpx.Response(200, request=httpx.Request("POST", u), json={})

    async def patch(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.patches.append(dict(kwargs.get("json") or {}))
        return httpx.Response(200, request=httpx.Request("PATCH", str(url)), json=self.row)


async def _approve(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://orch") as c:
        return await c.post("/v1/workflows/cpcs-checklists/c1/approve",
                            json={"approved_by": "ch@evamfinance.com"},
                            headers={"X-API-Key": "k", "X-User-Email": "ch@evamfinance.com",
                                     "X-User-Roles": "Credit Head"})


async def test_an_open_cs_keeps_the_line_out_of_cp_cs_completed(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    reg = _Register({"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cs-noc", "label": "NOC from lender", "condition_type": "CS",
         "status": "Pending"},
    ])
    app.state.http = reg
    r = await _approve(app)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cp_cs_completion"] == "e1", "the evidence still mints — disbursement is unblocked"
    assert not reg.patches, f"the stage must not move: {reg.patches}"
    assert "CP approved" in body["next"] and "NOC from lender" in body["next"]
    get_settings.cache_clear()


async def test_both_halves_satisfied_moves_the_line(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    reg = _Register({"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cs-noc", "condition_type": "CS", "status": "Completed"},
    ])
    app.state.http = reg
    r = await _approve(app)
    assert r.status_code == 200, r.text
    assert reg.patches == [{"stage": "CP/CS Completed"}]
    assert r.json()["stage"] == "CP/CS Completed"
    get_settings.cache_clear()


async def test_a_waived_cs_is_not_an_open_one(monkeypatch):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    reg = _Register({"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cs-x", "condition_type": "CS", "status": "Waived"},
    ])
    app.state.http = reg
    assert (await _approve(app)).status_code == 200
    assert reg.patches == [{"stage": "CP/CS Completed"}]
    get_settings.cache_clear()


async def test_a_sanctioned_line_is_still_offered_the_disburse_verb(monkeypatch):
    """The consequence of the rule: a line with a live CS chase never reaches
    'CP/CS Completed', so Disburse has to be reachable from 'Sanctioned' or the money
    could never move."""
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _Http({"id": LID, "stage": "Sanctioned"})
    r = await _get(app, {"subject_type": "Lending", "subject_id": LID})
    assert r.status_code == 200, r.text
    keys = {a["key"] for a in r.json()["actions"]}
    assert "disburse" in keys
    get_settings.cache_clear()


class _WithChecklist(_Http):
    """The line plus one checklist version, so the action counts have something to read."""

    def __init__(self, row: dict, items: list[dict], status: str = "Approved") -> None:
        super().__init__(row)
        self.items = items
        self.status = status

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        if "cpcs-checklists" in str(url):
            return httpx.Response(200, request=httpx.Request("GET", str(url)), json={
                "items": [{"checklist_version": 1, "status": self.status,
                           "items": self.items}]})
        return await super().get(url, **kwargs)


async def _actions(monkeypatch, row, items, status="Approved"):
    app = _app(monkeypatch, WORKFLOWS_API_KEYS="k")
    app.state.http = _WithChecklist(row, items, status)
    r = await _get(app, {"subject_type": "Lending", "subject_id": LID})
    assert r.status_code == 200, r.text
    return {a["key"]: a for a in r.json()["actions"]}


async def test_a_finished_half_stops_inviting_a_click(monkeypatch):
    """A satisfied checklist is a completed step, not an open one — and re-opening it is
    how a settled condition gets accidentally re-typed."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cs1", "condition_type": "CS", "status": "Completed"},
        {"key": "cs2", "condition_type": "CS", "status": "Pending"},
    ])
    cp, cs = by_key["cpcs.prepare"], by_key["cpcs.update-cs"]
    # The CP is shut by its APPROVAL, which outranks the item count and says so —
    # see test_the_cp_step_closes_on_its_approval_not_its_item_count.
    assert cp["label"].endswith("(1/1)") and cp["enabled"] is False
    # The CS half still has one open — it stays workable.
    assert cs["label"].endswith("(1/2)") and cs["enabled"] is True
    get_settings.cache_clear()


async def test_a_satisfied_cs_half_stops_inviting_a_click(monkeypatch):
    """The count rule, on the half it still governs: CS has no approval to close it, so
    "nothing left open" is what shuts it."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "Disbursed"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cs1", "condition_type": "CS", "status": "Waived"},
    ])
    cs = by_key["cpcs.update-cs"]
    assert cs["enabled"] is False and "satisfied" in cs["reason"]
    assert by_key["cpcs.prepare"]["enabled"] is False
    get_settings.cache_clear()


async def test_disburse_closes_only_when_the_line_is_fully_drawn(monkeypatch):
    """It must stay live at 'Disbursed' — that is where T2, T3 … are recorded — so the
    test is the money, not the stage."""
    part = await _actions(monkeypatch, {
        "id": LID, "stage": "Disbursed", "disbursed_amount": 4,
        "proposed_disbursement_amount": 10}, [])
    assert part["disburse"]["enabled"] is True, "4 of 10 Cr drawn — T2 still to record"

    full = await _actions(monkeypatch, {
        "id": LID, "stage": "Disbursed", "disbursed_amount": 10,
        "proposed_disbursement_amount": 10}, [])
    assert full["disburse"]["enabled"] is False
    assert "Fully disbursed" in full["disburse"]["reason"]
    get_settings.cache_clear()


async def test_the_cp_step_closes_on_its_approval_not_its_item_count(monkeypatch):
    """The checker's approval IS the decision — it minted the evidence the money moves
    on. Re-opening the preparer's screen afterwards invites a settled condition to be
    re-typed into a version nobody asked for."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cp2", "condition_type": "CP", "status": "Pending"},
    ], status="Approved")
    cp = by_key["cpcs.prepare"]
    assert cp["label"].endswith("(1/2)"), "still shows the true tally"
    assert cp["enabled"] is False
    assert "approved" in cp["reason"]
    get_settings.cache_clear()


async def test_a_returned_checklist_reopens_the_cp_step(monkeypatch):
    """Which is the whole point of a return: the maker has to be able to re-prepare."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "Sanctioned"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Pending"},
    ], status="Returned")
    assert by_key["cpcs.prepare"]["enabled"] is True
    # And the CS half stays shut until a CP approval exists.
    assert by_key["cpcs.update-cs"]["enabled"] is False
    get_settings.cache_clear()


async def test_a_cp_deferred_as_a_cs_lands_on_the_cs_half(monkeypatch):
    """The gap that shut both buttons on a line Today was still chasing.

    'Deferred as CS' converts a CP into a post-disbursement obligation, but the item
    keeps `condition_type: CP` until its first CS progress. Counting each half by that
    field alone made the item belong to NEITHER: the CP counted it done (correctly — it
    is decided as a CP), the CS never saw it, both halves read complete and locked, and
    the follow-up feed — which counts anything not Completed or Waived — kept reporting
    it outstanding with nowhere to go and work it."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "CP/CS Completed"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cp2", "condition_type": "CP", "status": "Deferred as CS"},
        {"key": "cp3", "condition_type": "CP", "status": "Deferred as CS"},
        {"key": "cs1", "condition_type": "CS", "status": "Completed"},
        {"key": "cs2", "condition_type": "CS", "status": "Completed"},
    ], status="Approved")
    cp, cs = by_key["cpcs.prepare"], by_key["cpcs.update-cs"]
    # As CPs they ARE decided — the deferral is the decision.
    assert cp["label"].endswith("(3/3)")
    # As CS obligations they are OPEN, and the tab that works them stays open with them.
    assert cs["label"].endswith("(2/4)"), cs["label"]
    assert cs["enabled"] is True, cs.get("reason")


async def test_a_deferred_item_that_has_since_been_worked_counts_once(monkeypatch):
    """First CS progress flips the item's type to CS. It must not then be counted
    twice — once as a CS and once as a leftover deferral."""
    by_key = await _actions(monkeypatch, {"id": LID, "stage": "Disbursed"}, [
        {"key": "cp1", "condition_type": "CP", "status": "Completed"},
        {"key": "cp2", "condition_type": "CS", "status": "Deferred as CS",
         "deferred_from": "CP"},
    ], status="Approved")
    assert by_key["cpcs.update-cs"]["label"].endswith("(0/1)")
