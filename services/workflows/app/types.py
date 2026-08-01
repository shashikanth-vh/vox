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
# Caller context — the TENANT and the human identity a workflow acts on behalf of.
# Threaded into every workflow input so the worker's Register calls run in the caller's
# tenant (never a fixed default) and, when signing is configured, re-mint a signed context
# so the Register authorizes writes as the HUMAN — not the worker's service key. Short-lived
# tokens are minted per activity (a durable workflow may sleep for days), so only the
# identity + live grant travel in history, never a token.
# --------------------------------------------------------------------------- #
@dataclass
class CallerContext:
    tenant: str = ""
    email: str = ""
    user_id: str = ""
    roles: list = field(default_factory=list)
    report_ids: list = field(default_factory=list)
    report_emails: list = field(default_factory=list)
    effective_views: dict = field(default_factory=dict)
    effective_operations: dict = field(default_factory=dict)
    decision: str | None = None


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
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


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

    # ---- Release-1 increment 2: human confirmation gates (flag-set by the orchestrator
    # from deployment settings; never trusted from the raw capture payload) --------------
    # With no EXACT canonical company match but CLOSE candidates, park the run and ask the
    # capturing RM to confirm (pick a candidate or "create new") instead of silently
    # creating a possible duplicate company.
    require_company_confirmation: bool = False
    # With SEVERAL active leads for the company, ask instead of auto-picking when the
    # ranked choice is a tie (ranking: owning RM > lens > sector > recency).
    require_lead_confirmation: bool = False
    # How long a parked run waits for its confirmation before failing loudly.
    confirmation_timeout_hours: float = 72.0
    state: str | None = None

    # The tenant + human this capture acts for (set by the orchestrator from the request).
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


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
    # Access user ids for the auto-created line assignments (verified server-side).
    rm_id: str | None = None
    analyst_id: str | None = None
    note: str | None = None
    # The tenant + human this conversion acts for (set by the orchestrator from the request).
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1
    # ---- Release-1 foundation knobs (all optional; 0 disables a timer) ----------------
    # SLA reminders while the run waits on a human: every N hours an operational event
    # ("sla_reminder") is emitted; after the escalation window a one-time "sla_escalation"
    # event fires. Delivery = structured log + optional ops webhook (see config).
    sla_reminder_hours: float = 24.0
    sla_escalation_hours: float = 72.0
    # Set by the orchestrator from settings: upsert Temporal search attributes for this run
    # (requires server-side registration — see README). Never set this from user input.
    emit_search_attributes: bool = False
    # Set ONLY by continue-as-new: how much of the decision window this run already spent
    # before continuing, so the timeout keeps counting across history resets.
    resumed_elapsed_hours: float = 0.0

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


# --------------------------------------------------------------------------- #
# Business lifecycle workflows — Lead Qualification → Deal Structuring → Document Collection.
#
# These are the governance-bearing workflows: they don't just advance a stage string, they make
# the real work HAPPEN and attach the IMMUTABLE evidence that the Register's evidence gate requires
# before it will accept a sensitive transition. So a deal reaches 'Sanctioned' only after this
# workflow has captured the Credit Committee's decision AND filed the sanction letter — the
# workflow is the audited path the reviewer's "sensitive transitions only through workflow" calls
# for, and the Register enforces it independently (a hand-rolled PATCH is refused all the same).
# --------------------------------------------------------------------------- #
@dataclass
class LeadQualificationInput:
    """Qualify a lead against the minimum bar to structure a deal. The workflow records the
    qualification review as durable evidence on the lead; a passing review hands off to structuring
    (a conversion request), a failing one records why and stops."""

    lead_id: str
    qualified_by: str
    # The qualification artefact (a completed screening memo / scorecard reference) + its digest.
    qualification_reference: str = ""
    qualification_sha256: str | None = None
    passed: bool = True
    reason: str | None = None
    # CONFIGURABLE checklist: [{key, label, required, passed, note}]. When present it is
    # AUTHORITATIVE — the workflow computes the outcome from it (every required item must
    # pass) and the legacy ``passed`` flag is ignored; the evaluation is recorded in the
    # qualification evidence. Item definitions come from deployment config (the orchestrator
    # merges WORKFLOWS_QUALIFICATION_CHECKLIST with the caller's per-item results).
    checklist: list = field(default_factory=list)
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class LeadQualificationResult:
    workflow_id: str
    lead_id: str
    status: str                     # Qualified / NotQualified
    evidence_id: str | None = None
    note: str | None = None
    # When a checklist drove the outcome: {"passed": bool, "failed_required": [keys],
    # "items_total": n, "items_passed": n} — the same summary filed in the evidence.
    checklist_summary: dict = field(default_factory=dict)


