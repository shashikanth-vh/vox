"""A CP/CS checklist waiting on its checker is CHASED, never expired.

Every other approval in the lending flow parks a durable run with its own clock: SLA
reminders, an escalation, and a hard deadline that ends the run as TimedOut. The CP/CS
checklist has none of that — the workflow files it, tells the checkers, and returns. So a
checklist left at 'Completed' waits indefinitely, and an unapproved checklist blocks
disbursement with nothing anywhere counting.

Expiring it would be worse: a timed-out checklist discards prepared work and walks the
line backwards, and unlike a committee decision there is no external deadline it answers
to. So it is chased instead — it appears in the follow-up feed from the moment it is
filed and turns escalated once it has waited 72 hours, the point the parked runs escalate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.test_handover import ADMIN, CREDIT_HEAD, _entity, _ready_lending

pytestmark = pytest.mark.asyncio


async def _filed_checklist(client: AsyncClient) -> tuple[str, str]:
    """A line with a checklist FILED and awaiting its checker."""
    lid = await _ready_lending(client, await _entity(client))
    r = await client.post("/v1/internal/cpcs-checklists", json={
        "lending_id": lid, "checklist_version": 9, "status": "Completed",
        "items": [{"key": "cp-new", "label": "Board resolution",
                   "condition_type": "CP", "status": "Completed", "required": True}]},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    return lid, r.json()["id"]


def _rows(body: dict, lending_id: str) -> list[dict]:
    return [i for i in body["items"]
            if i["kind"] == "cpcs-approval" and i["lending_id"] == lending_id]


async def test_a_filed_checklist_is_chased_from_the_moment_it_is_filed(client: AsyncClient):
    lid, cid = await _filed_checklist(client)
    body = (await client.get("/v1/internal/follow-ups", headers=ADMIN)).json()
    mine = _rows(body, lid)
    assert len(mine) == 1, body
    assert mine[0]["checklist_id"] == cid
    assert mine[0]["checklist_version"] == 9
    assert mine[0]["escalated"] is False, "fresh — chased, not yet urgent"


async def test_it_escalates_after_seventy_two_hours(client: AsyncClient):
    """The same point a parked run escalates at."""
    lid, cid = await _filed_checklist(client)
    from app.db.session import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text("UPDATE cp_cs_checklists SET created_at = :t WHERE id = :i"),
                        {"t": datetime.now(UTC) - timedelta(hours=80), "i": cid})
        await s.commit()
    mine = _rows((await client.get("/v1/internal/follow-ups", headers=ADMIN)).json(), lid)
    assert len(mine) == 1
    assert mine[0]["escalated"] is True
    assert mine[0]["waiting_hours"] >= 72


async def test_approving_it_ends_the_chase(client: AsyncClient):
    """The way out has to actually work — otherwise the reminder is just noise."""
    lid, cid = await _filed_checklist(client)
    assert _rows((await client.get("/v1/internal/follow-ups", headers=ADMIN)).json(), lid)
    ok = await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve", headers=CREDIT_HEAD)
    assert ok.status_code == 200, ok.text
    assert not _rows((await client.get("/v1/internal/follow-ups", headers=ADMIN)).json(), lid)


async def test_a_returned_checklist_is_not_chased_as_awaiting_approval(client: AsyncClient):
    """A return puts the ball back with the MAKER. Chasing the checker for it would send
    the reminder to the one person who cannot act on it."""
    lid, cid = await _filed_checklist(client)
    r = await client.post(f"/v1/internal/cpcs-checklists/{cid}/return",
                          json={"note": "name the security documents"}, headers=CREDIT_HEAD)
    assert r.status_code == 200, r.text
    assert not _rows((await client.get("/v1/internal/follow-ups", headers=ADMIN)).json(), lid)
