"""PRISM VocX — voice-based field touchpoint capture (formerly "VOX").

A captured touchpoint (transcript, GPS, attendees, structured key intel) is written to
the Register as an interaction with ``source: "VocX"`` through the platform SDK. The
capture id doubles as the idempotency key, so a flaky uplink retrying the same upload
can never create a duplicate touchpoint — Temporal-grade exactly-once effect without a
workflow.

Stateless and individually deployable: own Dockerfile, Compose service, Helm subchart.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from evam_backend_core.errors import register_exception_handlers
from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.middleware import RequestContextMiddleware
from evam_register_client import AsyncRegisterClient
from evam_register_client.errors import RegisterError
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

log = get_logger("vocx")

SUBJECT_TYPES = {"Lead", "Deal", "Entity", "Counterparty", "Lending", "Syndication",
                 "AssetMonetisation"}


class TouchpointIn(BaseModel):
    """One captured field touchpoint. Speech-to-text happens upstream — VocX receives
    the transcript and the intel already structured by the capture app."""

    model_config = ConfigDict(extra="forbid")

    # What the touchpoint is about (ATLAS refType/refId).
    subject_type: str = Field(max_length=30)
    subject_id: uuid.UUID
    interaction_type: str = Field(default="In-Person Meeting", max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: str | None = None            # ISO timestamp; defaults to now server-side
    # Who captured it.
    performed_by: str | None = Field(default=None, max_length=120)
    contact_name: str | None = Field(default=None, max_length=200)
    # The capture itself.
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    transcript: str | None = None
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    next_action: str | None = None
    next_action_date: str | None = None
    # Stable id of the recording/upload — becomes the idempotency key.
    capture_id: str | None = Field(default=None, max_length=180)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        app.state.register = AsyncRegisterClient(
            base_url=settings.register_base_url,
            api_key=settings.register_api_key,
            tenant=settings.register_tenant,
            actor="vocx",
        )
        log.info("vocx_started", extra={"register": settings.register_base_url})
        yield
        await app.state.register.aclose()

    app = FastAPI(title="PRISM VocX", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": settings.app_name}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict:
        return {"status": "ready", "service": settings.app_name}

    @app.post("/v1/touchpoints", status_code=201, tags=["Touchpoints"],
              summary="Capture a field touchpoint → an interaction in the Register")
    async def capture_touchpoint(payload: TouchpointIn, request: Request) -> ORJSONResponse:
        if payload.subject_type not in SUBJECT_TYPES:
            return ORJSONResponse(status_code=422, content={"error": {
                "type": "validation_error", "title": "Validation failed",
                "detail": f"Unknown subject_type '{payload.subject_type}'. "
                          f"One of: {', '.join(sorted(SUBJECT_TYPES))}."}})
        data = payload.model_dump(exclude_none=True, exclude={"capture_id"})
        subject_type = data.pop("subject_type")
        subject_id = str(data.pop("subject_id"))
        interaction_type = data.pop("interaction_type")
        # Exactly-once: the capture id keys the write; a retried upload replays.
        idem = f"vocx:{payload.capture_id}" if payload.capture_id else None
        try:
            interaction = await request.app.state.register.log_interaction(
                subject_type, subject_id, interaction_type,
                source="VocX", idempotency_key=idem,
                **data,
            )
        except RegisterError as exc:
            return ORJSONResponse(status_code=exc.status or 502, content={"error": {
                "type": "register_error", "title": "Register rejected the touchpoint",
                "detail": str(exc)}})
        return ORJSONResponse(status_code=201, content={
            "interaction_id": interaction["id"],
            "entity_id": interaction.get("entity_id"),
            "deal_id": interaction.get("deal_id"),
            "source": interaction.get("source"),
        })

    return app


app = create_app()
