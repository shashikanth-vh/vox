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
    """Identified → IM Circulated → Queries Received → IP Received → Sanctioned,
    each hop legal, the sanction carrying its allocation, and the server-side
    status_history recording every hop."""
    syn_id = await _mandate(client)
    lid = (await _lender(client, syn_id))["id"]

    for st in ("IM Circulated", "Queries Received", "IP Received"):
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


async def test_lender_never_moves_backwards(client: AsyncClient):
    """Forward-only (review decision): once the IM is out, a bank cannot fall back
    to Identified; once queries landed, it cannot un-receive them."""
    syn_id = await _mandate(client, code="BACK")
    lid = (await _lender(client, syn_id, status="Queries Received"))["id"]
    for st in ("Identified", "IM Circulated"):
        r = await _move(client, syn_id, lid, status=st)
        assert r.status_code == 422, f"{st}: {r.text}"


ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}


async def test_admin_corrects_a_mistaken_status_and_removes_a_mistaken_add(client: AsyncClient):
    """A mis-click is not a business event. The desk cannot move backward (above),
    but an ADMIN can correct any state to any canonical state — the history keeps
    the correction with the actor — and can remove a lender that was added by
    mistake. The vocabulary still bounds the correction, and the desk-role refusal
    now points at the Admin lane."""
    syn_id = await _mandate(client, code="OOPS")
    lid = (await _lender(client, syn_id, status="IM Under Preparation"))["id"]

    # The desk (non-admin human) is still refused — and told an Admin can fix it.
    r = await _move(client, syn_id, lid, status="Identified")
    assert r.status_code == 422 and "Admin can correct" in r.text

    # Admin walks it BACK — the correction lands, canonically, with history.
    r = await client.patch(f"/v1/syndication/{syn_id}/lenders/{lid}",
                           json={"status": "Identified"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Identified"
    assert body["status_history"][-1]["to"] == "Identified"
    assert body["status_history"][-1]["by"] == "admin@evamfinance.com"

    # Even Admin stays inside the vocabulary.
    r = await client.patch(f"/v1/syndication/{syn_id}/lenders/{lid}",
                           json={"status": "Sideways"}, headers=ADMIN)
    assert r.status_code == 422 and "not a lender status" in r.text

    # A correction INTO an outcome still carries its substance.
    r = await client.patch(f"/v1/syndication/{syn_id}/lenders/{lid}",
                           json={"status": "Declined"}, headers=ADMIN)
    assert r.status_code == 422 and "say why" in r.text

    # Mistaken add: Admin removes the row; a desk role may not.
    lid2 = (await _lender(client, syn_id, name="Wrong Bank"))["id"]
    r = await client.delete(f"/v1/syndication/{syn_id}/lenders/{lid2}", headers=RM)
    assert r.status_code == 403, r.text
    r = await client.delete(f"/v1/syndication/{syn_id}/lenders/{lid2}", headers=ADMIN)
    assert r.status_code == 204, r.text
    names = [x["lender_name"] for x in
             (await client.get(f"/v1/syndication/{syn_id}/lenders")).json()]
    assert "Wrong Bank" not in names and "HDFC Bank" in names


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
    # One vocabulary with the Lending book: the word is "Sanctioned" everywhere.
    assert "Sanctioned" in mine[0]["title"]
    assert "Kotak Mahindra" in mine[0]["title"] and "SYN-NOTI-1" in mine[0]["title"]
    assert "4" in mine[0]["title"]  # the allocation travels in the headline

    # The same outcome does not double-send (dedupe key is lender+outcome).
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


# --------------------------------------------- the field vocabulary, adopted


async def test_an_imported_spelling_moves_without_waiting_for_normalisation(client: AsyncClient):
    """The Excel books wrote "IM in Prep"; those rows sat LOCKED (an unknown status
    has no next steps). The API canonicalises BOTH sides of the transition check, so
    a stuck import comes alive — and the move stores the canonical spelling."""
    syn_id = await _mandate(client, code="ALIA")
    lid = (await _lender(client, syn_id, status="IM in Prep"))["id"]
    r = await _move(client, syn_id, lid, status="IM Circulated")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IM Circulated"


async def test_on_hold_anytime_and_resume_remembers(client: AsyncClient):
    """On Hold is settable from any live status; the resume goes back to work (the
    status it left is first in the history), and On Hold itself never dead-ends."""
    syn_id = await _mandate(client, code="HOLD")
    lid = (await _lender(client, syn_id, status="Queries Received"))["id"]
    r = await _move(client, syn_id, lid, status="On Hold")
    assert r.status_code == 200, r.text
    hops = r.json()["status_history"] or []
    assert hops[-1]["to"] == "On Hold" and hops[-1]["from"] == "Queries Received"
    back = await _move(client, syn_id, lid, status="Queries Received")
    assert back.status_code == 200, back.text


async def test_dropped_anytime_needs_its_why_and_is_terminal(client: AsyncClient):
    syn_id = await _mandate(client, code="DROP")
    lid = (await _lender(client, syn_id, status="IM Circulated"))["id"]
    bare = await _move(client, syn_id, lid, status="Dropped")
    assert bare.status_code == 422 and "why" in bare.text.lower()
    ok = await _move(client, syn_id, lid, status="Dropped",
                     note="Client chose another bank")
    assert ok.status_code == 200, ok.text
    stuck = await _move(client, syn_id, lid, status="Identified")
    assert stuck.status_code == 422 and "terminal" in stuck.text.lower()


async def test_sanctioned_disburses_but_never_declines(client: AsyncClient):
    """Money follows the approval — Sanctioned's only moves are Disbursed (landed)
    or Dropped (lapsed); a bank that already approved cannot 'decline'."""
    syn_id = await _mandate(client, code="DISB")
    lid = (await _lender(client, syn_id, status="IP Received"))["id"]
    assert (await _move(client, syn_id, lid, status="Sanctioned",
                        amount_cr=5)).status_code == 200
    no = await _move(client, syn_id, lid, status="Declined", note="x")
    assert no.status_code == 422, no.text
    ok = await _move(client, syn_id, lid, status="Disbursed")
    assert ok.status_code == 200, ok.text
    done = await _move(client, syn_id, lid, status="Identified")
    assert done.status_code == 422 and "terminal" in done.text.lower()


async def test_the_prep_step_sits_before_the_im(client: AsyncClient):
    syn_id = await _mandate(client, code="PREP")
    lid = (await _lender(client, syn_id, status="Identified"))["id"]
    assert (await _move(client, syn_id, lid,
                        status="IM Under Preparation")).status_code == 200
    # ...and from prep the IM goes out; it cannot jump the circulation.
    skip = await _move(client, syn_id, lid, status="Queries Received")
    assert skip.status_code == 422, skip.text
    assert (await _move(client, syn_id, lid, status="IM Circulated")).status_code == 200


async def test_the_mis_wording_for_a_sanction_is_a_sanction(client: AsyncClient):
    """Production carried 10 lender rows saying 'Final sanction received' — the MIS's
    phrase for Sanctioned (the mandate import already translates it). The row must
    behave as Sanctioned: Disbursed is its move, and 'Declined' makes no sense."""
    syn_id = await _mandate(client, code="FSR")
    lid = (await _lender(client, syn_id, status="Final sanction received"))["id"]
    no = await _move(client, syn_id, lid, status="Declined", note="x")
    assert no.status_code == 422, no.text
    ok = await _move(client, syn_id, lid, status="Disbursed")
    assert ok.status_code == 200 and ok.json()["status"] == "Disbursed"
