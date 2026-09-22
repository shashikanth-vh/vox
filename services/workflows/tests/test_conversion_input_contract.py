"""The conversion payload → workflow-input contract.

``start_conversion`` hands the API payload to the workflow as
``LeadConversionInput(**payload.model_dump(exclude=_CLIENT_ONLY_FIELDS))``.
Any payload field that is neither excluded nor a dataclass field is a
``TypeError`` — a 500 on EVERY Push to Deals, reachable only in a deployment
(the e2e conversion tests skip without a Temporal server, which is exactly how
``entity_id`` shipped broken). These tests pin the contract without Temporal:
they fail the suite the moment the payload model and the input dataclass drift.
"""

from __future__ import annotations

import dataclasses
import uuid

from app.api import _CLIENT_ONLY_FIELDS, LeadConversionIn
from app.types import CallerContext, LeadConversionInput


def _payload(**over) -> LeadConversionIn:
    base = dict(lead_id=str(uuid.uuid4()), requested_by="rm@evamfinance.com")
    base.update(over)
    return LeadConversionIn(**base)


def test_every_forwarded_payload_field_has_a_seat_in_the_workflow_input():
    """Set difference, so a future payload field added without a seat (or an
    exclude) names itself in the failure instead of 500ing in production."""
    forwarded = set(_payload().model_dump(exclude=_CLIENT_ONLY_FIELDS))
    accepted = {f.name for f in dataclasses.fields(LeadConversionInput)}
    homeless = forwarded - accepted
    assert not homeless, (
        f"LeadConversionIn field(s) {sorted(homeless)} are forwarded into "
        f"LeadConversionInput but the dataclass has no such field — add them to "
        f"_CLIENT_ONLY_FIELDS (pre-flight-only) or to LeadConversionInput.")


def test_a_dialog_resolved_client_payload_constructs_the_workflow_input():
    """The exact production shape that 500'd: an existing client resolved in the
    Push-to-Deals dialog rides in as entity_id. It is pre-flight material (the
    lead gets linked before the run starts) and must never reach the input."""
    payload = _payload(entity_id=str(uuid.uuid4()),
                       is_lending=True, lending_amount_cr=2.0,
                       company_name="Unique Sun Power", sector="Renewables")
    inp = LeadConversionInput(
        caller=CallerContext(), approver_notify=[],
        **payload.model_dump(exclude=_CLIENT_ONLY_FIELDS))
    assert inp.lead_id == payload.lead_id
    assert not hasattr(inp, "entity_id")
