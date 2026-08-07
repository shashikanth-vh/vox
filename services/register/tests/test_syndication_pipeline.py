"""Platform Deals lender pipeline: the ordered transition map on the nested lender
PATCH, the substance the two outcomes must carry (a decline says WHY, a sanction says
HOW MUCH), and the desk notification a terminal outcome mints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

RM = {"X-User-Email": "syn.rm@evamfinance.com", "X-User-Roles": "Syndication Head"}


async def _mandate(client: AsyncClient, code: str = "PIPE", **syn) -> str:
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": code})).json()["id"]
    r = await client.post("/v1/syndication",
                          json={"entity_id": eid, "status": "IM in Prep", **syn})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _lender(client: AsyncClient, syn_id: str, name: str = "HDFC Bank",
                  status: str = "Identified") -> dict:
    r = await client.post(f"/v1/syndication/{syn_id}/lenders",
                          json={"lender_name": name, "status": status})
    assert r.status_code == 201, r.text
    return r.json()


async def _move(client: AsyncClient, syn_id: str, lender_id: str, **body):
    return await client.patch(f"/v1/syndication/{syn_id}/lenders/{lender_id}",
                              json=body)


async def test_lender_walks_the_whole_pipeline(client: AsyncClient):
    """Identified → IM Circulated → Docs Pending → Queries Received → IP Received →
    Sanctioned, each hop legal, the sanction carrying its allocation, and the
    server-side status_history recording every hop."""
    syn_id = await _mandate(client)
    lid = (await _lender(client, syn_id))["id"]

    for st in ("IM Circulated", "Docs Pending", "Queries Received", "IP Received"):
        r = await _move(client, syn_id, lid, status=st)
        assert r.status_code == 200, f"{st}: {r.text}"

    r = await _move(client, syn_id, lid, status="Sanctioned", amount_cr=4.5,
                    note="Sanctioned at 9.8% for 7y door-to-door")
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["status"] == "Sanctioned" and float(row["amount_cr"]) == 4.5
    hops = [h.get("to") for h in (row["status_history"] or []) if h.get("to")]
    assert hops[-1] == "Sanctioned" and "IP Received" in hops


async def test_lender_cannot_skip_stages(client: AsyncClient):
    """Identified straight to Sanctioned is not a move — the 422 names the legal
    next steps so the caller learns the map, not just 'no'."""
    syn_id = await _mandate(client, code="SKIP")
    lid = (await _lender(client, syn_id))["id"]
    r = await _move(client, syn_id, lid, status="Sanctioned", amount_cr=4)
    assert r.status_code == 422, r.text
    assert "IM Circulated" in r.text


async def test_decline_requires_a_reason_and_is_terminal(client: AsyncClient):
    syn_id = await _mandate(client, code="DECL")
    lid = (await _lender(client, syn_id))["id"]

    bare = await _move(client, syn_id, lid, status="Declined")
    assert bare.status_code == 422 and "why" in bare.text.lower()

    ok = await _move(client, syn_id, lid, status="Declined",
                     note="Sector exposure cap — no renewables headroom this FY")
    assert ok.status_code == 200, ok.text

    back = await _move(client, syn_id, lid, status="Identified")
    assert back.status_code == 422 and "terminal" in back.text.lower()


async def test_sanction_requires_the_allocation(client: AsyncClient):
    syn_id = await _mandate(client, code="SANC")
    lid = (await _lender(client, syn_id, status="IP Received"))["id"]

    bare = await _move(client, syn_id, lid, status="Sanctioned")
    assert bare.status_code == 422 and "amount_cr" in bare.text

    ok = await _move(client, syn_id, lid, status="Sanctioned", amount_cr=6)
    assert ok.status_code == 200 and float(ok.json()["amount_cr"]) == 6


async def test_terminal_outcome_notifies_the_mandate_rm(client: AsyncClient):
    """The RM named on the mandate (matched by short name OR full name in the people
    directory) finds the sanction in their inbox — amount, bank and mandate in the
    title, without staring at the matrix."""
    p = await client.post("/v1/people", json={
        "name": "Priya", "full_name": "Priya Sharma", "role": "RM",
        "email": "syn.rm@evamfinance.com"})
    assert p.status_code == 201, p.text

    syn_id = await _mandate(client, code="NOTI", rm="Priya Sharma",
                            tracker_no="SYN-NOTI-1")
    lid = (await _lender(client, syn_id, name="Kotak Mahindra",
                         status="IP Received"))["id"]
    ok = await _move(client, syn_id, lid, status="Sanctioned", amount_cr=4)
    assert ok.status_code == 200, ok.text

    inbox = (await client.get("/v1/notifications", params={"unread_only": "true"},
                              headers=RM)).json()
    mine = [n for n in inbox["items"] if n["event"] == "lender.sanctioned"]
    assert mine, inbox
    assert "Kotak Mahindra" in mine[0]["title"] and "SYN-NOTI-1" in mine[0]["title"]
    assert "4" in mine[0]["title"]  # the allocation travels in the headline

    # The same outcome does not double-send (dedupe key is lender+outcome).
    # A second PATCH is a terminal-state 422 anyway — the ledger holds one row.
    again = (await client.get("/v1/notifications", headers=RM)).json()
    assert len([n for n in again["items"] if n["event"] == "lender.sanctioned"]) == 1


async def test_fi_master_seed_is_idempotent_and_respects_deletes(client: AsyncClient):
    """Bootstrap fills the lender master once; re-running adds nothing, and a lender
    the desk deleted at runtime STAYS deleted — defaults never resurrect."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    from app.seed.fi_master import DEFAULT_FI_MASTER, seed_fi_master
    from app.seed.loader import ensure_tenant

    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.execute(text("SELECT set_config('app.current_tenant', :t, false)"),
                              {"t": str(tenant_id)})
        assert await seed_fi_master(session, tenant_id) == len(DEFAULT_FI_MASTER)
        assert await seed_fi_master(session, tenant_id) == 0  # second run: no-op
        await session.commit()

    rows = (await client.get("/v1/counterparties",
                             params={"limit": 200, "with_total": True})).json()
    assert rows["total"] == len(DEFAULT_FI_MASTER)
    victim = next(r for r in rows["items"] if r["name"] == "Orix")
    assert (await client.delete(f"/v1/counterparties/{victim['id']}")).status_code in (200, 204)

    async with sm() as session:
        tenant_id = await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.execute(text("SELECT set_config('app.current_tenant', :t, false)"),
                              {"t": str(tenant_id)})
        assert await seed_fi_master(session, tenant_id) == 0  # the delete sticks
        await session.commit()
