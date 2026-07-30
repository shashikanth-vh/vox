"""R11 hardening: an unnamed (generic) API key fails closed under enforce_rbac — no
tenant-wide read, no deleted-row read — so a leaked generic key can't pivot the data plane."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _enforced():
    s = get_settings()
    prior = s.enforce_rbac
    s.enforce_rbac = True
    try:
        yield
    finally:
        s.enforce_rbac = prior


async def test_generic_key_cannot_read_under_rbac(client: AsyncClient, _enforced):
    # The `client` fixture presents the generic (unnamed) test key with NO user context.
    r = await client.get("/v1/leads")
    assert r.status_code == 403, r.text
    assert "unnamed" in r.text.lower()
    r = await client.get("/v1/entities")
    assert r.status_code == 403, r.text


async def test_generic_key_cannot_read_deleted_under_rbac(client: AsyncClient, _enforced):
    r = await client.get("/v1/leads", params={"include_deleted": "true"})
    assert r.status_code == 403, r.text
    assert "deleted" in r.text.lower() or "unnamed" in r.text.lower()


async def test_generic_key_still_reads_without_rbac(client: AsyncClient):
    # Compatibility mode (enforce_rbac off, the default fixture state): the generic key
    # still reads, so dev/local flows are unaffected.
    assert (await client.get("/v1/leads")).status_code == 200
