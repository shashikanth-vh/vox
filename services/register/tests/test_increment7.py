"""Increment 7 (Register side) — calendar lifecycle, document lifecycle, notifications.

* Calendar events: Scheduled → Completed/Cancelled with terminal freeze (DB trigger),
  organizer-owned lifecycle, reschedule-in-place.
* Document lifecycle: maker≠checker validation/rejection, mandatory rejection reasons,
  replacement chains (Superseded → successor), the idempotent expiry sweep, and the
  guard that keeps lifecycle statuses out of the generic create/update payloads.
* Notifications: idempotent creation with the per-channel delivery outbox, the human
  inbox (recipient-scoped), claim/lease/fencing, retry backoff, dead-letter + the
  audited Admin redrive.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from tests.test_decisions import _ctx, wf_client  # noqa: F401 - fixture import

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
BD_HEAD = {"X-User-Email": "bd@evamfinance.com", "X-User-Roles": "BD Head"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}
SVC = {"X-API-Key": "ntf-key"}


async def _entity(client) -> str:  # noqa: ANN001
    code = "CAL" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities", json={"code": code, "legal_name": f"Cal {code}",
                                               "entity_type": "Company"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------------------- #
# Calendar lifecycle
# --------------------------------------------------------------------------------------- #
async def test_calendar_event_reschedule_complete_and_freeze(client):
    eid = await _entity(client)
    start = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    r = await client.post("/v1/calendar-events",
                         json={"title": "Site visit", "starts_at": start,
                               "subject_type": "Entity", "subject_id": eid},
                         headers=BD_HEAD)
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["status"] == "Scheduled" and ev["organizer"] == "bd@evamfinance.com"
    assert ev["entity_id"] == eid

    # A reschedule UPDATES the Scheduled row (same identity, bumped version, audited).
    new_start = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    r = await client.patch(f"/v1/calendar-events/{ev['id']}",
                          json={"starts_at": new_start, "location": "Client HQ"},
                          headers=BD_HEAD)
    assert r.status_code == 200 and r.json()["location"] == "Client HQ"

    # A stranger (not organizer / Admin / Management) cannot manage the event.
    stranger = {"X-User-Email": "other@evamfinance.com", "X-User-Roles": "BD Head"}
    r = await client.post(f"/v1/calendar-events/{ev['id']}/cancel",
                         json={"note": "x"}, headers=stranger)
    assert r.status_code == 403, r.text

    # The organizer completes it — and the record freezes: no update, no cancel.
    r = await client.post(f"/v1/calendar-events/{ev['id']}/complete",
                         json={"note": "met CFO, docs promised"}, headers=BD_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Completed"
    assert r.json()["completed_by"] == "bd@evamfinance.com"
    assert (await client.patch(f"/v1/calendar-events/{ev['id']}",
                               json={"title": "rewrite"},
                               headers=BD_HEAD)).status_code == 409
    assert (await client.post(f"/v1/calendar-events/{ev['id']}/cancel",
                              json={"note": "too late"},
                              headers=BD_HEAD)).status_code == 409


async def test_calendar_cancel_needs_reasons_and_lists_scope(client):
    start = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    ev = (await client.post("/v1/calendar-events",
                            json={"title": "Intro call", "starts_at": start},
                            headers=BD_HEAD)).json()
    # Cancelling without a note is refused; with one it lands + freezes.
    assert (await client.post(f"/v1/calendar-events/{ev['id']}/cancel", json={},
                              headers=BD_HEAD)).status_code == 422
    r = await client.post(f"/v1/calendar-events/{ev['id']}/cancel",
                         json={"note": "client postponed"}, headers=BD_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Cancelled"
    assert r.json()["cancel_note"] == "client postponed"

    # An ordinary user sees their OWN calendar; another user's requires Admin/Management.
    mine = await client.get("/v1/calendar-events", headers=BD_HEAD)
    assert any(e["id"] == ev["id"] for e in mine.json()["items"])
    other = {"X-User-Email": "other@evamfinance.com", "X-User-Roles": "BD Head"}
    r = await client.get("/v1/calendar-events",
                        params={"organizer": "bd@evamfinance.com"}, headers=other)
    assert r.status_code == 403
    r = await client.get("/v1/calendar-events",
                        params={"organizer": "bd@evamfinance.com"}, headers=ADMIN)
    assert r.status_code == 200 and any(e["id"] == ev["id"] for e in r.json()["items"])


# --------------------------------------------------------------------------------------- #
# Document lifecycle
# --------------------------------------------------------------------------------------- #
async def _doc(client, eid, headers=BD_HEAD, **extra):  # noqa: ANN001
    r = await client.post("/v1/documents",
                         json={"subject_type": "Entity", "subject_id": eid,
                               "title": "Insurance policy", "slot_key": "insurance",
                               **extra},
                         headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_document_validation_is_maker_checker(client):
    eid = await _entity(client)
    doc = await _doc(client, eid)
    # The uploader cannot verify their own document…
    r = await client.post(f"/v1/documents/{doc['id']}/validate", json={},
                         headers=BD_HEAD)
    assert r.status_code == 422 and "different checker" in r.text.lower()
    # …a DIFFERENT checker can, fixing the validity window as they verify.
    r = await client.post(f"/v1/documents/{doc['id']}/validate",
                         json={"expires_on": "2027-03-31", "note": "policy sighted"},
                         headers=CREDIT_HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Verified" and body["verified_by"] == "ch@evamfinance.com"
    assert body["expires_on"] == "2027-03-31"
    # A verified document is settled — it cannot be verified or rejected again.
    assert (await client.post(f"/v1/documents/{doc['id']}/validate", json={},
                              headers=CREDIT_HEAD)).status_code == 409
    assert (await client.post(f"/v1/documents/{doc['id']}/reject",
                              json={"note": "no"},
                              headers=CREDIT_HEAD)).status_code == 409


async def test_document_rejection_needs_reasons_then_replacement_chains(client):
    eid = await _entity(client)
    doc = await _doc(client, eid)
    # Rejection without reasons is refused; with reasons it lands on the record.
    assert (await client.post(f"/v1/documents/{doc['id']}/reject", json={},
                              headers=CREDIT_HEAD)).status_code == 422
    r = await client.post(f"/v1/documents/{doc['id']}/reject",
                         json={"note": "policy expired at upload"}, headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Rejected"
    assert r.json()["status_note"] == "policy expired at upload"

    # The replacement is a NEW row answering the same slot; the old row chains to it.
    r = await client.post(f"/v1/documents/{doc['id']}/replace",
                         json={"title": "Insurance policy (renewed)"}, headers=BD_HEAD)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["status"] == "On File" and new["slot_key"] == "insurance"
    old = (await client.get(f"/v1/documents/{doc['id']}")).json()
    assert old["status"] == "Superseded" and old["superseded_by"] == new["id"]
    # A superseded document cannot be replaced again — replace its successor.
    assert (await client.post(f"/v1/documents/{doc['id']}/replace",
                              json={"title": "x"}, headers=BD_HEAD)).status_code == 409


async def test_lifecycle_statuses_cannot_be_set_directly(client):
    eid = await _entity(client)
    # Registration cannot mint a lifecycle status…
    r = await client.post("/v1/documents",
                         json={"subject_type": "Entity", "subject_id": eid,
                               "title": "X", "status": "Verified"}, headers=BD_HEAD)
    assert r.status_code == 422, r.text
    # …and neither can the generic PATCH.
    doc = await _doc(client, eid)
    r = await client.patch(f"/v1/documents/{doc['id']}", json={"status": "Verified"},
                          headers=BD_HEAD)
    assert r.status_code == 422, r.text


async def test_expiry_sweep_marks_lapsed_and_warns_upcoming_idempotently(
        client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"ntf-key": "svc_workflows"})
    eid = await _entity(client)
    today = datetime.now(UTC).date()
    lapsed = await _doc(client, eid, expires_on=(today - timedelta(days=1)).isoformat())
    soon = await _doc(client, eid, expires_on=(today + timedelta(days=3)).isoformat(),
                      slot_key="sanction", title="Sanction letter")
    far = await _doc(client, eid, expires_on=(today + timedelta(days=90)).isoformat(),
                     slot_key="far", title="Long-dated")

    # A human key cannot run the sweep — machine plumbing only.
    assert (await client.post("/v1/internal/documents/expiry-sweep", json={},
                              headers=ADMIN)).status_code == 403

    r = await client.post("/v1/internal/documents/expiry-sweep",
                         json={"warn_days": 7}, headers=SVC)
    assert r.status_code == 200, r.text
    report = r.json()
    assert [d["id"] for d in report["expired"]] == [lapsed["id"]]
    assert [d["id"] for d in report["expiring"]] == [soon["id"]]
    assert far["id"] not in {d["id"] for d in report["expiring"]}
    assert (await client.get(f"/v1/documents/{lapsed['id']}")).json()["status"] == "Expired"

    # Idempotent: a re-run reports NOTHING newly expired (already Expired drops out).
    r2 = (await client.post("/v1/internal/documents/expiry-sweep",
                            json={"warn_days": 7}, headers=SVC)).json()
    assert r2["expired"] == [] and [d["id"] for d in r2["expiring"]] == [soon["id"]]

    # An expired document can still be replaced — the recovery path.
    r = await client.post(f"/v1/documents/{lapsed['id']}/replace",
                         json={"title": "Insurance policy (renewed)",
                               "expires_on": (today + timedelta(days=365)).isoformat()},
                         headers=BD_HEAD)
    assert r.status_code == 201 and r.json()["status"] == "On File"


# --------------------------------------------------------------------------------------- #
# Notifications: idempotent store + delivery outbox + inbox
# --------------------------------------------------------------------------------------- #
async def _notify(c, **over):  # noqa: ANN001
    body = {"recipient": "rm@evamfinance.com", "event": "sla_escalation",
            "severity": "warning", "title": "SLA escalation — Lead:l1",
            "subject_type": "Lead", "subject_id": "l1",
            "workflow_id": f"leadconv-{uuid.uuid4().hex[:8]}",
            "dedupe_key": f"dk-{uuid.uuid4().hex}",
            "channels": ["email", "webhook"],
            "webhook_url": "http://hooks.local/notify", **over}
    r = await c.post("/v1/internal/notifications", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_notification_create_is_idempotent_with_channel_outbox(wf_client):  # noqa: F811
    n = await _notify(wf_client)
    assert {d["channel"] for d in n["deliveries"]} == {"email", "webhook"}
    assert all(d["status"] == "pending" for d in n["deliveries"])
    # A replay with the same dedupe key returns the ORIGINAL — never a second notify.
    again = await wf_client.post("/v1/internal/notifications", json={
        "recipient": n["recipient"], "event": n["event"], "title": n["title"],
        "dedupe_key": n["dedupe_key"], "channels": ["email"]})
    assert again.status_code == 201 and again.json()["id"] == n["id"]
    # Mis-specified channels are refused as a whole (nothing half-created).
    r = await wf_client.post("/v1/internal/notifications", json={
        "recipient": "x@y.z", "event": "e", "title": "t", "channels": ["sms"]})
    assert r.status_code == 422 and "sms_to" in r.text
    r = await wf_client.post("/v1/internal/notifications", json={
        "recipient": "x@y.z", "event": "e", "title": "t", "channels": ["pigeon"]})
    assert r.status_code == 422


async def test_delivery_claim_lease_retry_dead_and_redrive(wf_client):  # noqa: F811
    n = await _notify(wf_client)
    claim = (await wf_client.post("/v1/internal/notifications/deliveries/claim",
                                  json={"limit": 10, "lease_seconds": 60})).json()
    claimed = [c for c in claim["claimed"]
               if c["delivery_id"] in {d["id"] for d in n["deliveries"]}]
    assert len(claimed) == 2
    # Each claim carries everything needed to send — channel, target, content, token.
    email = next(c for c in claimed if c["channel"] == "email")
    assert email["target"] == "rm@evamfinance.com" and email["title"] == n["title"]
    # While leased, a second claim gets NOTHING (no double-send from two replicas).
    again = (await wf_client.post("/v1/internal/notifications/deliveries/claim",
                                  json={"limit": 10, "lease_seconds": 60})).json()
    assert not [c for c in again["claimed"]
                if c["delivery_id"] in {d["id"] for d in n["deliveries"]}]

    # delivered: terminal. A stale write-back afterwards is a NO-OP, not a regression.
    r = await wf_client.post(
        f"/v1/internal/notifications/deliveries/{email['delivery_id']}",
        json={"status": "delivered", "claim_token": email["claim_token"]})
    assert r.json()["status"] == "delivered"
    r = await wf_client.post(
        f"/v1/internal/notifications/deliveries/{email['delivery_id']}",
        json={"status": "retry", "claim_token": email["claim_token"]})
    assert r.json()["status"] == "ignored" and r.json()["current"] == "delivered"

    # retry with backoff 0 → immediately claimable again, attempts incremented; a WRONG
    # token is fenced out.
    hook = next(c for c in claimed if c["channel"] == "webhook")
    bad = await wf_client.post(
        f"/v1/internal/notifications/deliveries/{hook['delivery_id']}",
        json={"status": "retry", "claim_token": str(uuid.uuid4())})
    assert bad.json()["status"] == "ignored"
    r = await wf_client.post(
        f"/v1/internal/notifications/deliveries/{hook['delivery_id']}",
        json={"status": "retry", "claim_token": hook["claim_token"],
              "error": "HTTP 503", "backoff_seconds": 0})
    assert r.json()["status"] == "retry"
    re_claim = (await wf_client.post("/v1/internal/notifications/deliveries/claim",
                                     json={"limit": 10, "lease_seconds": 60})).json()
    hook2 = next(c for c in re_claim["claimed"]
                 if c["delivery_id"] == hook["delivery_id"])
    assert hook2["attempts"] == 2

    # dead-letter, then the audited Admin redrive brings it back to pending.
    r = await wf_client.post(
        f"/v1/internal/notifications/deliveries/{hook['delivery_id']}",
        json={"status": "dead", "claim_token": hook2["claim_token"],
              "error": "attempts exhausted"})
    assert r.json()["status"] == "dead"
    stats = (await wf_client.get(
        "/v1/internal/notifications/deliveries/stats")).json()
    assert stats["dead"] >= 1
    redrive_path = (f"/v1/internal/notifications/deliveries/"
                    f"{hook['delivery_id']}/redrive")
    non_admin = await wf_client.post(redrive_path, json={"reason": "fixed the hook"},
                                     headers={"X-Internal-Context": _ctx(path=redrive_path)})
    assert non_admin.status_code == 403
    admin_hdr = {"X-Internal-Context": _ctx(path=redrive_path,
                                            email="admin@evamfinance.com",
                                            roles=("Admin",))}
    assert (await wf_client.post(redrive_path, json={},
                                 headers=admin_hdr)).status_code == 422  # reason mandatory
    r = await wf_client.post(redrive_path, json={"reason": "provider restored"},
                            headers=admin_hdr)
    assert r.status_code == 200 and r.json()["status"] == "pending"


async def test_inbox_is_recipient_scoped(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"ntf-key": "svc_workflows"})
    rm = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM"}
    made = await client.post("/v1/internal/notifications", json={
        "recipient": "rm@evamfinance.com", "event": "document_expired",
        "severity": "critical", "title": "Document expired — Lending:ln1",
        "dedupe_key": f"dk-{uuid.uuid4().hex}"}, headers=SVC)
    assert made.status_code == 201, made.text
    nid = made.json()["id"]
    # A human key cannot write notifications.
    assert (await client.post("/v1/internal/notifications", json={
        "recipient": "x@y.z", "event": "e", "title": "t"},
        headers=ADMIN)).status_code == 403

    # The recipient sees it (unread), another user does not; anonymous is refused.
    inbox = (await client.get("/v1/notifications", headers=rm)).json()
    assert any(n["id"] == nid for n in inbox["items"]) and inbox["unread"] >= 1
    other = (await client.get("/v1/notifications", headers=BD_HEAD)).json()
    assert not any(n["id"] == nid for n in other["items"])
    assert (await client.get("/v1/notifications")).status_code == 403
    # Another user's inbox needs Admin.
    assert (await client.get("/v1/notifications",
                             params={"recipient": "rm@evamfinance.com"},
                             headers=BD_HEAD)).status_code == 403
    admin_view = (await client.get("/v1/notifications",
                                   params={"recipient": "rm@evamfinance.com"},
                                   headers=ADMIN)).json()
    assert any(n["id"] == nid for n in admin_view["items"])

    # Mark-read: only the recipient (or Admin); idempotent.
    assert (await client.post(f"/v1/notifications/{nid}/read",
                              headers=BD_HEAD)).status_code == 403
    r = await client.post(f"/v1/notifications/{nid}/read", headers=rm)
    assert r.status_code == 200 and r.json()["read_at"] is not None
    r2 = await client.post(f"/v1/notifications/{nid}/read", headers=rm)
    assert r2.json()["read_at"] == r.json()["read_at"]
