"""The Activity Log — the audit trail rendered as sentences a desk can read.

The screen used to read /v1/notifications: a per-user list of things still UNREAD, which
is empty on a busy register and always would be — it was never a history of what people
did. These tests pin the endpoint that replaced it: same immutable audit rows, rendered
in the desk's own words, with the company named rather than a UUID, and gated exactly as
the Audit tab is.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _as(email: str, roles: str) -> dict[str, str]:
    return {"X-User-Email": email, "X-User-Roles": roles}


async def test_activity_reads_the_audit_trail_in_plain_english(client):
    # A real operation: create a lead, then move it on. Both stamp audit rows.
    made = await client.post("/v1/leads", json={
        "company": "Helios Wind Private Limited", "sector": "Wind", "rm": "SD",
        "source": "RM", "status": "Active"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert made.status_code == 201, made.text
    lead = made.json()
    moved = await client.patch(f"/v1/leads/{lead['id']}", json={"temperature": "Hot"},
                               headers=_as("admin@evamfinance.com", "Admin"))
    assert moved.status_code == 200, moved.text

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "the trail must not be empty after two audited writes"

    # Newest first, and each row is a SENTENCE — not an action code and a UUID.
    top = items[0]
    assert top["area"] == "Leads"
    assert top["actor"] == "admin@evamfinance.com"
    assert "Updated lead" in top["summary"]
    # The before → after pair the repository records is what makes the row worth reading.
    assert "temperature" in top["summary"] and "Hot" in top["summary"]
    assert "Added a new lead" in items[1]["summary"]
    # No UUID leaks into what the desk reads.
    assert str(lead["id"]) not in top["summary"]


async def test_activity_names_the_company_a_tracker_row_belongs_to(client):
    ent = await client.post("/v1/entities", json={
        "legal_name": "Marut Solar Private Limited", "code": "MARUTSOL",
        "sector": "Solar"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert ent.status_code == 201, ent.text
    deal = await client.post("/v1/deals", json={
        "entity_id": ent.json()["id"], "rm": "SD", "is_lending": True},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert deal.status_code == 201, deal.text
    line = await client.post("/v1/lending", json={
        "entity_id": ent.json()["id"], "deal_id": deal.json()["id"],
        "stage": "Data Awaited", "amount_cr": 40},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert line.status_code == 201, line.text
    await client.patch(f"/v1/lending/{line.json()['id']}", json={"stage": "Diligence"},
                       headers=_as("admin@evamfinance.com", "Admin"))

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    rows = [x for x in r.json()["items"] if x["resource_type"] == "lending_tracker"]
    assert rows, "the lending write must appear on the trail"
    # An audit row carries a deal_id, not a company — the endpoint does the join so the
    # screen shows "Marut Solar", never a UUID the reader cannot place.
    assert rows[0]["company"] == "Marut Solar Private Limited", rows[0]
    assert rows[0]["area"] == "Lending"
    assert "Data Awaited" in rows[0]["summary"] and "Diligence" in rows[0]["summary"]


async def test_a_logged_interaction_reads_as_what_was_said_and_with_whom(client):
    """"Added a new interaction — —" told management nothing. The row carries the
    company and the interaction knows its type and words; the sentence now uses them."""
    ent = await client.post("/v1/entities", json={
        "legal_name": "Vayu Grid Private Limited", "code": "VAYUGRID",
        "sector": "Wind"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert ent.status_code == 201, ent.text
    lead = await client.post("/v1/leads", json={
        "company": "Vayu Grid Private Limited", "entity_id": ent.json()["id"],
        "sector": "Wind", "rm": "SD", "source": "RM", "status": "Active"},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert lead.status_code == 201, lead.text
    made = await client.post("/v1/interactions", json={
        "subject_type": "Lead", "subject_id": lead.json()["id"],
        "interaction_type": "Call",
        "summary": "Discussed sanction timeline; CFO to share Q2 numbers",
        "performed_by": "SD"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert made.status_code == 201, made.text

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    rows = [x for x in r.json()["items"]
            if x["resource_type"] == "interactions" and x["action"] == "create"]
    assert rows, "the interaction write must appear on the trail"
    top = rows[0]
    assert top["company"] == "Vayu Grid Private Limited", top
    assert "Call" in top["summary"]
    assert "Discussed sanction timeline" in top["summary"]

    # Deleting it must tell management WHAT was removed, not just that something was —
    # and the forensic Audit endpoint now carries the same company and sentence.
    iid = made.json()["id"]
    gone = await client.delete(f"/v1/interactions/{iid}",
                               headers=_as("admin@evamfinance.com", "Admin"))
    assert gone.status_code in (200, 204), gone.text

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    dels = [x for x in r.json()["items"]
            if x["resource_type"] == "interactions" and x["action"] == "delete"]
    assert dels, "the removal must appear on the trail"
    assert dels[0]["company"] == "Vayu Grid Private Limited"
    assert "it held:" in dels[0]["summary"] and "Call" in dels[0]["summary"]
    assert "Discussed sanction timeline" in dels[0]["summary"]

    a = await client.get("/v1/audit", params={"resource_type": "interactions"},
                         headers=_as("admin@evamfinance.com", "Admin"))
    assert a.status_code == 200, a.text
    audit_rows = a.json()
    assert audit_rows and audit_rows[0]["company"] == "Vayu Grid Private Limited"
    assert "Call" in audit_rows[0]["summary"], "audit rows speak the same sentences"


async def test_a_new_lender_names_the_lender_and_the_company(client):
    """"Added a new lender — —" told management neither which lender nor on whose
    mandate. The lender row is two hops from the company (lender → mandate → entity);
    the renderer walks both."""
    ent = await client.post("/v1/entities", json={
        "legal_name": "Tejas Hydro Private Limited", "code": "TEJASHYD",
        "sector": "Hydro"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert ent.status_code == 201, ent.text
    deal = await client.post("/v1/deals", json={
        "entity_id": ent.json()["id"], "rm": "SD", "is_syndication": True},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert deal.status_code == 201, deal.text
    tracker = await client.post("/v1/syndication", json={
        "entity_id": ent.json()["id"], "deal_id": deal.json()["id"]},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert tracker.status_code == 201, tracker.text
    lender = await client.post(f"/v1/syndication/{tracker.json()['id']}/lenders", json={
        "lender_name": "Kotak Mahindra Bank", "status": "Circulated", "amount_cr": 40},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert lender.status_code == 201, lender.text

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    rows = [x for x in r.json()["items"]
            if x["resource_type"] == "syndication_lenders" and x["action"] == "create"]
    assert rows, "the lender write must appear on the trail"
    assert rows[0]["company"] == "Tejas Hydro Private Limited", rows[0]
    assert "Kotak Mahindra Bank" in rows[0]["summary"]
    assert "Circulated" in rows[0]["summary"]


async def test_evidence_reaches_its_company_through_the_subject(client):
    """"Attached evidence … on the Lead" with Company "—" made management guess whose
    lead. The evidence row knows its subject; the subject knows its company."""
    ent = await client.post("/v1/entities", json={
        "legal_name": "Surya Grid Private Limited", "code": "SURYAGRID",
        "sector": "Solar"}, headers=_as("admin@evamfinance.com", "Admin"))
    assert ent.status_code == 201, ent.text
    lead = await client.post("/v1/leads", json={
        "company": "Surya Grid Private Limited", "entity_id": ent.json()["id"],
        "sector": "Solar", "rm": "SD", "source": "RM", "status": "Active"},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert lead.status_code == 201, lead.text
    ev = await client.post("/v1/evidence", json={
        "subject_type": "Lead", "subject_id": lead.json()["id"],
        "evidence_kind": "lead_qualification", "reference": "QUAL/SG/000123"},
        headers=_as("admin@evamfinance.com", "Admin"))
    assert ev.status_code == 201, ev.text

    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    rows = [x for x in r.json()["items"]
            if x["resource_type"] == "governance_evidence"]
    assert rows, "the evidence write must appear on the trail"
    assert rows[0]["company"] == "Surya Grid Private Limited", rows[0]
    assert "lead qualification" in rows[0]["summary"], "kind reads as words"
    assert "QUAL/SG/000123" in rows[0]["summary"]


async def test_named_operations_read_as_sentences_not_action_codes(client):
    """vox.approve / evidence.attach rendered as bare title-cased codes with no company.
    The verb map speaks the desk's language and the payload details surface."""
    from app.api.activity import _sentence

    assert _sentence("vox.approve", "vox_conversations",
                     {"recorder": "prashant@evamfinance.com",
                      "created_lead_id": "abc"}, "Vayu Grid") \
        == ("Approved a VOX conversation on Vayu Grid — "
            "recorded by prashant@evamfinance.com, filed a new lead")
    # The conversation's own words travel on the sentence when the report still
    # holds them (an erased conversation has none, by design).
    assert _sentence("vox.approve", "vox_conversations",
                     {"recorder": "pallavi@evamfinance.com"}, "Tri Electric",
                     '"Discussed BESS sizing and offtake" (12m30s)') \
        == ('Approved a VOX conversation on Tri Electric — recorded by '
            'pallavi@evamfinance.com — "Discussed BESS sizing and offtake" (12m30s)')
    assert _sentence("vox.erase", "vox_conversations",
                     {"recorder": "archana@evamfinance.com", "had_audio": True}, None) \
        == ("Erased a VOX conversation (content removed for everyone) — "
            "recorded by archana@evamfinance.com")
    assert _sentence("evidence.attach", "governance_evidence",
                     {"subject_type": "lending", "subject_id": "x",
                      "evidence_kind": "board_resolution", "reference": "BR-2026-04"},
                     "Blue Planet") \
        == "Attached evidence on Blue Planet — board resolution · BR-2026-04 on the lending"
    # Unmapped named actions keep the honest humanised fallback.
    assert _sentence("reconciliation.resolve", None, {}, None) == "Reconciliation resolve"


