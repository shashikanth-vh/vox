"""R9 hardening: delegated service reads (ATLAS unblocked), own-key read isolation, and
the fail-closed transition validator on the approval path."""

from __future__ import annotations

import uuid

import pytest
from evam_backend_core.internal_token import mint_internal_context
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

SIGN = "test-internal-signing-secret"
ATLAS_KEY = "svc-atlas-key"
PULSE_KEY = "svc-pulse-key"


def _as(email: str, roles: str) -> dict:
    return {"X-User-Email": email, "X-User-Roles": roles}


@pytest.fixture
def _svc_and_signing():
    s = get_settings()
    s.service_api_keys = {ATLAS_KEY: "svc_atlas", PULSE_KEY: "svc_pulse"}
    s.internal_signing_secret = SIGN
    s.internal_signing_algorithm = "HS256"
    try:
        yield
    finally:
        s.service_api_keys = {}
        s.internal_signing_secret = ""


# --------------------------------------------------------------------------- #
# svc_atlas: delegated read (with a signed user context) PASSES; own key is DENIED
# --------------------------------------------------------------------------- #
async def test_svc_atlas_delegated_read_passes_own_key_denied(client: AsyncClient,
                                                              _svc_and_signing):
    # Own key alone (no user context) → a read-only BFF may not read the data plane.
    own = await client.get("/v1/leads", headers={"X-API-Key": ATLAS_KEY})
    assert own.status_code == 403, own.text
    assert "forward a user context" in own.text

    # Delegated: svc_atlas key + a signed user context → the user's scope governs → 200.
    tok = mint_internal_context(
        signing_key=SIGN, tenant="EVAM", email="admin@evamfinance.com",
        user_id=str(uuid.uuid4()), roles=["Admin"],
        effective_operations={}, effective_views={"leads": "FULL"},
        method="GET", path="/v1/leads")
    ok = await client.get("/v1/leads",
                          headers={"X-API-Key": ATLAS_KEY, "X-Internal-Context": tok})
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------------------------- #
# svc_pulse own key: may read its intelligence context, NOT every table
# --------------------------------------------------------------------------- #
async def test_svc_pulse_own_key_read_is_scoped(client: AsyncClient, _svc_and_signing):
    # /v1/entities is on svc_pulse's read allowlist → own-key read allowed.
    ents = await client.get("/v1/entities", headers={"X-API-Key": PULSE_KEY})
    assert ents.status_code == 200, ents.text
    # /v1/deals is NOT → own-key read denied (no more tenant-wide read of every table).
    deals = await client.get("/v1/deals", headers={"X-API-Key": PULSE_KEY})
    assert deals.status_code == 403, deals.text


# --------------------------------------------------------------------------- #
# Approval path fails CLOSED on an unrecognised current state (shared validator)
# --------------------------------------------------------------------------- #
BD_HEAD = _as("bdhead.r9@evamfinance.com", "BD Head")


def test_transition_validator_fails_closed_on_unknown_state():
    """The SHARED validator (used by both the direct PATCH and the approval path) rejects an
    UNRECOGNISED current state instead of waving it through — the fail-closed property. A
    lead can no longer even be CREATED in such a state (see test_rbac_r10), so this locks the
    validator itself rather than routing through an unreachable API state."""
    from app.authz.matrix import transition_error

    # A known → forbidden target is rejected…
    assert transition_error("Lead", "status", "Dropped", "On Hold") is not None
    # …an UNRECOGNISED current state permits NO transition (fail closed)…
    err = transition_error("Lead", "status", "Qualified", "Active")
    assert err is not None and "unrecognised state" in err.lower()
    # …a known allowed transition passes, and a same-value no-op passes.
    assert transition_error("Lead", "status", "Active", "Dropped") is None
    assert transition_error("Lead", "status", "Active", "Active") is None
