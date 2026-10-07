"""The intake door: an approved website / WhatsApp enquiry becomes a lead, or an
interaction on the work the company already has, by one rule; a rejected one is
kept for the funnel; every delivery is signed and idempotent."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
SECRET = "s3cret-for-tests"


def _items(body):
    return body.get("items") if isinstance(body, dict) else body


def _signed(body: dict, *, secret: str = SECRET, ts: str | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode("utf-8")
    ts = ts or str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Intake-Timestamp": ts, "X-Intake-Signature": f"sha256={sig}",
                 "Content-Type": "application/json"}


def _capital(no: str | None = None, **over) -> dict:
    d = {
        "enquiry_no": no or f"EV{uuid.uuid4().hex[:6].upper()}",
        "submitted_at": "2026-10-04T14:36:00+05:30", "status": "approved",
        "approved_by": "mukesh@evamfinance.com", "approved_at": "2026-10-04T15:02:11+05:30",
        "verification": {"mobile_verified": True, "email_verified": True}, "intent": "capital",
        "contact": {"name": "Rajesh Kumar", "mobile": "9876543210", "email": "rajesh@example.com", "consent": True},
        "company": {"name": "Acme Renewables Pvt Ltd", "address": "Plot 14, Hitec City, Hyderabad, Telangana 500081"},
        "capital": {"persona": "advisor", "client_role": "developer", "sector": "electric-mobility",
                    "sub_sectors": ["EV fleets", "Charging"], "ticket_size": "15-100"},
        "assets": None,
        "business": {"years_in_operation": "3–10 years", "annual_revenue": "₹25–50 Cr",
                     "need": "Debt facility for a 10 MW solar portfolio"},
        "otp_verified": True,
    }
    d.update(over)
    return d


def _assets(no: str | None = None, **over) -> dict:
    d = {
        "enquiry_no": no or f"EV{uuid.uuid4().hex[:6].upper()}",
        "submitted_at": "2026-10-06T11:45:00+05:30", "status": "approved",
        "approved_by": "mukesh@evamfinance.com", "approved_at": "2026-10-06T12:10:00+05:30",
        "verification": {"mobile_verified": True, "email_verified": True}, "intent": "assets",
        "contact": {"name": "Rajesh Kumar", "mobile": "9876543210", "email": "rajesh@example.com", "consent": True},
        "company": {"name": "Green Power Infra LLP", "address": "3rd Floor, Anna Salai, Chennai, Tamil Nadu 600002"},
        "assets": {"role": "both", "on_behalf_of_client": False, "offerings": ["Entire project", "PPA"],
                   "status": "Operational", "status_other": None, "location": "Tamil Nadu / Coimbatore",
                   "buyer_size": "₹25–100 Cr", "buyer_value_basis": "DCF, per-MW", "buyer_location": "South India",
                   "buyer_criteria": ["operating solar, 10–30 MW, South India"]},
        "capital": None, "business": None, "otp_verified": True,
    }
    d.update(over)
    return d


@pytest.fixture
def intake_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "intake_webhook_secret", SECRET)
    yield
    monkeypatch.setattr(get_settings(), "intake_webhook_secret", "")


async def _roster(client: AsyncClient) -> None:
    for name, full, role, email in (("Mukesh", "Mukesh Rao", "BDRM", "mukesh@evamfinance.com"),
                                    ("Shubh", "Shubh Dave", "BD Head", "shubh@evamfinance.com")):
        r = await client.post("/v1/people", headers=ADMIN,
                              json={"name": name, "full_name": full, "role": role, "email": email})
        assert r.status_code == 201, r.text


async def _post(client: AsyncClient, body: dict, **kw):
    raw, headers = _signed(body, **kw)
    return await client.post("/v1/intake/enquiries", content=raw, headers=headers)


# ---- the door itself -------------------------------------------------------
async def test_unsigned_stale_or_wrongly_signed_deliveries_are_refused(client: AsyncClient, intake_on, monkeypatch):
    body = _capital()
    raw = json.dumps(body).encode()
    r = await client.post("/v1/intake/enquiries", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    r = await _post(client, body, secret="wrong")
    assert r.status_code == 403 and "signature" in r.text.lower()
    r = await _post(client, body, ts=str(int(time.time()) - 3600))
    assert r.status_code == 403 and "old" in r.text.lower()
    monkeypatch.setattr(get_settings(), "intake_webhook_secret", "")
    assert (await _post(client, body)).status_code == 503


# ---- 4. a new company → a new lead and its master row ----------------------
async def test_an_approved_capital_enquiry_becomes_a_lead(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    body = _capital("EV483920")
    r = await _post(client, body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["outcome"] == "lead_created" and out["replayed"] is False and out["rm"] == "Mukesh"
    lead = (await client.get(f"/v1/leads/{out['lead_id']}", headers=ADMIN)).json()
    assert lead["lead_no"] == out["lead_no"]
    assert lead["company"] == "Acme Renewables Pvt Ltd" and lead["contact"] == "Rajesh Kumar"
    assert lead["phone"] == "+919876543210" and lead["state"] == "Telangana" and lead["city"] == "Hyderabad"
    assert lead["sector"] == "EV Mobility" and lead["lens"] == "Mitigation"
    assert lead["source"] == "Inbound" and lead["source_name"] == "Website EV483920"
    assert lead["rm"] == "Mukesh" and lead["status"] == "Active" and lead["temperature"] == "Warm"
    assert "rajesh@example.com" in lead["notes"] and "Capital ask" in lead["notes"] and "ticket size: 15-100" in lead["notes"]
    assert lead["next_action"].startswith("Call back on website enquiry EV483920")
    assert lead["last_interaction_date"] == "2026-10-04"
    # The client master gained the company, born a Prospect.
    assert lead["entity_id"]
    ent = (await client.get(f"/v1/entities/{lead['entity_id']}", headers=ADMIN)).json()
    assert ent["legal_name"] == "Acme Renewables Pvt Ltd" and ent["lifecycle"] == "Prospect"
    # The RM was told, and the trail says what happened.
    n = (await db_session.execute(text(
        "SELECT title FROM notifications WHERE recipient = 'mukesh@evamfinance.com' "
        "AND event = 'intake.enquiry'"))).scalars().all()
    assert n and "Acme Renewables" in n[0]
    a = (await db_session.execute(text(
        "SELECT changes->>'outcome' FROM audit_log WHERE action = 'intake.enquiry' "
        "AND changes->>'enquiry_no' = 'EV483920'"))).scalar()
    assert a == "lead_created"
    # Redelivery of the same enquiry number writes nothing and answers the same.
    again = await _post(client, body)
    assert again.status_code == 201 and again.json()["replayed"] is True
    assert again.json()["lead_id"] == out["lead_id"]
    leads = _items((await client.get("/v1/leads", params={"company": "Acme Renewables Pvt Ltd"}, headers=ADMIN)).json())
    assert len(leads) == 1


# ---- 1. a client with a live deal → logged on the deal, no lead -----------
async def test_an_enquiry_from_a_client_with_a_live_deal_is_logged_on_the_deal(client: AsyncClient, intake_on):
    await _roster(client)
    ent = await client.post("/v1/entities", headers=ADMIN,
                            json={"code": "ACMEREN", "legal_name": "Acme Renewables Private Limited"})
    eid = ent.json()["id"]
    deal = await client.post("/v1/deals", headers=ADMIN, json={"entity_id": eid, "stage": "In Pipeline", "rm": "Shubh"})
    did = deal.json()["id"]
    assert (await client.post("/v1/lending", headers=ADMIN,
                              json={"entity_id": eid, "deal_id": did, "stage": "Data Awaited"})).status_code == 201
    r = await _post(client, _capital())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["outcome"] == "interaction_on_deal" and out["deal_id"] == did and out["lead_id"] is None
    assert "live deal" in out["note"]
    inters = _items((await client.get("/v1/interactions", params={"deal_id": did}, headers=ADMIN)).json())
    assert len(inters) == 1 and "Debt facility" in inters[0]["summary"] and inters[0]["direction"] == "inbound"
    assert _items((await client.get("/v1/leads", params={"company": "Acme Renewables Pvt Ltd"}, headers=ADMIN)).json()) == []


# ---- 2. an active lead → one more touch on it ------------------------------
async def test_an_enquiry_for_a_company_with_an_open_lead_lands_on_that_lead(client: AsyncClient, intake_on):
    await _roster(client)
    first = await client.post("/v1/leads", headers=ADMIN,
                              json={"company": "Acme Renewables Pvt. Ltd.", "rm": "Shubh", "status": "Active"})
    assert first.status_code == 201, first.text
    r = await _post(client, _capital())
    out = r.json()
    assert r.status_code == 201 and out["outcome"] == "interaction_on_lead"
    assert out["lead_id"] == first.json()["id"] and out["lead_no"] == first.json()["lead_no"]
    lead = (await client.get(f"/v1/leads/{first.json()['id']}", headers=ADMIN)).json()
    assert lead["next_action"].startswith("Call back on website enquiry") and lead["next_action_date"]
    assert lead["last_interaction_date"] == "2026-10-04"
    inters = _items((await client.get(f"/v1/leads/{first.json()['id']}/interactions", headers=ADMIN)).json())
    assert len(inters) == 1 and "EV" in inters[0]["summary"]


# ---- assets intent, and the address → state / city parse --------------------
async def test_an_assets_enquiry_becomes_a_lead_with_the_monetisation_ask_in_notes(client: AsyncClient, intake_on):
    await _roster(client)
    r = await _post(client, _assets("EV483921"))
    assert r.status_code == 201, r.text
    lead = (await client.get(f"/v1/leads/{r.json()['lead_id']}", headers=ADMIN)).json()
    assert lead["company"] == "Green Power Infra LLP" and lead["state"] == "Tamil Nadu" and lead["city"] == "Chennai"
    assert lead["sector"] == "Solar - General"           # from the buyer criteria
    assert "Asset monetisation — role: both" in lead["notes"] and "Entire project, PPA" in lead["notes"]
    assert lead["source_name"] == "Website EV483921"


# ---- rejected, unknown approver, duplicate contact --------------------------
async def test_a_rejected_enquiry_is_kept_but_creates_nothing(client: AsyncClient, intake_on):
    await _roster(client)
    before = len(_items((await client.get("/v1/leads", headers=ADMIN)).json()))
    r = await _post(client, _capital(status="rejected"))
    assert r.status_code == 201 and r.json()["outcome"] == "rejected" and r.json()["lead_id"] is None
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == before


async def test_an_approver_off_the_roster_hands_the_lead_to_the_bd_head(client: AsyncClient, intake_on):
    await _roster(client)
    r = await _post(client, _capital(approved_by="nobody@evamfinance.com"))
    out = r.json()
    assert r.status_code == 201 and out["rm"] == "Shubh"
    lead = (await client.get(f"/v1/leads/{out['lead_id']}", headers=ADMIN)).json()
    assert "not on the RM roster" in lead["notes"] and lead["rm"] == "Shubh"


async def test_the_same_contact_under_another_company_is_flagged_not_blocked(client: AsyncClient, intake_on):
    await _roster(client)
    assert (await _post(client, _capital())).status_code == 201
    other = _capital(company={"name": "Rajesh Logistics Pvt Ltd", "address": "MG Road, Bengaluru, Karnataka 560001"})
    r = await _post(client, other)
    assert r.status_code == 201 and r.json()["outcome"] == "lead_created"
    lead = (await client.get(f"/v1/leads/{r.json()['lead_id']}", headers=ADMIN)).json()
    assert "FLAG: same contact as lead" in lead["notes"] and "Acme Renewables" in lead["notes"]
    assert lead["state"] == "Karnataka" and lead["city"] == "Bengaluru"


async def test_a_whatsapp_delivery_is_the_same_contract(client: AsyncClient, intake_on):
    await _roster(client)
    r = await _post(client, _capital(channel="whatsapp"))
    lead = (await client.get(f"/v1/leads/{r.json()['lead_id']}", headers=ADMIN)).json()
    assert lead["source_name"].startswith("Whatsapp EV") and "Whatsapp enquiry" in lead["notes"]


# ---- PRISM-hosted approval: parked at submission, decided from the e-mail link ----
def _submitted(no: str | None = None, approvers=("mukesh@evamfinance.com",), **over) -> dict:
    d = _capital(no, status="submitted")
    d.pop("approved_by"), d.pop("approved_at")
    if approvers is not None:
        d["approvers"] = list(approvers)
    d.update(over)
    return d


async def test_a_submitted_enquiry_is_parked_and_answers_with_the_links(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    r = await _post(client, _submitted("EV483930"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "submitted" and out["outcome"] == "pending" and out["lead_id"] is None
    assert len(out["links"]) == 1 and out["links"][0]["recipient"] == "mukesh@evamfinance.com"
    assert out["approve_url"].startswith("http://test/v1/intake/enquiries/") and out["approve_url"].endswith("/approve")
    assert out["reject_url"].endswith("/reject") and out["approve_url"] != out["reject_url"]
    # Nothing was created: the desk sees no lead; the RM has only the approval
    # mail, not a "lead created" notification.
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []
    events = (await db_session.execute(text("SELECT event FROM notifications"))).scalars().all()
    assert events == ["intake.approval"]
    # Only the hash is stored — the table cannot be turned into a link.
    hashes = (await db_session.execute(text("SELECT token_hash, kind, recipient FROM lead_enquiry_tokens"))).all()
    assert len(hashes) == 2 and {h.kind for h in hashes} == {"approve", "reject"}
    assert all(len(h.token_hash) == 64 and h.token_hash not in out["approve_url"] for h in hashes)
    # OPENING the link changes nothing (mail scanners open links): the page shows
    # the enquiry and a button, and the lead is still not there.
    page = await client.get(out["approve_url"])
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Acme Renewables Pvt Ltd" in page.text and "Rajesh Kumar" in page.text and "<form method='post'>" in page.text
    assert "mukesh@evamfinance.com" in page.text and "Debt facility" in page.text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []
    # A redelivery of the same (still parked) enquiry gets its links again.
    again = await _post(client, _submitted("EV483930"))
    assert again.status_code == 201 and again.json()["replayed"] is True and again.json()["links"]
    assert again.json()["approve_url"] != out["approve_url"]
    assert (await db_session.execute(text("SELECT count(*) FROM lead_enquiries"))).scalar() == 1


async def test_pressing_approve_creates_the_lead_once_and_names_the_approver(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483931"))).json()
    done = await client.post(out["approve_url"])
    assert done.status_code == 200 and "text/html" in done.headers["content-type"], done.text
    assert "Lead LD-" in done.text and "created" in done.text and "Mukesh Rao" in done.text
    leads = _items((await client.get("/v1/leads", params={"company": "Acme Renewables Pvt Ltd"}, headers=ADMIN)).json())
    assert len(leads) == 1 and leads[0]["rm"] == "Mukesh" and leads[0]["source_name"] == "Website EV483931"
    assert "approved by mukesh@evamfinance.com" in leads[0]["notes"]
    enq = (await db_session.execute(text(
        "SELECT status, outcome, approved_by, approved_at, lead_id FROM lead_enquiries WHERE enquiry_no = 'EV483931'"))).one()
    assert enq.status == "approved" and enq.outcome == "lead_created" and enq.approved_by == "mukesh@evamfinance.com"
    assert enq.approved_at is not None and str(enq.lead_id) == leads[0]["id"]
    a = (await db_session.execute(text(
        "SELECT actor, changes->>'approved_by' FROM audit_log WHERE action = 'intake.enquiry' "
        "AND changes->>'enquiry_no' = 'EV483931' AND changes->>'status' = 'approved'"))).one()
    assert a[0] == "mukesh@evamfinance.com" and a[1] == "mukesh@evamfinance.com"
    # The same link again: "already approved", and still one lead. The reject
    # link is spent too — the decision is the enquiry's, not the token's.
    twice = await client.post(out["approve_url"])
    assert twice.status_code == 200 and "Already approved" in twice.text and "LD-" not in twice.text.split("<h1>")[1].split("</h1>")[0]
    assert "Already approved" in (await client.get(out["reject_url"])).text
    assert "Already approved" in (await client.post(out["reject_url"])).text
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1
    # The webhook replay answers with the decision now.
    again = (await _post(client, _submitted("EV483931"))).json()
    assert again["replayed"] is True and again["status"] == "approved" and again["lead_no"] == leads[0]["lead_no"]
    assert "links" not in again


async def test_pressing_reject_keeps_the_reason_and_creates_nothing(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483932"))).json()
    page = await client.get(out["reject_url"])
    assert page.status_code == 200 and "<textarea name='reason'" in page.text
    done = await client.post(out["reject_url"], data={"reason": "Not a climate business"})
    assert done.status_code == 200 and "Rejected" in done.text and "Not a climate business" in done.text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []
    enq = (await db_session.execute(text(
        "SELECT status, outcome, approved_by, note, payload->'decision'->>'reason' FROM lead_enquiries "
        "WHERE enquiry_no = 'EV483932'"))).one()
    assert enq[0] == "rejected" and enq[1] == "rejected" and enq[2] == "mukesh@evamfinance.com"
    assert "Not a climate business" in enq[3] and enq[4] == "Not a climate business"
    assert "Already rejected" in (await client.post(out["approve_url"])).text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []


async def test_an_unknown_or_expired_link_decides_nothing(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    r = await client.get("/v1/intake/enquiries/" + "x" * 43 + "/approve")
    assert r.status_code == 404 and "not recognised" in r.text
    assert (await client.post("/v1/intake/enquiries/" + "x" * 43 + "/approve")).status_code == 404
    assert (await client.get("/v1/intake/enquiries/short/approve")).status_code == 404
    out = (await _post(client, _submitted("EV483933"))).json()
    # The approve token on the reject route is not a reject token.
    swapped = out["approve_url"].rsplit("/", 1)[0] + "/reject"
    assert (await client.post(swapped)).status_code == 404
    await db_session.execute(text("UPDATE lead_enquiry_tokens SET expires_at = now() - interval '1 minute'"))
    await db_session.commit()
    assert (await client.get(out["approve_url"])).status_code == 410
    gone = await client.post(out["approve_url"])
    assert gone.status_code == 410 and "expired" in gone.text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []
    enq = (await db_session.execute(text("SELECT status FROM lead_enquiries WHERE enquiry_no = 'EV483933'"))).scalar()
    assert enq == "submitted"


async def test_when_the_website_names_nobody_the_bd_head_gets_the_links(client: AsyncClient, intake_on, db_session):
    """No approvers in the post and none configured → every BD Head on the roster
    is the approver: their own links, their own e-mail."""
    await _roster(client)
    out = (await _post(client, _submitted("EV483934", approvers=None))).json()
    assert [l["recipient"] for l in out["links"]] == ["shubh@evamfinance.com"]
    done = await client.post(out["approve_url"])
    assert done.status_code == 200 and "Shubh Dave" in done.text
    lead = _items((await client.get("/v1/leads", headers=ADMIN)).json())[0]
    assert lead["rm"] == "Shubh" and "not on the RM roster" not in lead["notes"]
    assert "approved by shubh@evamfinance.com" in lead["notes"]


async def test_a_link_issued_to_nobody_at_all_still_works_and_flags_the_lead(client: AsyncClient, intake_on):
    """A roster without a BD Head and no configured approvers: one unnamed pair
    (the website must mail it), and the lead is flagged for the desk."""
    r = await client.post("/v1/people", headers=ADMIN,
                          json={"name": "Mukesh", "full_name": "Mukesh Rao", "role": "BDRM", "email": "mukesh@evamfinance.com"})
    assert r.status_code == 201
    out = (await _post(client, _submitted("EV483939", approvers=None))).json()
    assert len(out["links"]) == 1 and out["links"][0]["recipient"] is None and "emailed" not in out["links"][0]
    page = await client.get(out["approve_url"])
    assert "BD Head" in page.text
    done = await client.post(out["approve_url"])
    assert done.status_code == 200
    lead = _items((await client.get("/v1/leads", headers=ADMIN)).json())[0]
    assert "not on the RM roster" in lead["notes"]


async def test_prism_mails_each_approver_the_buttons(client: AsyncClient, intake_on, db_session):
    """PRISM sends the Approve / Reject mail itself: one inbox notification and one
    pending e-mail delivery per approver, the HTML carrying that approver's links."""
    await _roster(client)
    out = (await _post(client, _submitted("EV483940", approvers=["mukesh@evamfinance.com", "shubh@evamfinance.com"]))).json()
    assert all(l["emailed"] is True for l in out["links"])
    rows = (await db_session.execute(text(
        "SELECT n.recipient, n.title, n.body, n.meta, d.channel, d.target, d.status "
        "FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id "
        "WHERE n.event = 'intake.approval' ORDER BY n.recipient"))).mappings().all()
    assert [r["recipient"] for r in rows] == ["mukesh@evamfinance.com", "shubh@evamfinance.com"]
    for r, link in zip(rows, out["links"]):
        assert r["channel"] == "email" and r["target"] == r["recipient"] and r["status"] == "pending"
        assert "Acme Renewables" in r["title"] and link["approve_url"] in r["body"] and link["reject_url"] in r["body"]
        html_ = r["meta"]["html"]
        assert f"href='{link['approve_url']}'" in html_ and f"href='{link['reject_url']}'" in html_
        assert ">Approve<" in html_ and ">Reject<" in html_ and "Rajesh Kumar" in html_ and "Debt facility" in html_
        # Mukesh's mail never carries Shubh's links.
        other = out["links"][1 - out["links"].index(link)]
        assert other["approve_url"] not in html_
    # A re-post of the still-waiting enquiry re-issues the links and mails again.
    again = (await _post(client, _submitted("EV483940", approvers=["mukesh@evamfinance.com"]))).json()
    assert again["replayed"] is True and again["links"][0]["emailed"] is True
    n = (await db_session.execute(text("SELECT count(*) FROM notification_deliveries WHERE target = 'mukesh@evamfinance.com'"))).scalar()
    assert n == 2