def test_raw_identifiers_never_reach_a_readable_sentence():
    """"entity id set to 5b127d72-…" is noise to a reader — the company is already
    named on the row, and the raw value stays on the audit record itself."""
    from app.api.activity import _sentence

    out = _sentence("update", "leads", {"values": {
        "entity_id": {"from": None, "to": "5b127d72-600c-434e-bf2c-306f169d6860"},
        "status": {"from": "Active", "to": "Converted"},
    }, "label": "LD-307"}, "WOG")
    assert "5b127d72" not in out
    assert "status Active → Converted" in out
    assert "more" not in out, "a skipped id must not be counted as '+1 more'"


async def test_a_sign_in_is_on_the_trail(client):
    ok = await client.post("/v1/session-events", json={"event": "signin"},
                           headers=_as("admin@evamfinance.com", "Admin"))
    assert ok.status_code == 201, ok.text
    r = await client.get("/v1/activity", headers=_as("admin@evamfinance.com", "Admin"))
    signins = [x for x in r.json()["items"] if x["area"] == "Session"]
    assert signins, "sign-ins are the row that says who was even here"
    assert signins[0]["summary"] == "Signed in to ATLAS"


async def test_the_activity_log_is_admin_only(client):
    """Same gate as the Audit tab, and read from the MATRIX rather than decided here —
    activity_log is Admin-only in evam_backend_core.rbac. A desk role that can see its
    own leads still cannot read who did what across the whole register."""
    denied = await client.get("/v1/activity", headers=_as("rm@evamfinance.com", "BDRM"))
    assert denied.status_code == 403, denied.text
    assert "admin" in denied.json()["error"]["detail"].lower()
