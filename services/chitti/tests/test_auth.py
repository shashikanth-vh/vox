from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

pytestmark = pytest.mark.asyncio


def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_model_discovery_refuses_missing_or_wrong_key():
    async with _client() as client:
        missing = await client.get("/v1/models")
        wrong = await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})

    for response in (missing, wrong):
        assert response.status_code == 401
        body = response.json()["error"]
        assert body["type"] == "unauthorized"
        assert body["request_id"] == response.headers["X-Request-ID"]


async def test_bearer_and_gateway_keys_are_accepted():
    async with _client() as client:
        bearer = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-client-key"}
        )
        gateway = await client.get(
            "/v1/models", headers={"X-API-Key": "gateway-service-key"}
        )

    assert bearer.status_code == 200
    assert gateway.status_code == 200
