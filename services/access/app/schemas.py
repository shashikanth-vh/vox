"""Request/response schemas for the Access service."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=200)
    full_name: str = Field(max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    is_active: bool = True
    reports_to: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=30)
    notes: str | None = None
    meta: dict[str, Any] | None = None
    roles: list[str] | None = None


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    reports_to: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=30)
    notes: str | None = None
    meta: dict[str, Any] | None = None


class UserRead(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    short_name: str | None
    is_active: bool
    reports_to: uuid.UUID | None
    phone: str | None
    notes: str | None
    meta: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    roles: list[str] = []


class RoleGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=30)


class GrantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=12)      # view | operation
    item: str = Field(max_length=60)
    role: str = Field(max_length=30)
    access: str = Field(max_length=12)    # NONE/READ/SCOPED/FULL/APPROVE


class ResolveRead(BaseModel):
    """What the gateway caches: identity + effective matrices + version."""

    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    roles: list[str]
    views: dict[str, str]
    operations: dict[str, str]
    version: int
    # The user's revocation epoch — bumped on any role change / (de)activation; carried in
    # the signed context and compared by sensitive-operation revalidation.
    epoch: int = 0
    # Transitive subordinates (id + email) — a Head's team, for Register team scope.
    reports: list[dict] = []
