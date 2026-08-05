"""Lending increment ④ — the LMS core.

The loan account opens itself on the FIRST Advaya-confirmed tranche (header from the
sanction terms, borrower from the entity, per-tenant account number), later tranches
raise the principal, and the statement ledger behaves like the servicing team's Excel:
Date | Particulars | Debit | Credit | Balance, with interest COMPUTED (balance × rate% ×
days ÷ day-count) through the preview-then-accrue pair — the "calculate the percentage"
option — never hand-keyed.
"""

from __future__ import annotations

import pytest

from tests.test_handover import ADMIN
from tests.test_increment4 import SVC, _accepted_line

pytestmark = pytest.mark.asyncio


def test_the_interest_formula_is_exact():
    from app.api.lms import interest_amount

    # 8.0 Cr at 15% for 7 days on a 365 base: 8 × 0.15 × 7/365.
    assert interest_amount(8.0, 15.0, 7, "365") == 0.02
    # A 360-day base divides differently — the classic banker's month.
    assert interest_amount(100000.0, 12.0, 30, "360") == 1000.0


async def test_tranches_open_and_grow_the_account_and_the_ledger_reconciles(
        client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "service_api_keys", {"trn-key": "svc_workflows"})
    lid = await _accepted_line(client, monkeypatch)
    # The terms give the account its rate / tenor / EMI — entered once, copied here.
    t = await client.post("/v1/internal/sanction-terms", json={
        "lending_id": lid, "amount_cr": 8.0, "rate_kind": "Fixed", "rate_pct": 15.0,
        "tenor_months": 54, "emi_amount": 0.18, "day_count": "365",
        # A DEFERRED covenant: no first due date — its schedule starts with the money.
        "covenants": [{"name": "Monthly stock statement", "covenant_type": "Reporting",
                       "frequency": "Monthly"}]}, headers=ADMIN)
    assert t.status_code == 201, t.text
    assert len(t.json()["seeded_covenant_ids"]) == 1

    # No tranche yet → no account (it is Advaya's confirmation that opens it).
    none = await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)
    assert none.status_code == 404

    # T1: the account opens with the disbursement row.
    r = await client.post(f"/v1/internal/lending/{lid}/tranches",
                          json={"tranche_ref": "T1", "amount": 5.0,
                                "disbursed_on": "2026-03-24"}, headers=SVC)
    assert r.status_code == 201, r.text
    body = (await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)).json()
    acct = body["account"]
    assert acct["borrower"] == "Handover Co" and acct["account_no"] >= 1
    assert acct["amount"] == 5.0 and acct["rate_pct"] == 15.0
    assert acct["status"] == "Standard" and acct["overdue_position"] == "Nil"
    assert body["entries"] == [{
        "entry_no": 1, "entry_date": "2026-03-24", "particulars": "Loan Disbursement",
        "entry_type": "Disbursement", "debit": 5.0, "credit": None, "balance": 5.0}]

    # The deferred covenant's schedule STAMPED ITSELF at the first tranche:
    # one Monthly cycle after 2026-03-24.
    eid = (await client.get(f"/v1/lending/{lid}")).json()["entity_id"]
    covs = (await client.get("/v1/covenants", params={"entity_id": eid},
                             headers=ADMIN)).json()
    cov_rows = covs.get("items", [])
    assert cov_rows and cov_rows[0]["first_due_on"] == "2026-04-24", cov_rows

    # T2 grows the principal — same account, its own ledger row.
    r2 = await client.post(f"/v1/internal/lending/{lid}/tranches",
                           json={"tranche_ref": "T2", "amount": 3.0,
                                 "disbursed_on": "2026-03-27"}, headers=SVC)
    assert r2.status_code == 201, r2.text
    body = (await client.get(f"/v1/lending/{lid}/loan-account", headers=ADMIN)).json()
    assert body["account"]["amount"] == 8.0
    assert body["entries"][1]["particulars"] == "Loan Disbursement (T2)"
    assert body["entries"][1]["balance"] == 8.0

    # The CALCULATOR: preview shows the figure AND its inputs, writes nothing.
    pv = await client.get(f"/v1/lending/{lid}/loan-account/interest-preview",
                          params={"upto": "2026-03-31"}, headers=ADMIN)
    assert pv.status_code == 200, pv.text
    p = pv.json()
    assert p["days"] == 7 and p["balance"] == 8.0 and p["interest"] == 0.02
    assert "×" in p["formula"] or "x" in p["formula"].lower()
    assert len((await client.get(f"/v1/lending/{lid}/loan-account",
                                 headers=ADMIN)).json()["entries"]) == 2

    # Accrue writes exactly that row; accruing to the same date again is refused.
    ac = await client.post(f"/v1/lending/{lid}/loan-account/accrue",
                           json={"upto": "2026-03-31"}, headers=ADMIN)
    assert ac.status_code == 201, ac.text
    assert ac.json()["debit"] == 0.02 and ac.json()["balance"] == 8.02
    dup = await client.post(f"/v1/lending/{lid}/loan-account/accrue",
                            json={"upto": "2026-03-31"}, headers=ADMIN)
    assert dup.status_code == 422

    # An EMI receipt credits the balance down.
    emi = await client.post(f"/v1/lending/{lid}/loan-account/entries",
                            json={"entry_date": "2026-04-05", "kind": "EMI",
                                  "amount": 0.5}, headers=ADMIN)
    assert emi.status_code == 201, emi.text
    assert emi.json()["credit"] == 0.5 and emi.json()["balance"] == 7.52
    assert emi.json()["particulars"] == "EMI Paid"

    # Classification is the servicing team's call; closure freezes the ledger.
    cl = await client.patch(f"/v1/lending/{lid}/loan-account",
                            json={"overdue_position": "1 EMI overdue",
                                  "status": "SMA"}, headers=ADMIN)
    assert cl.status_code == 200 and cl.json()["status"] == "SMA"
    done = await client.patch(f"/v1/lending/{lid}/loan-account",
                              json={"closed_on": "2026-12-31"}, headers=ADMIN)
    assert done.status_code == 200 and done.json()["status"] == "Closed"
    frozen = await client.post(f"/v1/lending/{lid}/loan-account/entries",
                               json={"entry_date": "2027-01-05", "kind": "Charge",
                                     "amount": 0.1}, headers=ADMIN)
    assert frozen.status_code == 409


