"""A mock Register that faithfully mimics the contract bits the client depends on:
API-key auth, the RFC-9457 error envelope, idempotency replay, optimistic-locking
(If-Match) version conflicts, keyset pagination, and transient failures."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from evam_register_client import AsyncRegisterClient, RegisterClientConfig


def _err(status: int, error_type: str, title: str, detail: str, request_id: str | None,
         **extra: Any) -> JSONResponse:
    body = {"error": {"type": error_type, "title": title, "status": status,
                      "detail": detail, "request_id": request_id, **extra}}
    return JSONResponse(body, status_code=status)


def build_mock() -> FastAPI:
    app = FastAPI()
    app.state.things = {}
    app.state.idem = {}
    app.state.flaky = {"503": 0, "429": 0}
    app.state.calls = {"create": 0}
    app.state.last = {}

    def auth_or_401(request: Request):
        rid = request.headers.get("x-request-id")
        if request.headers.get("x-api-key") != "test-key":
            return _err(401, "unauthorized", "Missing or invalid API key", "bad key", rid)
        return None

    @app.post("/v1/things", status_code=201)
    async def create_thing(request: Request,
                           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if (e := auth_or_401(request)):
            return e
        if idempotency_key and idempotency_key in app.state.idem:
            return JSONResponse(app.state.idem[idempotency_key], status_code=201)
        app.state.calls["create"] += 1
        payload = await request.json()
        obj = {"id": uuid.uuid4().hex, "version": 1, **payload}
        app.state.things[obj["id"]] = obj
        if idempotency_key:
            app.state.idem[idempotency_key] = obj
        return obj

    @app.get("/v1/things/{obj_id}")
    async def get_thing(obj_id: str, request: Request):
        if (e := auth_or_401(request)):
            return e
        rid = request.headers.get("x-request-id")
        obj = app.state.things.get(obj_id)
        if obj is None:
            return _err(404, "not_found", "Resource not found", f"no thing {obj_id}", rid)
        return obj

    @app.get("/v1/things")
    async def list_things(request: Request, limit: int = 50, cursor: str | None = None):
        if (e := auth_or_401(request)):
            return e
        items = list(app.state.things.values())
        start = int(cursor) if cursor else 0
        page = items[start:start + limit]
        nxt = str(start + limit) if start + limit < len(items) else None
        return {"items": page, "count": len(page), "next_cursor": nxt, "total": len(items)}

    @app.patch("/v1/things/{obj_id}")
    async def update_thing(obj_id: str, request: Request,
                           if_match: str | None = Header(default=None, alias="If-Match")):
        if (e := auth_or_401(request)):
            return e
        rid = request.headers.get("x-request-id")
        obj = app.state.things.get(obj_id)
        if obj is None:
            return _err(404, "not_found", "Resource not found", "gone", rid)
        if if_match is not None:
            want = int(if_match.strip('"'))
            if want != obj["version"]:
                return _err(409, "version_conflict", "Version conflict", "changed", rid,
                            expected_version=want, actual_version=obj["version"])
        obj.update(await request.json())
        obj["version"] += 1
        return obj

    @app.get("/flaky503")
    async def flaky503(request: Request):
        app.state.flaky["503"] += 1
        if app.state.flaky["503"] <= 2:
            return _err(503, "unavailable", "Service Unavailable", "warming up",
                        request.headers.get("x-request-id"))
        return {"ok": True, "tries": app.state.flaky["503"]}

    @app.get("/always503")
    async def always503(request: Request):
        return _err(503, "unavailable", "Service Unavailable", "down",
                    request.headers.get("x-request-id"))

    @app.get("/rate")
    async def rate(request: Request):
        app.state.flaky["429"] += 1
        if app.state.flaky["429"] <= 1:
            r = _err(429, "rate_limited", "Too Many Requests", "slow down",
                     request.headers.get("x-request-id"))
            r.headers["Retry-After"] = "0"
            return r
        return {"ok": True}

    # Vertical-helper endpoints — record what was received for assertions.
    @app.post("/v1/interactions", status_code=201)
    async def interactions(request: Request):
        app.state.last["interaction"] = await request.json()
        app.state.last["interaction_rid"] = request.headers.get("x-request-id")
        return {"id": uuid.uuid4().hex, "version": 1, **app.state.last["interaction"]}

    @app.post("/v1/financials", status_code=201)
    async def financials(request: Request):
        app.state.last["financial"] = await request.json()
        return {"id": uuid.uuid4().hex, "version": 1, "version_no": 1,
                **app.state.last["financial"]}

    @app.post("/v1/external-intelligence", status_code=201)
    async def intel(request: Request):
        app.state.last["intel"] = await request.json()
        return {"id": "intel-1", "version": 1, "is_dismissed": False,
                **app.state.last["intel"]}

    @app.post("/v1/external-intelligence/{iid}/acknowledge")
    async def ack(iid: str, request: Request):
        return {"id": iid, "acknowledged_by": "tester", "is_dismissed": False}

    @app.get("/v1/ref/{category}")
    async def ref(category: str):
        return [{"value": "A", "label": "A"}]

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


@pytest.fixture
def mock_app() -> FastAPI:
    return build_mock()


@pytest.fixture
async def reg(mock_app):
    cfg = RegisterClientConfig(retry_base_delay_s=0.001, retry_max_delay_s=0.005,
                               retry_max_attempts=3)
    transport = httpx.ASGITransport(app=mock_app)
    async with AsyncRegisterClient("http://reg", "test-key", tenant="EVAM", actor="tester",
                                   config=cfg, transport=transport) as client:
        yield client
