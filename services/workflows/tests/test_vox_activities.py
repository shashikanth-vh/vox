"""VOX activity + matching tests against the mock Register (no Temporal needed)."""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.activities import _canonical, _entity_code
from app.types import VoxTouchpoint

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Pure: canonical matching rules
# --------------------------------------------------------------------------- #
def test_canonical_strips_suffixes_and_case():
    assert _canonical("EcoSoch Solar Pvt. Ltd") == "ecosoch solar"
    assert _canonical("ECOSOCH   SOLAR PRIVATE LIMITED") == "ecosoch solar"
    assert _canonical("Sunrise Green Power LLP") == "sunrise green power"


def test_entity_code_is_deterministic():
    a = _entity_code("EcoSoch Solar Pvt Ltd")
    assert a == _entity_code("EcoSoch Solar Pvt Ltd")   # retries derive the same code
    assert a.startswith("ECOSOCHSOLAR-")


# --------------------------------------------------------------------------- #
# Activities against the mock Register
# --------------------------------------------------------------------------- #
async def test_resolve_entity_matches_canonically(mock_register):
    mock_register.state.entities.append(
        {"id": "e-1", "code": "ECOSOCH", "legal_name": "EcoSoch Solar Private Limited",
         "display_name": None})
    env = ActivityEnvironment()
    found = await env.run(activities.resolve_entity, "EcoSoch Solar Pvt Ltd")
    assert found and found["id"] == "e-1"
    missing = await env.run(activities.resolve_entity, "Some Other Company")
    assert missing is None


async def test_log_touchpoint_carries_full_fidelity(mock_register):
    tp = VoxTouchpoint(
        company_name="EcoSoch Solar", capture_id="cap-1",
        interaction_type="Site Visit / Due Diligence",
        transcript="All rooftops above P50.", audio_ref="s3://vox/cap-1.ogg",
        performed_by="Chetan", assigned_rm="Shubh",
        next_meeting_date="2026-08-04", next_action="Collect banking data",
        attendees=["Chetan", "CFO"], key_intel={"dscr": 1.8},
    )
    env = ActivityEnvironment()
    await env.run(activities.log_touchpoint, tp, "e-1", "l-1", "wf:test:interaction")
    written = mock_register.state.written[-1]
    # Logged against the LEAD (the register rolls it up to the entity timeline itself),
    # so the lead drawer's own timeline shows the touchpoint.
    assert written["subject_type"] == "Lead" and written["subject_id"] == "l-1"
    assert written["transcript"] == "All rooftops above P50."
    assert written["attachments"] == [{"kind": "audio", "uri": "s3://vox/cap-1.ogg"}]
    assert written["source"] == "VOX"
    assert written["source_ref"]                       # the Temporal workflow id
    assert written["meta"]["assigned_rm"] == "Shubh"
    assert written["meta"]["acting_rm"] == "Chetan"
    assert written["meta"]["calendar"]["status"] == "pending"
    assert written["key_intel"] == {"dscr": 1.8}


async def test_create_lead_backfills_company_from_entity(mock_register):
    """An entity_id-only capture (no company_name) names the lead after the entity's
    legal name — not the '(unknown)' placeholder the grids would otherwise display."""
    mock_register.state.entities.append(
        {"id": "e-77", "code": "SUNRISE", "legal_name": "Sunrise Green Power LLP",
         "display_name": None})
    tp = VoxTouchpoint(entity_id="e-77", performed_by="Chetan")
    env = ActivityEnvironment()
    lead = await env.run(activities.create_lead, tp, "e-77", "wf:test:lead")
    assert lead["company"] == "Sunrise Green Power LLP"


async def test_create_lead_defaults_rm_to_acting_rm(mock_register):
    tp = VoxTouchpoint(company_name="New Co", performed_by="Chetan",
                       occurred_at="2026-07-25T10:00:00+05:30",
                       next_action="Send intro deck", next_action_date="2026-07-30")
    env = ActivityEnvironment()
    lead = await env.run(activities.create_lead, tp, "e-9", "wf:test:lead")
    assert lead["rm"] == "Chetan"
    assert lead["status"] == "Active"
    assert lead["last_interaction_date"] == "2026-07-25"