async def test_employees_ticked_enquiry_approver_get_the_links_first(client: AsyncClient, intake_on, monkeypatch):
    """The Employees master tick beats the env list and the BD Head default."""
    await _roster(client)
    r = await client.post("/v1/people", headers=ADMIN,
                          json={"name": "Priya", "full_name": "Priya Nair", "role": "BDRM",
                                "email": "priya@evamfinance.com", "enquiry_approver": True})
    assert r.status_code == 201 and r.json()["enquiry_approver"] is True
    monkeypatch.setattr(get_settings(), "intake_approvers", "ops@evamfinance.com")
    try:
        out = (await _post(client, _submitted("EV483943", approvers=None))).json()
    finally:
        monkeypatch.setattr(get_settings(), "intake_approvers", "")
    assert [l["recipient"] for l in out["links"]] == ["priya@evamfinance.com"]
    # Unticking returns the roster to the BD Head default.
    pid = r.json()["id"]
    r2 = await client.patch(f"/v1/people/{pid}", headers=ADMIN, json={"enquiry_approver": False})
    assert r2.status_code == 200 and r2.json()["enquiry_approver"] is False, r2.text
    out2 = (await _post(client, _submitted("EV483944", approvers=None))).json()
    assert [l["recipient"] for l in out2["links"]] == ["shubh@evamfinance.com"]


