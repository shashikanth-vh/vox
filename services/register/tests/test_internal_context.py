"""The signed internal context — the production identity/authorization channel.

Proves the realignment to the reference architecture: with a signing secret configured,
the Register authenticates the caller by VERIFYING the gateway's signed token and enforces
the LIVE effective permissions carried inside it — never a client-asserted header, never a
stale compiled matrix.
"""

from __future__ import annotations

import uuid

import pytest
from evam_backend_core.internal_token import mint_internal_context
from httpx import AsyncClient

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio

SECRET = "test-internal-signing-secret"


def _ctx(**over) -> str:
    base = {
        "signing_key": SECRET, "tenant": "EVAM", "email": "rm@evamfinance.com",
        "user_id": str(uuid.uuid4()), "roles": ["BDRM"],
        "effective_operations": {}, "effective_views": {}, "matrix_version": 1}
    base.update(over)
    return mint_internal_context(**base)


@pytest.fixture(autouse=True)
def _enable_signing():
    s = get_settings()
    s.internal_signing_secret = SECRET
    s.internal_signing_algorithm = "HS256"
    try:
        yield
    finally:
        s.internal_signing_secret = ""


async def test_identity_comes_from_the_signed_token(client: AsyncClient):
    tok = _ctx(email="owner@evamfinance.com",
               effective_operations={"add_lead": "FULL", "create_client": "FULL"},
               effective_views={"leads": "FULL", "clients": "FULL"})
    r = await client.post("/v1/entities", json={"code": "IC-1", "legal_name": "IC One"},
                          headers={"X-Internal-Context": tok})
    assert r.status_code == 201, r.text
    # created_by is stamped from the TOKEN identity, not X-Actor / a header.
    assert r.json()["created_by"] == "owner@evamfinance.com"


async def test_live_grant_in_token_is_authoritative_allow(client: AsyncClient):
    """A role that statically cannot delete (BDRM) CAN when the signed live grant says so —
    the Register enforces the forwarded effective matrix, not the compiled one."""
    ent = (await client.post("/v1/entities", json={"code": "IC-DEL", "legal_name": "Del"},
                             headers={"X-Internal-Context": _ctx(
                                 effective_operations={"create_client": "FULL"},
                                 effective_views={"clients": "FULL"})})).json()
    tok = _ctx(roles=["BDRM"], effective_operations={"delete_row": "FULL"})
    r = await client.delete(f"/v1/entities/{ent['id']}", headers={"X-Internal-Context": tok})
    assert r.status_code == 204, r.text


async def test_live_grant_in_token_is_authoritative_deny(client: AsyncClient):
    """Even an Admin role is denied when the live grant revokes the op — the token wins."""
    ent = (await client.post("/v1/entities", json={"code": "IC-DN", "legal_name": "Deny"},
                             headers={"X-Internal-Context": _ctx(
                                 roles=["Admin"], effective_operations={"create_client": "FULL"},
                                 effective_views={"clients": "FULL"})})).json()
    tok = _ctx(roles=["Admin"], effective_operations={"delete_row": "NONE"})
    r = await client.delete(f"/v1/entities/{ent['id']}", headers={"X-Internal-Context": tok})
    assert r.status_code == 403, r.text


async def test_forged_plaintext_headers_are_ignored_when_token_present(client: AsyncClient):
    """A valid token for a plain RM, plus forged X-User-Roles: Admin headers → the token
    identity wins, so the Admin-only audit view stays denied."""
    tok = _ctx(roles=["BDRM"], effective_views={"leads": "SCOPED"})
    r = await client.get("/v1/audit", headers={
        "X-Internal-Context": tok,
        "X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"})
    assert r.status_code == 403, r.text


async def test_invalid_token_is_rejected(client: AsyncClient):
    r = await client.get("/v1/entities",
                         headers={"X-Internal-Context": "not.a.jwt"})
    assert r.status_code == 403, r.text


async def test_token_for_other_tenant_is_rejected(client: AsyncClient):
    tok = _ctx(tenant="OTHER")
    r = await client.get("/v1/entities", headers={"X-Internal-Context": tok,
                                                  "X-Tenant": "EVAM"})
    assert r.status_code == 403, r.text
