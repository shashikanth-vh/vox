"""Signed internal-context mint/verify — the production identity channel."""

from __future__ import annotations

import time

import pytest

from evam_backend_core.internal_token import (
    InternalTokenError,
    mint_internal_context,
    verify_internal_context,
)

SECRET = "test-internal-signing-secret"


def _mint(**over):
    base = {
        "signing_key": SECRET, "tenant": "EVAM", "email": "rm@evamfinance.com",
        "user_id": "00000000-0000-0000-0000-000000000001", "roles": ["BDRM"],
        "effective_operations": {"add_lead": "FULL"},
        "effective_views": {"leads": "SCOPED"},
        "matrix_version": 7, "decision": "SCOPED"}
    base.update(over)
    return mint_internal_context(**base)


def test_roundtrip_carries_identity_and_effective_grant():
    ic = verify_internal_context(_mint(), verify_key=SECRET)
    assert ic.email == "rm@evamfinance.com" and ic.tenant == "EVAM"
    assert ic.roles == ["BDRM"]
    assert ic.effective_operations == {"add_lead": "FULL"}
    assert ic.effective_views == {"leads": "SCOPED"}
    assert ic.matrix_version == 7 and ic.decision == "SCOPED"


def test_tampered_token_is_rejected():
    token = _mint()
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(InternalTokenError):
        verify_internal_context(tampered, verify_key=SECRET)


def test_wrong_key_is_rejected():
    with pytest.raises(InternalTokenError):
        verify_internal_context(_mint(), verify_key="the-wrong-secret")


def test_expired_token_is_rejected():
    old = _mint(now=int(time.time()) - 10_000, ttl_seconds=60)
    with pytest.raises(InternalTokenError):
        verify_internal_context(old, verify_key=SECRET)
