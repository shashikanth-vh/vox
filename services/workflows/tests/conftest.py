"""A mock Register so activities can be tested without a running Register (or Postgres)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from evam_register_client import AsyncRegisterClient

from app import activities


def build_mock() -> FastAPI:
    app = FastAPI()
    app.state.written = []

    @app.post("/v1/interactions", status_code=201)
    async def interactions(request: Request):
        if request.headers.get("x-api-key") != "test-key":
            return JSONResponse(
                {"error": {"type": "unauthorized", "title": "bad key", "status": 401,
                           "detail": "x", "request_id": None}}, status_code=401)
        body = await request.json()
        app.state.written.append(body)
        # The real Register denormalises entity_id from an Entity subject.
        return {"id": uuid.uuid4().hex, "version": 1, "entity_id": body["subject_id"], **body}

    @app.get("/v1/entities/{eid}/dossier")
    async def dossier(eid: str):
        return {"entity": {"id": eid}, "counts": {"interactions": 1, "deals": 0}}

    return app


@pytest.fixture
def mock_register(monkeypatch) -> FastAPI:
    app = build_mock()

    def _client() -> AsyncRegisterClient:
        return AsyncRegisterClient("http://reg", "test-key", tenant="EVAM", actor="workflows",
                                   transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(activities, "_client", _client)
    return app
