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
    # Deal + product-line opening facts from the Push-to-Deals dialog. The dialog
    # collects a figure and a stage/status PER ticked line — the single amount_cr
    # above is the committee-facing total and must not overload every line (it used
    # to land verbatim on lending AND syndication, so both showed the combined sum).
    temperature: str | None = Field(default=None, max_length=10)
    lending_amount_cr: float | None = Field(default=None, ge=0)
    lending_stage: str | None = Field(default=None, max_length=40)
    syn_amount_cr: float | None = Field(default=None, ge=0)
    syn_type: str | None = Field(default=None, max_length=80)
    syn_mandate_status3: str | None = Field(default=None, max_length=40)
    syn_status: str | None = Field(default=None, max_length=40)
    syn_facility: str | None = Field(default=None, max_length=2000)
    syn_tenor: str | None = Field(default=None, max_length=20)
    syn_priority: str | None = Field(default=None, max_length=10)
    syn_im_status: str | None = Field(default=None, max_length=40)
    syn_potential: str | None = Field(default=None, max_length=4000)
    syn_existing: str | None = Field(default=None, max_length=4000)
    syn_price: str | None = Field(default=None, max_length=2000)
    # Asset-monetisation opening facts captured in the Push-to-Deals dialog. The AM
    # book is a plain update surface (no workflow), so what the RM typed at push time
    # must land ON the row — there is no later ceremony to carry it.
    am_value_cr: float | None = Field(default=None, ge=0)
    am_size_mw: float | None = Field(default=None, ge=0)
    am_deal_type: str | None = Field(default=None, max_length=80)
    am_status: str | None = Field(default=None, max_length=40)
    # The human who approved the conversion (from the orchestrator's verified decision).
    # Recorded as provenance in the conversion trail — a service key never becomes the
    # audit actor, so this is data, not identity.
    approved_by: str | None = Field(default=None, max_length=200)


class LeadConvertResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: uuid.UUID
    deal_id: uuid.UUID
    lending_id: uuid.UUID | None = None
    syndication_id: uuid.UUID | None = None
    asset_mon_id: uuid.UUID | None = None
