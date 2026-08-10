"""A notification says WHICH line it is about, in the words the desk uses.

A workflow runs in a deterministic sandbox and cannot look anything up, so the title it
builds names its subject the only way it can — "Awaiting checker approval —
Lending:814ef731-03bc-46b6-ab2e-971168008c55". Today showed six of those in a row, and a
UUID is not something anyone on a credit desk recognises.

The naming therefore happens in the ACTIVITY, which is allowed to read the register on
the way to the inbox. These tests hold that line: the title is resolved when it can be,
the raw subject survives when it cannot, and NOTHING here is ever allowed to stop a
notification being written.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.config import get_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _notifications_on(monkeypatch):
    """The fan-out only runs when the deployment enables notifications."""
    get_settings.cache_clear()
    monkeypatch.setenv("WORKFLOWS_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOWS_NOTIFY_CHANNELS", "")
    yield
    get_settings.cache_clear()


def _seed_line(reg, *, company="Suryodaya Energy Pvt Ltd", tracker_no="L042"):
    """A lending line the way the register holds it: the NAME is on the entity, the
    quotable number on the tracker line."""
    eid, lid = uuid.uuid4().hex, uuid.uuid4().hex
    reg.state.entities.append({"id": eid, "legal_name": company, "code": "SURYO"})
    reg.state.lending[lid] = {"id": lid, "entity_id": eid, "tracker_no": tracker_no,
                              "stage": "Sanctioned"}
    return lid


async def _emit(reg, *, subject_type, subject_id, event="awaiting_checker_approval",
                title=None):
    """Raise the ops event exactly as ``_emit_ops`` in the workflow does, and hand back
    the notification the register actually stored."""
    subject = f"{subject_type}:{subject_id}"
    detail = {"subject": subject, "notify": {
        "recipients": ["arun@evamfinance.com"], "severity": "info",
        "title": title if title is not None
        else event.replace("_", " ").capitalize() + f" — {subject}",
        "discriminator": subject,
        "subject_type": subject_type, "subject_id": subject_id}}
    await ActivityEnvironment().run(activities.emit_operational_event, event, detail)
    rows = list(reg.state.notifications.values())
    assert len(rows) == 1, rows
    return rows[0]


async def test_a_lending_subject_becomes_the_company_and_its_number(mock_register):
    """THE REPORTED CASE. "Lending:814ef731-…" is what the workflow can say; "Suryodaya
    Energy Pvt Ltd · L042" is what the credit head can act on."""
    lid = _seed_line(mock_register)
    row = await _emit(mock_register, subject_type="Lending", subject_id=lid)

    assert row["title"] == "Awaiting checker approval — Suryodaya Energy Pvt Ltd · L042"
    assert lid not in row["title"]
    # The BINDING is untouched — the row still points at the line it came from, so
    # clicking through and every downstream filter behave exactly as before.
    assert row["subject_type"] == "Lending" and row["subject_id"] == lid


async def test_a_deal_subject_is_named_the_same_way(mock_register):
    """Deals reach Today too ("Committee decided — Deal:7e840392-…")."""
    eid, did = uuid.uuid4().hex, uuid.uuid4().hex
    mock_register.state.entities.append({"id": eid, "legal_name": "Aurora Wind Ltd"})
    mock_register.state.deals[did] = {"id": did, "entity_id": eid, "deal_no": "AURORA"}
    row = await _emit(mock_register, subject_type="Deal", subject_id=did,
                      event="committee_decided")
    assert row["title"] == "Committee decided — Aurora Wind Ltd · AURORA"


async def test_a_lead_carries_its_company_on_its_own_row(mock_register):
    """A lead has no entity yet — the company name is on the lead itself, so this must
    not depend on the entity hop."""
    lead_id = uuid.uuid4().hex
    mock_register.state.leads[lead_id] = {"id": lead_id, "company": "Vayu Solar",
                                          "lead_no": "LD-207", "status": "Active"}
    row = await _emit(mock_register, subject_type="Lead", subject_id=lead_id,
                      event="approval_requested")
    assert row["title"] == "Approval requested — Vayu Solar · LD-207"


async def test_a_line_with_no_number_yet_is_named_by_company_alone(mock_register):
    """Half a name beats a UUID: a line numbered later still reads as its company."""
    lid = _seed_line(mock_register, tracker_no="")
    row = await _emit(mock_register, subject_type="Lending", subject_id=lid)
    assert row["title"] == "Awaiting checker approval — Suryodaya Energy Pvt Ltd"


async def test_an_unresolvable_subject_keeps_its_raw_id(mock_register):
    """A subject the register cannot find (deleted, wrong tenant, a type with no REST
    resource) must still NOTIFY. Losing the name is a cost; losing the notification is
    a defect — the maker would never learn their item was decided."""
    missing = uuid.uuid4().hex
    row = await _emit(mock_register, subject_type="Lending", subject_id=missing)
    assert row["title"] == f"Awaiting checker approval — Lending:{missing}"

    mock_register.state.notifications.clear()
    row = await _emit(mock_register, subject_type="EwsCase", subject_id=missing,
                      event="ews_escalated")
    assert row["title"] == f"Ews escalated — EwsCase:{missing}"


async def test_a_register_that_is_down_still_delivers_the_notification(mock_register,
                                                                      monkeypatch):
    """The lookup is decoration. If reading the line raises anything at all, the title
    falls back and the inbox row is still written."""
    lid = _seed_line(mock_register)
    real_get = activities.AsyncRegisterClient.get

    async def _boom(self, resource, obj_id, **kw):
        if resource in ("lending", "entities"):
            raise RuntimeError("register unreachable")
        return await real_get(self, resource, obj_id, **kw)

    monkeypatch.setattr(activities.AsyncRegisterClient, "get", _boom)
    row = await _emit(mock_register, subject_type="Lending", subject_id=lid)
    assert row["title"] == f"Awaiting checker approval — Lending:{lid}"


async def test_a_title_already_in_desk_language_is_left_alone(mock_register):
    """Only the raw "Type:uuid" is rewritten. A title an author wrote deliberately —
    the register's own maker notifications, for instance — passes through verbatim."""
    lid = _seed_line(mock_register)
    row = await _emit(mock_register, subject_type="Lending", subject_id=lid,
                      title="CP/CS checklist v1 approved")
    assert row["title"] == "CP/CS checklist v1 approved"


async def test_every_recipient_gets_the_same_named_title(mock_register):
    """The lookup happens ONCE for the fan-out, not once per recipient — and everyone
    reads the same sentence."""
    lid = _seed_line(mock_register)
    subject = f"Lending:{lid}"
    await ActivityEnvironment().run(
        activities.emit_operational_event, "sla_escalation",
        {"subject": subject, "notify": {
            "recipients": ["arun@evamfinance.com", "divya.rao@evamfinance.com"],
            "severity": "warning", "title": f"Sla escalation — {subject}",
            "discriminator": subject,
            "subject_type": "Lending", "subject_id": lid}})
    titles = {r["title"] for r in mock_register.state.notifications.values()}
    assert titles == {"Sla escalation — Suryodaya Energy Pvt Ltd · L042"}
    assert len(mock_register.state.notifications) == 2
