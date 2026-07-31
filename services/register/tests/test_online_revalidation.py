"""Sensitive-operation ONLINE revalidation (release-one authority model).

With REGISTER_ONLINE_REVALIDATION on, delete/restore (and assignments, imports,
break-glass) re-resolve the caller against Access LIVE before acting: a revoked grant or
advanced revocation epoch refuses (403), and an unreachable Access FAILS CLOSED (503) —
the static matrix is never consulted as a fallback.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}


@pytest.fixture
def _reval_on(monkeypatch):
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "online_revalidation", True)
    monkeypatch.setattr(s, "access_url", "http://access.test")
    yield


async def _entity(client) -> str:  # noqa: ANN001
    code = "REV" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities",
                          json={"code": code, "legal_name": "Reval Co"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_delete_refused_when_grant_revoked_online(client, _reval_on, monkeypatch):
    from app.core import access_client

    async def _revoked(tenant, email, operation, token_epoch=None):  # noqa: ANN001
        return f"operation '{operation}' is no longer granted to '{email}'."
    monkeypatch.setattr(access_client, "revalidate_operation", _revoked)
    eid = await _entity(client)
    r = await client.delete(f"/v1/entities/{eid}", headers=ADMIN)
    assert r.status_code == 403, r.text
    assert "no longer granted" in r.text
    # The row is untouched.
    assert (await client.get(f"/v1/entities/{eid}")).status_code == 200


async def test_delete_fails_closed_when_access_unreachable(client, _reval_on, monkeypatch):
    from app.core import access_client

    async def _down(tenant, email, operation, token_epoch=None):  # noqa: ANN001
        raise access_client.AccessUnavailableError("connect timeout")
    monkeypatch.setattr(access_client, "revalidate_operation", _down)
    eid = await _entity(client)
    r = await client.delete(f"/v1/entities/{eid}", headers=ADMIN)
    assert r.status_code == 503, r.text
    assert (await client.get(f"/v1/entities/{eid}")).status_code == 200


async def test_delete_proceeds_when_revalidation_passes(client, _reval_on, monkeypatch):
    from app.core import access_client

    async def _ok(tenant, email, operation, token_epoch=None):  # noqa: ANN001
        return None
    monkeypatch.setattr(access_client, "revalidate_operation", _ok)
    eid = await _entity(client)
    assert (await client.delete(f"/v1/entities/{eid}", headers=ADMIN)).status_code == 204


async def test_epoch_advance_refuses_stale_context(client, _reval_on, monkeypatch):
    """A context minted under epoch N is refused once Access reports epoch > N — the
    'reject tokens issued before a revocation' rule, enforced at the sensitive ops."""
    import httpx

    from app.core import access_client

    async def fake_get(self, url, params=None, headers=None):  # noqa: ANN001
        return httpx.Response(200, json={
            "id": str(uuid.uuid4()), "email": params["email"], "full_name": "A",
            "is_active": True, "roles": ["Admin"],
            "views": {}, "operations": {"delete_row": "FULL"},
            "version": 5, "epoch": 9, "reports": []})
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    # Token epoch 3 < fresh epoch 9 → refused.
    problem = await access_client.revalidate_operation("EVAM", "admin@evamfinance.com",
                                                       "delete_row", token_epoch=3)
    assert problem is not None and "epoch" in problem
    # Same epoch → allowed.
    assert await access_client.revalidate_operation("EVAM", "admin@evamfinance.com",
                                                    "delete_row", token_epoch=9) is None
