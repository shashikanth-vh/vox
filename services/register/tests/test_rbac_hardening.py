"""R7 hardening: service principals, mandatory signed context, transition policy, and
per-table export view-gating."""

from __future__ import annotations

import uuid

import pytest
from evam_backend_core.internal_token import mint_internal_context
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

SIGN = "test-internal-signing-secret"
PULSE_KEY = "svc-pulse-key"
VOX_KEY = "svc-vox-key"


def _as(email, roles, uid=None):
    h = {"X-User-Email": email, "X-User-Roles": roles}
    if uid:
        h["X-User-Id"] = str(uid)
    return h


# --------------------------------------------------------------------------- #
# Service principals — a named machine key is bound to its operation allowlist
# --------------------------------------------------------------------------- #
@pytest.fixture
def _service_keys():
    s = get_settings()
    s.service_api_keys = {PULSE_KEY: "svc_pulse", VOX_KEY: "svc_vox"}
    try:
        yield
    finally:
        s.service_api_keys = {}


async def test_service_principal_allowlist(client: AsyncClient, _service_keys):
    ent = (await client.post("/v1/entities",
                             json={"code": "SP-1", "legal_name": "SP"})).json()
    # svc_pulse may write intelligence…
    ok = await client.post("/v1/external-intelligence",
                           json={"entity_id": ent["id"], "intel_type": "News"},
                           headers={"X-API-Key": PULSE_KEY})
    assert ok.status_code == 201, ok.text
    # …but NOT create a lead (add_lead not on its allowlist) → 403.
    denied = await client.post("/v1/leads", json={"company": "SP", "entity_id": ent["id"]},
                               headers={"X-API-Key": PULSE_KEY})
    assert denied.status_code == 403, denied.text
    # svc_vox may create the lead.
    vox = await client.post("/v1/leads", json={"company": "SP", "entity_id": ent["id"]},
                            headers={"X-API-Key": VOX_KEY})
    assert vox.status_code == 201, vox.text


# --------------------------------------------------------------------------- #
# Signed context is the SOLE identity path once configured — no legacy downgrade
# --------------------------------------------------------------------------- #
@pytest.fixture
def _signing():
    s = get_settings()
    s.internal_signing_secret = SIGN
    s.internal_signing_algorithm = "HS256"
    try:
        yield
    finally:
        s.internal_signing_secret = ""


async def test_plaintext_headers_ignored_when_signing_configured(client: AsyncClient, _signing):
    # Forged plaintext identity, NO signed token → identity is NOT applied (machine call);
    # the row is stamped with the actor, never the forged e-mail.
    r = await client.post("/v1/entities", json={"code": "SIGN-1", "legal_name": "S1"},
                          headers={**_as("ghost@evamfinance.com", "Admin"),
                                   "X-Actor": "pytest"})
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] != "ghost@evamfinance.com"


async def test_token_bound_to_method_and_path(client: AsyncClient, _signing):
    # A token minted for GET /v1/leads cannot be replayed against POST /v1/entities.
    tok = mint_internal_context(
        signing_key=SIGN, tenant="EVAM", email="rm@evamfinance.com",
        user_id=str(uuid.uuid4()), roles=["Admin"],
        effective_operations={"create_client": "FULL"}, effective_views={"clients": "FULL"},
        method="GET", path="/v1/leads")
    r = await client.post("/v1/entities", json={"code": "BND", "legal_name": "B"},
                          headers={"X-Internal-Context": tok})
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# Transition policy
# --------------------------------------------------------------------------- #
ADMIN = _as("admin@evamfinance.com", "Admin")


async def test_lead_transition_policy(client: AsyncClient):
    ent = (await client.post("/v1/entities",
                             json={"code": "TR-1", "legal_name": "TR"})).json()
    lead = (await client.post("/v1/leads",
                              json={"company": "TR", "entity_id": ent["id"]})).json()
    # Active → Dropped is allowed…
    assert (await client.patch(f"/v1/leads/{lead['id']}", json={"status": "Dropped"},
                               headers=ADMIN)).status_code == 200
    # …an undefined jump (Dropped → On Hold) is rejected.
    bad = await client.patch(f"/v1/leads/{lead['id']}", json={"status": "On Hold"},
                             headers=ADMIN)
    assert bad.status_code == 422, bad.text


