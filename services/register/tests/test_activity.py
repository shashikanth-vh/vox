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


async def test_named_operations_read_as_sentences_not_action_codes(client):
    """vox.approve / evidence.attach rendered as bare title-cased codes with no company.
    The verb map speaks the desk's language and the payload details surface."""
    from app.api.activity import _sentence

    assert _sentence("vox.approve", "vox_conversations",
                     {"recorder": "prashant@evamfinance.com",
                      "created_lead_id": "abc"}, "Vayu Grid") \
        == ("Approved a VOX conversation on Vayu Grid — "
            "recorded by prashant@evamfinance.com, filed a new lead")
    assert _sentence("vox.erase", "vox_conversations",
                     {"recorder": "archana@evamfinance.com", "had_audio": True}, None) \
        == ("Erased a VOX conversation (content removed for everyone) — "
            "recorded by archana@evamfinance.com")
    assert _sentence("evidence.attach", "governance_evidence",
                     {"subject_type": "lending", "subject_id": "x",
                      "evidence_kind": "board_resolution", "reference": "BR-2026-04"},
                     "Blue Planet") \
        == "Attached evidence on Blue Planet — board_resolution · BR-2026-04 on the lending"
    # Unmapped named actions keep the honest humanised fallback.
    assert _sentence("reconciliation.resolve", None, {}, None) == "Reconciliation resolve"


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
