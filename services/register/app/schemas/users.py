"""Request/response schemas for user management & RBAC."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import CreateModel, ReadModel, UpdateModel


# --------------------------------------------------------------------------- #
# Users (the Employees governance table)
# --------------------------------------------------------------------------- #
class UserCreate(CreateModel):
    email: str = Field(max_length=200)  # domain-validated server-side (SSO integrity)
    full_name: str = Field(max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    is_active: bool = True
    reports_to: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=30)
    notes: str | None = None
    meta: dict[str, Any] | None = None
    # Convenience: initial roles granted at creation (Admin owns the role-assignment step).
    roles: list[str] | None = None


class UserUpdate(UpdateModel):
    full_name: str | None = Field(default=None, max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    reports_to: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=30)
    notes: str | None = None
    meta: dict[str, Any] | None = None


class UserRead(ReadModel):
    email: str
    full_name: str
    short_name: str | None
    is_active: bool
    reports_to: uuid.UUID | None
    person_id: uuid.UUID | None
    phone: str | None
    notes: str | None
    meta: dict[str, Any] | None
    # Stacked roles (populated by the users endpoints).
    roles: list[str] = []


class RoleGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(max_length=30)


# --------------------------------------------------------------------------- #
# Line assignments (assignment-driven permission)
# --------------------------------------------------------------------------- #
class AssignmentCreate(CreateModel):
    user_id: uuid.UUID
    subject_type: str = Field(max_length=30)   # Lead / Deal / Lending / Syndication / AssetMonetisation
    subject_id: uuid.UUID
    assignment_role: str = Field(max_length=30)  # BDRM / Deal Analyst / Syn RM / AM RM
    note: str | None = None


class AssignmentRead(ReadModel):
    user_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    assignment_role: str
    assigned_by: str | None
    ended_at: datetime | None
    ended_by: str | None
    note: str | None


# --------------------------------------------------------------------------- #
# Change requests (request → approve/reject)
# --------------------------------------------------------------------------- #
class ChangeRequestCreate(CreateModel):
    subject_type: str = Field(max_length=30)
    subject_id: uuid.UUID
    field: str = Field(max_length=60)          # e.g. "stage" (Lending) / "status" (Syn, AM)
    to_value: str = Field(max_length=120)
    note: str | None = None


class ChangeRequestDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = None


class ChangeRequestRead(ReadModel):
    subject_type: str
    subject_id: uuid.UUID
    field: str
    from_value: str | None
    to_value: str
    note: str | None
    requested_by: str
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None


# --------------------------------------------------------------------------- #
# /v1/me — effective permissions for the calling user
# --------------------------------------------------------------------------- #
class MeRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]
    views: dict[str, str]        # view → NONE/READ/SCOPED/FULL
    operations: dict[str, str]   # operation → NONE/READ/SCOPED/FULL/APPROVE
    assignments: list[AssignmentRead]