# --------------------------------------------------------------------------- #
# Export gating
# --------------------------------------------------------------------------- #
CREDIT_HEAD = _as("ch@evamfinance.com", "Credit Head")


async def test_export_skips_unviewable_tables_and_gates_deleted(client: AsyncClient):
    await client.post("/v1/entities", json={"code": "EX-1", "legal_name": "EX"})
    # Credit Head has NO access to the leads view → leads are absent from their export.
    counts = (await client.get("/v1/export/counts", headers=CREDIT_HEAD)).json()
    assert "leads" not in counts
    # include_deleted is a backup capability → Management (no backup_restore) is refused.
    mgmt = _as("mgmt@evamfinance.com", "Management")
    r = await client.get("/v1/export/counts", params={"include_deleted": True}, headers=mgmt)
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# Attribution: a service acting FOR a verified person
# --------------------------------------------------------------------------- #
async def test_a_service_may_attribute_a_row_to_the_person_it_acted_for(
        client: AsyncClient, _service_keys):
    """VocX holds the authority; the RM dictated the row.

    Stamped with the service, a captured lead lands in nobody's own book (scope rule 3
    matches created_by against the user's e-mail) — so the RM who recorded it could not
    see what they had just filed, while an Admin could. X-On-Behalf-Of moves the
    ATTRIBUTION without moving the authority.
    """
    ent = (await client.post("/v1/entities",
                             json={"code": f"OB-{uuid.uuid4().hex[:6]}",
                                   "legal_name": "OnBehalf Co"})).json()
    lead = await client.post("/v1/leads", json={"company": "OnBehalf Co", "entity_id": ent["id"]},
                             headers={"X-API-Key": VOX_KEY,
                                      "X-On-Behalf-Of": "priya@evamfinance.com"})
    assert lead.status_code == 201, lead.text
    assert lead.json()["created_by"] == "priya@evamfinance.com"

    # Authority is UNCHANGED — it is still the service's allowlist that decides. A
    # principal that may not create a lead does not gain the right by naming a person.
    denied = await client.post("/v1/leads", json={"company": "OnBehalf Co", "entity_id": ent["id"]},
                               headers={"X-API-Key": PULSE_KEY,
                                        "X-On-Behalf-Of": "priya@evamfinance.com"})
    assert denied.status_code == 403, denied.text


async def test_on_behalf_of_is_ignored_without_a_named_service_key(client: AsyncClient):
    """A generic key is not a principal that has verified anybody, and a claim is not an
    identity: the header is honoured only for a named service."""
    ent = (await client.post("/v1/entities",
                             json={"code": f"OB-{uuid.uuid4().hex[:6]}",
                                   "legal_name": "NoService Co"})).json()
    lead = await client.post("/v1/leads", json={"company": "NoService Co", "entity_id": ent["id"]},
                             headers={"X-On-Behalf-Of": "someone.else@evamfinance.com"})
    assert lead.status_code == 201, lead.text
    assert lead.json()["created_by"] != "someone.else@evamfinance.com"


async def test_a_verified_user_always_outranks_an_attribution_claim(
        client: AsyncClient, _service_keys):
    """When the caller IS a person, their own verified e-mail is the actor — a header
    cannot make their write look like someone else's."""
    ent = (await client.post("/v1/entities",
                             json={"code": f"OB-{uuid.uuid4().hex[:6]}",
                                   "legal_name": "Verified Co"})).json()
    lead = await client.post(
        "/v1/leads", json={"company": "Verified Co", "entity_id": ent["id"]},
        headers={**_as("arun@evamfinance.com", "BDRM"),
                 "X-API-Key": VOX_KEY, "X-On-Behalf-Of": "priya@evamfinance.com"})
    assert lead.status_code == 201, lead.text
    assert lead.json()["created_by"] == "arun@evamfinance.com"
