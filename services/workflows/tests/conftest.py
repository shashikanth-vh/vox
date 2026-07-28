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
    app.state.written = []       # every interaction body, for assertions
    app.state.entities = []      # rows the entity search returns
    app.state.leads = {}         # id -> lead row

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

    @app.get("/v1/entities")
    async def list_entities():
        # The real Register does trigram search; the mock returns everything and lets
        # the activity's canonical comparison do the matching.
        return {"items": app.state.entities, "next_cursor": None}

    @app.post("/v1/entities", status_code=201)
    async def create_entity(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "version": 1, **body}
        app.state.entities.append(row)
        return row

    @app.get("/v1/leads")
    async def list_leads(request: Request):
        # Honour entity_id/status like the real Register — a mock that ignores
        # filters is how the wrong-company lead bug slipped past the tests.
        rows = list(app.state.leads.values())
        eid = request.query_params.get("entity_id")
        if eid:
            rows = [r for r in rows if str(r.get("entity_id")) == eid]
        status = request.query_params.get("status")
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"items": rows, "next_cursor": None}

    @app.post("/v1/leads", status_code=201)
    async def create_lead(request: Request):
        body = await request.json()
        row = {"id": uuid.uuid4().hex, "version": 1, "status": "Active", **body}
        app.state.leads[row["id"]] = row
        return row

    @app.get("/v1/leads/{lid}")
    async def get_lead(lid: str):
        return app.state.leads[lid]

    @app.patch("/v1/leads/{lid}")
    async def patch_lead(lid: str, request: Request):
        body = await request.json()
        app.state.leads[lid] = {**app.state.leads[lid], **body}
        return app.state.leads[lid]

    return app


@pytest.fixture
def mock_register(monkeypatch) -> FastAPI:
    app = build_mock()

    def _client() -> AsyncRegisterClient:
        return AsyncRegisterClient("http://reg", "test-key", tenant="EVAM", actor="workflows",
                                   transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(activities, "_client", _client)
    return app
