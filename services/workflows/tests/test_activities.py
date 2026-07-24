"""Activities exercised via Temporal's ActivityEnvironment against a mock Register."""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from app import activities
from app.types import InteractionInput

pytestmark = pytest.mark.asyncio


async def test_write_interaction(mock_register):
    env = ActivityEnvironment()
    inp = InteractionInput(entity_id="e-1", interaction_type="Phone Call",
                           summary="promoter call", source="VOX", performed_by="RM1")
    result = await env.run(activities.write_interaction, inp, "idem-1")

    assert result["entity_id"] == "e-1"
    sent = mock_register.state.written[0]
    assert sent["subject_type"] == "Entity" and sent["subject_id"] == "e-1"
    assert sent["source"] == "VOX" and sent["interaction_type"] == "Phone Call"


async def test_fetch_dossier(mock_register):
    env = ActivityEnvironment()
    d = await env.run(activities.fetch_dossier, "e-1")
    assert d["counts"]["interactions"] == 1
