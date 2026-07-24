"""A complete minimal PRISM service built on evam-backend-core.

Demonstrates the standard: subclass settings, inherit RecordBase, build CRUD with the
repository + api_router, assemble with create_service_app. Everything cross-cutting
(logging, errors, pool+timeouts+retry, pagination, health) is inherited.

Run (against a Postgres with a `widgets` table + the settings' env):
    uvicorn examples.widget_service:app
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import SettingsConfigDict
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from evam_backend_core.app import create_service_app
from evam_backend_core.config import BaseServiceSettings
from evam_backend_core.crud import CRUDRepository
from evam_backend_core.db.base import RecordBase
from evam_backend_core.db.session import get_session
from evam_backend_core.pagination import Page
from evam_backend_core.router import api_router


# 1) Settings — only what's service-specific; everything else is inherited.
class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WIDGETS_", extra="ignore")
    app_name: str = "prism-widgets"
    db_name: str = "widgets"


# 2) Model — tenant-aware, versioned, auditable, soft-deletable for free.
class Widget(RecordBase):
    __tablename__ = "widgets"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str | None] = mapped_column(String(40))  # stage/status → auto history


# 3) Schemas.
class WidgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    status: str | None = None


class WidgetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    status: str | None
    version: int


# 4) Resource — CRUD via the shared repository + retry-bound router.
_repo = CRUDRepository(Widget, searchable=["name"], filterable=["status"])
_TENANT = uuid.uuid5(uuid.NAMESPACE_DNS, "example-tenant")  # demo tenant id
router = api_router(prefix="/v1/widgets", tags=["Widgets"])


@router.post("", response_model=WidgetRead, status_code=201)
async def create_widget(payload: WidgetCreate, session=Depends(get_session),
                        actor: str | None = Header(default="api", alias="X-Actor")) -> WidgetRead:
    obj = await _repo.create(session, _TENANT, actor or "api", payload.model_dump())
    return WidgetRead.model_validate(obj)


@router.get("", response_model=Page[WidgetRead])
async def list_widgets(request: Request, session=Depends(get_session),
                       limit: int = 50, cursor: str | None = None) -> Page:
    rows, next_cursor, total = await _repo.list(session, _TENANT, limit=limit, cursor=cursor)
    return Page(items=[WidgetRead.model_validate(r) for r in rows],
                count=len(rows), next_cursor=next_cursor, total=total)


# 5) App — one call wires logging, errors, pool, retry, health, CORS.
app = create_service_app(settings=Settings(), routers=[router], title="PRISM Widgets",
                         description="Example service on evam-backend-core.")
