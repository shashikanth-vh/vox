"""Workflow inputs/outputs. Plain dataclasses so Temporal's default data converter can
serialise them, and so they're safe to import inside the workflow sandbox.

Design note for newcomers: these are the ONLY shapes that cross the workflow boundary.
Keep them JSON-friendly (str/float/bool/dict/list), give every optional field a default,
and never put live objects (clients, sessions) in here — Temporal persists these values
in workflow history and replays them on recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Legacy reference workflow (kept: the simplest possible example of the pattern)
# --------------------------------------------------------------------------- #
@dataclass
class InteractionInput:
    """A field interaction to record durably against an entity (e.g. from VOX)."""

    entity_id: str
    interaction_type: str
    summary: str | None = None
    notes: str | None = None
    performed_by: str | None = None
    source: str = "Temporal"


@dataclass
class IngestResult:
    interaction_id: str
    dossier_counts: dict


# --------------------------------------------------------------------------- #
# VOX touchpoint — the genuine end-to-end capture workflow
# --------------------------------------------------------------------------- #
@dataclass
class VoxTouchpoint:
    """Everything a VOX capture can carry. Only ``company_name`` OR ``entity_id`` is
    required — the workflow resolves the company by canonical name and creates the
    entity + lead when they don't exist yet (the approved "new company" scenario)."""

    # Who the touchpoint is about — one of these two.
    company_name: str | None = None       # canonical resolution happens in the workflow
    entity_id: str | None = None          # pass directly when the caller already knows it

    # Stable id of the recording/upload. Doubles as the business workflow id
    # (vox-{capture_id}) and the idempotency root — retries can never duplicate.
    capture_id: str | None = None

    # The capture itself.
    interaction_type: str = "In-Person Meeting"
    direction: str | None = None
    occurred_at: str | None = None        # ISO timestamp
    summary: str | None = None
    notes: str | None = None              # the approved note text
    transcript: str | None = None
    audio_ref: str | None = None          # object-store / device URI of the recording
    language: str | None = None
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = None
    attendees: list | None = None
    key_intel: dict | None = None
    next_steps: list | None = None
    contact_name: str | None = None

    # People. performed_by = the acting RM in the field; assigned_rm = who owns the
    # company/lead (defaults to the acting RM for a brand-new lead). assigned_rm_id is
    # that RM's Access user id — when present, a VOX-created lead is ASSIGNED to them
    # (a real LineAssignment), so the actual BDRM owns it, not just a name string.
    performed_by: str | None = None
    assigned_rm: str | None = None
    assigned_rm_id: str | None = None

    # Follow-up (drives the lead's next_action / next meeting; the calendar hand-off is
    # recorded on the interaction's meta for a calendar integration to pick up).
    next_action: str | None = None
    next_action_date: str | None = None   # ISO date
    next_meeting_date: str | None = None  # ISO date

    # Hints used only when the workflow has to CREATE the company.
    sector: str | None = None
    lens: str | None = None
    state: str | None = None


@dataclass
class VoxResult:
    """What the workflow did, in full — so the caller (and the Temporal UI) can see
    every decision: matched vs created, which lead, which interaction."""

    workflow_id: str
    entity_id: str
    entity_created: bool
    lead_id: str | None
    lead_created: bool
    interaction_id: str
    follow_up: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Lead conversion — human-in-the-loop (signal-driven approval)
# --------------------------------------------------------------------------- #
@dataclass
class LeadConversionInput:
    """Convert a lead into a deal once a Head approves. The workflow waits for an
    approve/reject signal; approval creates the deal (+ requested product lines),
    links it back, and marks the lead Converted."""

    lead_id: str
    requested_by: str
    # Which product lines the deal should open with.
    is_lending: bool = False
    is_syndication: bool = False
    is_asset_mon: bool = False
    product_type: str | None = None
    amount_cr: float | None = None
    rm: str | None = None
    analyst: str | None = None
    note: str | None = None
    # Auto-reject if nobody decides within this window.
    approval_timeout_hours: int = 24 * 7


@dataclass
class LeadConversionResult:
    workflow_id: str
    lead_id: str
    status: str                     # Approved / Rejected / TimedOut
    decided_by: str | None = None
    decision_note: str | None = None
    deal_id: str | None = None
    lending_id: str | None = None
    syndication_id: str | None = None
    asset_mon_id: str | None = None
