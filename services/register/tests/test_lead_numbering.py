"""The register mints the lead number, and it continues the DESK'S sequence.

Reported from the desk: import the ledger, then add a lead — "The write violates a
database constraint". The browser was guessing the number (row COUNT plus one, stepping
up on each 409), which only works while the numbers run unbroken from 1. An imported
ledger holds 201 leads numbered to LD-210, so the guess opened at LD-202, walked into
the taken ones and gave up — on a company that had nothing wrong with it.

The allocator has always existed for exactly this; it was scanning for the wrong prefix
("L-"), so it could not see a single LD-207 and would have restarted the book at L-0001.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _lead(client: AsyncClient, **extra) -> dict:
    body = {"company": f"Numbering Co {uuid.uuid4().hex[:6]}", **extra}
    r = await client.post("/v1/leads", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_the_register_allocates_a_lead_number(client: AsyncClient):
    """No lead_no from the caller: the row still comes back quotable."""
    row = await _lead(client)
    assert str(row["lead_no"]).startswith("LD-"), row["lead_no"]


async def test_it_continues_the_imported_sequence_rather_than_restarting(client: AsyncClient):
    """The desk's ledger numbers past the row count. The next number has to be the next
    one the DESK would say, not the next free slot in some other series."""
    assert (await _lead(client, lead_no="LD-207"))["lead_no"] == "LD-207"
    assert (await _lead(client, lead_no="LD-210"))["lead_no"] == "LD-210"
    assert (await _lead(client))["lead_no"] == "LD-211"


async def test_a_number_the_caller_supplies_still_wins(client: AsyncClient):
    """Import and migration paths carry their own numbers — the allocator must not
    overrule them."""
    assert (await _lead(client, lead_no="LD-999"))["lead_no"] == "LD-999"


async def test_the_number_is_never_reused_after_the_highest_is_deleted(client: AsyncClient):
    """A number someone has quoted must not come back on a different company."""
    high = await _lead(client, lead_no="LD-500")
    assert (await client.delete(f"/v1/leads/{high['id']}")).status_code in (200, 204)
    nxt = await _lead(client)
    assert nxt["lead_no"] != "LD-500", "a deleted row's number was handed out again"


async def test_two_leads_in_a_row_do_not_collide(client: AsyncClient):
    """The guard the advisory lock exists for — and the failure the desk actually saw."""
    seen = {(await _lead(client))["lead_no"] for _ in range(5)}
    assert len(seen) == 5, seen
