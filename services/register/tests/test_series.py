"""Number series — the mint is sequential, per-series, and service-lane only.

The credit-note reference the maker used to type is now drawn from here: the
orchestrator mints ``CN/<company>/<yyyymm>-<seq>`` at send time. What matters is that
the same number can never be issued twice — the mint is a single atomic upsert — and
that a human key cannot burn numbers directly.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from tests.test_handover import ADMIN

pytestmark = pytest.mark.asyncio

SVC = {"X-API-Key": "trn-key"}


async def test_series_numbers_are_sequential_per_key_and_service_only(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"trn-key": "svc_workflows"})

    # A human key cannot mint — numbers come from the orchestrator at send time.
    human = await client.post("/v1/internal/number-series/next",
                              json={"series_key": "credit-note/ACME/202608"},
                              headers=ADMIN)
    assert human.status_code == 403

    one = await client.post("/v1/internal/number-series/next",
                            json={"series_key": "credit-note/ACME/202608"}, headers=SVC)
    assert one.status_code == 200, one.text
    assert one.json()["value"] == 1
    two = await client.post("/v1/internal/number-series/next",
                            json={"series_key": "credit-note/ACME/202608"}, headers=SVC)
    assert two.json()["value"] == 2

    # A different series (another company, another month) starts its own count.
    other = await client.post("/v1/internal/number-series/next",
                              json={"series_key": "credit-note/ZETA/202608"}, headers=SVC)
    assert other.json()["value"] == 1


async def test_concurrent_mints_never_share_a_number(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"trn-key": "svc_workflows"})
    key = {"series_key": "credit-note/RACE/202608"}
    results = await asyncio.gather(*[
        client.post("/v1/internal/number-series/next", json=key, headers=SVC)
        for _ in range(6)])
    values = sorted(r.json()["value"] for r in results)
    assert values == [1, 2, 3, 4, 5, 6]
