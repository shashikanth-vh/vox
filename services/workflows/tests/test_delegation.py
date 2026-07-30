"""The worker propagates the caller's TENANT and re-mints a signed context so the Register
authorizes writes as the human — not the worker's service key."""

from __future__ import annotations

import pytest

from app import activities
from app.config import get_settings
from app.types import CallerContext

pytestmark = pytest.mark.asyncio


async def test_client_propagates_tenant_and_delegates_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "internal_signing_secret", "test-signing-secret")

    caller = CallerContext(tenant="TENANTB", email="rm@evamfinance.com", user_id="u-1",
                           roles=["BDRM"], effective_operations={"add_lead": "FULL"},
                           decision="SCOPED")
    reg = activities._client(caller)
    try:
        # Tenant comes from the CALLER, never the fixed default.
        assert reg.config.tenant == "TENANTB"
        # A signed context is minted so the Register enforces as the human.
        assert "X-Internal-Context" in (reg.config.extra_headers or {})
    finally:
        await reg.aclose()

    # No identity → no delegation, but the tenant is still honoured.
    reg2 = activities._client(CallerContext(tenant="TENANTB"))
    try:
        assert reg2.config.tenant == "TENANTB"
        assert not (reg2.config.extra_headers or {})
    finally:
        await reg2.aclose()


async def test_client_without_signing_uses_service_key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "internal_signing_secret", "")
    reg = activities._client(CallerContext(tenant="TENANTC", email="rm@evamfinance.com"))
    try:
        assert reg.config.tenant == "TENANTC"
        assert not (reg.config.extra_headers or {})  # dev: service key, no delegation
    finally:
        await reg.aclose()