# ---- one tap: a real click on the e-mail button decides at once ---------------
_CLICK = {"Sec-Fetch-User": "?1", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "cross-site"}


async def test_a_real_tap_on_approve_in_the_mail_decides_at_once(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483945"))).json()
    done = await client.get(out["approve_url"], headers=_CLICK)
    assert done.status_code == 200 and "Lead LD-" in done.text and "created" in done.text
    leads = _items((await client.get("/v1/leads", headers=ADMIN)).json())
    assert len(leads) == 1 and leads[0]["rm"] == "Mukesh"
    enq = (await db_session.execute(text("SELECT status, approved_by FROM lead_enquiries WHERE enquiry_no = 'EV483945'"))).one()
    assert enq[0] == "approved" and enq[1] == "mukesh@evamfinance.com"
    # Tapping again, or a scanner opening it later: already approved, still one lead.
    assert "Already approved" in (await client.get(out["approve_url"], headers=_CLICK)).text
    assert "Already approved" in (await client.get(out["approve_url"])).text
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1


async def test_a_scanner_opening_the_link_gets_the_confirm_page_and_nothing_happens(client: AsyncClient, intake_on):
    await _roster(client)
    out = (await _post(client, _submitted("EV483946"))).json()
    for headers in ({}, {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
                    {"Sec-Fetch-User": "?1", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "empty"}):
        page = await client.get(out["approve_url"], headers=headers)
        assert page.status_code == 200 and "<form method='post'>" in page.text and "Lead LD-" not in page.text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []
    # Reject always asks for the reason, even on a real tap.
    page = await client.get(out["reject_url"], headers=_CLICK)
    assert "<textarea name='reason'" in page.text
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []


async def test_one_tap_can_be_switched_off(client: AsyncClient, intake_on, monkeypatch):
    await _roster(client)
    out = (await _post(client, _submitted("EV483947"))).json()
    monkeypatch.setattr(get_settings(), "intake_one_tap", False)
    try:
        page = await client.get(out["approve_url"], headers=_CLICK)
    finally:
        monkeypatch.setattr(get_settings(), "intake_one_tap", True)
    assert "<form method='post'>" in page.text and _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []


async def test_configured_approvers_and_the_no_mail_switch(client: AsyncClient, intake_on, monkeypatch, db_session):
    await _roster(client)
    monkeypatch.setattr(get_settings(), "intake_approvers", "ops@evamfinance.com, Shubh@evamfinance.com")
    try:
        out = (await _post(client, _submitted("EV483941", approvers=None))).json()
    finally:
        monkeypatch.setattr(get_settings(), "intake_approvers", "")
    assert [l["recipient"] for l in out["links"]] == ["ops@evamfinance.com", "shubh@evamfinance.com"]
    monkeypatch.setattr(get_settings(), "intake_send_email", False)
    try:
        out2 = (await _post(client, _submitted("EV483942"))).json()
    finally:
        monkeypatch.setattr(get_settings(), "intake_send_email", True)
    assert out2["links"][0]["recipient"] == "mukesh@evamfinance.com" and "emailed" not in out2["links"][0]
    n = (await db_session.execute(text(
        "SELECT count(*) FROM notifications WHERE event = 'intake.approval' AND meta->>'enquiry_no' = 'EV483942'"))).scalar()
    assert n == 0


async def test_each_approver_gets_their_own_pair_and_the_first_click_wins(client: AsyncClient, intake_on):
    await _roster(client)
    out = (await _post(client, _submitted("EV483935", approvers=["mukesh@evamfinance.com", "Shubh@evamfinance.com",
                                                                  "mukesh@evamfinance.com"]))).json()
    assert [l["recipient"] for l in out["links"]] == ["mukesh@evamfinance.com", "shubh@evamfinance.com"]
    shubh = out["links"][1]
    done = await client.post(shubh["approve_url"])
    assert done.status_code == 200 and "Shubh Dave" in done.text
    lead = _items((await client.get("/v1/leads", headers=ADMIN)).json())[0]
    assert lead["rm"] == "Shubh" and "approved by shubh@evamfinance.com" in lead["notes"]
    late = await client.post(out["links"][0]["approve_url"])
    assert "Already approved" in late.text and "shubh@evamfinance.com" in late.text
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1


async def test_the_links_point_at_the_configured_public_origin(client: AsyncClient, intake_on, monkeypatch):
    await _roster(client)
    monkeypatch.setattr(get_settings(), "intake_public_base_url", "https://prism-evamfinance.com/")
    try:
        out = (await _post(client, _submitted("EV483936"))).json()
    finally:
        monkeypatch.setattr(get_settings(), "intake_public_base_url", "")
    assert out["approve_url"].startswith("https://prism-evamfinance.com/v1/intake/enquiries/")
    # …and the approval through the existing rule still runs behind that origin's path.
    path = out["approve_url"].split("prism-evamfinance.com", 1)[1]
    assert (await client.post(path)).status_code == 200


async def test_a_submitted_enquiry_for_a_company_with_a_live_deal_lands_on_the_deal_when_approved(client: AsyncClient, intake_on):
    await _roster(client)
    ent = await client.post("/v1/entities", headers=ADMIN,
                            json={"code": "ACMEREN", "legal_name": "Acme Renewables Private Limited"})
    eid = ent.json()["id"]
    did = (await client.post("/v1/deals", headers=ADMIN, json={"entity_id": eid, "stage": "In Pipeline", "rm": "Shubh"})).json()["id"]
    assert (await client.post("/v1/lending", headers=ADMIN,
                              json={"entity_id": eid, "deal_id": did, "stage": "Data Awaited"})).status_code == 201
    out = (await _post(client, _submitted("EV483937"))).json()
    done = await client.post(out["approve_url"])
    assert done.status_code == 200 and "live deal" in done.text
    inters = _items((await client.get("/v1/interactions", params={"deal_id": did}, headers=ADMIN)).json())
    assert len(inters) == 1 and inters[0]["direction"] == "inbound"
    assert _items((await client.get("/v1/leads", headers=ADMIN)).json()) == []


async def test_two_approvers_pressing_at_once_make_one_lead(client: AsyncClient, intake_on):
    """The decision takes the enquiry row FOR UPDATE, so simultaneous presses
    serialise: one creates the lead, the other sees 'Already approved'."""
    import asyncio

    await _roster(client)
    out = (await _post(client, _submitted("EV483938", approvers=["mukesh@evamfinance.com", "shubh@evamfinance.com"]))).json()
    a, b = await asyncio.gather(client.post(out["links"][0]["approve_url"]),
                                client.post(out["links"][1]["approve_url"]))
    texts = sorted([a.text, b.text], key=lambda t: "Already approved" in t)
    assert "created" in texts[0] and "Already approved" in texts[1]
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1


# ---- the chase: nobody decided -------------------------------------------------
async def _sweep(client: AsyncClient):
    r = await client.post("/v1/internal/intake/sweep", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_reminder_goes_to_the_same_approvers_after_three_days(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483950"))).json()
    assert await _sweep(client) == {"reminded": [], "escalated": [], "stranded": []}   # too early
    await db_session.execute(text("UPDATE lead_enquiries SET received_at = now() - interval '4 days' WHERE enquiry_no = 'EV483950'"))
    await db_session.commit()
    assert (await _sweep(client))["reminded"] == ["EV483950"]
    assert (await _sweep(client))["reminded"] == []                                   # once
    mails = (await db_session.execute(text(
        "SELECT n.title, n.meta->>'kind', d.target FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id "
        "WHERE n.event = 'intake.approval' ORDER BY n.created_at"))).all()
    assert [m[1] for m in mails] == ["approval", "reminder"] and all(m[2] == "mukesh@evamfinance.com" for m in mails)
    assert mails[1][0].startswith("Reminder")
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    row = next(r for r in rows if r["enquiry_no"] == "EV483950")
    assert row["stage"] == "reminded" and row["approvers"] == ["mukesh@evamfinance.com"] and row["reminded_at"]
    # The first links still work: the reminder adds, it does not revoke.
    assert (await client.post(out["approve_url"])).status_code == 200
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1


async def test_an_expired_enquiry_is_escalated_to_the_bd_head_with_fresh_links(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483951"))).json()
    await db_session.execute(text("UPDATE lead_enquiry_tokens SET expires_at = now() - interval '1 hour'"))
    await db_session.commit()
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    assert next(r for r in rows if r["enquiry_no"] == "EV483951")["stage"] == "expired"
    assert (await _sweep(client))["escalated"] == ["EV483951"]
    assert (await _sweep(client))["escalated"] == []
    mail = (await db_session.execute(text(
        "SELECT n.title, n.meta->>'approve_url', d.target FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id "
        "WHERE n.meta->>'kind' = 'escalation'"))).one()
    assert mail[0].startswith("Escalated") and mail[2] == "shubh@evamfinance.com"
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    row = next(r for r in rows if r["enquiry_no"] == "EV483951")
    assert row["stage"] == "escalated" and "shubh@evamfinance.com" in row["approvers"] and "did not decide" in row["note"]
    # Mukesh's old link is dead; Shubh's new one approves and Shubh owns the lead.
    assert (await client.post(out["approve_url"])).status_code == 410
    path = mail[1].split("/v1/", 1)[1]
    done = await client.post("/v1/" + path)
    assert done.status_code == 200 and "Shubh Dave" in done.text
    lead = _items((await client.get("/v1/leads", headers=ADMIN)).json())[0]
    assert lead["rm"] == "Shubh" and "approved by shubh@evamfinance.com" in lead["notes"]


async def test_the_desk_can_list_and_resend(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483952"))).json()
    # Listing needs a signed-in user with access to leads; a bare machine key is refused.
    assert (await client.get("/v1/enquiries")).status_code == 403
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    row = next(r for r in rows if r["enquiry_no"] == "EV483952")
    assert row["stage"] == "waiting" and row["company"] == "Acme Renewables Pvt Ltd" and row["need"].startswith("Debt facility")
    assert row["mobile"] == "+919876543210" and row["expires_at"] and row["lead_no"] is None
    r = await client.post(f"/v1/enquiries/{row['id']}/resend", headers=ADMIN, json={})
    assert r.status_code == 200 and r.json()["links"][0]["recipient"] == "mukesh@evamfinance.com"
    r2 = await client.post(f"/v1/enquiries/{row['id']}/resend", headers=ADMIN, json={"approvers": ["shubh@evamfinance.com"]})
    assert r2.status_code == 200 and r2.json()["links"][0]["recipient"] == "shubh@evamfinance.com"
    kinds = (await db_session.execute(text(
        "SELECT n.meta->>'kind', d.target FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id "
        "WHERE n.event = 'intake.approval' ORDER BY n.created_at"))).all()
    assert [k[0] for k in kinds] == ["approval", "resend", "resend"] and kinds[2][1] == "shubh@evamfinance.com"
    a = (await db_session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'intake.resend'"))).scalar()
    assert a == 2
    # Decided enquiries cannot be re-sent, and show their outcome in the list.
    assert (await client.post(out["approve_url"])).status_code == 200
    assert (await client.post(f"/v1/enquiries/{row['id']}/resend", headers=ADMIN, json={})).status_code == 422
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    row = next(r for r in rows if r["enquiry_no"] == "EV483952")
    assert row["stage"] == "approved" and row["lead_no"].startswith("LD-") and row["approved_by"] == "mukesh@evamfinance.com"


# ---- the mail and the page carry the whole enquiry; Admin can delete ----------
async def test_the_mail_and_the_page_carry_every_detail_of_the_enquiry(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    body = _submitted("EV483960", company={"name": "Acme Renewables Pvt Ltd", "cin": "U40106TG2019PTC123456",
                                            "address": "Plot 14, Hitec City, Hyderabad, Telangana 500081"},
                      message="Please call after 4 pm")
    out = (await _post(client, body)).json()
    html_ = (await db_session.execute(text(
        "SELECT meta->>'html' FROM notifications WHERE meta->>'enquiry_no' = 'EV483960'"))).scalar()
    for needle in ("U40106TG2019PTC123456", "Hyderabad, Telangana", "Sub sectors", "EV fleets, Charging",
                   "Ticket size", "15-100", "Persona", "advisor", "Client role", "developer",
                   "Years in operation", "3–10 years", "Annual revenue", "Debt facility for a 10 MW solar portfolio",
                   "Please call after 4 pm", "Looking for", "Capital"):
        assert needle in html_, needle
    page = (await client.get(out["approve_url"])).text
    for needle in ("U40106TG2019PTC123456", "Ticket size", "15-100", "Please call after 4 pm", "Annual revenue"):
        assert needle in page, needle
    done = await client.post(out["approve_url"])
    assert "close this page" in done.text and "/ui/leads" in done.text and "Ticket size" in done.text


async def test_an_admin_can_delete_an_enquiry_and_its_links_but_not_the_lead(client: AsyncClient, intake_on, db_session):
    await _roster(client)
    out = (await _post(client, _submitted("EV483961"))).json()
    rows = (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"]
    eid = next(r for r in rows if r["enquiry_no"] == "EV483961")["id"]
    bdrm = {"X-User-Email": "mukesh@evamfinance.com", "X-User-Roles": "BDRM"}
    assert (await client.delete(f"/v1/enquiries/{eid}", headers=bdrm)).status_code == 403
    # Approve first, then delete: the lead survives, the enquiry and its links go.
    assert (await client.post(out["approve_url"])).status_code == 200
    r = await client.delete(f"/v1/enquiries/{eid}", headers=ADMIN)
    assert r.status_code == 200 and r.json()["deleted"] == "EV483961" and r.json()["lead_id"]
    assert all(x["enquiry_no"] != "EV483961" for x in (await client.get("/v1/enquiries", headers=ADMIN)).json()["items"])
    assert (await db_session.execute(text("SELECT count(*) FROM lead_enquiry_tokens"))).scalar() == 0
    assert (await client.get(out["approve_url"])).status_code == 404
    assert len(_items((await client.get("/v1/leads", headers=ADMIN)).json())) == 1
    a = (await db_session.execute(text("SELECT actor FROM audit_log WHERE action = 'intake.delete'"))).scalar()
    assert a == "admin@evamfinance.com"
    assert (await client.delete(f"/v1/enquiries/{eid}", headers=ADMIN)).status_code == 404
