"""Increment 8 (Register side) — covenants, EWS cases, waivers, deal closure.

* The recurring engine: schedule projection is pure and clamped; generation is
  DB-idempotent (one observation per covenant+period, ever); overdue and lapsed-waiver
  flips are reported exactly once.
* A breach auto-opens its (deduped) EWS case in the same transaction as the result.
* A waiver takes effect only against a durable, senior-authority, TIME-BOXED decision;
  the sweep expires it and the breach is live again with a fresh case.
* The EWS lifecycle: assign → investigate → escalate (reasons mandatory) → close
  (disposition + note; an escalated case only closes with senior authority); Closed
  rows are frozen by the database.
* Deal closure is open-item validated and never a bare stage edit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.api.covenants import add_months, breach_test, due_dates
from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}
ANALYST = {"X-User-Email": "da@evamfinance.com", "X-User-Roles": "Deal Analyst"}
BDRM = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM"}
SVC = {"X-API-Key": "cov-key"}


# --------------------------------------------------------------------------------------- #
# Pure schedule + breach math (exactly what the sweep runs)
# --------------------------------------------------------------------------------------- #
async def test_add_months_clamps_short_months():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)   # leap year
    assert add_months(date(2026, 10, 31), 2) == date(2026, 12, 31)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)  # year rollover


async def test_due_dates_projection_and_onetime():
    quarterly = due_dates(date(2026, 3, 31), "Quarterly", date(2026, 12, 31))
    assert quarterly == [date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30),
                         date(2026, 12, 31)]
    assert due_dates(date(2026, 3, 31), "OneTime", date(2027, 1, 1)) == [date(2026, 3, 31)]
    assert due_dates(date(2027, 1, 1), "Monthly", date(2026, 12, 31)) == []  # not yet due


async def test_breach_test_operators():
    assert breach_test(">=", 1.20, 1.05) is True      # DSCR below the floor → breach
    assert breach_test(">=", 1.20, 1.20) is False
    assert breach_test("<=", 4.0, 5.1) is True        # leverage above the cap → breach
    assert breach_test("<", 4.0, 4.0) is True
    assert breach_test("=", 100.0, 100.0) is False


# --------------------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------------------- #
async def _entity(client) -> str:  # noqa: ANN001
    code = "COV" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities", json={"code": code, "legal_name": f"Cov {code}",
                                                "entity_type": "Company"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _deal(client, eid) -> str:  # noqa: ANN001
    r = await client.post("/v1/deals", json={"entity_id": eid, "stage": "New Inquiry"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _covenant(client, eid, did=None, **over):  # noqa: ANN001
    body = {"entity_id": eid, "deal_id": did, "name": "DSCR >= 1.20",
            "covenant_type": "Financial", "metric": "dscr", "operator": ">=",
            "threshold": 1.20, "frequency": "Quarterly",
            "first_due_on": (datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
            **over}
    r = await client.post("/v1/covenants", json={k: v for k, v in body.items()
                                                 if v is not None}, headers=CREDIT_HEAD)
    assert r.status_code == 201, r.text
    return r.json()


async def _sweep(client) -> dict:  # noqa: ANN001
    r = await client.post("/v1/internal/covenants/run-sweep", json={}, headers=SVC)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _svc_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "service_api_keys", {"cov-key": "svc_workflows"})


async def _seed_waiver_decision(mid: str, *, roles=("Credit Head",),
                                valid_days: int | None = 90,
                                subject_id: str | None = None) -> str:
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    wf = f"waiver-{uuid.uuid4().hex[:12]}"
    async with get_sessionmaker()() as s:
        await s.execute(text(
            "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, "
            "subject_id, run_id, decided_by, decided_by_id, roles, valid_days, "
            "tenant_id, note) "
            "SELECT :wf, 'Approved', 'Monitoring', :sid, 'run-1', "
            "'ch@evamfinance.com', 'u-7', CAST(:roles AS jsonb), :vd, tenant_id, "
            "'headroom restored by Q3' "  # noqa: S608
            "FROM monitoring_reporting WHERE id = CAST(:mid AS uuid)"),
            {"wf": wf, "sid": subject_id or mid, "mid": mid,
             "roles": '["' + '","'.join(roles) + '"]', "vd": valid_days})
        await s.commit()
    return wf


# --------------------------------------------------------------------------------------- #
# The recurring engine
# --------------------------------------------------------------------------------------- #
async def test_sweep_generates_idempotently_and_flags_overdue_once(client):
    eid = await _entity(client)
    cov = await _covenant(client, eid)                    # first due YESTERDAY, grace 0
    first = await _sweep(client)
    mine = [o for o in first["overdue"]
            if o["covenant_name"] == cov["name"] and o["entity_id"] == eid]
    assert first["generated"] >= 1
    assert len(mine) == 1                                 # yesterday's period, now overdue

    # RECURRING path: the second sweep re-projects the SAME schedule — the DB unique
    # index absorbs every period already generated, and the overdue flip was one-shot.
    second = await _sweep(client)
    assert not [o for o in second["overdue"] if o["entity_id"] == eid]
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    async with get_sessionmaker()() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM monitoring_reporting WHERE record_type='Covenant' "
            "AND details->>'covenant_id' = :cid"), {"cid": cov["id"]})).scalar()
    assert n == 1                                         # one row per period, ever

    # Definitions are credit governance: an RM can neither define nor amend.
    r = await client.post("/v1/covenants", json={
        "entity_id": eid, "name": "X", "covenant_type": "Reporting",
        "first_due_on": "2027-01-01"}, headers=BDRM)
    assert r.status_code == 403
    # A Financial covenant without its test shape is refused.
    r = await client.post("/v1/covenants", json={
        "entity_id": eid, "name": "Y", "covenant_type": "Financial",
        "first_due_on": "2027-01-01"}, headers=CREDIT_HEAD)
    assert r.status_code == 422


async def test_result_breach_opens_deduped_ews_case(client):
    eid = await _entity(client)
    cov = await _covenant(client, eid, breach_severity="Red")
    report = await _sweep(client)
    obs_id = next(o["id"] for o in report["overdue"] if o["entity_id"] == eid)

    # The analyst submits a FAILING actual → Breached, and the EWS case opens in the
    # same transaction (severity from the covenant definition).
    r = await client.post(f"/v1/monitoring/{obs_id}/result",
                          json={"actual_value": 1.05}, headers=ANALYST)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Breached" and body["breached"] is True
    case_id = body["ews_case_id"]
    assert case_id is not None
    case = (await client.get(f"/v1/ews-cases/{case_id}", headers=CREDIT_HEAD)).json()
    assert case["source"] == "covenant" and case["source_ref"] == obs_id
    assert case["severity"] == "Red" and case["status"] == "Open"
    assert cov["name"] in case["title"]

    # A result is a FACT — no overwrite; a corrected figure is a governance event.
    r = await client.post(f"/v1/monitoring/{obs_id}/result",
                          json={"actual_value": 1.30}, headers=ANALYST)
    assert r.status_code == 409

    # A financial observation without the actual is refused; a PASSING result on the
    # next period is Compliant and opens NOTHING.
    nxt = await _covenant(client, eid, name="Leverage <= 4.0", metric="leverage",
                          operator="<=", threshold=4.0)
    report = await _sweep(client)
    obs2 = next(o["id"] for o in report["overdue"]
                if o["covenant_name"] == nxt["name"])
    assert (await client.post(f"/v1/monitoring/{obs2}/result", json={},
                              headers=ANALYST)).status_code == 422
    r = await client.post(f"/v1/monitoring/{obs2}/result",
                          json={"actual_value": 3.2}, headers=ANALYST)
    assert r.status_code == 200 and r.json()["status"] == "Compliant"
    assert r.json()["ews_case_id"] is None


# --------------------------------------------------------------------------------------- #
# Waivers: decision-backed, time-boxed, expiring
# --------------------------------------------------------------------------------------- #
async def test_waiver_requires_senior_timeboxed_decision_and_expires(client):
    eid = await _entity(client)
    await _covenant(client, eid)
    report = await _sweep(client)
    obs_id = next(o["id"] for o in report["overdue"] if o["entity_id"] == eid)
    r = await client.post(f"/v1/monitoring/{obs_id}/result",
                          json={"actual_value": 0.9}, headers=ANALYST)
    assert r.json()["status"] == "Breached"

    # An invented reference is refused; a decision made WITHOUT senior authority is
    # refused; one WITHOUT a validity window is refused (a waiver is never open-ended).
    r = await client.post(f"/v1/monitoring/{obs_id}/waive",
                          json={"decision_ref": "invented"}, headers=CREDIT_HEAD)
    assert r.status_code == 422
    junior = await _seed_waiver_decision(obs_id, roles=("BDRM",))
    assert (await client.post(f"/v1/monitoring/{obs_id}/waive",
                              json={"decision_ref": junior},
                              headers=CREDIT_HEAD)).status_code == 403
    openended = await _seed_waiver_decision(obs_id, valid_days=None)
    assert (await client.post(f"/v1/monitoring/{obs_id}/waive",
                              json={"decision_ref": openended},
                              headers=CREDIT_HEAD)).status_code == 422

    # The REAL waiver: senior, subject-bound, time-boxed → Waived with a lapse date.
    good = await _seed_waiver_decision(obs_id, valid_days=90)
    r = await client.post(f"/v1/monitoring/{obs_id}/waive",
                          json={"decision_ref": good}, headers=CREDIT_HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Waived" and body["waiver_status"] == "Granted"
    assert body["waiver_decision_ref"] == good and body["waiver_valid_until"]

    # EXPIRY: lapse the window, sweep → the waiver expires ONCE, the breach is live
    # again, and a FRESH case opens under the waiver_expiry source.
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    async with get_sessionmaker()() as s:
        await s.execute(text(
            "UPDATE monitoring_reporting SET waiver_valid_until = :d "
            "WHERE id = CAST(:mid AS uuid)"),
            {"d": datetime.now(UTC).date() - timedelta(days=1), "mid": obs_id})
        await s.commit()
    report = await _sweep(client)
    lapsed = [w for w in report["waivers_expired"] if w["id"] == obs_id]
    assert len(lapsed) == 1
    report2 = await _sweep(client)
    assert not [w for w in report2["waivers_expired"] if w["id"] == obs_id]
    obs = [c for c in (await client.get(
        "/v1/ews-cases", params={"entity_id": eid},
        headers=CREDIT_HEAD)).json()["items"] if c["source"] == "waiver_expiry"]
    assert len(obs) == 1 and obs[0]["severity"] == "Red"


# --------------------------------------------------------------------------------------- #
# The EWS lifecycle
# --------------------------------------------------------------------------------------- #
async def test_ews_lifecycle_escalation_authority_and_freeze(client):
    eid = await _entity(client)
    r = await client.post("/v1/ews-cases", json={
        "entity_id": eid, "severity": "Amber", "title": "Promoter pledge spike",
        "source": "manual"}, headers=CREDIT_HEAD)
    assert r.status_code == 201, r.text
    case = r.json()
    cid = case["id"]

    # Dedupe: the SAME trigger source can never spawn a second case.
    dup = await client.post("/v1/ews-cases", json={
        "entity_id": eid, "severity": "Red", "title": "duplicate",
        "source": case["source"], "source_ref": case["source_ref"]},
        headers=CREDIT_HEAD)
    assert dup.status_code == 201 and dup.json()["id"] == cid

    # Assign → UnderInvestigation; escalation REQUIRES reasons.
    r = await client.post(f"/v1/ews-cases/{cid}/assign",
                          json={"assignee": "da@evamfinance.com"}, headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "UnderInvestigation"
    assert (await client.post(f"/v1/ews-cases/{cid}/escalate", json={},
                              headers=CREDIT_HEAD)).status_code == 422
    # A scoped desk (Deal Analyst) outside this company's scope is refused outright —
    # the central evaluator, not case fields, decides reach.
    assert (await client.post(f"/v1/ews-cases/{cid}/escalate",
                              json={"note": "x"}, headers=ANALYST)).status_code == 403
    r = await client.post(f"/v1/ews-cases/{cid}/escalate",
                          json={"note": "pledge > 60%, lender chatter"},
                          headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Escalated"

    # An ESCALATED case cannot be buried below the level it escalated to: the assignee
    # is refused; senior credit authority closes it (disposition + note mandatory).
    assert (await client.post(f"/v1/ews-cases/{cid}/close",
                              json={"disposition": "Resolved", "note": "x"},
                              headers=ANALYST)).status_code == 403
    assert (await client.post(f"/v1/ews-cases/{cid}/close",
                              json={"disposition": "Resolved", "note": ""},
                              headers=CREDIT_HEAD)).status_code == 422
    r = await client.post(f"/v1/ews-cases/{cid}/close",
                          json={"disposition": "Downgraded",
                                "note": "pledge released after refinancing"},
                          headers=CREDIT_HEAD)
    assert r.status_code == 200 and r.json()["status"] == "Closed"
    assert r.json()["closed_by"] == "ch@evamfinance.com"

    # Closed = frozen: every further action is refused (409), backed by the DB trigger.
    for path, body in ((f"/v1/ews-cases/{cid}/assign", {"assignee": "x@y.z"}),
                       (f"/v1/ews-cases/{cid}/escalate", {"note": "again"}),
                       (f"/v1/ews-cases/{cid}/close",
                        {"disposition": "Resolved", "note": "again"})):
        assert (await client.post(path, json=body,
                                  headers=CREDIT_HEAD)).status_code == 409


async def test_ews_auto_escalation_is_service_only_and_idempotent(client):
    eid = await _entity(client)
    case = (await client.post("/v1/ews-cases", json={
        "entity_id": eid, "title": "DSO stretch", "source": "manual"},
        headers=CREDIT_HEAD)).json()
    cid = case["id"]
    # A human key cannot drive the SLA plumbing.
    r = await client.post(f"/v1/internal/ews-cases/{cid}/auto-escalate",
                          json={"reason": "sla"}, headers=ADMIN)
    assert r.status_code == 403
    r = await client.post(f"/v1/internal/ews-cases/{cid}/auto-escalate",
                          json={"reason": "Investigation SLA (72h) lapsed.",
                                "workflow_id": "ews-x-1"}, headers=SVC)
    assert r.status_code == 200 and r.json()["status"] == "Escalated"
    assert r.json()["escalated_by"] == "system:sla"
    # Idempotent replay; and a case closed meanwhile comes back Closed, untouched.
    again = await client.post(f"/v1/internal/ews-cases/{cid}/auto-escalate",
                              json={"reason": "retry"}, headers=SVC)
    assert again.status_code == 200 and again.json()["escalated_by"] == "system:sla"


# --------------------------------------------------------------------------------------- #
# Deal closure with open-item validation
# --------------------------------------------------------------------------------------- #
async def test_deal_close_validates_open_items_then_closes(client):
    eid = await _entity(client)
    did = await _deal(client, eid)
    for fs in ("In Screening", "In Pipeline"):
        assert (await client.patch(f"/v1/deals/{did}",
                                   json={"stage": fs})).status_code == 200
    # Three kinds of open item: a mid-pipeline product line, an open EWS case, and a
    # breached covenant observation.
    line = (await client.post("/v1/lending", json={
        "entity_id": eid, "deal_id": did, "stage": "Diligence"})).json()
    case = (await client.post("/v1/ews-cases", json={
        "entity_id": eid, "deal_id": did, "title": "Offtaker payment delay",
        "source": "manual"}, headers=CREDIT_HEAD)).json()
    await _covenant(client, eid, did)
    report = await _sweep(client)
    obs_id = next(o["id"] for o in report["overdue"] if o["deal_id"] == did)
    await client.post(f"/v1/monitoring/{obs_id}/result", json={"actual_value": 0.8},
                      headers=ANALYST)

    items = (await client.get(f"/v1/deals/{did}/open-items")).json()
    assert items["blocked"] is True
    assert [x["id"] for x in items["lines"]] == [line["id"]]
    assert {c["id"] for c in items["ews_cases"]} >= {case["id"]}
    assert any(c["id"] == obs_id for c in items["covenants"])
    r = await client.post(f"/v1/deals/{did}/close",
                          json={"outcome": "won", "note": "trying too early"})
    assert r.status_code == 422 and "cannot close" in r.text.lower()

    # Resolve each: the line reaches a terminal, the case closes, the breach is waived.
    assert (await client.patch(f"/v1/lending/{line['id']}",
                               json={"stage": "Rejected"})).status_code == 200
    # (The covenant-breach case auto-opened above must close too.)
    open_cases = (await client.get("/v1/ews-cases",
                                   params={"deal_id": did, "open_only": "true"},
                                   headers=CREDIT_HEAD)).json()["items"]
    for c in open_cases:
        assert (await client.post(f"/v1/ews-cases/{c['id']}/close",
                                  json={"disposition": "Resolved",
                                        "note": "settled before closure"},
                                  headers=CREDIT_HEAD)).status_code == 200
    wf = await _seed_waiver_decision(obs_id, valid_days=180)
    assert (await client.post(f"/v1/monitoring/{obs_id}/waive",
                              json={"decision_ref": wf},
                              headers=CREDIT_HEAD)).status_code == 200

    assert (await client.get(f"/v1/deals/{did}/open-items")).json()["blocked"] is False
    # A note is mandatory; then the close lands through the normal funnel policy.
    assert (await client.post(f"/v1/deals/{did}/close",
                              json={"outcome": "won"})).status_code == 422
    r = await client.post(f"/v1/deals/{did}/close",
                          json={"outcome": "won", "note": "facility fully placed"})
    assert r.status_code == 200 and r.json()["stage"] == "Closed Won"
    # Terminal is final — funnel policy, not this endpoint.
    r = await client.post(f"/v1/deals/{did}/close",
                          json={"outcome": "lost", "note": "flip"})
    assert r.status_code == 422
