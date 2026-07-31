"""The committee-decision request contract: facility-specific outcomes are first-class, the
grouped form still exists, and exactly ONE of the two forms must be used. Pure model tests —
the endpoint's coverage validation (every line decided, no unknowns/duplicates) is enforced
against the live lending book and exercised in the orchestrator tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api import CommitteeDecisionIn, FacilityDecision


def test_grouped_form_is_valid_alone():
    d = CommitteeDecisionIn(by="chair@evamfinance.com", approved=True)
    assert d.approved is True and d.facilities is None


def test_facility_form_is_valid_alone():
    d = CommitteeDecisionIn(
        by="chair@evamfinance.com",
        facilities=[FacilityDecision(lending_id="a", approved=True),
                    FacilityDecision(lending_id="b", approved=False, note="tenor too long")])
    assert d.approved is None
    assert [f.approved for f in d.facilities] == [True, False]


def test_neither_form_is_rejected():
    with pytest.raises(ValidationError, match="exactly one"):
        CommitteeDecisionIn(by="chair@evamfinance.com")


def test_both_forms_together_are_rejected():
    with pytest.raises(ValidationError, match="exactly one"):
        CommitteeDecisionIn(by="chair@evamfinance.com", approved=True,
                            facilities=[FacilityDecision(lending_id="a", approved=True)])


def test_empty_facility_list_is_rejected():
    # An empty list is NOT a decision on anything — it must not slip through as "form chosen".
    with pytest.raises(ValidationError, match="must not be empty"):
        CommitteeDecisionIn(by="chair@evamfinance.com", facilities=[])
