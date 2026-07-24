"""Shared Pydantic building blocks for request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ReadModel(ORMModel):
    """Fields every read response carries (from RegisterBase)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None
    deleted_at: datetime | None = None


class CreateModel(BaseModel):
    """Base for create payloads — forbids unknown fields to catch client typos early."""

    model_config = ConfigDict(extra="forbid")


class UpdateModel(BaseModel):
    """Base for PATCH payloads — every field optional, unknown fields rejected.

    ``expected_version`` (optional) enables optimistic concurrency from the body; the
    ``If-Match`` header is honoured too. If neither is supplied the update still uses
    the row's own version guard, so a lost update is impossible either way.
    """

    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = None
