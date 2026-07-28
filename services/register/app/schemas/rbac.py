"""Request/response schemas for the Register-side RBAC flows (assignments, requests)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import CreateModel, ReadModel


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
# Lead → deal conversion (transactional; replaces workflow-side compensation)
# --------------------------------------------------------------------------- #
class LeadConvertRequest(CreateModel):
    is_lending: bool = False
    is_syndication: bool = False
    is_asset_mon: bool = False
    product_type: str | None = Field(default=None, max_length=60)
    amount_cr: float | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    # When provided, the primary-owner LineAssignment is created on each new product line
    # (Deal Analyst on lending, Syn RM on syndication, AM RM on asset-mon), so the owner's
    # scoped access covers the line immediately — not just the rm/analyst name string.
    rm_id: uuid.UUID | None = None
    analyst_id: uuid.UUID | None = None
    note: str | None = None


class LeadConvertResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: uuid.UUID
    deal_id: uuid.UUID
    lending_id: uuid.UUID | None = None
    syndication_id: uuid.UUID | None = None
    asset_mon_id: uuid.UUID | None = None
