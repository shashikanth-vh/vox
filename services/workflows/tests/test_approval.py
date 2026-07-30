"""The decision-verification gate. The single-winner decision RECORD is the sole authority:
the worker derives outcome, approver AND note from it — never the signal's latest-caller token
or note. A direct Temporal signal cannot convert or spoof a rejection; a transient Register
read must retry (not discard); and two approvers of the same outcome yield the SAME attribution."""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.config import get_settings

pytestmark = pytest.mark.asyncio

SIGN = "test-approval-signing-secret"
WF = "leadconv-EVAMdeadbeef00-lead1"
LEAD = "lead1"


async def _run(kind: str, by: str, wf: str = WF, *, tenant: str = "EVAM") -> dict:
    # The token/decision_ref args are vestigial (the record is authority); pass empties.
    return await ActivityEnvironment().run(
        activities.verify_decision, kind, by, "", wf, LEAD, tenant, "")


def _seed(mock, kind: str = "Approved", *, wf: str = WF, note: str | None = None,
          decided_by: str = "head@evamfinance.com") -> None:
    """Mimic the orchestrator's synchronous persist onto the single-winner resource."""
    mock.state.decisions[wf] = {
        "id": "rec-" + kind.lower(), "workflow_id": wf, "decision": kind,
        "lead_id": LEAD, "decided_by": decided_by, "decided_by_id": "u-1",
        "roles": ["BD Head"], "operations": {"push_lead_to_deals": "FULL"}, "views": {},
        "note": note}


async def test_no_recorded_decision_is_invalid(mock_register, monkeypatch):
    """A direct signal with no persisted decision — for BOTH approve and reject — is INVALID."""
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    assert (await _run("Approved", "x@y.com"))["valid"] is False
    assert (await _run("Rejected", "x@y.com"))["valid"] is False


async def test_record_backed_decision_is_valid_with_identity_from_record(mock_register, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    _seed(mock_register, "Approved", note="board approved")
    res = await _run("Approved", "someone@else.com")
    assert res["valid"] is True
    # Identity, grant AND note all come from the persisted record, not the caller.
    assert res["email"] == "head@evamfinance.com"
    assert res["operations"].get("push_lead_to_deals") == "FULL"
    assert res["note"] == "board approved"
    assert res["decision"] == "Approved"


async def test_signal_kind_must_match_the_recorded_decision(mock_register, monkeypatch):
    """An Approved record does not satisfy a reject signal (or vice-versa) — the outcome can't
    be flipped by signalling the opposite kind."""
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    _seed(mock_register, "Approved")
    assert (await _run("Approved", "h@e.com"))["valid"] is True
    assert (await _run("Rejected", "h@e.com"))["valid"] is False


async def test_same_outcome_different_approver_keeps_the_recorded_attribution(mock_register,
                                                                             monkeypatch):
    """Two approvers submit the same outcome; the record holds the FIRST approver. The worker
    must attribute to the record, never to a later caller."""
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    _seed(mock_register, "Approved", decided_by="first.head@evamfinance.com",
          note="first note")
    # A later caller ('by') is irrelevant — attribution comes from the record.
    res = await _run("Approved", "second.head@evamfinance.com")
    assert res["email"] == "first.head@evamfinance.com"
    assert res["note"] == "first note"


async def test_transient_read_failure_raises_so_temporal_retries(mock_register, monkeypatch):
    """P0: a TRANSIENT decision-read failure must RAISE (so Temporal retries and the decision
    is not consumed) — it must NOT be swallowed into an 'invalid' that discards the decision."""
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    _seed(mock_register, "Approved")
    mock_register.state.decision_fail = 6   # past the client's retry budget (3)
    with pytest.raises(Exception):  # noqa: B017 - RegisterError propagates → activity retries
        await _run("Approved", "h@e.com")


async def test_retry_workflow_id_is_url_safe_and_resolvable(mock_register, monkeypatch):
    """A retry attempt uses a URL-safe '-r2' id (never '#2'); its decision resolves normally."""
    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    retry_wf = f"{WF}-r2"
    assert "#" not in retry_wf
    _seed(mock_register, "Approved", wf=retry_wf)
    res = await _run("Approved", "h@e.com", wf=retry_wf)
    assert res["valid"] is True and res["email"] == "head@evamfinance.com"


async def test_dev_mode_trusts_the_signal(monkeypatch):
    # No signing configured (dev) → the signal is trusted (identity = 'by'), no record needed.
    monkeypatch.setattr(get_settings(), "internal_signing_secret", "")
    res = await _run("Approved", "rm@evamfinance.com")
    assert res["valid"] is True and res["email"] == "rm@evamfinance.com"
    assert res["decision"] == "Approved"


async def test_conversion_fails_closed_without_verified_approver(monkeypatch):
    """In production (signing on) a conversion with NO verified approver is refused — no
    fallback to the original requester's authority."""
    from temporalio.exceptions import ApplicationError

    from app.types import CallerContext, LeadConversionInput

    monkeypatch.setattr(get_settings(), "internal_signing_secret", SIGN)
    inp = LeadConversionInput(
        lead_id="lead1", requested_by="rm@evamfinance.com", is_lending=True,
        caller=CallerContext(tenant="EVAM", email="rm@evamfinance.com"))
    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(
            activities.convert_lead_txn, inp, "wf:leadconv-x:convert", None, None)