async def test_the_committee_decision_reads_back_on_the_line(client):
    """The sanction-terms screen shows the committee's own words — the recorded
    decision, decider and note come back through the line-scoped read."""
    from tests.test_handover import _entity, _ready_lending

    eid = await _entity(client)
    lid = await _ready_lending(client, eid)
    r = await client.get(f"/v1/lending/{lid}/committee-decision", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "Approved"
    assert body["decided_by"] == "ch@evamfinance.com"


async def test_lms_roles_split_the_maker_from_the_checker(client, monkeypatch):
    """The servicing pair (RBAC v3.5): the LMS OPERATOR posts routine ledger events
    but cannot classify; the LMS MANAGEMENT holds classification/closure. The LMS
    covenant tab's observations read answers under either role."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "service_api_keys", {"trn-key": "svc_workflows"})
    lid = await _accepted_line(client, monkeypatch)
    t = await client.post("/v1/internal/sanction-terms", json={
        "lending_id": lid, "amount_cr": 2.0, "rate_kind": "Fixed", "rate_pct": 12.0,
        "day_count": "365"}, headers=ADMIN)
    assert t.status_code == 201, t.text
    r = await client.post(f"/v1/internal/lending/{lid}/tranches",
                          json={"tranche_ref": "T1", "amount": 2.0,
                                "disbursed_on": "2026-04-01"}, headers=SVC)
    assert r.status_code == 201, r.text

    OPERATOR = {**ADMIN, "X-User-Email": "ops@evamfinance.com",
                "X-User-Roles": "LMS Operator"}
    AUTHORIZER = {**ADMIN, "X-User-Email": "authz@evamfinance.com",
                  "X-User-Roles": "LMS Management"}

    # The operator's verb: a routine receipt posts.
    ok = await client.post(f"/v1/lending/{lid}/loan-account/entries",
                           json={"entry_date": "2026-04-10", "kind": "Receipt",
                                 "amount": 0.1}, headers=OPERATOR)
    assert ok.status_code == 201, ok.text

    # The hard-to-reverse verb is NOT the operator's — four-eyes over account state.
    no = await client.patch(f"/v1/lending/{lid}/loan-account",
                            json={"status": "SMA"}, headers=OPERATOR)
    assert no.status_code == 403, no.text
    yes = await client.patch(f"/v1/lending/{lid}/loan-account",
                             json={"status": "SMA"}, headers=AUTHORIZER)
    assert yes.status_code == 200, yes.text

    # The LMS covenant tab's read: the company's observations list (none generated
    # yet — the sweep makes them as periods fall due).
    eid = (await client.get(f"/v1/lending/{lid}")).json()["entity_id"]
    obs = await client.get("/v1/covenants/observations",
                           params={"entity_id": eid}, headers=OPERATOR)
    assert obs.status_code == 200, obs.text
    assert obs.json()["items"] == []