@dataclass
class SanctionDecisionInput:
    """The Credit Committee's recorded decision on a structured deal — the input the structuring
    workflow waits for (delivered by signal) before it may file sanction evidence and advance."""

    approved: bool = False
    decided_by: str = ""
    committee_reference: str = ""       # the committee minute / resolution reference
    sanction_letter_reference: str = ""  # the issued sanction letter reference
    note: str | None = None


@dataclass
class DealStructuringInput:
    """Structure a deal through the credit pipeline to the sanction milestone. The workflow walks
    the ordered stages (Diligence → Note Circulated), circulates the credit note, then waits for the
    Credit Committee decision; on approval it FILES the committee-approval + sanction-letter evidence
    and only then advances the deal to 'Sanctioned' (which the Register's evidence gate now accepts).
    Mandatory sanction fields (product_type, rm) are supplied so the transition is complete."""

    deal_id: str
    requested_by: str
    product_type: str | None = None
    rm: str | None = None
    credit_note_reference: str = ""     # the structured credit note circulated to committee
    decision_timeout_hours: int = 24 * 14
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1
    # ---- Release-1 foundation knobs (all optional; 0 disables a timer) ----------------
    # SLA reminders while the run waits on a human: every N hours an operational event
    # ("sla_reminder") is emitted; after the escalation window a one-time "sla_escalation"
    # event fires. Delivery = structured log + optional ops webhook (see config).
    sla_reminder_hours: float = 24.0
    sla_escalation_hours: float = 72.0
    # Set by the orchestrator from settings: upsert Temporal search attributes for this run
    # (requires server-side registration — see README). Never set this from user input.
    emit_search_attributes: bool = False
    # Set ONLY by continue-as-new: how much of the decision window this run already spent
    # before continuing, so the timeout keeps counting across history resets.
    resumed_elapsed_hours: float = 0.0



@dataclass
class DealStructuringResult:
    workflow_id: str
    deal_id: str
    # Sanctioned / PartiallySanctioned / Rejected / TimedOut / NoLendingLine — the BUSINESS
    # outcome (the workflow's technical progress is the separate `status` query).
    status: str
    decided_by: str | None = None
    stage: str | None = None
    evidence_ids: list = field(default_factory=list)
    note: str | None = None
    # Facility-specific outcomes: lending line id → Sanctioned / Rejected / NoDecision.
    line_outcomes: dict = field(default_factory=dict)
    # Which circulation of the credit note the committee decided on (1 = the original;
    # each committee-rework revision bumps it).
    credit_note_version: int = 0


@dataclass
class SyndicationMandateInput:
    """Drive a syndication MANDATE (a syndication_tracker row) from IM preparation to
    sanction and allocation. The mandate's status pipeline is enforced by the Register
    (ordered transitions + the syndication_sanction evidence gate); lender-level activity
    arrives as signals and lands on the deal's OTHER syndication rows — each move going
    through the same policy-enforcing API, never a side-door write."""

    syndication_id: str                 # the MANDATE row this run drives
    deal_id: str
    requested_by: str
    # The IM circulated at start (optional — it can arrive later via the circulate signal).
    im_reference: str = ""
    im_sha256: str | None = None
    decision_timeout_hours: int = 24 * 14
    # After sanction, how long to wait for the lender allocation before completing without
    # one (the allocation can still be recorded later by a fresh run / directly).
    allocation_timeout_hours: float = 24.0 * 7
    sla_reminder_hours: float = 24.0
    sla_escalation_hours: float = 72.0
    emit_search_attributes: bool = False
    resumed_elapsed_hours: float = 0.0
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class SyndicationMandateResult:
    workflow_id: str
    syndication_id: str
    # Sanctioned / Rejected / TimedOut / Cancelled — the BUSINESS outcome.
    status: str
    decided_by: str | None = None
    im_version: int = 0                 # which IM circulation the decision was made on
    # lender row id → allocated amount (₹ Cr); empty when the allocation window lapsed.
    allocations: dict = field(default_factory=dict)
    evidence_ids: list = field(default_factory=list)
    note: str | None = None


@dataclass
class SanctionExpiryInput:
    """Watch a sanctioned facility's validity window. Started (abandoned child) by the
    structuring workflow when the committee set ``valid_days``: reminds ops before the
    deadline and, if the line is STILL at 'Sanctioned' when it lapses, files the
    ``sanction_expired`` evidence — the sanction lapsed unprogressed, immutably on record."""

    lending_id: str
    deal_id: str
    valid_days: int
    # Days before expiry to raise the ops reminder (0 = no reminder).
    remind_before_days: int = 7
    decision_ref: str = ""              # the per-line committee decision this window came from
    emit_search_attributes: bool = False
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class SanctionExpiryResult:
    workflow_id: str
    lending_id: str
    status: str                         # Progressed / Expired
    stage_at_close: str | None = None
    evidence_id: str | None = None


@dataclass
class DocumentItem:
    """One required document in a collection checklist, with the reference + digest that will be
    filed as evidence once it is received."""

    name: str
    reference: str = ""
    sha256: str | None = None
    received: bool = False


@dataclass
class DocumentCollectionInput:
    """Collect the executed documentation for a sanctioned line and, once the mandatory set is
    complete, file the executed-agreement evidence. The checklist is driven by signals as documents
    arrive; the workflow completes when every mandatory item is received (or it times out)."""

    subject_type: str                # "Deal" or "Lending"
    subject_id: str
    requested_by: str
    required_documents: list = field(default_factory=list)   # list[str] of mandatory names
    collection_timeout_hours: int = 24 * 30
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class DocumentCollectionResult:
    workflow_id: str
    subject_type: str
    subject_id: str
    status: str                     # Complete / TimedOut
    received: list = field(default_factory=list)      # names received
    outstanding: list = field(default_factory=list)   # names still missing
    evidence_ids: list = field(default_factory=list)


@dataclass
class AdvayaHandoffInput:
    """Hand a Lending line that is 'Ready for Disbursement' OVER to Advaya. The workflow creates the
    durable, immutable handover PACKAGE (authoritative amounts read server-side) and advances the
    line to 'Disbursed' — PRISM's terminal. The amounts are NOT taken from here; only the
    handover metadata is. A real future integration would additionally record the acknowledgement."""

    lending_id: str
    requested_by: str
    # Handover-package metadata (authoritative amounts come from the Lending row; the package
    # reference/digest are GENERATED server-side; identities come from the authenticated context).
    executed_document_refs: list[dict] = field(default_factory=list)
    cpcs_checklist_version: int | None = None
    delivery_method: str | None = None
    recipient: str | None = None
    note: str | None = None
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class AdvayaHandoffResult:
    workflow_id: str
    lending_id: str
    status: str                     # Prepared / TimedOut
    handover_package_id: str | None = None
    handover_key: str | None = None
    note: str | None = None


@dataclass
class CpcsChecklistInput:
    """MAKER prepares the authoritative CP/CS checklist for a Lending line. A DIFFERENT checker then
    approves it (separate orchestrator endpoint), after which cp_cs_completion may be minted."""

    lending_id: str
    requested_by: str
    items: list[dict] = field(default_factory=list)
    deal_id: str | None = None
    checklist_version: int = 1
    note: str | None = None
    caller: CallerContext = field(default_factory=CallerContext)
    # Input-contract version — bump when this dataclass changes shape, so running
    # workflows and new workers can tell which contract an input was written under.
    schema_version: int = 1


@dataclass
class CpcsChecklistResult:
    workflow_id: str
    lending_id: str
    checklist_id: str | None = None
    status: str = "Completed"       # the prepared checklist's status (awaiting approval)
    note: str | None = None
