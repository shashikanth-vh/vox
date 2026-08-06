"""The Orchestrator API — the operational front door of the workflow plane.

This is what was missing between "workflows exist" and "workflows run": an HTTP service
that STARTS workflows with **stable business workflow ids**, delivers **signals**
(approve/reject) and answers **status** queries. VocX, ATLAS, the gateway — anything that
can POST JSON — can now trigger durable work without a Temporal client or CLI.

Identity of a run = its business id:
    VOX capture        →  vox-{capture_id}
    lead conversion    →  leadconv-{lead_id}
Starting the same id twice attaches to the existing run instead of duplicating it —
idempotent starts on top of the Register-level idempotency the activities already carry.

Run it:  python -m app.api   (same image as the worker; a second container/deployment).
"""

from __future__ import annotations

import contextlib

import orjson
import hashlib
import hmac
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

from evam_backend_core.logging import configure_logging, get_logger
from evam_backend_core.oidc import (
    OidcError,
    TokenVerifier,
    bearer_token,
    build_verifier,
)
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import httpx
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from app.codec import build_data_converter
from app.config import get_settings
from app.types import (
    AssetMonetisationInput,
    SyndicationMandateInput,
    AdvayaHandoffInput,
    CallerContext,
    CovenantMonitorInput,
    CpcsChecklistInput,
    DealStructuringInput,
    DocumentCollectionInput,
    DocumentExpiryInput,
    EwsCaseInput,
    LeadConversionInput,
    LeadQualificationInput,
    VoxTouchpoint,
)
from app.workflows import (
    AssetMonetisationWorkflow,
    SyndicationMandateWorkflow,
    AdvayaHandoffWorkflow,
    CovenantMonitorWorkflow,
    CpcsChecklistWorkflow,
    DealStructuringWorkflow,
    DocumentCollectionWorkflow,
    DocumentExpiryMonitorWorkflow,
    EwsCaseWorkflow,
    LeadConversionWorkflow,
    LeadQualificationWorkflow,
    VoxTouchpointWorkflow,
)

log = get_logger("orchestrator")

# --------------------------------------------------------------------------------------- #
# THE MAKER CATALOGUE — "what can I do next on this line, and if not, why not?"
#
# The approver's half of every governed flow has always been server-described: the pending
# list hands back the verbs, and Today renders whatever it is given. The MAKER's half was
# not, so ATLAS had no way to start a committee run, prepare a checklist or attest a
# handover — the whole spine lived in Postman.
#
# This is the same idea pointed the other way. The sequencing rules stay HERE, next to the
# workflows that enforce them, and the UI renders what it is handed. A UI that keeps its
# own copy of these rules drifts: the Lending stage dropdown offered four stages the
# register would always refuse, because it was guessing.
#
# An unavailable action is still RETURNED, with a reason. "Available once the committee has
# sanctioned this facility" teaches the process; a hidden button teaches nothing.
# --------------------------------------------------------------------------------------- #

# Roles that may do MAKER work in each vertical. Wider than the approver sets in
# _APPROVER_ROLES — preparing is not deciding — and deliberately so: the four-eyes rules
# that stop a preparer approving their own work are enforced at the write, not here.
# The CREDIT desk prepares the CAM, raises the committee and works the sanction —
# the sourcing RM (BD) hands over at qualification and does not drive credit steps.
_CREDIT_MAKERS = {"Credit Head", "Deal Analyst", "Management", "Admin"}
_SYN_MAKERS = {"Syn Head", "Syn RM", "Management", "Admin"}
_AM_MAKERS = {"AM Head", "AM RM", "Management", "Admin"}


def _f(name: str, label: str, kind: str = "text", *, required: bool = False,
       options: list[str] | None = None, placeholder: str | None = None,
       default: Any = None, help_text: str | None = None) -> dict[str, Any]:
    """One field of an action's form. `kind` is text | textarea | number | date | select."""
    field: dict[str, Any] = {"name": name, "label": label, "type": kind,
                             "required": required}
    if options is not None:
        field["options"] = options
    if placeholder:
        field["placeholder"] = placeholder
    if default is not None:
        field["default"] = default
    if help_text:
        field["help"] = help_text
    return field


_NOTE = _f("note", "Note", "textarea", placeholder="Context for the approver (optional)")

# Fields the SERVER fills from the verified caller. Never asked of the user: the identity
# on a governance action is the token's, not a text box's.
_IDENTITY_FIELDS = ("requested_by", "by")

# run gate: None = don't care · "none" = no live run · "live" = a run is open ·
#           "returned" = a run is open and parked back with the maker.
#
# Every action's `form` + `body` must satisfy the endpoint's own schema — required fields
# covered, nothing extra. A test walks the OpenAPI and checks exactly that, because the
# first version of this catalogue was written from the endpoint NAMES and got most of the
# bodies wrong; the first thing a user saw was `amount_cr: Extra inputs are not permitted`.
_MAKER_ACTIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "Lending": (
        {
            "key": "cam.workbench",
            "label": "CAM workbench",
            "method": "POST", "url": "/v1/cam/{subject_id}/generate",
            "roles": _CREDIT_MAKERS,
            # The CAM is drafted, reworked and decided BEFORE the committee sanctions —
            # its approved version is what the committee reads.
            "stages": {"Data Awaited", "Diligence", "Note Circulated"},
            "stage_reason": "The CAM is prepared before the committee decision — this "
                            "facility is already past that point.",
            # Selecting source documents + a prompt doc and reworking a draft is a
            # conversation, not a flat form — the client renders its own screen.
            "screen": "cam-workbench",
            "prefill": {"deal_id": "deal_id"},
            "form": [],
        },
        {
            "key": "deal-structuring.start",
            "label": "Send to credit committee",
            "method": "POST", "url": "/v1/workflows/deal-structurings",
            "roles": _CREDIT_MAKERS, "run": "none",
            "stages": {"Data Awaited", "Diligence", "Note Circulated"},
            "stage_reason": "The committee decision has already been taken on this facility.",
            "run_reason": "A committee run is already open on this deal.",
            "prefill": {"deal_id": "deal_id"},
            "form": [_f("credit_note_reference", "Credit note reference",
                        placeholder="Auto-numbered — CN/<company>/<yyyymm>-<seq>",
                        help_text="Leave blank and the register issues the next number "
                                  "in this company's series; type one only to override."),
                     _f("product_type", "Product", placeholder="Term Loan"),
                     _f("rm", "Relationship manager")],
        },
        {
            "key": "deal-structuring.revise-credit-note",
            "label": "File a revised credit note",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/revise-credit-note",
            "roles": _CREDIT_MAKERS, "run": "returned",
            "run_reason": "Available when the committee has returned this run for revision.",
            "form": [_f("reference", "Revised credit note reference", required=True,
                        placeholder="CN/<COMPANY>/2026-01-v2"),
                     _f("sha256", "Document digest (optional)",
                        help_text="The SHA-256 of the revised note, if you have it.")],
        },
        {
            "key": "run.resubmit",
            "label": "Send back for decision",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/control",
            "roles": _CREDIT_MAKERS, "run": "returned",
            "run_reason": "Available once this run has been returned to you.",
            "constant": {"action": "resubmit"},
            "form": [_f("note", "What you changed", "textarea", required=True)],
        },
        {
            "key": "sanction.terms",
            "label": "Enter sanction terms",
            "method": "POST", "url": "/v1/internal/sanction-terms",
            "roles": _CREDIT_MAKERS, "stages": {"Sanctioned"},
            "stage_reason": "Available once the committee has sanctioned this facility.",
            # Amount, rate, tenor, EMI — plus the CP/CS item lists and the covenant
            # register the terms SEED. Lists of structured rows need their own screen.
            "screen": "sanction-terms",
            "prefill": {"lending_id": "id", "deal_id": "deal_id"},
            "form": [],
        },
        {
            "key": "cpcs.prepare",
            "label": "Conditions Precedent",
            "method": "POST", "url": "/v1/workflows/cpcs-checklists",
            "roles": _CREDIT_MAKERS, "stages": {"Sanctioned"},
            "stage_reason": "Available once the committee has sanctioned this facility.",
            # The checklist is a LIST of conditions, each with its own evidence — a flat
            # form cannot express it honestly, so it waits for its own screen rather than
            # shipping a JSON box that looks like a feature. This step works the CP half
            # (the pre-disbursement conditions, read from the sanction letter); its
            # approval releases disbursement, carrying any conditions NOT met.
            "screen": "cpcs-checklist",
            "prefill": {"lending_id": "id"},
            "form": [_f("checklist_version", "Version", "number", default=1),
                     _NOTE],
        },
        {
            # The CS half is its OWN step: it starts once disbursement is in motion and
            # a new version is filed each time documents arrive, until nothing is open —
            # the chase reminders on Today run off the latest APPROVED version.
            "key": "cpcs.update-cs",
            "label": "Conditions Subsequent",
            "method": "POST", "url": "/v1/workflows/cpcs-checklists",
            "roles": _CREDIT_MAKERS,
            # Gated on the CP checklist being APPROVED (checked below with live data),
            # not on a stage move the analyst may not have made yet.
            "stages": {"Sanctioned", "CP/CS Completed", "Ready for Disbursement",
                       "Disbursed"},
            "stage_reason": "Conditions subsequent are worked once the CP checklist is "
                            "approved and disbursement is in motion.",
            "screen": "cpcs-checklist",
            "prefill": {"lending_id": "id"},
            "form": [_f("checklist_version", "Version", "number", default=1),
                     _NOTE],
        },
        {
            # A FALLBACK only: the CP approval auto-moves the stage itself. This stays
            # for the rare case where that auto-move failed (register unreachable at the
            # moment of approval) — the evidence gate still guards it.
            "key": "lending.cpcs-complete",
            "label": "Move to CP/CS Completed",
            "method": "PATCH", "url": "/v1/lending/{subject_id}",
            "roles": _CREDIT_MAKERS, "stages": {"Sanctioned"},
            "stage_reason": "The line moves here automatically when the CP checklist is "
                            "approved.",
            "evidence": ("cp_cs_completion",),
            "constant": {"stage": "CP/CS Completed"},
            "form": [],
        },
        {
            # ONE verb for the desk: shows the unmet CPs, stages the line if the CP
            # approval just landed, prepares the request package (unmet CPs in its
            # note), and marks it SENT. Generic over the partner — Advaya today,
            # PRISM's own arm tomorrow.
            "key": "disburse",
            "label": "Disburse",
            "method": "POST", "url": "/v1/workflows/disburse",
            "roles": _CREDIT_MAKERS,
            # 'Disbursed' too: the same dialog records the partner's answers and each
            # later tranche (T2, T3, ...) — the whole journey lives in one place.
            "stages": {"CP/CS Completed", "Ready for Disbursement", "Disbursed"},
            "stage_reason": "Disbursement follows the Conditions Precedent approval.",
            "screen": "disburse",
            "prefill": {"lending_id": "id"},
            "form": [],
        },
    ),
    "Syndication": (
        {
            "key": "syndication.start",
            "label": "Start the mandate run",
            "method": "POST", "url": "/v1/workflows/syndications",
            "roles": _SYN_MAKERS, "run": "none",
            "run_reason": "A mandate run is already open on this line.",
            "prefill": {"syndication_id": "id", "deal_id": "deal_id"},
            "form": [_f("im_reference", "Information memorandum reference"),
                     _f("im_sha256", "IM digest (optional)")],
        },
        {
            "key": "syndication.lender-update",
            "label": "Record a lender response",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/lender-update",
            "roles": _SYN_MAKERS, "run": "live",
            "run_reason": "Start the mandate run first.",
            # Addressed by the lender ROW id from the run's own state, which a person
            # cannot type — it needs the lender list on screen to pick from.
            "needs_screen": "the syndication chase screen",
            "form": [_f("status", "Status", "select", required=True,
                        options=["Identified", "IM Circulated", "Queries Received",
                                 "IP Received", "Sanctioned", "Declined"]),
                     _NOTE],
        },
        {
            "key": "syndication.allocate",
            "label": "Allocate the sanctioned amounts",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/allocate",
            "roles": _SYN_MAKERS, "run": "live",
            "run_reason": "Start the mandate run first.",
            "needs_screen": "the allocation screen",
            "form": [],
        },
    ),
    "AssetMonetisation": (
        {
            "key": "asset-monetisation.start",
            "label": "Start the mandate run",
            "method": "POST", "url": "/v1/workflows/asset-monetisations",
            "roles": _AM_MAKERS, "run": "none",
            "run_reason": "A mandate run is already open on this asset.",
            "prefill": {"asset_mon_id": "id", "deal_id": "deal_id"},
            "form": [_f("teaser_reference", "Teaser reference"),
                     _f("teaser_sha256", "Teaser digest (optional)")],
        },
        {
            "key": "asset-monetisation.record-nda",
            "label": "Record an NDA",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/record-nda",
            "roles": _AM_MAKERS, "run": "live",
            "run_reason": "Start the mandate run first.",
            "needs_screen": "the buyer list screen",
            "form": [_f("reference", "NDA reference", required=True),
                     _f("data_room", "Data room")],
        },
        {
            "key": "asset-monetisation.record-offer",
            "label": "Record an offer",
            "method": "POST", "url": "/v1/workflows/{workflow_id}/record-offer",
            "roles": _AM_MAKERS, "run": "live",
            "run_reason": "Start the mandate run first.",
            "needs_screen": "the buyer list screen",
            "form": [_f("kind", "Offer", "select", required=True, options=["NBO", "BO"]),
                     _f("amount_cr", "Offer \u20b9 Cr", "number", required=True),
                     _f("reference", "Offer reference")],
        },
    ),
}


# Which identity key each endpoint declares — `requested_by` on a run START, `by` on a
# signal, neither on the two register routes. Filled from the verified caller.
_IDENTITY_FOR: dict[str, tuple[str, ...]] = {
    # Screen-driven steps: the workbench and the terms screen speak to endpoints that
    # attribute to the verified caller themselves — no identity field to fill.
    "cam.workbench": (),
    "sanction.terms": (),
    "deal-structuring.start": ("requested_by",),
    "deal-structuring.revise-credit-note": ("by",),
    "run.resubmit": ("by",),
    "cpcs.prepare": ("requested_by",),
    "cpcs.update-cs": ("requested_by",),
    "disburse": ("requested_by",),
    # A plain stage write: the register attributes it to the verified caller from
    # the forwarded identity, so there is no identity FIELD to fill.
    "lending.cpcs-complete": (),
    "lending.ready-for-disbursement": (),
    "syndication.start": ("requested_by",),
    "syndication.lender-update": ("by",),
    "syndication.allocate": ("by",),
    "asset-monetisation.start": ("requested_by",),
    "asset-monetisation.record-nda": ("by",),
    "asset-monetisation.record-offer": ("by",),
}


# The deterministic workflow id a subject's run carries, by subject type. Same construction
# the start routes use, so the lookup cannot drift from the thing it looks up.
_RUN_ID_FOR: dict[str, tuple[str, str]] = {
    "Lending": ("struct", "deal_id"),          # the committee run lives on the DEAL
    "Syndication": ("synd", "id"),
    "AssetMonetisation": ("amon", "id"),
}

# Business statuses that mean "the maker has it back".
_RETURNED_STATES = {"ReturnedForInformation", "Returned", "ReturnedToMaker"}


# Evidence kinds in the words a credit manager uses, for the "still waiting on …" reason.
# Why a handover step is not yet available — phrased as the thing to do next.
_PACKAGE_REASON: dict[str, str] = {
    "Approved": "The package must be approved by a different checker before it can be "
                "submitted.",
    "Submitted": "Advaya's outcome applies to a SUBMITTED package — prepare it, have a "
                 "checker approve it, then submit it.",
}

_EVIDENCE_LABEL: dict[str, str] = {
    "cp_cs_completion": "an approved CP/CS checklist",
    "executed_agreement": "the executed agreement",
    "credit_committee_approval": "the credit committee's approval",
    "sanction_letter": "the sanction letter",
    "advaya_acknowledgement": "Advaya's acknowledgement",
}


# The signed internal context is BOUND to the route it was minted for, and the register
# compares it against `request.url.path` — which never carries a query string. Minting it
# with one (…/cpcs-checklists?lending_id=…) produced a path mismatch and a 403 on every
# orchestrator read that filters, silently: the caller discarded the problem and used its
# empty default. That is why a CP/CS screen re-opened on version 1 after v1 was approved,
# and handed the user a 409 for a checklist they had just filled in.
def _token_path(path: str) -> str:
    """The path claim for a signed internal context — route only, never the query."""
    return path.split("?", 1)[0]


def _plane_of(spec: dict, url: str) -> str:
    """'orchestrator' or 'register' — which service answers this action's url.

    Derived from the prefix, because every orchestrator route lives under /v1/workflows
    and everything else in the catalogue is a register route; an action that needs to say
    otherwise sets "plane" on its spec explicitly.
    """
    declared = spec.get("plane")
    if declared:
        return str(declared)
    return "orchestrator" if url.startswith("/v1/workflows") else "register"


def _evaluate_action(action: dict[str, Any], *, roles: set[str], stage: str,
                     run_state: str) -> tuple[bool, str]:
    """Is this action available, and if not, what does the user need to know?

    Order matters: the role answer is about WHO you are and never changes with the
    subject, so it is checked first; the stage answer is the one that teaches the
    sequence, so it comes before the run-state answer.
    """
    needed = action.get("roles")
    if needed is not None and roles and not (roles & needed):
        return False, ("This step is done by " + ", ".join(sorted(needed)) + ".")
    stages = action.get("stages")
    if stages is not None and stage and stage not in stages:
        return False, action.get("stage_reason", f"Not available at stage '{stage}'.")
    waiting = action.get("needs_screen")
    if waiting:
        return False, (f"This step needs {waiting}, which is not built yet — drive it from "
                       "the API collection for now.")
    want_run = action.get("run")
    if want_run == "none" and run_state != "none":
        return False, action.get("run_reason", "A run is already open on this subject.")
    if want_run == "live" and run_state == "none":
        return False, action.get("run_reason", "No run is open on this subject yet.")
    if want_run == "returned" and run_state != "returned":
        return False, action.get("run_reason",
                                 "Available when this run is returned to you.")
    return True, ""



# Where a subject is read from, and which of its fields carries the stage the catalogue
# gates on. Kept beside the catalogue so adding a subject type is one edit, not three.
_SUBJECT_PATH: dict[str, str] = {
    "Lending": "/v1/lending",
    "Syndication": "/v1/syndication",
    "AssetMonetisation": "/v1/asset-monetisation",
}
_STAGE_FIELD: dict[str, str] = {
    "Lending": "stage",
    "Syndication": "status",
    "AssetMonetisation": "status",
}


# Who may decide which workflow, keyed by the workflow-id PREFIX. leadconv is a lead→deal
# conversion (a BD decision); struct is the Credit Committee's sanction decision on a structured
# deal (credit authority only).
_APPROVER_ROLES: dict[str, set[str]] = {
    "leadconv": {"BD Head", "Management", "Admin"},
    "struct": {"Credit Head", "Management", "Admin"},
    # Handing a facility OVER to Advaya is a money-movement authorization — senior credit authority.
    "handover": {"Credit Head", "Management", "Admin"},
    # Approving a CP/CS checklist (the checker) — senior credit authority.
    "cpcs": {"Credit Head", "Management", "Admin"},
    # Deciding a CAM (the committee's working copy) — the same seniority.
    "cam": {"Credit Head", "Management", "Admin"},
    # The syndication desk's sanction call on a mandate.
    "synd": {"Syn Head", "Management", "Admin"},
    # The AM desk's closure call on an asset-monetisation mandate.
    "amon": {"AM Head", "Management", "Admin"},
}


def _uuid_or_none(value: str | None) -> str | None:
    """Reject a non-UUID value for an id field at the API door — an unset client
    variable arrives as the LITERAL string "null"/"undefined", which is truthy, so it
    sails past every `if entity_id:` check and dies deep in a register query instead.
    Empty strings normalise to None (field genuinely not provided)."""
    if value is None or value.strip() == "":
        return None
    try:
        uuid.UUID(value.strip())
    except ValueError:
        raise ValueError(f"'{value}' is not a UUID — was a client variable unset?") from None
    return value.strip()


class VoxTouchpointIn(BaseModel):
    """The HTTP shape of a VOX capture — mirrors ``types.VoxTouchpoint``."""

    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(max_length=180)   # required here: it IS the workflow id
    company_name: str | None = Field(default=None, max_length=300)
    entity_id: str | None = None
    interaction_type: str = Field(default="In-Person Meeting", max_length=60)
    direction: str | None = Field(default=None, max_length=20)
    occurred_at: str | None = None
    summary: str | None = Field(default=None, max_length=300)
    notes: str | None = None
    transcript: str | None = None
    audio_ref: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=20)
    gps_lat: float | None = None
    gps_lng: float | None = None
    location: str | None = Field(default=None, max_length=200)
    attendees: list[Any] | None = None
    key_intel: dict[str, Any] | None = None
    next_steps: list[Any] | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    performed_by: str | None = Field(default=None, max_length=120)
    assigned_rm: str | None = Field(default=None, max_length=120)
    assigned_rm_id: str | None = None
    next_action: str | None = None
    next_action_date: str | None = None
    next_meeting_date: str | None = None
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)

    _ids_are_uuids = field_validator("entity_id", "assigned_rm_id")(_uuid_or_none)


class LeadConversionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str
    requested_by: str = Field(max_length=200)
    is_lending: bool = False
    is_syndication: bool = False
    is_asset_mon: bool = False
    product_type: str | None = Field(default=None, max_length=60)
    amount_cr: float | None = None
    rm: str | None = Field(default=None, max_length=120)
    analyst: str | None = Field(default=None, max_length=120)
    # Access user ids for the auto-created product-line assignments (verified server-side).
    # Without these, RM/analyst line assignments are never created on conversion.
    rm_id: str | None = None
    analyst_id: str | None = None
    note: str | None = None
    approval_timeout_hours: int = Field(default=24 * 7, ge=1, le=24 * 90)
    # THE CLIENT, when the lead has not been linked to one yet. The Push-to-Deals dialog
    # collects these ("One save: client + deal + product rows"), and a deal cannot exist
    # without a company — so the conversion resolves the company by canonical name and
    # CREATES it when it is genuinely new, exactly as a VOX capture does. Omitted fields
    # simply are not set on a newly created client; an already-linked lead ignores them.
    company_name: str | None = Field(default=None, max_length=300)
    sector: str | None = Field(default=None, max_length=60)
    lens: str | None = Field(default=None, max_length=20)
    state: str | None = Field(default=None, max_length=60)
    industry: str | None = Field(default=None, max_length=200)
    about: str | None = None

    _ids_are_uuids = field_validator("rm_id", "analyst_id")(_uuid_or_none)

    @field_validator("lead_id")
    @classmethod
    def _lead_id_is_uuid(cls, value: str) -> str:
        out = _uuid_or_none(value)
        if out is None:
            raise ValueError("lead_id is required and must be a UUID.")
        return out


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    note: str | None = None


class CompanyConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # One of the run's proposed candidate entity ids, or "" = "this really is a NEW
    # company". The workflow whitelists against its own candidates — an id it never
    # proposed is ignored, so this can steer only among legitimate choices.
    entity_id: str = Field(default="", max_length=60)
    by: str = Field(max_length=200)


class LeadSelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: str = Field(max_length=60)
    by: str = Field(max_length=200)


class SyndicationStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    syndication_id: str = Field(max_length=64)
    deal_id: str = Field(max_length=64)
    requested_by: str = Field(max_length=200)
    im_reference: str = Field(default="", max_length=500)
    im_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    decision_timeout_hours: int = Field(default=24 * 14, ge=1, le=24 * 90)
    allocation_timeout_hours: float = Field(default=24.0 * 7, ge=1, le=24 * 90)


class SyndicationDecisionIn(BaseModel):
    """The Syn Head's recorded decision on a mandate — persist-before-signal, like every
    decision in the platform."""

    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    approved: bool
    sanction_reference: str = Field(default="", max_length=500)
    conditions: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=2000)


class LenderUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lender_row_id: str = Field(max_length=64)
    status: str = Field(max_length=40)
    note: str = Field(default="", max_length=1000)
    by: str = Field(max_length=200)


class BuyerUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_row_id: str = Field(max_length=64)
    status: str = Field(max_length=40)
    note: str = Field(default="", max_length=1000)
    by: str = Field(max_length=200)


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # lender row id → allocated amount (₹ Cr); validated in-run against the mandate.
    allocations: dict[str, float] = Field(min_length=1)
    by: str = Field(max_length=200)


class AmStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_mon_id: str = Field(max_length=64)
    deal_id: str = Field(max_length=64)
    requested_by: str = Field(max_length=200)
    teaser_reference: str = Field(default="", max_length=500)
    teaser_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    decision_timeout_hours: int = Field(default=24 * 60, ge=1, le=24 * 365)


class AmDecisionIn(BaseModel):
    """The AM Head's closure decision on a mandate — approved = the sale CLOSES;
    rejected = the mandate is LOST/dropped, with the reason on record."""

    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    approved: bool
    closure_reference: str = Field(default="", max_length=500)
    note: str | None = Field(default=None, max_length=2000)


class NdaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_row_id: str = Field(max_length=64)
    reference: str = Field(min_length=1, max_length=500)
    data_room: bool = False
    by: str = Field(max_length=200)


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_row_id: str = Field(max_length=64)
    kind: str = Field(pattern="^(nbo|binding)$")
    amount_cr: float = Field(gt=0)
    reference: str = Field(default="", max_length=500)
    by: str = Field(max_length=200)


class WaiverDecisionIn(BaseModel):
    """A covenant-waiver decision, recorded through the front door. The Register's
    single-winner decision store accepts writes only from the workflow service principal
    carrying a verified approver identity — so the senior credit human records it HERE,
    and this service persists it under its principal with the approver's delegated,
    route-bound context. Authority (_WAIVER_AUTHORITY) and the subject binding are
    enforced by the Register from that context, never from these fields."""
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=3, max_length=200)
    decision: str = Field(pattern="^(Approved|Rejected)$")
    subject_id: str = Field(max_length=64)          # the Monitoring observation row
    valid_days: int = Field(ge=1, le=730)           # a waiver is ALWAYS time-boxed
    note: str = Field(default="", max_length=1000)
    by: str = Field(default="", max_length=200)


class CreditNoteRevisionIn(BaseModel):
    """A REVISED credit note for a structuring run awaiting (or returned for) a committee
    decision — the committee-rework loop's artefact. Filed as the next credit_note version
    on every lending line; the run's `state` query reports the current version."""

    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=500)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    by: str = Field(max_length=200)


class ControlIn(BaseModel):
    """A run-control action on a waiting workflow. ``cancel`` ends the run; ``return`` parks
    it as ReturnedForInformation (the deciders want more from the requester); ``resubmit``
    puts it back to AwaitingDecision and RESTARTS its SLA clock."""

    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(cancel|return|resubmit)$")
    by: str = Field(max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class ChecklistItemIn(BaseModel):
    """One qualification checklist RESULT from the caller. The item definitions (which keys
    exist, which are required) come from deployment config — the caller only says what
    passed; unknown keys are refused at merge time."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=60)
    passed: bool
    note: str | None = Field(default=None, max_length=500)


class LeadQualificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: str
    qualified_by: str = Field(max_length=200)
    qualification_reference: str = Field(default="", max_length=500)
    qualification_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    passed: bool = True
    reason: str | None = Field(default=None, max_length=2000)
    # Per-item results against the deployment's configured checklist. Required whenever the
    # deployment configures one (the workflow then COMPUTES the outcome; `passed` above is
    # ignored); refused when it doesn't (results against no definitions mean nothing).
    checklist: list[ChecklistItemIn] | None = None


class DealStructuringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deal_id: str
    requested_by: str = Field(max_length=200)
    product_type: str | None = Field(default=None, max_length=60)
    rm: str | None = Field(default=None, max_length=120)
    credit_note_reference: str = Field(default="", max_length=500)
    decision_timeout_hours: int = Field(default=24 * 14, ge=1, le=24 * 90)


class FacilityDecision(BaseModel):
    """The committee's outcome for ONE lending facility. Committee approval is
    facility-specific: each line gets its own recorded outcome, note, CONDITIONS (a
    conditional approval) and validity window."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(max_length=60)
    approved: bool
    note: str | None = Field(default=None, max_length=2000)
    # Conditional approval: the conditions the sanction carries (filed as governance
    # evidence on the line) and how many days it stays valid before lapsing unprogressed
    # (a monitor watches the window and files the expiry).
    conditions: str | None = Field(default=None, max_length=4000)
    valid_days: int | None = Field(default=None, ge=1, le=3650)


class CommitteeDecisionIn(BaseModel):
    """The Credit Committee's recorded decision on a structured deal, delivered through the
    orchestrator (fresh-authorized + durably persisted BEFORE the workflow is signalled).

    TWO submission forms, exactly one of which must be used:
    * ``facilities`` — FACILITY-SPECIFIC outcomes: one entry per lending line, each with its
      own approve/reject (+ note/conditions). Every line of the deal must be covered.
    * ``approved``   — a GROUPED submission: one outcome applied to every line — but still
      RECORDED as a separate per-facility decision for each line, so the audit trail always
      answers per facility. A single deal-wide result never implicitly sanctions lines."""

    model_config = ConfigDict(extra="forbid")
    by: str = Field(max_length=200)
    approved: bool | None = None
    facilities: list[FacilityDecision] | None = None
    committee_reference: str = Field(default="", max_length=500)
    sanction_letter_reference: str = Field(default="", max_length=500)
    note: str | None = Field(default=None, max_length=2000)
    # Grouped-form conditional approval: applied to EVERY line (still recorded per
    # facility). Facility-specific submissions carry these per entry instead.
    conditions: str | None = Field(default=None, max_length=4000)
    valid_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> "CommitteeDecisionIn":
        if (self.approved is None) == (self.facilities is None):
            raise ValueError(
                "Provide exactly one of 'approved' (grouped) or 'facilities' "
                "(facility-specific outcomes).")
        if self.facilities is not None and not self.facilities:
            raise ValueError("'facilities' must not be empty.")
        return self


class DocumentCollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str = Field(pattern="^(Deal|Lending)$")
    subject_id: str
    requested_by: str = Field(max_length=200)
    required_documents: list[str] = Field(default_factory=list, max_length=100)
    collection_timeout_hours: int = Field(default=24 * 30, ge=1, le=24 * 120)


class HandoverDocRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class AdvayaHandoverIn(BaseModel):
    """MAKER prepares the Advaya handover of a Lending line that is 'Ready for Disbursement'.
    Requires senior credit authority (Credit Head / Management / Admin). The workflow prepares the
    durable handover package (authoritative amounts + package digest generated server-side); a
    DIFFERENT checker must then approve it to advance the line to 'Disbursed'."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str
    requested_by: str = Field(max_length=200)          # the maker (authenticated)
    executed_document_refs: list[HandoverDocRef] = Field(min_length=1, max_length=100)
    cpcs_checklist_version: int | None = Field(default=None, ge=1)
    delivery_method: str = Field(min_length=1, max_length=60)
    recipient: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class CpcsItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=1000)
    condition_type: str = Field(pattern="^(CP|CS)$")
    required: bool = True
    status: str = Field(default="Pending", pattern="^(Pending|Completed|Waived|Deferred as CS)$")
    reason: str | None = Field(default=None, max_length=1000)
    expiry_date: str | None = None
    evidence_ref: str | None = Field(default=None, max_length=300)
    note: str | None = None


class CpcsChecklistIn(BaseModel):
    """MAKER prepares the CP/CS checklist for a Lending line. A DIFFERENT checker then approves it."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str
    requested_by: str = Field(max_length=200)          # the maker (authenticated)
    items: list[CpcsItemIn] = Field(min_length=1)
    deal_id: str | None = Field(default=None, max_length=64)
    checklist_version: int = Field(default=1, ge=1)
    note: str | None = Field(default=None, max_length=2000)


class DisburseIn(BaseModel):
    """One click from the desk: stage the line if needed, prepare the request package
    (unmet CPs riding in its note), and mark it SENT to the disbursement partner —
    Advaya today, PRISM's own arm tomorrow; the flow does not care."""
    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(min_length=1, max_length=64)
    requested_by: str = Field(default="", max_length=200)
    proposed_amount: float | None = Field(default=None, gt=0)
    proposed_date: str | None = Field(default=None, max_length=10)
    recipient: str = Field(default="Advaya (disbursement partner)", max_length=200)
    note: str | None = Field(default=None, max_length=4000)


class CpcsApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(max_length=200)
    # Optional: an approval may carry the checker's words — forwarded to the register's
    # audit record. (A note used to be REFUSED here: typing one blocked the approval.)
    note: str | None = Field(default=None, max_length=4000)


class CamApproveIn(BaseModel):
    """Committee approval of a CAM. Unlike the CP/CS approve, a note is welcome — the
    committee often approves WITH observations, and they belong on the record."""

    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class CheckerRejectIn(BaseModel):
    """The checker's TERMINAL refusal (CP/CS checklist or handover package). The decider
    is the AUTHENTICATED caller; ``rejected_by`` is only a dev fallback. Note mandatory —
    a terminal refusal must say why, permanently."""

    model_config = ConfigDict(extra="forbid")
    rejected_by: str = Field(max_length=200)
    note: str = Field(min_length=1, max_length=2000)


# The Push-to-Deals CLIENT fields: consumed by the conversion pre-flight (link-or-create
# the company) and deliberately NOT carried into workflow history.
_CLIENT_ONLY_FIELDS = {"company_name", "sector", "lens", "state", "industry", "about"}


class CheckerReturnIn(BaseModel):
    """The checker's RETURN-TO-MAKER (CP/CS checklist or handover package): amend and
    come back — non-terminal, the loop continues. The decider is the AUTHENTICATED
    caller; ``returned_by`` is only a dev fallback. Reasons are mandatory: a return
    without them is useless to the maker."""

    model_config = ConfigDict(extra="forbid")
    returned_by: str = Field(max_length=200)
    note: str = Field(min_length=1, max_length=2000)


class AdvayaHandoverApproveIn(BaseModel):
    """CHECKER approves a prepared handover. The checker is the AUTHENTICATED caller (resolved from
    the verified identity), and must be a different person than the maker (enforced by the
    Register). ``approved_by`` is only a dev fallback when no OIDC identity is configured."""

    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(max_length=200)


class DocumentReceivedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    reference: str = Field(default="", max_length=500)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")


class DocExpiryMonitorIn(BaseModel):
    """Start (or attach to) the tenant's document-expiry monitor. Overrides default to
    deployment settings when omitted."""

    model_config = ConfigDict(extra="forbid")
    # Additional ops recipients notified on every expiry/warn (each document's uploader
    # is always notified).
    notify: list[str] = Field(default_factory=list)
    interval_hours: float | None = Field(default=None, gt=0, le=24 * 30)
    warn_days: int | None = Field(default=None, ge=0, le=365)


class CovenantMonitorIn(BaseModel):
    """Start (or attach to) the tenant's covenant monitor. Overrides default to
    deployment settings when omitted."""

    model_config = ConfigDict(extra="forbid")
    notify: list[str] = Field(default_factory=list)
    interval_hours: float | None = Field(default=None, gt=0, le=24 * 30)
    horizon_days: int | None = Field(default=None, ge=0, le=400)


class EwsCaseStartIn(BaseModel):
    """Attach a Temporal clock to an EWS case the Register already holds — the case
    record stays the single source of truth; the run keeps it honest against its SLAs."""

    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=64)
    notify: list[str] = Field(default_factory=list)
    assign_sla_hours: float | None = Field(default=None, gt=0, le=24 * 30)
    investigation_sla_hours: float | None = Field(default=None, gt=0, le=24 * 60)
    escalated_reminder_hours: float | None = Field(default=None, gt=0, le=24 * 30)


def _problem(status: int, title: str, detail: str) -> ORJSONResponse:
    return ORJSONResponse(status_code=status, content={"error": {
        "type": title.lower().replace(" ", "_"), "title": title, "detail": detail}})


def _upstream_detail(response: Any) -> str:
    """The register's refusal, in words the person who pushed the button can act on.

    A 422 answers `detail: "One or more fields are invalid."` and puts the useful part in
    `errors[]` — which field, and why. Reading only `detail` turned a precise complaint
    ("Extra inputs are not permitted: industry_type") into a shrug, and cost an afternoon
    of guessing at a screen that just said the client could not be created.
    """
    if not (response.headers.get("content-type", "")).startswith("application/json"):
        return (response.text or "").strip()[:500] or f"HTTP {response.status_code}"
    try:
        err = (response.json() or {}).get("error", {})
    except ValueError:
        return (response.text or "").strip()[:500] or f"HTTP {response.status_code}"
    detail = str(err.get("detail") or err.get("title") or f"HTTP {response.status_code}")
    fields = []
    for item in err.get("errors") or []:
        loc = ".".join(str(p) for p in (item.get("loc") or []) if p != "body")
        msg = item.get("msg") or item.get("type") or ""
        fields.append(f"{loc}: {msg}" if loc else str(msg))
    return f"{detail} ({'; '.join(fields)})" if fields else detail


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        # One Temporal client per process; connecting lazily on first request would hide
        # a bad TEMPORAL_ADDRESS until traffic arrives — fail loud at startup instead.
        app.state.temporal = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace,
            data_converter=build_data_converter(settings.payload_encryption_key))
        app.state.http = httpx.AsyncClient(timeout=10.0)
        app.state.oidc = (
            build_verifier(
                app.state.http, issuer=settings.oidc_issuer,
                audience=settings.oidc_audience or None,
                issuers_spec=settings.oidc_issuers,
                email_claim=settings.oidc_email_claim,
                allowed_domains=settings.oidc_allowed_domains.split(",")))
        log.info("orchestrator_started", extra={"temporal": settings.temporal_address,
                                                "task_queue": settings.task_queue})
        yield
        await app.state.http.aclose()

    app = FastAPI(title="PRISM Orchestrator", version="0.1.0",
                  default_response_class=ORJSONResponse, lifespan=lifespan,
                  docs_url="/docs", openapi_url="/openapi.json")

    def denied(provided: str | None) -> ORJSONResponse | None:
        keys = settings.api_key_list()
        if not keys:
            return None
        if provided and any(hmac.compare_digest(provided, k) for k in keys):
            return None
        return _problem(401, "Unauthorized", "Missing or invalid X-API-Key.")

    async def start(request: Request, workflow_cls: Any, arg: Any,
                    workflow_id: str, *, restart_if_closed: bool = False,
                    memo: dict | None = None) -> WorkflowHandle:
        """Idempotent start: if the business id is already RUNNING, attach to it. When
        ``restart_if_closed`` and the prior run has CLOSED (rejected/timed-out/failed),
        start a fresh attempt under a **URL-safe** ``{id}-r{n}`` suffix so a conversion can be
        retried cleanly without colliding with the terminal history. (``#`` was NOT URL-safe:
        browsers/clients treat everything after it as a fragment, so the generated approval and
        decision-lookup URLs silently dropped the suffix and addressed the wrong workflow.)
        ``memo`` records the initiator + tenant for subject-level status scoping."""
        client: Client = request.app.state.temporal
        try:
            return await client.start_workflow(
                workflow_cls.run, arg, id=workflow_id, task_queue=settings.task_queue,
                memo=memo)
        except TemporalError as exc:
            if "already started" not in str(exc).lower():
                raise
            handle = client.get_workflow_handle(workflow_id)
            if not restart_if_closed:
                return handle
            desc = await handle.describe()
            if desc.status == WorkflowExecutionStatus.RUNNING:
                return handle
            # Prior attempt is terminal → new attempt id (URL-safe suffix).
            n = 2
            while True:
                retry_id = f"{workflow_id}-r{n}"
                try:
                    return await client.start_workflow(
                        workflow_cls.run, arg, id=retry_id,
                        task_queue=settings.task_queue, memo=memo)
                except TemporalError as exc2:
                    if "already started" not in str(exc2).lower():
                        raise
                    h = client.get_workflow_handle(retry_id)
                    if (await h.describe()).status == WorkflowExecutionStatus.RUNNING:
                        return h
                    n += 1

    async def _attempts(client: Client, base_id: str) -> list[tuple[str, Any]]:
        """Every existing attempt for a deterministic business id, oldest first — the
        base id plus its ``-rN`` retries. The retry suffix means "the newest attempt IS
        the run" for any reader; resolving only the base id reads a dead first attempt
        and mis-states the subject's whole workflow state."""
        out: list[tuple[str, Any]] = []
        cur, n = base_id, 2
        while True:
            try:
                desc = await client.get_workflow_handle(cur).describe()
            except (RPCError, TemporalError):
                break
            out.append((cur, desc))
            cur = f"{base_id}-r{n}"
            n += 1
        return out

    async def _live_run_business(client: Client,
                                 base_id: str) -> tuple[str, str] | None:
        """(workflow_id, business_status) of the newest RUNNING attempt — None when no
        attempt is live. The business status comes from the run's own state query;
        a run without one reports ''."""
        for wf_id, desc in reversed(await _attempts(client, base_id)):
            if desc.status == WorkflowExecutionStatus.RUNNING:
                business = ""
                with contextlib.suppress(RPCError, TemporalError):
                    st = await client.get_workflow_handle(wf_id).query("state")
                    if isinstance(st, dict):
                        business = str(st.get("business_status") or "")
                return wf_id, business
        return None

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": "prism-orchestrator"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Any:
        client = request.app.state.temporal
        try:
            await client.service_client.check_health()
        except Exception as exc:  # noqa: BLE001
            return _problem(503, "Not ready", f"Temporal unreachable: {exc}")
        # Server-up is necessary but NOT sufficient: a run started while no WORKER polls
        # the task queue just sits queued, and a ?wait=true caller dies on the timeout —
        # exactly the cold-start window after a stack wipe. Ready means pollers > 0.
        try:
            from temporalio.api.enums.v1 import TaskQueueType
            from temporalio.api.taskqueue.v1 import TaskQueue
            from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
            resp = await client.service_client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=settings.temporal_namespace,
                    task_queue=TaskQueue(name=settings.task_queue),
                    task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW))
            pollers = len(resp.pollers)
        except Exception as exc:  # noqa: BLE001
            return _problem(503, "Not ready", f"Task-queue check failed: {exc}")
        if pollers == 0:
            return _problem(503, "Not ready",
                            f"No worker is polling task queue '{settings.task_queue}' yet.")
        return {"status": "ready", "service": "prism-orchestrator",
                "worker_pollers": pollers}

    @app.post("/v1/workflows/vox-touchpoints", status_code=202, tags=["Workflows"],
              summary="Start (or attach to) a VOX touchpoint workflow")
    async def start_vox(payload: VoxTouchpointIn, request: Request,
                        wait: bool = Query(default=False,
                                           description="Block until the run completes "
                                                       "and return its result"),
                        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                        ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        if not payload.company_name and not payload.entity_id:
            return _problem(422, "Validation failed",
                            "Provide company_name or entity_id.")
        caller, verified = _caller_context(request)
        # FAIL CLOSED: with signing configured, a workflow that will WRITE (create entity /
        # lead / interaction) must carry a verified, route-bound delegated identity — never
        # start it to run under the service key's authority.
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"vox-{_tenant_slug(caller.tenant)}-{payload.capture_id}"
        memo = {"initiator": (caller.email or ""), "tenant": caller.tenant}
        handle = await start(
            request, VoxTouchpointWorkflow,
            VoxTouchpoint(
                caller=caller,
                # Deployment policy, not capture payload: whether ambiguity parks the run.
                require_company_confirmation=settings.vox_confirm_ambiguous_company,
                require_lead_confirmation=settings.vox_confirm_lead_selection,
                confirmation_timeout_hours=settings.vox_confirmation_timeout_hours,
                create_calendar_event=settings.calendar_events_enabled,
                **payload.model_dump()),
            wf_id, memo=memo)
        if wait:
            try:
                result = await handle.result()
            except (RPCError, TemporalError) as exc:
                # Surface the run's OWN failure chain — a bare 500 hides the register
                # refusal / activity error that actually killed the capture.
                chain: list[str] = []
                cur: BaseException | None = exc
                while cur is not None and len(chain) < 4:
                    msg = str(cur).strip() or cur.__class__.__name__
                    if msg not in chain:
                        chain.append(msg)
                    cur = cur.__cause__
                return _problem(502, "Workflow run failed",
                                f"VOX run '{wf_id}' did not complete: "
                                + " <- ".join(chain))
            return ORJSONResponse(status_code=200,
                                  content={"workflow_id": wf_id, "result": result})
        return ORJSONResponse(status_code=202, content={
            "workflow_id": wf_id, "status": "started",
            "status_url": f"/v1/workflows/{wf_id}"})

    def _delegated_headers(request: Request, who: str, caller: CallerContext,
                           method: str, path: str) -> dict[str, str]:
        """Headers that write to the Register AS the verified human — a server-minted,
        route-bound context in production, forwarded identity headers in dev."""
        tenant = caller.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=caller.email or who, user_id=caller.user_id or who,
                roles=list(caller.roles), effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method=method, path=_token_path(path))
        else:
            headers["X-User-Email"] = who
            if caller.user_id:
                headers["X-User-Id"] = caller.user_id
            if caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        return headers

    async def _read_lead(request: Request, caller: CallerContext,
                         lead_id: str) -> tuple[dict, ORJSONResponse | None]:
        """The lead row, or a problem response naming what went wrong."""
        try:
            resp = await request.app.state.http.get(
                f"{settings.register_base_url.rstrip('/')}/v1/leads/{lead_id}",
                headers={"X-API-Key": settings.register_api_key,
                         "X-Tenant": caller.tenant or settings.register_tenant})
        except httpx.HTTPError as exc:
            return {}, _problem(503, "Service unavailable",
                                f"The lead could not be read before starting the run: {exc}")
        if resp.status_code == 404:
            return {}, _problem(404, "Not found", f"Lead '{lead_id}' does not exist.")
        if resp.status_code >= 300:
            return {}, _problem(502, "Upstream error",
                                f"The Register refused to read the lead (HTTP "
                                f"{resp.status_code}).")
        return resp.json(), None

    async def _settle_people(request: Request, caller: CallerContext, lead_row: dict,
                             payload: LeadConversionIn) -> ORJSONResponse | None:
        """Refuse a conversion whose RM or analyst is not a person on the roster — HERE,
        not after somebody has approved it.

        The register validates these names on the convert call, which happens on the far
        side of a human approval. So a lead naming an RM who was never added under People
        was accepted, parked, shown to an approver, approved — and only then failed, with
        the approver holding an error about somebody else's data. The rule is the
        register's own (GET /v1/people/resolve accepts the handle, the full name, the
        e-mail or its local part), asked before the run starts.

        A lookup that cannot RUN is not a refusal: the register being unreachable must
        not block a conversion the roster would have allowed, and the convert call will
        check again anyway.
        """
        base = settings.register_base_url.rstrip("/")
        svc = {"X-API-Key": settings.register_api_key,
               "X-Tenant": caller.tenant or settings.register_tenant}
        for label, name in (("RM", (payload.rm or lead_row.get("rm") or "").strip()),
                            ("analyst", (payload.analyst or "").strip())):
            if not name:
                continue
            try:
                got = await request.app.state.http.get(
                    f"{base}/v1/people/resolve", params={"name": name}, headers=svc)
                if got.status_code != 200:
                    continue                       # cannot check ≠ refuse
                body = got.json() or {}
            except (httpx.HTTPError, ValueError, AttributeError):
                continue
            if body.get("resolved"):
                continue
            candidates = body.get("candidates") or []
            if candidates:
                who = ", ".join(
                    f"{c.get('full_name') or c.get('name')}"
                    + (f" <{c['email']}>" if c.get("email") else "")
                    for c in candidates)
                return _problem(
                    422, "Validation failed",
                    f"The {label} on this lead, '{name}', matches {len(candidates)} "
                    f"people on record ({who}). Set it to the full name or the e-mail "
                    f"address and push it again.")
            return _problem(
                422, "Validation failed",
                f"The {label} on this lead is '{name}', who is not a person on record. "
                f"Add them under People (Employees) — the e-mail matters, it is what "
                f"binds the entry to their sign-in — or set the lead's {label} to "
                f"someone from that list, then push it again. Nothing was converted.")
        return None

    async def _settle_lead_company(request: Request, caller: CallerContext, who: str,
                                   lead_row: dict, payload: LeadConversionIn
                                   ) -> tuple[str, ORJSONResponse | None]:
        """Give an unlinked lead its company, the same way a VOX capture does: match the
        name CANONICALLY against the client master ('Pvt Ltd' == 'Private Limited'), and
        create the client when it is genuinely new — then link it to the lead. Both writes
        run AS THE HUMAN, so creating a client still needs their authority.

        Without a company name anywhere, nothing can be resolved — that is the one case
        the caller must fix, and the message says how."""
        from app.activities import _canonical, _entity_code

        name = ((payload.company_name or lead_row.get("company") or "").strip())
        if not name or name == "(unknown)":
            return "", _problem(
                422, "Validation failed",
                f"Lead '{lead_row.get('lead_no') or payload.lead_id}' has no company "
                "name, and a deal must belong to one. Set the lead's company (or send "
                "company_name with the conversion) and push it again.")
        tenant = caller.tenant or settings.register_tenant
        base = settings.register_base_url.rstrip('/')
        svc = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        # 1. Existing client? Canonical comparison, exactly the VOX matching rules.
        entity_id = ""
        try:
            found = await request.app.state.http.get(
                f"{base}/v1/entities", params={"q": name[:60], "limit": 50}, headers=svc)
            # A search that did not RUN must never be read as "no such client" — that
            # turns one refused read into a duplicate company on the register.
            if found.status_code != 200:
                return "", _problem(
                    503, "Service unavailable",
                    f"The client master could not be searched (HTTP {found.status_code}), "
                    f"so '{name}' cannot be matched against existing clients. Retry once "
                    "the register is reachable.")
            wanted = _canonical(name)
            for row in (found.json() or {}).get("items", []):
                for candidate in (row.get("legal_name"), row.get("display_name")):
                    if candidate and _canonical(candidate) == wanted:
                        entity_id = str(row["id"])
                        break
                if entity_id:
                    break
        except (httpx.HTTPError, KeyError, AttributeError) as exc:
            return "", _problem(503, "Service unavailable",
                                f"The client master could not be searched: {exc}")
        # 2. Genuinely new → create it, as the human (create_client authority applies).
        if not entity_id:
            body = {k: v for k, v in {
                "code": _entity_code(name), "legal_name": name, "display_name": name,
                "sector": payload.sector, "lens": payload.lens, "state": payload.state,
                # `toi` (type of industry) is the register's field name. This said
                # `industry_type`, which EntityCreate forbids, so EVERY genuinely-new
                # company was refused 422 the moment it reached the create branch.
                "toi": payload.industry, "register_status": "Pipeline",
                "notes": payload.about or f"Created when lead "
                                          f"{lead_row.get('lead_no') or ''} was pushed "
                                          "to deals.",
            }.items() if v}
            try:
                created = await request.app.state.http.post(
                    f"{base}/v1/entities", json=body,
                    headers=_delegated_headers(request, who, caller, "POST", "/v1/entities"))
            except httpx.HTTPError as exc:
                return "", _problem(502, "Upstream unavailable",
                                    f"The client could not be created: {exc}")
            if created.status_code >= 300:
                return "", _problem(
                    created.status_code if created.status_code < 500 else 502,
                    "Client could not be created", _upstream_detail(created))
            entity_id = str(created.json()["id"])
        # 3. Link it to the lead so the conversion (and every later read) sees it.
        path = f"/v1/leads/{payload.lead_id}"
        try:
            linked = await request.app.state.http.patch(
                f"{base}{path}", json={"entity_id": entity_id},
                headers=_delegated_headers(request, who, caller, "PATCH", path))
        except httpx.HTTPError as exc:
            return "", _problem(502, "Upstream unavailable",
                                f"The lead could not be linked to its client: {exc}")
        if linked.status_code >= 300:
            return "", _problem(502, "Upstream error",
                                f"The lead could not be linked to its client (HTTP "
                                f"{linked.status_code}).")
        return entity_id, None

    @app.post("/v1/workflows/lead-conversions", status_code=202, tags=["Workflows"],
              summary="Request a lead→deal conversion (waits for approve/reject)")
    async def start_conversion(payload: LeadConversionIn, request: Request,
                               x_api_key: str | None = Header(default=None,
                                                              alias="X-API-Key"),
                               ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        # The requester is bound to the VERIFIED identity when OIDC is on (and mandatory
        # under require_auth) — a conversion can never be requested under a spoofed name.
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        payload = payload.model_copy(update={"requested_by": requested_by})
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "a conversion.")
        # PRE-FLIGHT: a deal MUST belong to a company, and the workflow enforces that a
        # few hundred milliseconds in — which surfaced as "pending approval" followed by
        # a silently FAILED run that reached no approver's queue. Settle the company
        # HERE: link the existing client, or create it (the Push-to-Deals dialog's
        # promise — "one save: client + deal + product rows"), or refuse with the remedy.
        lead_row, err = await _read_lead(request, caller, payload.lead_id)
        if err is not None:
            return err
        if str(lead_row.get("status") or "").lower() == "converted":
            return _problem(
                409, "Conflict",
                f"Lead '{lead_row.get('lead_no') or payload.lead_id}' is already "
                "Converted; it has left the lead register.")
        # ONE live request per lead — checked BEFORE the pre-flight links people or
        # companies. Pushing again while a run was open answered 202 "started" while
        # silently attaching to the old run — a returned request looked simply
        # ignorable, and the maker had no way to learn the real next step.
        live = None
        if (tclient := getattr(request.app.state, "temporal", None)) is not None:
            with contextlib.suppress(Exception):
                live = await _live_run_business(
                    tclient, f"leadconv-{_tenant_slug(caller.tenant)}-{payload.lead_id}")
        if live is not None:
            live_id, business = live
            if business in _RETURNED_STATES or "return" in business.lower():
                return _problem(
                    409, "Conflict",
                    "The approver RETURNED this conversion request — it is still open. "
                    "Amend the lead if needed, then RESUBMIT it (Push to Deals offers "
                    "Resubmit / Withdraw on a returned request). A new request cannot "
                    f"be raised while it is open. [{live_id}]")
            return _problem(
                409, "Conflict",
                "This lead's conversion is already with the approver — the request is "
                "open and awaiting their decision. Withdraw it or wait for the "
                f"decision before raising another. [{live_id}]")
        if (err := await _settle_people(request, caller, lead_row, payload)) is not None:
            return err
        if not lead_row.get("entity_id"):
            entity_id, err = await _settle_lead_company(
                request, caller, requested_by, lead_row, payload)
            if err is not None:
                return err
            log.info("conversion_company_linked",
                     extra={"lead": payload.lead_id, "entity": entity_id})
        wf_id = f"leadconv-{_tenant_slug(caller.tenant)}-{payload.lead_id}"
        # Record the INITIATOR + tenant in the workflow memo so status/result can be scoped
        # to the initiator or an approver — not any same-tenant caller. The real lead_id is
        # carried here too, so a decision NEVER derives it from a (retry-suffixed) workflow id.
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lead_id": payload.lead_id}
        handle = await start(request, LeadConversionWorkflow,
                             LeadConversionInput(
                                 caller=caller,
                                 emit_search_attributes=settings.search_attributes_enabled,
                                 approver_notify=settings.approver_notify_list(),
                                 # The client fields are settled by the pre-flight above
                                 # (link-or-create); the workflow reads the company from
                                 # the lead itself, so they never travel into history.
                                 **payload.model_dump(exclude=_CLIENT_ONLY_FIELDS)),
                             wf_id, restart_if_closed=True, memo=memo)
        wf_id = handle.id  # may be the #n retry id if a prior attempt had closed
        return ORJSONResponse(status_code=202, content={
            "workflow_id": wf_id, "status": "pending approval",
            "approve_url": f"/v1/workflows/{wf_id}/approve",
            "reject_url": f"/v1/workflows/{wf_id}/reject",
            "status_url": f"/v1/workflows/{wf_id}"})

    @app.post("/v1/internal/monitors/document-expiry", status_code=202, tags=["Internal"],
              summary="Start (or attach to) this tenant's document-expiry monitor")
    async def start_doc_expiry_monitor(
            payload: DocExpiryMonitorIn, request: Request,
            x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        """Idempotent per tenant: the monitor's workflow id is ``doc-expiry-{tenant}``,
        so a second start attaches to the run already keeping the clock. Deploy-time
        one-liner (compose/Helm post-start hook) or a manual ops action — either way the
        tenant ends up with exactly one monitor."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        caller, _verified = _caller_context(request)
        wf_id = f"doc-expiry-{_tenant_slug(caller.tenant)}"
        handle = await start(
            request, DocumentExpiryMonitorWorkflow,
            DocumentExpiryInput(
                interval_hours=payload.interval_hours or settings.doc_expiry_interval_hours,
                warn_days=(payload.warn_days if payload.warn_days is not None
                           else settings.doc_expiry_warn_days),
                notify=payload.notify,
                emit_search_attributes=settings.search_attributes_enabled,
                caller=caller),
            wf_id, memo={"initiator": (caller.email or ""), "tenant": caller.tenant})
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "monitoring",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/internal/monitors/document-expiry/stop", status_code=202,
              tags=["Internal"], summary="Stop this tenant's document-expiry monitor")
    async def stop_doc_expiry_monitor(
            request: Request,
            x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        caller, _verified = _caller_context(request)
        wf_id = f"doc-expiry-{_tenant_slug(caller.tenant)}"
        client: Client = request.app.state.temporal
        try:
            await client.get_workflow_handle(wf_id).signal(
                DocumentExpiryMonitorWorkflow.stop)
        except TemporalError as exc:
            return _problem(404, "Not found", f"No monitor for this tenant: {exc}")
        return ORJSONResponse(status_code=202,
                              content={"workflow_id": wf_id, "status": "stopping"})

    @app.post("/v1/internal/monitors/covenants", status_code=202, tags=["Internal"],
              summary="Start (or attach to) this tenant's covenant monitor")
    async def start_covenant_monitor(
            payload: CovenantMonitorIn, request: Request,
            x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        """Idempotent per tenant (workflow id ``cov-monitor-{tenant}``): the recurring
        covenant clock — generate due observations, flag overdue submissions, expire
        lapsed waivers — with one run keeping the whole schedule honest."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        caller, _verified = _caller_context(request)
        wf_id = f"cov-monitor-{_tenant_slug(caller.tenant)}"
        handle = await start(
            request, CovenantMonitorWorkflow,
            CovenantMonitorInput(
                interval_hours=payload.interval_hours or settings.covenant_interval_hours,
                horizon_days=(payload.horizon_days if payload.horizon_days is not None
                              else settings.covenant_horizon_days),
                notify=payload.notify,
                emit_search_attributes=settings.search_attributes_enabled,
                caller=caller),
            wf_id, memo={"initiator": (caller.email or ""), "tenant": caller.tenant})
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "monitoring",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/internal/monitors/covenants/stop", status_code=202,
              tags=["Internal"], summary="Stop this tenant's covenant monitor")
    async def stop_covenant_monitor(
            request: Request,
            x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        caller, _verified = _caller_context(request)
        wf_id = f"cov-monitor-{_tenant_slug(caller.tenant)}"
        client: Client = request.app.state.temporal
        try:
            await client.get_workflow_handle(wf_id).signal(CovenantMonitorWorkflow.stop)
        except TemporalError as exc:
            return _problem(404, "Not found", f"No monitor for this tenant: {exc}")
        return ORJSONResponse(status_code=202,
                              content={"workflow_id": wf_id, "status": "stopping"})

    @app.post("/v1/workflows/ews-cases", status_code=202, tags=["Workflows"],
              summary="Attach the SLA clock to an EWS case (idempotent per case)")
    async def start_ews_case(payload: EwsCaseStartIn, request: Request,
                             x_api_key: str | None = Header(default=None,
                                                            alias="X-API-Key")) -> Any:
        """The Register's case record stays the single source of truth; this run keeps
        it honest against its SLAs (unassigned reminder → auto-escalation → escalated
        re-alerts) and completes when the record closes."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        caller, _verified = _caller_context(request)
        wf_id = f"ews-{_tenant_slug(caller.tenant)}-{payload.case_id}"
        handle = await start(
            request, EwsCaseWorkflow,
            EwsCaseInput(
                case_id=payload.case_id,
                assign_sla_hours=(payload.assign_sla_hours
                                  or settings.ews_assign_sla_hours),
                investigation_sla_hours=(payload.investigation_sla_hours
                                         or settings.ews_investigation_sla_hours),
                escalated_reminder_hours=(payload.escalated_reminder_hours
                                          or settings.ews_escalated_reminder_hours),
                notify=payload.notify,
                emit_search_attributes=settings.search_attributes_enabled,
                caller=caller),
            wf_id, memo={"initiator": (caller.email or ""), "tenant": caller.tenant,
                         "case_id": payload.case_id})
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "watching",
            "sync_url": f"/v1/workflows/{handle.id}/ews-sync",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/{workflow_id}/ews-sync", status_code=202,
              tags=["Workflows"],
              summary="Nudge an EWS case run to re-read its record now")
    async def ews_sync(workflow_id: str, request: Request,
                       x_api_key: str | None = Header(default=None,
                                                      alias="X-API-Key")) -> Any:
        """Call after any register-side case action (assign / escalate / close) so the
        clock reacts immediately instead of on its next deadline. The signal carries
        NOTHING — the run re-reads the durable record, so a forged nudge is harmless."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        if not workflow_id.startswith("ews-"):
            return _problem(404, "Not found",
                            "ews-sync only addresses EWS case runs (ews-…).")
        client: Client = request.app.state.temporal
        try:
            await client.get_workflow_handle(workflow_id).signal(
                EwsCaseWorkflow.case_updated)
        except TemporalError as exc:
            return _problem(404, "Not found", f"No such case run: {exc}")
        return ORJSONResponse(status_code=202,
                              content={"workflow_id": workflow_id, "status": "nudged"})

    def _tenant_slug(tenant: str) -> str:
        """A workflow-id-safe, COLLISION-FREE tenant slug, so a tenant-B run can never
        collide with (or be reached as) a tenant-A run. A readable alnum prefix PLUS a hash
        of the full code disambiguates codes that share an alnum form (``A-B`` vs ``AB``)."""
        t = (tenant or settings.register_tenant).strip()
        alnum = re.sub(r"[^A-Za-z0-9]", "", t) or "T"
        return f"{alnum}{hashlib.sha256(t.encode()).hexdigest()[:10]}"

    def _auth_enforced() -> bool:
        """Production identity posture: any of OIDC / require_auth / signed context on."""
        return bool(settings.oidc_issuer or settings.require_auth
                    or settings.internal_signing_secret)

    def _wf_tenant_denied(request: Request, workflow_id: str) -> ORJSONResponse | None:
        """A workflow may be approved / rejected / read ONLY within its own tenant: the
        request's X-Tenant must reproduce the tenant slug embedded in the business id
        (``{prefix}-{tenantSlug}-{business_id}``). A LEGACY id with no embedded slug FAILS
        CLOSED under the production identity posture (it can't be tenant-verified), and is
        allowed only in dev."""
        parts = workflow_id.split("-", 2)
        if len(parts) < 3:
            if _auth_enforced():
                return _problem(403, "Forbidden",
                                "This workflow id predates tenant binding and cannot be "
                                "tenant-verified; refused.")
            return None
        want = _tenant_slug(request.headers.get("X-Tenant") or settings.register_tenant)
        if parts[1] != want:
            return _problem(403, "Forbidden",
                            "This workflow belongs to a different tenant.")
        return None

    def _caller_context(request: Request,
                        requested_by: str = "") -> tuple[CallerContext, bool]:
        """The TENANT + human identity a workflow acts for, and whether a VERIFIED,
        route-bound delegated identity was present. The tenant comes from the request; the
        identity + live grant come from the gateway's SIGNED context when present, verified
        AND bound to this route + tenant. The ``verified`` flag lets the caller FAIL CLOSED in
        production rather than starting a workflow that would run under the service key."""
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        cc = CallerContext(tenant=tenant, email=requested_by or "")
        raw = request.headers.get("X-Internal-Context")
        if not (settings.internal_signing_secret and raw):
            return cc, False
        from evam_backend_core.internal_token import (
            InternalTokenError,
            verify_internal_context,
        )
        try:
            ic = verify_internal_context(
                raw, verify_key=settings.internal_signing_secret,
                algorithms=(settings.internal_signing_algorithm,))
        except InternalTokenError as exc:
            log.warning("orchestrator_context_verify_failed", extra={"error": str(exc)})
            return cc, False
        # REQUIRE the route binding to be present AND exact: a token with no method/path, or
        # one minted for another route (e.g. VocX's /v1/touchpoints) or another tenant, does
        # NOT delegate. The previous hop MUST mint a token bound to THIS route + tenant.
        bound_ok = (bool(ic.method) and ic.method == request.method
                    and bool(ic.path) and ic.path == request.url.path
                    and bool(ic.tenant) and ic.tenant == tenant)
        if not bound_ok:
            log.warning("orchestrator_context_binding_rejected",
                        extra={"tok_method": ic.method, "tok_path": ic.path,
                               "tok_tenant": ic.tenant, "req_method": request.method,
                               "req_path": request.url.path, "req_tenant": tenant})
            return cc, False
        return CallerContext(
            tenant=ic.tenant, email=ic.email or requested_by or "",
            user_id=ic.user_id, roles=list(ic.roles),
            report_ids=list(ic.report_ids), report_emails=list(ic.report_emails),
            effective_views=ic.effective_views,
            effective_operations=ic.effective_operations, decision=ic.decision), True

    async def _verified_email(request: Request,
                              fallback: str) -> tuple[str, ORJSONResponse | None]:
        """The caller's trustworthy identity. With OIDC configured it is the e-mail from
        the VERIFIED bearer token — never a caller-supplied string. With no OIDC and
        ``require_auth`` on, the request is REFUSED rather than trusting the fallback (so a
        production orchestrator can never approve on an unauthenticated say-so). Only in
        dev (require_auth off, no OIDC) does the supplied ``fallback`` stand in."""
        verifier: TokenVerifier | None = request.app.state.oidc
        if verifier is None:
            if settings.require_auth:
                return "", _problem(
                    401, "Unauthorized",
                    "This orchestrator requires a verified identity; set "
                    "WORKFLOWS_OIDC_ISSUER (or WORKFLOWS_OIDC_ISSUERS for several IdPs).")
            return fallback, None
        token = bearer_token(request.headers.get("Authorization"))
        if not token:
            return "", _problem(401, "Unauthorized", "Bearer token required.")
        try:
            ident = await verifier.verify(token)
        except OidcError as exc:
            return "", _problem(401, "Unauthorized", f"Invalid token: {exc}")
        return ident.email, None

    def _mint_approval(workflow_id: str, decision: str, decided_by: str,
                       approver: CallerContext | None) -> str:
        """A SIGNED, workflow-AND-decision-BOUND approval record. This is the anti-bypass
        primitive: the worker requires (in production) a token minted HERE — bound to THIS
        workflow id AND to THIS decision (``Approved`` / ``Rejected``, carried in the
        immutable ``operation`` claim) — before it will honour the signal. A direct Temporal
        signal cannot forge it (no signing secret), and an approve token cannot be replayed as
        a reject (or vice-versa) because the decision is signed into the token."""
        if not settings.internal_signing_secret:
            return ""
        from evam_backend_core.internal_token import mint_internal_context
        a = approver or CallerContext(tenant=settings.register_tenant, email=decided_by)
        return mint_internal_context(
            signing_key=settings.internal_signing_secret,
            algorithm=settings.internal_signing_algorithm,
            ttl_seconds=max(settings.internal_token_ttl_seconds, 600),
            tenant=a.tenant, email=a.email or decided_by,
            user_id=a.user_id or decided_by, roles=list(a.roles),
            effective_views=a.effective_views,
            effective_operations=a.effective_operations, decision="FULL",
            method="APPROVE", path=f"/approval/{workflow_id}", operation=decision)

    async def _decider(request: Request, workflow_id: str, decision: str, payload: DecisionIn
                       ) -> tuple[str, CallerContext | None, str, ORJSONResponse | None]:
        """The trustworthy decider identity, its resolved approver context, a FRESH role check,
        and a SIGNED approval token bound to this workflow AND this ``decision``
        (Approved/Rejected). The approver's identity + live grant are resolved AT DECISION TIME
        (a role revoked mid-wait is caught now) via Access, scoped to the workflow's tenant.
        The token is the worker's FRESH-path proof; the durable decision record (persisted
        synchronously, below) is the authority when the token has since expired."""
        # Authenticate first (401), THEN authorize the decision to the workflow's tenant (403).
        decided_by, err = await _verified_email(request, payload.by)
        if err is not None:
            return "", None, "", err
        if (denied := _wf_tenant_denied(request, workflow_id)) is not None:
            return "", None, "", denied
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        prefix = workflow_id.split("-", 1)[0]
        needed = _APPROVER_ROLES.get(prefix)
        approver: CallerContext | None = None
        if settings.access_url:
            try:
                resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": decided_by},
                    headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
            except httpx.HTTPError as exc:
                return "", None, "", _problem(502, "Upstream unavailable", f"Access: {exc}")
            body = resp.json() if resp.status_code == 200 else {}
            roles = set(body.get("roles", []))
            # FRESH authority check: a role revoked mid-wait is caught here, now.
            if needed and not (roles & needed):
                return "", None, "", _problem(
                    403, "Forbidden",
                    f"'{decided_by}' lacks an approver role {sorted(needed)} for {prefix}.")
            approver = CallerContext(
                tenant=tenant, email=decided_by, user_id=str(body.get("id") or decided_by),
                roles=list(roles), effective_views=body.get("views", {}),
                effective_operations=body.get("operations", {}), decision="FULL")
        token = _mint_approval(workflow_id, decision, decided_by, approver)
        return decided_by, approver, token, None

    async def _lending_lines_for_deal(request: Request, deal_id: str,
                                      caller: CallerContext | None, who: str) -> list[str]:
        """Ids of the lending lines belonging to ``deal_id``, read as the deciding human.

        Used to record a subject-bound committee decision per line (see the committee-decision
        handler). Never raises: an unreachable Register or an empty list just means no lending
        line is sanctioned by this decision.
        """
        headers = {"X-Tenant": request.headers.get("X-Tenant", settings.register_tenant),
                   "X-API-Key": settings.register_api_key}
        path = "/v1/lending"
        if settings.internal_signing_secret and caller is not None and caller.email:
            from evam_backend_core.internal_token import mint_internal_context

            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=settings.internal_token_ttl_seconds,
                tenant=headers["X-Tenant"], email=caller.email,
                user_id=caller.user_id or caller.email, roles=list(caller.roles),
                report_ids=list(caller.report_ids), report_emails=list(caller.report_emails),
                effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="GET", path=_token_path(path))
        else:
            headers["X-User-Email"] = who
            if caller is not None and caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.get(
                f"{settings.register_base_url.rstrip('/')}{path}",
                params={"deal_id": deal_id, "limit": 50}, headers=headers)
            if rr.status_code >= 300:
                # Silence here means the deal sanctions and its facility does NOT — the exact
                # divergence an operator must be told about, so this is an ERROR, not a warning.
                log.error("lending_lines_lookup_failed",
                          extra={"deal_id": deal_id, "status": rr.status_code,
                                 "impact": "lending line(s) will NOT be sanctioned by this "
                                           "decision; re-send the committee decision once the "
                                           "Register is reachable"})
                return []
            return [str(r["id"]) for r in (rr.json().get("items") or []) if r.get("id")]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.error("lending_lines_lookup_error",
                      extra={"deal_id": deal_id, "error": str(exc),
                             "impact": "lending line(s) will NOT be sanctioned by this decision"})
            return []

    async def _persist_decision(request: Request, workflow_id: str, decision: str,
                                decided_by: str, note: str | None,
                                approver: CallerContext | None, lead_id: str | None,
                                extra: dict | None = None
                                ) -> tuple[dict | None, ORJSONResponse | None]:
        """Record the decision on the dedicated SINGLE-WINNER decision resource SYNCHRONOUSLY —
        BEFORE the API acknowledges and before the signal is delivered — so the outcome is
        durable at ACCEPTANCE time and the FIRST decision atomically wins:

        * the Register enforces one decision per (tenant, workflow_id) with a UNIQUE
          constraint, so a concurrent Approve+Reject can never both persist;
        * replaying the SAME decision returns the original record (idempotent);
        * the OPPOSITE decision returns 409 — surfaced to the caller as 409, and NOT signalled;
        * provenance is set server-side from the delegated approver context we mint here — never
          a client field.

        ``lead_id`` is the REAL lead id (from the workflow memo), never derived from a
        retry-suffixed workflow id. Returns ``(record, error)`` — the record is the AUTHORITATIVE
        one the Register holds (the first approver's, on an idempotent replay), so the caller
        reports the true approver, not the latest caller. In dev (no signing) returns
        ``(None, None)`` and the worker trusts the signal."""
        if not settings.internal_signing_secret:
            return None, None
        tenant = (approver.tenant if approver and approver.tenant
                  else (request.headers.get("X-Tenant") or settings.register_tenant).strip())
        a = approver or CallerContext(tenant=tenant, email=decided_by)
        # Mint a delegated approver context bound to THIS write, so the Register records the
        # decision AS the verified human (server-controlled provenance), scoped to the tenant.
        from evam_backend_core.internal_token import mint_internal_context
        ctx_token = mint_internal_context(
            signing_key=settings.internal_signing_secret,
            algorithm=settings.internal_signing_algorithm,
            ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
            tenant=tenant, email=a.email or decided_by, user_id=a.user_id or decided_by,
            roles=list(a.roles), effective_views=a.effective_views,
            effective_operations=a.effective_operations, decision="FULL",
            method="POST", path="/v1/internal/decisions")
        body = {"workflow_id": workflow_id, "decision": decision, "note": note}
        if lead_id:
            body["lead_id"] = lead_id
        if extra:
            body.update({k: v for k, v in extra.items() if v is not None})
        try:
            resp = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}/v1/internal/decisions",
                json={k: v for k, v in body.items() if v is not None},
                headers={"X-API-Key": settings.register_api_key, "X-Tenant": tenant,
                         "X-Internal-Context": ctx_token})
        except httpx.HTTPError as exc:
            return None, _problem(502, "Upstream unavailable",
                                  f"Could not durably record the decision (Register: {exc}).")
        if resp.status_code == 409:
            return None, _problem(409, "Conflict",
                                  "A different decision has already been recorded for this "
                                  "workflow; it cannot be changed.")
        if resp.status_code >= 300:
            return None, _problem(502, "Upstream error",
                                  f"Register refused the decision record ({resp.status_code}).")
        return resp.json(), None

    async def _has_approver_role(request: Request, workflow_id: str, who: str,
                                 tenant: str) -> bool:
        """Whether ``who`` holds an approver role for this workflow's vertical (via Access)."""
        prefix = workflow_id.split("-", 1)[0]
        needed = _APPROVER_ROLES.get(prefix)
        if not (needed and settings.access_url and who):
            return False
        try:
            resp = await request.app.state.http.get(
                f"{settings.access_url.rstrip('/')}/v1/resolve",
                params={"email": who},
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
        except httpx.HTTPError:
            return False
        roles = set(resp.json().get("roles", [])) if resp.status_code == 200 else set()
        return bool(roles & needed)

    async def _authorised_for(prefix: str, request: Request, who: str, tenant: str) -> bool:
        """Whether ``who`` holds an authorising role for ``prefix`` (fresh via Access when configured,
        else the forwarded roles header in dev). Used to gate a workflow START by authority."""
        needed = _APPROVER_ROLES.get(prefix, set())
        if not needed:
            return False
        if settings.access_url and who:
            try:
                resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": who},
                    headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
            except httpx.HTTPError:
                return False
            roles = set(resp.json().get("roles", [])) if resp.status_code == 200 else set()
            return bool(roles & needed)
        raw = request.headers.get("X-User-Roles") or ""
        return bool({r.strip() for r in raw.split(",") if r.strip()} & needed)

    async def _status_scope_denied(request: Request, workflow_id: str, desc: Any,
                                   who: str) -> ORJSONResponse | None:
        """Only the INITIATOR (workflow memo) or an approver-role holder may read a run."""
        # ``memo`` on a Temporal description is an ASYNC accessor, not a dict — read it via
        # memo_value, or the initiator is never recognised and a legitimate requester is 403'd.
        initiator = str(await desc.memo_value("initiator", "") or "").strip().lower()
        if who and who.strip().lower() == initiator:
            return None
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        if await _has_approver_role(request, workflow_id, who, tenant):
            return None
        return _problem(403, "Forbidden",
                        "You may not read this workflow's status (not its initiator or an "
                        "approver).")

    async def _reconcile_closed(handle: WorkflowHandle, desc: Any, workflow_id: str,
                                decision: str) -> Any:
        """Reconcile a decision against a workflow that is no longer RUNNING.

        Two cases the caller must be able to retry safely:

        * **Already applied (idempotent retry).** The signal was delivered and the run
          COMPLETED with THIS decision's outcome, but the caller lost the response and retried.
          Return the AUTHORITATIVE completed result (200) — never a misleading 409.
        * **Closed without applying this decision.** The run timed out / failed / completed
          with a different outcome. Return 409 with guidance to start a fresh attempt (which
          gets a new ``-r2`` id); the persisted decision row remains for reconciliation."""
        status = desc.status
        if status == WorkflowExecutionStatus.COMPLETED:
            try:
                result = await handle.result()
            except (RPCError, TemporalError):
                result = None
            if isinstance(result, dict) and result.get("status") == decision:
                return ORJSONResponse(status_code=200, content={
                    "workflow_id": workflow_id, "status": "already_applied",
                    "result": result})
            return _problem(409, "Conflict",
                            f"Workflow '{workflow_id}' already completed with a different "
                            "outcome; start a new attempt.")
        return _problem(409, "Conflict",
                        f"Workflow '{workflow_id}' is closed "
                        f"({status.name if status else 'UNKNOWN'}) and did not apply this "
                        "decision; start a new attempt.")

    async def _decide(request: Request, workflow_id: str, name: str, decision: str,
                      payload: DecisionIn) -> Any:
        """Shared approve/reject path — authenticate + fresh-authorize, confirm the workflow is
        RUNNING, DURABLY record the decision, then deliver it — and RECONCILE idempotently
        against a closed workflow so retries are safe:

        * The RUNNING check before persistence guards against writing a decision for a
          nonexistent / already-closed workflow (which a later run reusing the id could
          consume).
        * If the run closes in the tiny window between that check and the signal, or a prior
          delivery already applied it and the caller is retrying, ``_reconcile_closed`` returns
          the authoritative applied result (or a clear 409) instead of a misleading error."""
        decided_by, approver, token, err = await _decider(
            request, workflow_id, decision, payload)
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        # Not pending → reconcile (idempotent retry of an applied decision, or a clear 409).
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return await _reconcile_closed(handle, desc, workflow_id, decision)
        # The REAL lead id comes from the memo (an ASYNC accessor, not a dict), never a
        # retry-suffixed workflow id.
        lead_id = await desc.memo_value("lead_id", None)
        record, perr = await _persist_decision(
            request, workflow_id, decision, decided_by, payload.note, approver, lead_id)
        if perr is not None:
            return perr   # not acknowledged unless durably recorded first
        # Report the AUTHORITATIVE approver from the persisted record (the FIRST approver on an
        # idempotent replay) — not the latest caller — so the API response matches the run + DB.
        authoritative_by = (record.get("decided_by") if record else "") or decided_by
        decision_ref = str(record.get("id") or "") if record else ""
        try:
            await handle.signal(
                name, args=[authoritative_by, payload.note, token, decision_ref])
        except RPCError:
            # A signal RPCError may mean the run CLOSED — or just a TRANSIENT Temporal/network
            # blip while the run is still fine. Do NOT guess from the error code: re-describe
            # and act on the ACTUAL state. Still RUNNING → the decision is persisted but
            # undelivered → 503 "retry delivery" (a retry re-signals safely; the run ignores a
            # duplicate once decided). Closed → reconcile (already-applied vs a real conflict).
            try:
                desc2 = await handle.describe()
            except (RPCError, TemporalError):
                return _problem(503, "Delivery failed",
                                "Decision persisted; Temporal is unreachable — retry delivery.")
            if desc2.status == WorkflowExecutionStatus.RUNNING:
                return _problem(503, "Delivery failed",
                                "Decision persisted but signal delivery failed transiently; "
                                "retry delivery.")
            return await _reconcile_closed(handle, desc2, workflow_id, decision)
        out: dict[str, Any] = {"workflow_id": workflow_id, "signalled": name,
                               "by": authoritative_by}
        if record:
            out["decision"] = record.get("decision")
            out["note"] = record.get("note")
        return out

    @app.post("/v1/workflows/{workflow_id}/approve", tags=["Workflows"],
              summary="Approve a pending human-in-the-loop workflow")
    async def approve(workflow_id: str, payload: DecisionIn, request: Request,
                      x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                      ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide(request, workflow_id, "approve", "Approved", payload)

    @app.post("/v1/workflows/{workflow_id}/reject", tags=["Workflows"],
              summary="Reject a pending human-in-the-loop workflow")
    async def reject(workflow_id: str, payload: DecisionIn, request: Request,
                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide(request, workflow_id, "reject", "Rejected", payload)

    async def _start_business(request: Request, x_api_key: str | None, requested_by_raw: str,
                              workflow_cls: Any, arg_cls: Any, id_prefix: str, id_suffix: str,
                              extra_memo: dict, extra_content: dict | None = None,
                              **arg_fields: Any) -> Any:
        """Shared start path for the business-lifecycle workflows (qualification / structuring /
        document collection): API-key gate → verified initiator → fail-closed delegated identity →
        idempotent start under a tenant-bound business id."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, requested_by_raw)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"{id_prefix}-{_tenant_slug(caller.tenant)}-{id_suffix}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                **extra_memo}
        import dataclasses as _dc
        field_names = {f.name for f in _dc.fields(arg_cls)}
        if "emit_search_attributes" in field_names:
            arg_fields.setdefault("emit_search_attributes",
                                  settings.search_attributes_enabled)
        if "approver_notify" in field_names:
            arg_fields.setdefault("approver_notify", settings.approver_notify_list())
        handle = await start(request, workflow_cls, arg_cls(caller=caller, **arg_fields), wf_id,
                             restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "started",
            "status_url": f"/v1/workflows/{handle.id}",
            **(extra_content or {})})

    @app.post("/v1/workflows/lead-qualifications", status_code=202, tags=["Workflows"],
              summary="Start a lead-qualification workflow")
    async def start_qualification(payload: LeadQualificationIn, request: Request,
                                  x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                  ) -> Any:
        merged, err = _merged_checklist(payload.checklist)
        if err is not None:
            return err
        return await _start_business(
            request, x_api_key, payload.qualified_by, LeadQualificationWorkflow,
            LeadQualificationInput, "qual", payload.lead_id, {"lead_id": payload.lead_id},
            lead_id=payload.lead_id, qualified_by=payload.qualified_by,
            qualification_reference=payload.qualification_reference,
            qualification_sha256=payload.qualification_sha256, passed=payload.passed,
            reason=payload.reason, checklist=merged)

    def _merged_checklist(results: list[ChecklistItemIn] | None
                          ) -> tuple[list, ORJSONResponse | None]:
        """The deployment's checklist DEFINITIONS (config) merged with the caller's per-item
        RESULTS. Config-less deployments keep the legacy passed flag (results are refused —
        they would assert against nothing); a configured deployment REQUIRES results for
        every defined item, and unknown keys are refused."""
        import json as _json

        if not settings.qualification_checklist:
            if results:
                return [], _problem(
                    422, "Validation failed",
                    "This deployment has no qualification checklist configured "
                    "(WORKFLOWS_QUALIFICATION_CHECKLIST) — send the plain 'passed' flag.")
            return [], None
        try:
            definitions = _json.loads(settings.qualification_checklist)
            assert isinstance(definitions, list) and definitions
        except (ValueError, AssertionError):
            return [], _problem(500, "Misconfigured",
                                "WORKFLOWS_QUALIFICATION_CHECKLIST is not a JSON list.")
        by_key = {r.key: r for r in (results or [])}
        unknown = sorted(set(by_key) - {str(d.get("key")) for d in definitions})
        if unknown:
            return [], _problem(422, "Validation failed",
                                f"Unknown checklist keys: {', '.join(unknown)}.")
        missing = [str(d.get("key")) for d in definitions if str(d.get("key")) not in by_key]
        if missing:
            return [], _problem(
                422, "Validation failed",
                f"The configured checklist requires a result for every item; "
                f"missing: {', '.join(missing)}.")
        merged = []
        for d in definitions:
            r = by_key[str(d.get("key"))]
            merged.append({"key": str(d.get("key")), "label": d.get("label"),
                           "required": bool(d.get("required", True)),
                           "passed": r.passed, "note": r.note})
        return merged, None

    def _series_stem(name: str) -> str:
        """'EcoSoch Solar Pvt. Ltd' → 'ECOSOCHSOLAR' — the company chunk of an
        auto-numbered reference, alphanumeric and bounded so the number stays typeable."""
        return re.sub(r"[^A-Z0-9]", "", str(name).upper())[:12] or "COMPANY"

    async def _mint_credit_note_ref(request: Request,
                                    deal_id: str) -> tuple[str, Any | None]:
        """The next credit-note number for this deal's company —
        ``CN/<company>/<yyyymm>-<seq>`` from the register's number series (atomic, so
        concurrent sends never draw the same number). Fail CLOSED: sending would put an
        unnumbered note before the committee, so a register that cannot mint refuses
        the send rather than letting one through without a reference."""
        from datetime import UTC, datetime

        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        base = settings.register_base_url.rstrip("/")
        svc = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        try:
            deal = await request.app.state.http.get(f"{base}/v1/deals/{deal_id}",
                                                    headers=svc)
        except httpx.HTTPError as exc:
            return "", _problem(503, "Service unavailable",
                                "The deal could not be read to number the credit note: "
                                f"{exc}")
        if deal.status_code != 200:
            return "", _problem(404 if deal.status_code == 404 else 502,
                                "Deal lookup failed",
                                f"Deal '{deal_id}' could not be read "
                                f"(HTTP {deal.status_code}) — the credit note cannot "
                                "be numbered without its company.")
        stem = _series_stem(deal.json().get("company") or "")
        yyyymm = datetime.now(UTC).strftime("%Y%m")
        try:
            mint = await request.app.state.http.post(
                f"{base}/v1/internal/number-series/next",
                json={"series_key": f"credit-note/{stem}/{yyyymm}"}, headers=svc)
        except httpx.HTTPError as exc:
            return "", _problem(503, "Service unavailable",
                                f"The credit-note number could not be minted: {exc}")
        if mint.status_code != 200:
            return "", _problem(502, "Numbering failed",
                                "The register refused to mint the credit-note number "
                                f"(HTTP {mint.status_code}).")
        return f"CN/{stem}/{yyyymm}-{int(mint.json()['value']):02d}", None

    @app.post("/v1/workflows/deal-structurings", status_code=202, tags=["Workflows"],
              summary="Start a deal-structuring workflow (awaits the committee decision)")
    async def start_structuring(payload: DealStructuringIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        _, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        # ONE live committee request per deal — checked BEFORE any number is minted, so
        # a refused send never burns a series number. Without this, a second send while
        # a run was open (returned or not) answered 202 "started" while silently
        # attaching to the old run, which never learned the new reference.
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        live = None
        if (tclient := getattr(request.app.state, "temporal", None)) is not None:
            with contextlib.suppress(Exception):
                live = await _live_run_business(
                    tclient, f"struct-{_tenant_slug(tenant)}-{payload.deal_id}")
        if live is not None:
            live_id, business = live
            if business in _RETURNED_STATES or "return" in business.lower():
                return _problem(
                    409, "Conflict",
                    "The committee RETURNED this request to you — it is still open. "
                    "Amend it from the deal drawer ('File a revised credit note'), then "
                    "'Send back for decision'. A new request cannot be raised while it "
                    f"is open. [{live_id}]")
            return _problem(
                409, "Conflict",
                "This deal is already with the credit committee — the request is open "
                "and awaiting their decision. Withdraw it (cancel) or wait for the "
                f"decision before raising another. [{live_id}]")
        # The credit note is NUMBERED FROM A SERIES, not typed: a blank reference draws
        # the next number for this company from the register. A typed one is an explicit
        # override and is used as-is. Minting sits behind the SAME gates the start does
        # (API key + verified identity), so an unauthenticated probe can never burn
        # series numbers — the number is drawn only for a send that will actually start.
        reference = (payload.credit_note_reference or "").strip()
        if not reference:
            reference, err = await _mint_credit_note_ref(request, payload.deal_id)
            if err is not None:
                return err
        return await _start_business(
            request, x_api_key, payload.requested_by, DealStructuringWorkflow,
            DealStructuringInput, "struct", payload.deal_id,
            {"deal_id": payload.deal_id, "subject_type": "Deal"},
            extra_content={"credit_note_reference": reference},
            deal_id=payload.deal_id, requested_by=payload.requested_by,
            product_type=payload.product_type, rm=payload.rm,
            credit_note_reference=reference,
            decision_timeout_hours=payload.decision_timeout_hours)

    @app.post("/v1/workflows/document-collections", status_code=202, tags=["Workflows"],
              summary="Start a document-collection workflow (awaits document signals)")
    async def start_documents(payload: DocumentCollectionIn, request: Request,
                              x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                              ) -> Any:
        return await _start_business(
            request, x_api_key, payload.requested_by, DocumentCollectionWorkflow,
            DocumentCollectionInput, "docs", payload.subject_id,
            {"subject_id": payload.subject_id, "subject_type": payload.subject_type},
            subject_type=payload.subject_type, subject_id=payload.subject_id,
            requested_by=payload.requested_by, required_documents=payload.required_documents,
            collection_timeout_hours=payload.collection_timeout_hours)

    @app.post("/v1/workflows/advaya-handover", status_code=202, tags=["Workflows"],
              summary="MAKER prepares the Advaya handover (senior credit authority)")
    async def start_advaya_handover(payload: AdvayaHandoverIn, request: Request,
                                    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                    ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        # AUTHORITY — handing over to Advaya is a money-movement authorization (Credit Head /
        # Management / Admin), checked fresh via Access (or the forwarded roles in dev).
        if not await _authorised_for("handover", request, requested_by, caller.tenant):
            return _problem(403, "Forbidden",
                            "Handing a facility over to Advaya requires Credit Head / Management / "
                            "Admin authority.")
        # The package carries the CONDITIONS STILL OPEN at handover — the CS items (and
        # CPs deferred as CS) the analyst keeps chasing while the money moves. Advaya
        # sees exactly what was outstanding; the frozen snapshot proves it later.
        note = payload.note
        try:
            cl = await request.app.state.http.get(
                f"{settings.register_base_url.rstrip('/')}/v1/internal/cpcs-checklists",
                params={"lending_id": payload.lending_id, "limit": 50},
                headers={"X-API-Key": settings.register_api_key, "X-Tenant": caller.tenant})
            rows = cl.json() if cl.status_code == 200 else []
            rows = rows.get("items", rows) if isinstance(rows, dict) else rows
            approved = [x for x in rows if x.get("status") == "Approved"]
            latest = max(approved, key=lambda x: x.get("checklist_version") or 0,
                         default=None)
            open_conditions = [
                f"{i.get('label') or i.get('key')} — {i.get('status') or 'Pending'}"
                for i in ((latest or {}).get("items") or [])
                if str(i.get("status") or "Pending") not in ("Completed", "Waived")]
            if open_conditions:
                note = (((note + "\n\n") if note else "")
                        + "Conditions outstanding at handover (subsequent / deferred — "
                          "being chased in parallel):\n- " + "\n- ".join(open_conditions))
        except (httpx.HTTPError, AttributeError, ValueError):
            pass   # the handover proceeds; the conditions ride on best-effort
        wf_id = f"handover-{_tenant_slug(caller.tenant)}-{payload.lending_id}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lending_id": payload.lending_id}
        handle = await start(
            request, AdvayaHandoffWorkflow,
            AdvayaHandoffInput(
                caller=caller, lending_id=payload.lending_id, requested_by=requested_by,
                executed_document_refs=[d.model_dump() for d in payload.executed_document_refs],
                cpcs_checklist_version=payload.cpcs_checklist_version,
                delivery_method=payload.delivery_method, recipient=payload.recipient,
                note=note,
                approver_notify=settings.approver_notify_list()),
            wf_id, restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "prepared",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/disburse", tags=["Workflows"],
              summary="DISBURSE: send the disbursement request, unmet CPs riding along")
    async def disburse(payload: DisburseIn, request: Request,
                       x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                       ) -> Any:
        """The desk's single verb. Stages the line to 'Ready for Disbursement' if the CP
        approval just landed, prepares the request package with every CP condition NOT
        met spelled out in its note, and marks it SENT — a recorded intent, generic over
        the partner (Advaya today). The partner's answers come back through
        'Disbursement Update', phase by phase."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        who, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, who)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required.")
        lid = payload.lending_id
        line, lerr = await _register_get_as(request, f"/v1/lending/{lid}", who, caller)
        if lerr is not None:
            return lerr
        stage = str(line.get("stage") or "")
        if stage not in ("CP/CS Completed", "Ready for Disbursement"):
            return _problem(409, "Conflict",
                            f"The line is {stage!r} — disbursement follows the CP "
                            "checklist approval.")
        # Stage + proposed figures: fold "Move to Ready for Disbursement" into the verb.
        amount = payload.proposed_amount or line.get("proposed_disbursement_amount")
        date_ = payload.proposed_date or line.get("proposed_disbursement_date")
        if stage == "CP/CS Completed":
            if not amount or not date_:
                return _problem(422, "Validation failed",
                                "Enter the proposed drawdown amount and date — they "
                                "travel with the disbursement request.")
            moved = await _register_patch_as(
                request, f"/v1/lending/{lid}", who, caller,
                {"stage": "Ready for Disbursement",
                 "proposed_disbursement_amount": amount,
                 "proposed_disbursement_date": str(date_)})
            if not (isinstance(moved, ORJSONResponse) and moved.status_code == 200):
                return moved
        # Every CP condition NOT met rides with the request — the partner sees exactly
        # what is outstanding, and the frozen package snapshot proves it later.
        unmet: list[str] = []
        try:
            cl, _cerr = await _register_get_as(
                request, f"/v1/internal/cpcs-checklists?lending_id={lid}&limit=50",
                who, caller)
            rows = cl.get("items", cl) if isinstance(cl, dict) else cl
            approved_rows = [x for x in (rows or []) if x.get("status") == "Approved"]
            latest = max(approved_rows, key=lambda x: x.get("checklist_version") or 0,
                         default=None)
            unmet = [
                f"{i.get('label') or i.get('key')} — {i.get('status') or 'Pending'}"
                for i in ((latest or {}).get("items") or [])
                if str(i.get("condition_type")) == "CP"
                and str(i.get("status") or "Pending") != "Completed"]
        except (AttributeError, ValueError, TypeError):
            pass
        note = ((payload.note + "\n\n") if payload.note else "")
        if unmet:
            note += ("Conditions Precedent NOT met at disbursement request "
                     "(waived / deferred / outstanding):\n- " + "\n- ".join(unmet))
        prep = await _register_post_as(
            request, "/v1/internal/handover-packages", who, caller,
            {"lending_id": lid, "recipient": payload.recipient,
             "delivery_method": "Portal",
             **({"note": note} if note.strip() else {})})
        prep_failed = not (isinstance(prep, ORJSONResponse) and prep.status_code == 200)
        if prep_failed:
            # A package may already exist (a resend after a rejection) — sending is
            # still the intent, so fall through to submit; any other failure surfaces.
            body_txt = bytes(getattr(prep, "body", b"") or b"").decode("utf-8", "ignore")
            if "already" not in body_txt.lower():
                return prep
        sent = await _register_post_as(
            request, f"/v1/internal/handover-packages/{lid}/submit", who, caller, {})
        if not (isinstance(sent, ORJSONResponse) and sent.status_code == 200):
            return sent
        pkg = orjson.loads(sent.body)
        return ORJSONResponse(status_code=200, content={
            "status": "Requested", "lending_id": lid,
            "sent_to": payload.recipient,
            "unmet_conditions": unmet,
            "package_sha256": pkg.get("package_sha256"),
            "next": "Await the partner's manual confirmation, then record each phase "
                    "with 'Disbursement Update' — the first disbursed tranche opens "
                    "the loan account."})

    @app.post("/v1/workflows/advaya-handover/{lending_id}/approve", tags=["Workflows"],
              summary="CHECKER approves a prepared Advaya handover (different person)")
    async def approve_advaya_handover(lending_id: str, payload: AdvayaHandoverApproveIn,
                                      request: Request,
                                      x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                      ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        approved_by, err = await _verified_email(request, payload.approved_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, approved_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to approve.")
        if not await _authorised_for("handover", request, approved_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Approving an Advaya handover requires Credit Head / Management / "
                            "Admin authority.")
        # Approve at the Register, AS the verified checker (server-minted delegated context). The
        # Register enforces checker != maker and advances the stage transactionally.
        tenant = checker.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            path = f"/v1/internal/handover-packages/{lending_id}/approve"
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=checker.email or approved_by,
                user_id=checker.user_id or approved_by, roles=list(checker.roles),
                effective_views=checker.effective_views,
                effective_operations=checker.effective_operations, decision="FULL",
                method="POST", path=_token_path(path))
        else:
            headers["X-User-Email"] = approved_by
            if checker.user_id:
                headers["X-User-Id"] = checker.user_id
            if checker.roles:
                headers["X-User-Roles"] = ",".join(checker.roles)
        try:
            reg_resp = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}"
                f"/v1/internal/handover-packages/{lending_id}/approve",
                json={}, headers=headers)
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable",
                            f"Could not approve the handover (Register: {exc}).")
        if reg_resp.status_code >= 300:
            ct = reg_resp.headers.get("content-type", "")
            detail = (reg_resp.json().get("error", {}).get("detail")
                      if ct.startswith("application/json") else reg_resp.text)
            return _problem(reg_resp.status_code if reg_resp.status_code < 500 else 502,
                            "Handover approval refused", str(detail))
        return ORJSONResponse(status_code=200, content=reg_resp.json())

    async def _register_get_as(request: Request, path: str, who: str,
                               caller: CallerContext) -> tuple[dict, Any]:
        """GET from the Register AS the verified human, so the register's own row scope
        decides what this caller may see. Returns (row, None) or ({}, _problem())."""
        tenant = caller.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=caller.email or who, user_id=caller.user_id or who,
                roles=list(caller.roles), effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="GET", path=_token_path(path))
        else:
            headers["X-User-Email"] = who
            if caller.user_id:
                headers["X-User-Id"] = caller.user_id
            if caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.get(
                f"{settings.register_base_url.rstrip('/')}{path}", headers=headers)
        except httpx.HTTPError as exc:
            return {}, _problem(502, "Upstream unavailable", f"Register unreachable: {exc}")
        if rr.status_code == 404:
            return {}, _problem(404, "Not found", f"No such record: {path}.")
        if rr.status_code >= 300:
            return {}, _problem(rr.status_code if rr.status_code < 500 else 502,
                                "Register refused the read", _upstream_detail(rr))
        return (rr.json() or {}), None

    async def _register_post_as(request: Request, path: str, who: str, caller: CallerContext,
                                body: dict) -> Any:
        """POST to the Register AS the verified human (server-minted delegated context in prod, or
        forwarded identity headers in dev). Returns the passthrough response or a _problem()."""
        tenant = caller.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=caller.email or who, user_id=caller.user_id or who,
                roles=list(caller.roles), effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="POST", path=_token_path(path))
        else:
            headers["X-User-Email"] = who
            if caller.user_id:
                headers["X-User-Id"] = caller.user_id
            if caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable", f"Register unreachable: {exc}")
        if rr.status_code >= 300:
            ct = rr.headers.get("content-type", "")
            detail = (rr.json().get("error", {}).get("detail")
                      if ct.startswith("application/json") else rr.text)
            return _problem(rr.status_code if rr.status_code < 500 else 502,
                            "Register refused the request", str(detail))
        return ORJSONResponse(status_code=200, content=rr.json())

    async def _register_patch_as(request: Request, path: str, who: str,
                                 caller: CallerContext, body: dict) -> Any:
        """PATCH to the Register AS the verified human — same delegation as the POST
        twin. Used for the stage auto-move a decision implies."""
        tenant = caller.tenant or (request.headers.get("X-Tenant") or settings.register_tenant)
        headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
        if settings.internal_signing_secret:
            from evam_backend_core.internal_token import mint_internal_context
            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=max(settings.internal_token_ttl_seconds, 120),
                tenant=tenant, email=caller.email or who, user_id=caller.user_id or who,
                roles=list(caller.roles), effective_views=caller.effective_views,
                effective_operations=caller.effective_operations, decision="FULL",
                method="PATCH", path=_token_path(path))
        else:
            headers["X-User-Email"] = who
            if caller.user_id:
                headers["X-User-Id"] = caller.user_id
            if caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        try:
            rr = await request.app.state.http.patch(
                f"{settings.register_base_url.rstrip('/')}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable", f"Register unreachable: {exc}")
        if rr.status_code >= 300:
            ct = rr.headers.get("content-type", "")
            detail = (rr.json().get("error", {}).get("detail")
                      if ct.startswith("application/json") else rr.text)
            return _problem(rr.status_code if rr.status_code < 500 else 502,
                            "Register refused the request", str(detail))
        return ORJSONResponse(status_code=200, content=rr.json())


    @app.post("/v1/workflows/advaya-handover/{lending_id}/reject", tags=["Workflows"],
              summary="CHECKER rejects the prepared handover (terminal for this attempt)")
    async def reject_advaya_handover(lending_id: str, payload: CheckerRejectIn, request: Request,
                                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        rejected_by, err = await _verified_email(request, payload.rejected_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, rejected_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to reject.")
        if not await _authorised_for("handover", request, rejected_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Rejecting a handover requires Credit Head / Management / Admin "
                            "authority.")
        return await _register_post_as(
            request, f"/v1/internal/handover-packages/{lending_id}/reject", rejected_by,
            checker, {"note": payload.note})

    @app.post("/v1/workflows/advaya-handover/{lending_id}/return", tags=["Workflows"],
              summary="CHECKER returns the prepared handover to its maker (amend and re-prepare)")
    async def return_advaya_handover(lending_id: str, payload: CheckerReturnIn, request: Request,
                                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                     ) -> Any:
        """NON-terminal: the line does NOT move (it stays at 'Ready for Disbursement');
        the maker re-prepares the package — same row, fresh manifest + digest — and a
        different checker approves the rebuilt one. Reasons mandatory and audited."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        returned_by, err = await _verified_email(request, payload.returned_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, returned_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to return.")
        if not await _authorised_for("handover", request, returned_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Returning a handover requires Credit Head / Management / Admin "
                            "authority.")
        return await _register_post_as(
            request, f"/v1/internal/handover-packages/{lending_id}/return", returned_by,
            checker, {"note": payload.note})

    @app.post("/v1/workflows/cpcs-checklists", status_code=202, tags=["Workflows"],
              summary="MAKER prepares the CP/CS checklist")
    async def start_cpcs_checklist(payload: CpcsChecklistIn, request: Request,
                                   x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                   ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        requested_by, err = await _verified_email(request, payload.requested_by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, requested_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to start "
                            "this workflow.")
        wf_id = f"cpcs-{_tenant_slug(caller.tenant)}-{payload.lending_id}-v{payload.checklist_version}"
        memo = {"initiator": (caller.email or requested_by or ""), "tenant": caller.tenant,
                "lending_id": payload.lending_id}
        handle = await start(
            request, CpcsChecklistWorkflow,
            CpcsChecklistInput(
                caller=caller, lending_id=payload.lending_id, requested_by=requested_by,
                items=[i.model_dump(mode="json") for i in payload.items], deal_id=payload.deal_id,
                checklist_version=payload.checklist_version, note=payload.note,
                approver_notify=settings.approver_notify_list()),
            wf_id, restart_if_closed=True, memo=memo)
        return ORJSONResponse(status_code=202, content={
            "workflow_id": handle.id, "status": "prepared",
            "status_url": f"/v1/workflows/{handle.id}"})

    @app.post("/v1/workflows/cpcs-checklists/{checklist_id}/approve", tags=["Workflows"],
              summary="CHECKER approves the CP/CS checklist (different person, senior authority)")
    async def approve_cpcs_checklist(checklist_id: str, payload: CpcsApproveIn, request: Request,
                                     x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                     ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        approved_by, err = await _verified_email(request, payload.approved_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, approved_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to approve.")
        if not await _authorised_for("cpcs", request, approved_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Approving a CP/CS checklist requires Credit Head / Management / Admin "
                            "authority.")
        approved = await _register_post_as(
            request, f"/v1/internal/cpcs-checklists/{checklist_id}/approve", approved_by,
            checker, {"note": payload.note} if payload.note else {})
        if approved.status_code >= 300:
            return approved

        # MINT cp_cs_completion from the approval itself.
        #
        # The evidence is DERIVED from the approved checklist — the register verifies the
        # citation and refuses one that is not backed by an approved, four-eyes checklist —
        # so there is nothing for a human to type, and asking them to file it separately is
        # how an approved checklist sat there with the line still 'Sanctioned' and no sign
        # of what was missing. Approving is the act; the evidence is its record.
        row = orjson.loads(approved.body)
        lending_id = str(row.get("lending_id") or "")
        if lending_id:
            ev = await _register_post_as(
                request, "/v1/evidence", approved_by, checker,
                {"subject_type": "Lending", "subject_id": lending_id,
                 "evidence_kind": "cp_cs_completion",
                 "reference": f"CPCS/{row.get('checklist_version') or 1}/{checklist_id}",
                 "decision_ref": checklist_id,
                 "note": "Minted on approval of the CP/CS checklist."})
            if ev.status_code >= 300:
                # The checklist IS approved; only the evidence failed. Say both, so nobody
                # re-approves looking for a different outcome.
                detail = orjson.loads(ev.body).get("error", {}).get("detail", "")
                log.error("cpcs_evidence_not_minted",
                          extra={"checklist": checklist_id, "lending": lending_id,
                                 "detail": detail})
                row["cp_cs_completion"] = None
                row["warning"] = (
                    "The checklist is approved, but the cp_cs_completion evidence could not "
                    f"be filed ({detail}). The line cannot reach 'CP/CS Completed' until it "
                    "is.")
                return ORJSONResponse(status_code=200, content=row)
            row["cp_cs_completion"] = orjson.loads(ev.body).get("id")
            # The approval IS the milestone: move the line to 'CP/CS Completed' here,
            # as the approver, instead of leaving a dropdown for someone to discover.
            # Best-effort — a failed move leaves the fallback action and says so.
            moved = await _register_patch_as(
                request, f"/v1/lending/{lending_id}", approved_by, checker,
                {"stage": "CP/CS Completed"})
            if isinstance(moved, ORJSONResponse) and moved.status_code == 200:
                row["stage"] = "CP/CS Completed"
                row["next"] = ("The line is at 'CP/CS Completed' — work the Conditions "
                               "Subsequent and prepare the disbursement request to "
                               "Advaya (unmet CPs travel with it).")
            else:
                log.warning("cpcs_stage_automove_failed",
                            extra={"checklist": checklist_id, "lending": lending_id})
                row["next"] = ("Approved and evidenced, but the stage could not be moved "
                               "automatically — use 'Move to CP/CS Completed'.")
        return ORJSONResponse(status_code=200, content=row)


    @app.post("/v1/workflows/cpcs-checklists/{checklist_id}/reject", tags=["Workflows"],
              summary="CHECKER rejects the CP/CS checklist (terminal — the loop breaks)")
    async def reject_cpcs_checklist(checklist_id: str, payload: CheckerRejectIn, request: Request,
                                    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                    ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        rejected_by, err = await _verified_email(request, payload.rejected_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, rejected_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to reject.")
        if not await _authorised_for("cpcs", request, rejected_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Rejecting a CP/CS checklist requires Credit Head / Management / "
                            "Admin authority.")
        return await _register_post_as(
            request, f"/v1/internal/cpcs-checklists/{checklist_id}/reject", rejected_by,
            checker, {"note": payload.note})

    @app.post("/v1/workflows/cpcs-checklists/{checklist_id}/return", tags=["Workflows"],
              summary="CHECKER returns the CP/CS checklist to its maker (amend and resubmit)")
    async def return_cpcs_checklist(checklist_id: str, payload: CheckerReturnIn, request: Request,
                                    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                    ) -> Any:
        """The middle verb of the triad: NON-terminal. The row becomes 'Returned' with the
        reasons on record; the maker amends by preparing the NEXT checklist_version, which
        re-enters the checker queue. Same authority as approve/reject — through the SAME
        door, so a UI never needs the register's internal lane."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        returned_by, err = await _verified_email(request, payload.returned_by)
        if err is not None:
            return err
        checker, verified = _caller_context(request, returned_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound checker identity is required to return.")
        if not await _authorised_for("cpcs", request, returned_by, checker.tenant):
            return _problem(403, "Forbidden",
                            "Returning a CP/CS checklist requires Credit Head / Management / "
                            "Admin authority.")
        return await _register_post_as(
            request, f"/v1/internal/cpcs-checklists/{checklist_id}/return", returned_by,
            checker, {"note": payload.note})

    # ------------------------------------------------------------------ #
    # CAM decisions — the committee's triad on a SUBMITTED CAM, through the
    # same door as the CP/CS checker verbs (a UI never needs the register's
    # internal lane). The register enforces four-eyes and records who decided.
    # ------------------------------------------------------------------ #
    async def _decide_cam(request: Request, report_id: str, decision: str,
                          decider: str, note: str | None) -> Any:
        who, err = await _verified_email(request, decider)
        if err is not None:
            return err
        checker, verified = _caller_context(request, who)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound committee identity is required.")
        if not await _authorised_for("cam", request, who, checker.tenant):
            return _problem(403, "Forbidden",
                            "Deciding a CAM requires Credit Head / Management / Admin "
                            "authority.")
        body: dict[str, Any] = {"decision": decision}
        if note:
            body["note"] = note
        return await _register_post_as(
            request, f"/v1/internal/cam-reports/{report_id}/decide", who, checker, body)

    @app.post("/v1/workflows/cam-reports/{report_id}/approve", tags=["Workflows"],
              summary="COMMITTEE approves the CAM (different person, senior authority)")
    async def approve_cam_report(report_id: str, payload: CamApproveIn, request: Request,
                                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                 ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide_cam(request, report_id, "Approved",
                                 payload.approved_by, payload.note)

    @app.post("/v1/workflows/cam-reports/{report_id}/reject", tags=["Workflows"],
              summary="COMMITTEE rejects the CAM (terminal for this version)")
    async def reject_cam_report(report_id: str, payload: CheckerRejectIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide_cam(request, report_id, "Rejected",
                                 payload.rejected_by, payload.note)

    @app.post("/v1/workflows/cam-reports/{report_id}/return", tags=["Workflows"],
              summary="COMMITTEE returns the CAM to its analyst (amend and resubmit)")
    async def return_cam_report(report_id: str, payload: CheckerReturnIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        return await _decide_cam(request, report_id, "Returned",
                                 payload.returned_by, payload.note)

    @app.post("/v1/workflows/{workflow_id}/committee-decision", tags=["Workflows"],
              summary="Record the Credit Committee decision (durable) and signal the workflow")
    async def committee_decision(workflow_id: str, payload: CommitteeDecisionIn, request: Request,
                                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                 ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        # The OVERALL outcome (the deal-level submission record; used for authority binding
        # and reconciliation): Approved when ANY facility is approved — the deal got a
        # sanction — Rejected only when every facility is refused.
        if payload.facilities is not None:
            outcome = ("Approved" if any(f.approved for f in payload.facilities)
                       else "Rejected")
        else:
            outcome = "Approved" if payload.approved else "Rejected"
        # FRESH authority check (committee roles) via Access, bound to the workflow's tenant.
        decided_by, approver, _token, err = await _decider(
            request, workflow_id, outcome, DecisionIn(by=payload.by, note=payload.note))
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return await _reconcile_closed(handle, desc, workflow_id, outcome)
        deal_id = await desc.memo_value("deal_id", None)
        if not deal_id:
            return _problem(409, "Conflict", "This workflow has no bound deal to decide on.")
        # Resolve the deal's lending lines FIRST so a facility-specific submission can be
        # validated against reality: every line must receive an outcome, and an unknown
        # lending_id is refused — a committee cannot decide a facility that does not exist.
        lines = await _lending_lines_for_deal(request, deal_id, approver, decided_by)
        if payload.facilities is not None:
            wanted = {f.lending_id: f for f in payload.facilities}
            if len(wanted) != len(payload.facilities):
                return _problem(422, "Validation failed",
                                "Duplicate lending_id in 'facilities'.")
            actual = {str(x) for x in lines}
            if set(wanted) != actual:
                return _problem(
                    422, "Validation failed",
                    f"'facilities' must cover exactly this deal's lending lines "
                    f"{sorted(actual)}; got {sorted(wanted)}.")
            line_outcome = {lid: ("Approved" if f.approved else "Rejected")
                            for lid, f in wanted.items()}
            line_note = {lid: (f.note or payload.note) for lid, f in wanted.items()}
            line_conditions = {lid: f.conditions for lid, f in wanted.items()}
            line_valid_days = {lid: f.valid_days for lid, f in wanted.items()}
        else:
            line_outcome = {str(x): outcome for x in lines}
            line_note = {str(x): payload.note for x in lines}
            line_conditions = {str(x): payload.conditions for x in lines}
            line_valid_days = {str(x): payload.valid_days for x in lines}
        # DURABLY record the committee decision (single-winner, subject-bound, provenance server-set)
        # BEFORE signalling — so the evidence gate can VERIFY the sanction against it, and a raw
        # signal alone can never manufacture a committee outcome.
        record, perr = await _persist_decision(
            request, workflow_id, outcome, decided_by, payload.note, approver, None,
            extra={"kind": "committee", "subject_type": "Deal", "subject_id": deal_id,
                   "run_id": desc.run_id,
                   "committee_reference": payload.committee_reference or workflow_id,
                   "sanction_letter_reference": payload.sanction_letter_reference})
        if perr is not None:
            return perr
        # A committee decision sanctions the DEAL *and* its lending facility. Evidence
        # verification binds a decision to its subject (Register: _verify_committee_decision
        # rejects "a different subject"), so a Deal-scoped decision cannot authorise the
        # Lending-scoped evidence the lending line's own Sanctioned gate requires. Record a
        # SUBJECT-BOUND decision per lending line here — with THIS human's committee authority,
        # which the workflow (a service principal) could never supply — keyed
        # "{workflow_id}:lending:{lending_id}" so it stays single-winner per line. The workflow
        # then cites that key when filing the line's evidence. Best-effort: a line that cannot
        # be recorded simply is not sanctioned; the deal outcome still stands.
        for line in lines:
            lid = str(line)
            await _persist_decision(
                request, f"{workflow_id}:lending:{lid}", line_outcome[lid], decided_by,
                line_note[lid], approver, None,
                extra={"kind": "committee", "subject_type": "Lending", "subject_id": lid,
                       "run_id": desc.run_id,
                       "committee_reference": payload.committee_reference or workflow_id,
                       "sanction_letter_reference": payload.sanction_letter_reference,
                       "conditions": line_conditions[lid],
                       "valid_days": line_valid_days[lid]})
        authoritative_by = (record.get("decided_by") if record else "") or decided_by
        # The committee read the CAM as part of this decision (the approval card carries
        # the filed document). Close the loop on the register: each APPROVED line's open
        # CAM version — filed but deliberately left Draft by the workbench — is submitted
        # and recorded Approved by the same decider. Best-effort: a line whose CAM cannot
        # be closed out keeps its sanction; the CAM just stays open for the analyst.
        for line in lines:
            lid = str(line)
            if line_outcome.get(lid) != "Approved":
                continue
            try:
                cams, cerr = await _register_get_as(
                    request, f"/v1/internal/cam-reports?lending_id={lid}",
                    decided_by, approver)
                if cerr is not None or not isinstance(cams, list):
                    continue
                open_cam = next(
                    (r for r in reversed(cams)
                     if r.get("status") in ("Draft", "Returned")
                     and (r.get("draft_md") or r.get("document_id"))), None)
                if open_cam is None:
                    continue
                rid = open_cam["id"]
                await _register_post_as(
                    request, f"/v1/internal/cam-reports/{rid}/submit",
                    decided_by, approver, {})
                await _register_post_as(
                    request, f"/v1/internal/cam-reports/{rid}/decide",
                    decided_by, approver,
                    {"decision": "Approved",
                     "note": "Sanctioned by the credit committee "
                             f"({payload.committee_reference or workflow_id})."})
            except Exception as exc:  # noqa: BLE001 — never block the sanction on this
                log.warning("cam_closeout_failed",
                            extra={"lending_id": lid, "error": str(exc)})
        # The signal is only a WAKE-UP: the workflow re-reads the authoritative decision record and
        # derives the outcome/approver/note/references from it — nothing here is trusted by the run.
        try:
            await handle.signal(DealStructuringWorkflow.committee_decision, "")
        except RPCError:
            try:
                desc2 = await handle.describe()
            except (RPCError, TemporalError):
                return _problem(503, "Delivery failed",
                                "Decision persisted; Temporal is unreachable — retry delivery.")
            if desc2.status == WorkflowExecutionStatus.RUNNING:
                return _problem(503, "Delivery failed",
                                "Decision persisted but signal delivery failed transiently; "
                                "retry delivery.")
            return await _reconcile_closed(handle, desc2, workflow_id, outcome)
        return {"workflow_id": workflow_id, "decision": outcome, "by": authoritative_by,
                "facilities": {lid: {"outcome": line_outcome[lid],
                                     "conditions": line_conditions[lid],
                                     "valid_days": line_valid_days[lid]}
                               for lid in line_outcome}}

    @app.post("/v1/workflows/{workflow_id}/document-received", tags=["Workflows"],
              summary="Signal that a required document was received")
    async def document_received(workflow_id: str, payload: DocumentReceivedIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if (denied_t := _wf_tenant_denied(request, workflow_id)) is not None:
            return denied_t
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except (RPCError, TemporalError) as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc}")
        # Only the initiator or an approver-role holder may feed documents into the run.
        if (scope_err := await _status_scope_denied(request, workflow_id, desc, who)) is not None:
            return scope_err
        try:
            await handle.signal(DocumentCollectionWorkflow.document_received,
                                args=[payload.name, payload.reference, payload.sha256 or ""])
        except (RPCError, TemporalError) as exc:
            return _problem(503, "Delivery failed", f"Signal delivery failed: {exc}")
        return {"workflow_id": workflow_id, "document_received": payload.name}

    async def _deliver_confirmation(workflow_id: str, request: Request, signal: str,
                                    args: list, x_api_key: str | None) -> Any:
        """Shared delivery path for a parked VOX capture's confirmation signals: API-key
        gate → verified identity → tenant binding → the run must be RUNNING and actually
        WAITING on that confirmation (else 409 — a confirmation for nothing is a bug)."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        acted_by, err = await _verified_email(request, args[-1] or "")
        if err is not None:
            return err
        args[-1] = acted_by                      # the VERIFIED identity, not the body's
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict", "This run is no longer waiting on anything.")
        try:
            pending = await handle.query("pending_confirmation")
        except (RPCError, TemporalError):
            pending = {}
        wanted = {"confirm_company": "company", "select_lead": "lead"}[signal]
        if (pending or {}).get("kind") != wanted:
            return _problem(409, "Conflict",
                            f"This run is not awaiting a {wanted} confirmation.")
        await handle.signal(signal, args=args)
        log.info("vox_confirmation", extra={"workflow": workflow_id, "signal": signal,
                                            "by": acted_by})
        return {"workflow_id": workflow_id, "delivered": signal, "by": acted_by,
                "candidates_were": pending.get("candidates", [])}

    @app.post("/v1/workflows/{workflow_id}/confirm-company", tags=["Workflows"],
              summary="Answer an ambiguous-company confirmation on a parked VOX capture")
    async def confirm_company(workflow_id: str, payload: CompanyConfirmIn, request: Request,
                              x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                              ) -> Any:
        return await _deliver_confirmation(workflow_id, request, "confirm_company",
                                           [payload.entity_id, payload.by], x_api_key)

    @app.post("/v1/workflows/{workflow_id}/select-lead", tags=["Workflows"],
              summary="Answer a multi-lead selection on a parked VOX capture")
    async def select_lead(workflow_id: str, payload: LeadSelectIn, request: Request,
                          x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                          ) -> Any:
        return await _deliver_confirmation(workflow_id, request, "select_lead",
                                           [payload.lead_id, payload.by], x_api_key)

    @app.post("/v1/workflows/syndications", status_code=202, tags=["Workflows"],
              summary="Start a syndication-mandate workflow (IM → decision → allocation)")
    async def start_syndication(payload: SyndicationStartIn, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                ) -> Any:
        return await _start_business(
            request, x_api_key, payload.requested_by, SyndicationMandateWorkflow,
            SyndicationMandateInput, "synd", payload.syndication_id,
            {"syndication_id": payload.syndication_id, "subject_type": "Syndication",
             "deal_id": payload.deal_id},
            syndication_id=payload.syndication_id, deal_id=payload.deal_id,
            requested_by=payload.requested_by, im_reference=payload.im_reference,
            im_sha256=payload.im_sha256,
            decision_timeout_hours=payload.decision_timeout_hours,
            allocation_timeout_hours=payload.allocation_timeout_hours)

    @app.post("/v1/workflows/{workflow_id}/syndication-decision", tags=["Workflows"],
              summary="Record the Syn Head's decision on a mandate (durable, then signal)")
    async def syndication_decision(workflow_id: str, payload: SyndicationDecisionIn,
                                   request: Request,
                                   x_api_key: str | None = Header(default=None,
                                                                  alias="X-API-Key"),
                                   ) -> Any:
        """Same trust posture as the committee: fresh authority (Syn Head vertical),
        DURABLY recorded (single-winner, subject-bound, kind='syndication') BEFORE the run
        is signalled — the signal is only a wake-up the workflow verifies fail-closed."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        outcome = "Approved" if payload.approved else "Rejected"
        decided_by, approver, _token, err = await _decider(
            request, workflow_id, outcome, DecisionIn(by=payload.by, note=payload.note))
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict", "This run is no longer awaiting a decision.")
        syndication_id = await desc.memo_value("syndication_id", None)
        if not syndication_id:
            return _problem(409, "Conflict", "This workflow has no bound mandate.")
        _rec, err = await _persist_decision(
            request, workflow_id, outcome, decided_by, payload.note, approver, None,
            extra={"kind": "syndication", "subject_type": "Syndication",
                   "subject_id": str(syndication_id), "run_id": desc.run_id,
                   "committee_reference": payload.sanction_reference or workflow_id,
                   "conditions": payload.conditions})
        if err is not None:
            return err
        try:
            await handle.signal("syndication_decision", args=[workflow_id])
        except RPCError as exc:
            log.warning("syndication_signal_failed", extra={"workflow": workflow_id,
                                                            "error": exc.message})
            return _problem(409, "Conflict",
                            "The decision was recorded but the run closed before it could "
                            "be delivered.")
        log.info("syndication_decision", extra={"workflow": workflow_id,
                                                "decision": outcome, "by": decided_by})
        return {"workflow_id": workflow_id, "decision": outcome, "by": decided_by}

    async def _deliver_signal(workflow_id: str, request: Request, signal: str, args: list,
                              by_index: int, x_api_key: str | None) -> Any:
        """Verified-identity signal delivery for the syndication run's business signals
        (IM circulation / lender activity / allocation). The payload's effects all go
        through the Register's policy-enforcing API from inside the run — the run is the
        audit; the endpoint's job is identity, tenant binding and liveness."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        acted_by, err = await _verified_email(request, args[by_index] or "")
        if err is not None:
            return err
        args[by_index] = acted_by
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict", "This run is closed.")
        if _auth_enforced():
            scope_err = await _status_scope_denied(request, workflow_id, desc, acted_by)
            if scope_err is not None:
                return scope_err
        await handle.signal(signal, args=args)
        log.info("workflow_signal", extra={"workflow": workflow_id, "signal": signal,
                                           "by": acted_by})
        return {"workflow_id": workflow_id, "delivered": signal, "by": acted_by}

    @app.post("/v1/workflows/{workflow_id}/circulate-im", tags=["Workflows"],
              summary="Circulate the (next version of the) IM on a syndication run")
    async def circulate_im(workflow_id: str, payload: CreditNoteRevisionIn,
                           request: Request,
                           x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                           ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "circulate_im",
            [payload.reference, payload.sha256 or "", payload.by], 2, x_api_key)

    @app.post("/v1/workflows/{workflow_id}/lender-update", tags=["Workflows"],
              summary="Record lender-level activity on a syndication run")
    async def lender_update(workflow_id: str, payload: LenderUpdateIn, request: Request,
                            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                            ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "lender_update",
            [payload.lender_row_id, payload.status, payload.note, payload.by], 3, x_api_key)

    @app.post("/v1/workflows/{workflow_id}/allocate", tags=["Workflows"],
              summary="Record the post-sanction lender allocation on a syndication run")
    async def allocate(workflow_id: str, payload: AllocationIn, request: Request,
                       x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                       ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "allocate",
            [payload.allocations, payload.by], 1, x_api_key)

    @app.post("/v1/workflows/asset-monetisations", status_code=202, tags=["Workflows"],
              summary="Start an asset-monetisation workflow (teaser → offers → closure)")
    async def start_asset_mon(payload: AmStartIn, request: Request,
                              x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                              ) -> Any:
        return await _start_business(
            request, x_api_key, payload.requested_by, AssetMonetisationWorkflow,
            AssetMonetisationInput, "amon", payload.asset_mon_id,
            {"asset_mon_id": payload.asset_mon_id, "subject_type": "AssetMonetisation",
             "deal_id": payload.deal_id},
            asset_mon_id=payload.asset_mon_id, deal_id=payload.deal_id,
            requested_by=payload.requested_by, teaser_reference=payload.teaser_reference,
            teaser_sha256=payload.teaser_sha256,
            decision_timeout_hours=payload.decision_timeout_hours)

    @app.post("/v1/workflows/{workflow_id}/am-decision", tags=["Workflows"],
              summary="Record the AM Head's closure decision (durable, then signal)")
    async def am_decision(workflow_id: str, payload: AmDecisionIn, request: Request,
                          x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                          ) -> Any:
        """Persist-before-signal with kind='asset_monetisation' (subject-bound, AM Head
        authority) — the run verifies the record fail-closed before acting."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        outcome = "Approved" if payload.approved else "Rejected"
        decided_by, approver, _token, err = await _decider(
            request, workflow_id, outcome, DecisionIn(by=payload.by, note=payload.note))
        if err is not None:
            return err
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict", "This run is no longer awaiting a decision.")
        asset_mon_id = await desc.memo_value("asset_mon_id", None)
        if not asset_mon_id:
            return _problem(409, "Conflict", "This workflow has no bound mandate.")
        _rec, err = await _persist_decision(
            request, workflow_id, outcome, decided_by, payload.note, approver, None,
            extra={"kind": "asset_monetisation", "subject_type": "AssetMonetisation",
                   "subject_id": str(asset_mon_id), "run_id": desc.run_id,
                   "committee_reference": payload.closure_reference or workflow_id})
        if err is not None:
            return err
        try:
            await handle.signal("am_decision", args=[workflow_id])
        except RPCError as exc:
            log.warning("am_signal_failed", extra={"workflow": workflow_id,
                                                   "error": exc.message})
            return _problem(409, "Conflict",
                            "The decision was recorded but the run closed before it could "
                            "be delivered.")
        log.info("am_decision", extra={"workflow": workflow_id, "decision": outcome,
                                       "by": decided_by})
        return {"workflow_id": workflow_id, "decision": outcome, "by": decided_by}

    @app.post("/v1/workflows/{workflow_id}/circulate-teaser", tags=["Workflows"],
              summary="Circulate the (next version of the) teaser on an AM run")
    async def circulate_teaser(workflow_id: str, payload: CreditNoteRevisionIn,
                               request: Request,
                               x_api_key: str | None = Header(default=None,
                                                              alias="X-API-Key"),
                               ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "circulate_teaser",
            [payload.reference, payload.sha256 or "", payload.by], 2, x_api_key)

    @app.post("/v1/workflows/{workflow_id}/buyer-update", tags=["Workflows"],
              summary="Record buyer-level activity on an AM run")
    async def buyer_update(workflow_id: str, payload: BuyerUpdateIn, request: Request,
                           x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                           ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "buyer_update",
            [payload.buyer_row_id, payload.status, payload.note, payload.by], 3, x_api_key)

    @app.post("/v1/workflows/{workflow_id}/record-nda", tags=["Workflows"],
              summary="Record a buyer's NDA (and data-room grant) on an AM run")
    async def record_nda(workflow_id: str, payload: NdaIn, request: Request,
                         x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                         ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "record_nda",
            [payload.buyer_row_id, payload.reference, payload.data_room, payload.by], 3,
            x_api_key)

    @app.post("/v1/workflows/{workflow_id}/record-offer", tags=["Workflows"],
              summary="Record a buyer's NBO / binding offer on an AM run")
    async def record_offer(workflow_id: str, payload: OfferIn, request: Request,
                           x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                           ) -> Any:
        return await _deliver_signal(
            workflow_id, request, "record_offer",
            [payload.buyer_row_id, payload.kind, payload.amount_cr, payload.reference,
             payload.by], 4, x_api_key)

    @app.post("/v1/workflows/{workflow_id}/revise-credit-note", tags=["Workflows"],
              summary="Circulate a revised credit note to the committee (rework loop)")
    async def revise_credit_note(workflow_id: str, payload: CreditNoteRevisionIn,
                                 request: Request,
                                 x_api_key: str | None = Header(default=None,
                                                                alias="X-API-Key"),
                                 ) -> Any:
        """Committee rework, completed: return-for-information parks the run, this delivers
        the revised note (filed as the NEXT immutable credit_note version on every line),
        resubmit restores the decision window. Verified identity + tenant binding; the run
        must still be awaiting its decision."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        revised_by, err = await _verified_email(request, payload.by)
        if err is not None:
            return err
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict",
                            "This run is closed — a revised note needs a fresh "
                            "structuring request.")
        if _auth_enforced():
            scope_err = await _status_scope_denied(request, workflow_id, desc, revised_by)
            if scope_err is not None:
                return scope_err
        await handle.signal("revise_credit_note",
                            args=[payload.reference, payload.sha256 or "", revised_by])
        log.info("credit_note_revised", extra={"workflow": workflow_id,
                                               "reference": payload.reference,
                                               "by": revised_by})
        return {"workflow_id": workflow_id, "delivered": "revise_credit_note",
                "reference": payload.reference, "by": revised_by}

    _CONTROL_OUTCOME = {"cancel": "Cancelled", "return": "ReturnedForInformation",
                        "resubmit": "Resubmitted"}

    @app.post("/v1/workflows/{workflow_id}/control", tags=["Workflows"],
              summary="Cancel / return-for-information / resubmit a waiting run")
    async def control_workflow(workflow_id: str, payload: ControlIn, request: Request,
                               x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                               ) -> Any:
        """The lifecycle controls every real approval flow needs. Same trust posture as a
        decision: verified identity, tenant binding, and the action is PERSISTED as an
        immutable control record BEFORE the workflow is signalled — the signal is only a
        wake-up, and the run verifies the record (fail-closed) before acting on it."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        acted_by, err = await _verified_email(request, payload.by)
        if err is not None:
            return err
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        if desc.status != WorkflowExecutionStatus.RUNNING:
            return _problem(409, "Conflict",
                            f"This run is {desc.status.name if desc.status else 'closed'} — "
                            "run-control applies only to a waiting run.")
        # WHO may control a run: the INITIATOR (they asked; they may withdraw or resubmit)
        # or an approver-role holder for this vertical (they may also send it back for
        # information). Enforced only under the production identity posture, like reads.
        if _auth_enforced():
            scope_err = await _status_scope_denied(request, workflow_id, desc, acted_by)
            if scope_err is not None:
                return scope_err
        outcome = _CONTROL_OUTCOME[payload.action]
        caller, _verified = _caller_context(request, acted_by)
        # Durable first: one immutable record per control action (a fresh reference each
        # time — a run can be returned and resubmitted more than once).
        ref = f"{workflow_id}:control:{uuid.uuid4().hex[:12]}"
        _rec, err = await _persist_decision(
            request, ref, outcome, acted_by, payload.note, caller, None,
            extra={"kind": "control", "run_id": desc.run_id})
        if err is not None:
            return err
        try:
            await handle.signal("control", args=[outcome, ref])
        except RPCError as exc:
            # The record is durable; the run may have closed in the race window. Truthful
            # answer: recorded, not delivered — the reconciler/status read tells the rest.
            log.warning("control_signal_failed", extra={"workflow": workflow_id,
                                                        "action": outcome,
                                                        "error": exc.message})
            return _problem(409, "Conflict",
                            "The control action was recorded but the run closed before it "
                            "could be delivered.")
        log.info("run_control", extra={"workflow": workflow_id, "action": outcome,
                                       "by": acted_by})
        return {"workflow_id": workflow_id, "action": outcome, "by": acted_by,
                "control_ref": ref}

    # kind → (workflow-id prefix, how its decision is delivered: "approve/reject" =
    # the generic approve/reject routes; a name = a dedicated decision route; None =
    # signal-driven, no single decision URL). CP/CS is deliberately absent: its ids
    # are checklist-versioned (…-v{n}) and already returned by the checklist APIs.
    _LOOKUP_KINDS: dict[str, tuple[str, str | None]] = {
        "lead-conversion": ("leadconv", "approve/reject"),
        "lead-qualification": ("qual", None),
        "deal-structuring": ("struct", "committee-decision"),
        "document-collection": ("docs", None),
        "syndication": ("synd", "syndication-decision"),
        "asset-monetisation": ("amon", "am-decision"),
        # The handover CHECKER approval is keyed by the LENDING id (the subject),
        # not the workflow id — a sentinel marks that so the URL is still served.
        "advaya-handover": ("handover", "subject:advaya-handover/{subject_id}/approve"),
        "ews-case": ("ews", None),
    }

    def _action_urls(decide: str | None, workflow_id: str, subject_id: str) -> dict[str, str]:
        """The ready-made URLs a client acts on — one construction site for the lookup
        AND the pending list, so the two can never drift.

        Every PARKED run advertises the full triad the platform enforces: approve (or a
        named decision route), RETURN-for-revision, and reject. Return is always the
        run-control lane (``/control {action:"return"}``) — that is how a waiting run
        goes back to its requester without being decided — so a client that reads this
        block can render all three buttons without knowing any of the routing rules.
        """
        urls = {"status_url": f"/v1/workflows/{workflow_id}",
                "control_url": f"/v1/workflows/{workflow_id}/control"}
        if decide == "approve/reject":
            urls["approve_url"] = f"/v1/workflows/{workflow_id}/approve"
            urls["reject_url"] = f"/v1/workflows/{workflow_id}/reject"
        elif decide is not None and decide.startswith("subject:"):
            base = ("/v1/workflows/"
                    + decide.removeprefix("subject:").format(subject_id=subject_id))
            urls["approve_url"] = base
            # …/{subject}/approve → …/{subject}/return, …/reject
            stem = base.rsplit("/", 1)[0]
            urls["return_url"] = f"{stem}/return"
            urls["reject_url"] = f"{stem}/reject"
        elif decide is not None:
            urls["decision_url"] = f"/v1/workflows/{workflow_id}/{decide}"
        if "return_url" not in urls:
            # The parked run's return lane: back to the requester, non-terminal, the
            # SLA clock restarts when they resubmit.
            urls["return_url"] = f"/v1/workflows/{workflow_id}/control"
        return urls

    # The approval-bearing kinds and their Temporal workflow TYPE names — the pending
    # list filters on WorkflowType + ExecutionStatus, both BUILT-IN search attributes,
    # so it needs no custom search-attribute registration on the server.
    # CP/CS checklists and handover packages are NOT here: their prepare-workflows
    # complete immediately and the wait lives as a Prepared REGISTER row — those
    # queues are read from the Register below, which also covers makers who used the
    # register lane directly.
    _PENDING_TYPES: dict[str, str] = {
        "lead-conversion": "LeadConversionWorkflow",
        "deal-structuring": "DealStructuringWorkflow",
        "syndication": "SyndicationMandateWorkflow",
        "asset-monetisation": "AssetMonetisationWorkflow",
    }

    # Register-sourced pending kinds: (queue path, approver-role prefix). NB the two
    # vocabularies differ: a maker's finished CHECKLIST is 'Completed' (awaiting the
    # check); a handover PACKAGE awaiting its check is 'Prepared'.
    _PENDING_REGISTER_QUEUES: dict[str, tuple[str, str]] = {
        "cpcs-checklist": ("/v1/internal/cpcs-checklists?status=Completed", "cpcs"),
        "advaya-handover": ("/v1/internal/handover-packages?status=Prepared", "handover"),
        # A SUBMITTED CAM is with the committee — same durable-row queue pattern.
        "cam-report": ("/v1/internal/cam-reports?status=Submitted", "cam"),
    }

    @app.get("/v1/workflows/actions", tags=["Workflows"],
             summary="What this user can do NEXT on a subject (the maker's half)")
    async def subject_actions(request: Request, subject_type: str, subject_id: str,
                              x_api_key: str | None = Header(default=None,
                                                             alias="X-API-Key"),
                              ) -> Any:
        """The maker's counterpart to ``/v1/workflows/pending``.

        Given one line — a lending facility, a syndication mandate, an asset-monetisation
        mandate — answer what this caller may do to it right now, each with the URL, the
        method and the form to collect. Unavailable steps come back too, with the reason,
        because the sequence is the thing a user most needs to learn.

        The point is that the sequencing rules stay on THIS side. A client that decides
        for itself which buttons to show ends up offering steps the platform refuses —
        which is exactly what the lending stage dropdown used to do.
        """
        if (resp := denied(x_api_key)) is not None:
            return resp
        who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if _auth_enforced() and not who:
            return _problem(401, "Unauthorized",
                            "A verified identity is required to list actions.")
        if subject_type not in _MAKER_ACTIONS:
            return _problem(422, "Validation failed",
                            f"Unknown subject_type '{subject_type}'; one of: "
                            f"{', '.join(sorted(_MAKER_ACTIONS))}.")
        try:
            if not _uuid_or_none(subject_id):
                raise ValueError("empty")
        except ValueError:
            return _problem(422, "Validation failed",
                            f"subject_id '{subject_id}' is not a valid id — was a "
                            "client variable left unset?")
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()

        # ROLES. Fail CLOSED and say so, rather than answering "nothing you can do" —
        # an empty list that means "we could not ask" is indistinguishable from an empty
        # list that means "you may do nothing", and that ambiguity cost a day once.
        roles: set[str] = set()
        if _auth_enforced():
            if not settings.access_url:
                return _problem(503, "Service unavailable",
                                "Roles cannot be resolved: the Access service is not "
                                "configured, so no action can be offered safely.")
            try:
                role_resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": who},
                    headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant})
            except httpx.HTTPError as exc:
                return _problem(503, "Service unavailable",
                                f"Roles cannot be resolved: {exc}")
            if role_resp.status_code != 200:
                return _problem(503, "Service unavailable",
                                f"Roles cannot be resolved (HTTP "
                                f"{role_resp.status_code}).")
            roles = set(role_resp.json().get("roles", []))

        # SUBJECT. Read as the human, so the register's own scope decides whether this
        # caller may see the line at all.
        caller, _v = _caller_context(request, who)
        path = _SUBJECT_PATH[subject_type] + "/" + subject_id
        row, read_err = await _register_get_as(request, path, who, caller)
        if read_err is not None:
            return read_err
        stage = str(row.get(_STAGE_FIELD[subject_type]) or "")

        # RUN. The deterministic id the start route would mint — resolved to the NEWEST
        # attempt (-rN retries): after a failed first attempt was retried, reading the
        # base id described the dead run, so the drawer offered the wrong actions and
        # revise/resubmit targeted a workflow that could no longer hear them.
        prefix, id_field = _RUN_ID_FOR[subject_type]
        run_key = str(row.get(id_field) or "")
        workflow_id = f"{prefix}-{_tenant_slug(tenant)}-{run_key}" if run_key else ""
        if workflow_id:
            with contextlib.suppress(RPCError, TemporalError, AttributeError):
                found = await _attempts(request.app.state.temporal, workflow_id)
                if found:
                    workflow_id = found[-1][0]
        run_state, run_info = "none", None
        # Provenance for governance evidence: the run that PRODUCED the thing being
        # attested. Captured whatever the run's status, because the run that sanctioned a
        # facility has normally finished by the time its executed agreement is filed —
        # reading it only while RUNNING left the executed-agreement action with no
        # workflow_id to cite, and the register refuses evidence that cannot name one.
        run_prov: dict[str, str] = {}
        run_uuid = ""
        if workflow_id:
            with contextlib.suppress(RPCError, TemporalError, AttributeError):
                handle = request.app.state.temporal.get_workflow_handle(workflow_id)
                desc = await handle.describe()
                run_uuid = str(getattr(desc, "run_id", "") or "")
                if desc.status == WorkflowExecutionStatus.RUNNING:
                    run_state = "live"
                    # "Returned" is a BUSINESS state (the `state` query) — the technical
                    # stage stays "Awaiting committee decision" while a run is parked
                    # with its maker, so reading only `status` here kept the drawer's
                    # revise/resubmit actions disabled exactly when they were needed.
                    stage_q = ""
                    with contextlib.suppress(RPCError, TemporalError):
                        stage_q = str(await handle.query("status") or "")
                    business = ""
                    with contextlib.suppress(RPCError, TemporalError):
                        st = await handle.query("state")
                        if isinstance(st, dict):
                            business = str(st.get("business_status") or "")
                    if (business in _RETURNED_STATES or "return" in business.lower()
                            or "return" in stage_q.lower()):
                        run_state = "returned"
                    run_info = {"workflow_id": workflow_id, "status": "RUNNING",
                                "stage": ("Returned for revision — amend and resubmit"
                                          if run_state == "returned" else stage_q),
                                "business_status": business,
                                "status_url": f"/v1/workflows/{workflow_id}"}

        # What governance evidence is already on file for this subject — read once, so an
        # action that depends on it can be offered (or refused, by name) without the user
        # having to submit to find out.
        on_file: set[str] = set()
        evidence_refs: list[dict[str, str]] = []
        ev_rows, ev_problem = await _register_get_as(
            request, f"/v1/evidence?subject_type={subject_type}&subject_id={subject_id}",
            who, caller)
        if ev_problem is None:
            for row_ev in (ev_rows.get("items") or []):
                if row_ev.get("invalidated"):
                    continue
                kind = str(row_ev.get("evidence_kind") or "")
                on_file.add(kind)
                # The handover package must reference the EXECUTED AGREEMENT by digest —
                # the register reconciles the submitted refs against the evidence on file
                # and refuses a package that omits it. That is not something to ask a user
                # to retype; hand the client the ref it must include.
                if kind == "executed_agreement" and row_ev.get("sha256"):
                    evidence_refs.append({"reference": str(row_ev.get("reference") or
                                                           "Executed facility agreement"),
                                          "sha256": str(row_ev["sha256"])})

        # The handover package's state. Three actions claim to be sequenced — prepare,
        # approve, submit, then Advaya's outcome — and the reasons SAID so while nothing
        # enforced it, so a user could open "Record an Advaya confirmation" on a package
        # that had not been submitted and be refused after filling the form in. The plane
        # knows the state; it gates on it.
        package_status = ""
        if subject_type == "Lending":
            pkg, pkg_problem = await _register_get_as(
                request, f"/v1/lending/{subject_id}/handover-package", who, caller)
            if pkg_problem is None:
                package_status = str(pkg.get("status") or "")

        # The citation a governance evidence must carry.
        #
        # Not the run's own id: a deal's structuring workflow covers every facility on that
        # deal, so citing it against ONE lending line is a claim the register rejects — the
        # decision it verifies against is recorded PER LINE, under "{run}:lending:{id}".
        # So the plane asks the register which decision it actually holds for this subject
        # and cites that, rather than composing an identifier and hoping. A subject with no
        # recorded decision yields no citation, and the action that needs one is disabled
        # with that reason instead of failing at submit.
        if run_uuid and workflow_id:
            candidate = (f"{workflow_id}:lending:{subject_id}"
                         if subject_type == "Lending" else workflow_id)
            decision, problem = await _register_get_as(
                request, f"/v1/internal/decisions/{candidate}", who, caller)
            if problem is None and str(decision.get("subject_id") or "") == str(subject_id):
                run_prov = {"workflow_id": candidate, "run_id": run_uuid}

        # The NEXT CP/CS version, so the screen opens on it. A checklist is keyed on
        # (lending, version), so a client that always defaults to 1 hands the user a 409
        # after they have filled the whole form in — the plane knows the answer, so it
        # gives it rather than letting them guess.
        next_version = 1
        latest_checklist_status = ""
        latest_checklist_items: list = []
        cam_ready = True
        if subject_type == "Lending":
            existing, ver_problem = await _register_get_as(
                request, f"/v1/internal/cpcs-checklists?lending_id={subject_id}&limit=50",
                who, caller)
            if ver_problem is not None:
                # Do not quietly fall back to 1. Defaulting on a FAILED read is how this
                # hid: the screen opened on a version that already existed and refused the
                # user's work at submit. Say the number is unknown instead.
                log.warning("cpcs_next_version_unavailable",
                            extra={"lending": subject_id})
                next_version = 0
            else:
                rows = existing.get("items") if isinstance(existing, dict) else existing
                versions = [int(r.get("checklist_version") or 0) for r in (rows or [])]
                next_version = (max(versions) + 1) if versions else 1
                if rows:
                    latest = max(rows, key=lambda r: int(r.get("checklist_version") or 0))
                    latest_checklist_status = str(latest.get("status") or "")
                    latest_checklist_items = list(latest.get("items") or [])
            # The committee reads the CAM — so "Send to credit committee" waits for one.
            # Fail-open on an unreadable answer (the gate informs, it must not wedge).
            cams, cam_err = await _register_get_as(
                request, f"/v1/internal/cam-reports?lending_id={subject_id}", who, caller)
            if cam_err is None and isinstance(cams, list):
                cam_ready = any(r.get("draft_md") or r.get("document_id") for r in cams)
            else:
                cam_ready = True

        actions = []
        for spec in _MAKER_ACTIONS[subject_type]:
            enabled, reason = _evaluate_action(spec, roles=roles, stage=stage,
                                               run_state=run_state)
            # The CS step opens on the CP APPROVAL itself — the live checklist state,
            # not a stage the analyst may not have moved yet. (Latest version awaiting
            # a checker also waits: no stacking a new version on an undecided one.)
            if (spec["key"] == "cpcs.update-cs" and enabled
                    and latest_checklist_status != "Approved"):
                enabled = False
                reason = ("Conditions subsequent open once the CP checklist is "
                          "APPROVED — send it for checking and get the checker's "
                          "approval first."
                          if latest_checklist_status else
                          "Prepare and get the CP checklist approved first.")
            # The committee decides ON the CAM: no CAM on the line, nothing to send.
            if spec["key"] == "deal-structuring.start" and enabled and not cam_ready:
                enabled = False
                reason = ("Prepare the CAM first (CAM workbench) — the committee "
                          "reads it with the request.")
            url = spec["url"].format(workflow_id=workflow_id, subject_id=subject_id)
            body = dict(spec.get("constant") or {})
            for field, source in (spec.get("prefill") or {}).items():
                value = row.get(source)
                if value:
                    body[field] = str(value)
            # WHO is doing this comes from the verified token, never from a form field.
            # Only fill the identity key this endpoint actually declares.
            for key in _IDENTITY_FIELDS:
                if key in _IDENTITY_FOR.get(spec["key"], ()):
                    body[key] = who
            # WHICH RUN produced it. Governance evidence must cite the run behind it, and
            # that is a fact about the platform rather than something to ask a person for
            # — the register refuses invented values, and rightly. An action that needs it
            # and has no run to cite is disabled with that reason, instead of failing at
            # submit after the user has filled the form in.
            wanted_pkg = spec.get("package")
            # A tuple means ANY of these states — "Disbursement Update" must stay
            # available after the partner ACCEPTS (each disbursed phase is one more
            # update), not only while the request is freshly sent.
            wanted_set = ((wanted_pkg,) if isinstance(wanted_pkg, str)
                          else tuple(wanted_pkg or ()))
            if wanted_set and enabled and package_status not in wanted_set:
                first = wanted_set[0]
                enabled, reason = False, (
                    _PACKAGE_REASON.get(first, f"Available once the handover package "
                                               f"is {first!r}.")
                    + (f" It is currently {package_status!r}."
                       if package_status else " No package has been prepared yet."))
            needed = [k for k in (spec.get("evidence") or ()) if k not in on_file]
            if needed and enabled:
                enabled, reason = False, (
                    "Still waiting on " + " and ".join(_EVIDENCE_LABEL.get(k, k) for k in needed)
                    + ".")
            if spec.get("provenance"):
                if run_prov:
                    body.update(run_prov)
                elif enabled:
                    enabled, reason = False, (
                        "No workflow run has been recorded against this line yet, and "
                        "governance evidence must cite the run that produced it. Run the "
                        "structuring workflow first.")
            form = spec["form"]
            if spec["key"] in ("cpcs.prepare", "cpcs.update-cs") and next_version:
                form = [{**f, "default": next_version} if f["name"] == "checklist_version"
                        else f for f in form]
            # The two condition buttons SHOW their progress — "Conditions Precedent
            # (4/9)" reads as a status, not a mystery step. Counts come from the latest
            # checklist version; a completed CP is anything no longer being chased.
            label = spec["label"]
            if latest_checklist_items and spec["key"] in ("cpcs.prepare", "cpcs.update-cs"):
                want = "CP" if spec["key"] == "cpcs.prepare" else "CS"
                rel = [i for i in latest_checklist_items
                       if str(i.get("condition_type")) == want]
                done_states = ({"Completed", "Waived", "Deferred as CS"}
                               if want == "CP" else {"Completed", "Waived"})
                if rel:
                    done = sum(1 for i in rel if str(i.get("status")) in done_states)
                    label = f"{label} ({done}/{len(rel)})"
            actions.append({
                "key": spec["key"], "label": label, "method": spec["method"],
                "url": url, "enabled": enabled,
                # WHICH SERVICE the url belongs to. The catalogue spans both planes —
                # starting a workflow is the orchestrator's, filing evidence is the
                # register's — and a client that assumed one of them sent every register
                # action to the orchestrator and got 404s. The client must be TOLD, not
                # left to guess: that is the whole point of a described action.
                "plane": _plane_of(spec, url),
                # Named screen for the steps a flat form cannot express; absent means the
                # client builds its dialog from `form`.
                **({"screen": spec["screen"]} if spec.get("screen") else {}),
                # Refs the client must include in what it sends (the handover's mandatory
                # executed-agreement digest). Kept OUT of `body` because the client adds to
                # them rather than replacing them.
                **({"evidence_refs": evidence_refs}
                   if spec.get("needs_evidence_refs") and evidence_refs else {}),
                **({"reason": reason} if not enabled else {}),
                "body": body, "form": form,
            })
        return {
            "subject": {"type": subject_type, "id": subject_id, "stage": stage},
            "run": run_info,
            "scoped_to": {"email": who, "roles": sorted(roles)},
            "actions": actions,
        }

    @app.get("/v1/workflows/pending", tags=["Workflows"],
             summary="Every run parked awaiting an approval, tenant-wide (the Today list)")
    async def pending_approvals(request: Request, kind: str | None = None,
                                x_api_key: str | None = Header(default=None,
                                                               alias="X-API-Key"),
                                ) -> Any:
        """The approver's landing list: every RUNNING run in this tenant that is parked
        on a decision, across ALL subjects, each with its subject, requester, waiting
        stage and ready-made action URLs. Mid-flight runs (circulating a note/IM, not
        yet parked) are excluded. Under the production identity posture the list is
        scoped to the verticals the CALLER holds an approver role for — an RM gets an
        empty list, a Credit Head sees committee + handover items, Management sees all.
        Optional ``kind`` narrows to one workflow kind. Oldest waiting first."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        _who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if _auth_enforced() and not _who:
            return _problem(401, "Unauthorized",
                            "A verified identity is required to list pending approvals.")
        valid_kinds = set(_PENDING_TYPES) | set(_PENDING_REGISTER_QUEUES)
        if kind is not None and kind not in valid_kinds:
            return _problem(422, "Validation failed",
                            f"Unknown kind '{kind}'; one of: "
                            f"{', '.join(sorted(valid_kinds))}.")
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        slug = _tenant_slug(tenant)
        caller_roles: set[str] | None = None
        if _auth_enforced():
            # Role scoping decides what this approver may SEE. If we cannot establish
            # it, the queue must NOT quietly come back empty: an empty list reads as
            # "nothing needs you", and an approver who believes that stops looking.
            # Say so instead — the UI can then show an error, not a clean desk.
            if not settings.access_url:
                return _problem(503, "Service unavailable",
                                "Approver roles cannot be resolved (WORKFLOWS_ACCESS_URL "
                                "is not configured), so this queue cannot be scoped. It "
                                "is NOT necessarily empty — fix the configuration and "
                                "retry.")
            try:
                role_resp = await request.app.state.http.get(
                    f"{settings.access_url.rstrip('/')}/v1/resolve",
                    params={"email": _who},
                    headers={"X-API-Key": settings.access_api_key,
                             "X-Tenant": tenant})
            except httpx.HTTPError as exc:
                return _problem(503, "Service unavailable",
                                "The access service could not be reached to resolve your "
                                f"approver roles ({exc}); this queue is unscoped, not "
                                "empty. Retry shortly.")
            if role_resp.status_code != 200:
                return _problem(503, "Service unavailable",
                                "The access service could not resolve your approver roles "
                                f"(HTTP {role_resp.status_code}); this queue is unscoped, "
                                "not empty.")
            caller_roles = set(role_resp.json().get("roles", []))
        client: Client = request.app.state.temporal
        pending: list[dict[str, Any]] = []
        for k, wtype in _PENDING_TYPES.items():
            if kind is not None and k != kind:
                continue
            prefix, decide = _LOOKUP_KINDS[k]
            if (caller_roles is not None
                    and not (caller_roles & _APPROVER_ROLES.get(prefix, set()))):
                continue
            try:
                listing = client.list_workflows(
                    f"WorkflowType = '{wtype}' AND ExecutionStatus = 'Running'")
                async for ex in listing:
                    wf_id = ex.id
                    if not wf_id.startswith(f"{prefix}-{slug}-"):
                        continue  # another tenant's run
                    subject_id = re.sub(r"-r\d+$", "",
                                        wf_id[len(prefix) + len(slug) + 2:])
                    handle = client.get_workflow_handle(wf_id)
                    stage: Any = None
                    requested_by: str | None = None
                    with contextlib.suppress(RPCError, TemporalError):
                        desc = await handle.describe()
                        requested_by = (str(await desc.memo_value("initiator", "") or "")
                                        or None)
                    with contextlib.suppress(RPCError, TemporalError):
                        stage = await handle.query("status")
                    # A conversion is pending its whole RUNNING life; the others are
                    # pending only once parked ("Awaiting …" / "… awaiting checker …").
                    # An unanswerable stage query stays listed (fail open: visibility
                    # only — the decision POST still enforces authority).
                    if (k != "lead-conversion" and stage is not None
                            and "awaiting" not in str(stage).lower()):
                        continue
                    # BUSINESS status rides along: a run the committee RETURNED is still
                    # parked (technical stage unchanged), but it is the MAKER's to-do,
                    # not the approver's — without this field the approver's Today kept
                    # showing it as an approval request after they had returned it.
                    business = ""
                    with contextlib.suppress(RPCError, TemporalError):
                        st = await handle.query("state")
                        if isinstance(st, dict):
                            business = str(st.get("business_status") or "")
                    pending.append({
                        "kind": k, "subject_id": subject_id, "workflow_id": wf_id,
                        "status": "RUNNING", "stage": stage,
                        "business_status": business,
                        "requested_by": requested_by,
                        "started_at": (ex.start_time.isoformat()
                                       if ex.start_time else None),
                        **_action_urls(decide, wf_id, subject_id)})
            except RPCError as exc:
                return _problem(502, "Bad gateway",
                                "Temporal visibility refused the pending-approvals "
                                f"query for {wtype}: {exc.message}")
        # CP/CS checklists + handover packages awaiting the checker: read from the
        # REGISTER (the durable rows are the queue, whichever lane prepared them).
        for k, (queue_path, role_prefix) in _PENDING_REGISTER_QUEUES.items():
            if kind is not None and k != kind:
                continue
            if (caller_roles is not None
                    and not (caller_roles & _APPROVER_ROLES.get(role_prefix, set()))):
                continue
            try:
                queue_resp = await request.app.state.http.get(
                    f"{settings.register_base_url.rstrip('/')}{queue_path}",
                    headers={"X-API-Key": settings.register_api_key,
                             "X-Tenant": tenant})
            except (httpx.HTTPError, AttributeError):
                continue  # register briefly unreachable — the Temporal kinds still serve
            if queue_resp.status_code != 200:
                continue
            for row in queue_resp.json():
                lending_id = str(row.get("lending_id") or "")
                # The checker gets the WHOLE triad, never approve alone: a queue that
                # offers one verb reads as "approve or ignore", which is not the
                # governance the platform actually enforces. One base path per kind,
                # the three verbs hang off it.
                if k == "cpcs-checklist":
                    base = f"/v1/workflows/cpcs-checklists/{row.get('id')}"
                    extra = {"checklist_id": row.get("id"),
                             "checklist_version": row.get("checklist_version")}
                elif k == "cam-report":
                    base = f"/v1/workflows/cam-reports/{row.get('id')}"
                    extra = {"report_id": row.get("id"),
                             "checklist_version": row.get("report_version")}
                else:
                    base = f"/v1/workflows/advaya-handover/{lending_id}"
                    extra = {"package_id": row.get("id")}
                pending.append({
                    "kind": k, "subject_id": lending_id, "workflow_id": None,
                    "status": row.get("status"),
                    "stage": "Awaiting checker approval",
                    "requested_by": row.get("prepared_by"),
                    "started_at": row.get("created_at"),
                    "approve_url": f"{base}/approve",
                    "return_url": f"{base}/return",
                    "reject_url": f"{base}/reject", **extra})
        # REMINDERS — not decisions. The register computes what must keep being raised
        # (CS conditions still open after CP approval; covenant cycles due on disbursed
        # lines) and Today shows them until the work lands. No verbs: there is nothing
        # to approve here, only documents to chase and compliance to record.
        _REMINDER_KINDS = {"cs-followup", "covenant-due"}
        # The RM (BDRM) or the analyst CALLS the borrower and collects the documents;
        # the reminder must reach both, plus the credit line that owns the covenant —
        # and the SERVICING desk: post-handover the LMS Operator runs the monthly
        # chase (call, collect, record) and LMS Management oversees it, until closure.
        _REMINDER_ROLES = {"BDRM", "Deal Analyst", "Credit Head", "Management", "Admin",
                           "LMS Operator", "LMS Management"}
        # WHO sees the whole tenant's chases vs their OWN BOOK: the desks that run
        # whole books (credit seniors, the servicing pair) are unscoped; an IC's
        # reminders are the lines they prepared or that name them as RM/analyst —
        # Today says "on your book" and must mean it.
        _WHOLE_BOOK_REMINDER_ROLES = {"Credit Head", "Management", "Admin",
                                      "LMS Operator", "LMS Management"}
        if (kind is None or kind in _REMINDER_KINDS) and (
                caller_roles is None or caller_roles & _REMINDER_ROLES):
            reg_headers = {"X-API-Key": settings.register_api_key, "X-Tenant": tenant}
            base_url = settings.register_base_url.rstrip("/")
            try:
                # The poll IS the cron: mint any covenant observations newly due (the
                # sweep is idempotent — replaying is a no-op) so each open PERIOD gets
                # its own reminder, closeable by recording the received document.
                with contextlib.suppress(httpx.HTTPError):
                    await request.app.state.http.post(
                        f"{base_url}/v1/internal/covenants/run-sweep",
                        json={"horizon_days": 35}, headers=reg_headers)
                fu_params: dict[str, str] = {}
                if (caller_roles is not None and _who
                        and not (caller_roles & _WHOLE_BOOK_REMINDER_ROLES)):
                    fu_params["scope_email"] = _who
                # A PURELY-servicing caller sees the serviced book, whole — but not
                # LOS's pre-handover chases: their world starts at the handover.
                _LMS_ONLY = {"LMS Operator", "LMS Management"}
                if (caller_roles is not None and caller_roles
                        and caller_roles <= _LMS_ONLY):
                    fu_params["serviced_only"] = "true"
                fu = await request.app.state.http.get(
                    f"{base_url}/v1/internal/follow-ups", headers=reg_headers,
                    params=fu_params)
                for row in (fu.json().get("items", []) if fu.status_code == 200 else []):
                    if kind is not None and row.get("kind") != kind:
                        continue
                    company = row.get("company") or ""
                    if row.get("kind") == "cs-followup":
                        stage_txt = (f"{row.get('count')} condition(s) outstanding"
                                     + (f" · {company}" if company else "")
                                     + " — chase the documents")
                    else:
                        stage_txt = (f"Covenant '{row.get('name')}' "
                                     + ("OVERDUE since " if row.get("overdue") else "due ")
                                     + str(row.get("due_on") or "")
                                     + (f" · {company}" if company else "")
                                     + " — call the borrower for the documents")
                    pending.append({
                        "kind": row["kind"],
                        "subject_id": row.get("lending_id") or row.get("entity_id") or "",
                        "workflow_id": None, "status": "Reminder", "stage": stage_txt,
                        "requested_by": row.get("prepared_by") or "",
                        "started_at": row.get("created_at") or row.get("due_on") or "",
                        # What the client needs to CLOSE the cycle in place.
                        **({"monitoring_id": row["monitoring_id"]}
                           if row.get("monitoring_id") else {}),
                        **({"metric": row["metric"]} if row.get("metric") else {}),
                        **({"covenant_name": row["name"]} if row.get("name") else {})})
            except (httpx.HTTPError, AttributeError, ValueError):
                pass   # reminders are additive; the decision queue still serves

        pending.sort(key=lambda r: r["started_at"] or "")
        # Always say WHAT the list was scoped to. An empty queue then distinguishes
        # "nothing is waiting" from "you hold no approver role" — the second one is a
        # provisioning problem the approver can act on, and it used to be invisible.
        out: dict[str, Any] = {"count": len(pending), "pending": pending}
        if caller_roles is not None:
            # The role prefix comes from the Temporal map for parked runs and from the
            # queue map for the register-sourced checker kinds.
            prefixes = {k: _LOOKUP_KINDS[k][0] for k in _PENDING_TYPES}
            prefixes.update({k: role for k, (_p, role) in _PENDING_REGISTER_QUEUES.items()})
            approver_kinds = sorted(
                k for k, prefix in prefixes.items()
                if caller_roles & _APPROVER_ROLES.get(prefix, set()))
            out["scoped_to"] = {"email": _who, "roles": sorted(caller_roles),
                                "approver_for": approver_kinds}
            if not approver_kinds:
                out["note"] = (
                    f"{_who or 'This identity'} holds no approver role in this tenant "
                    f"(roles: {sorted(caller_roles) or 'none'}), so nothing can appear "
                    "here. An Admin grants approval authority in Access.")
        return out

    @app.get("/v1/workflows", tags=["Workflows"],
             summary="Find a subject's runs (newest attempt first) — clients never build ids")
    async def find_workflows(kind: str, subject_id: str, request: Request,
                             x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                             ) -> Any:
        """DISCOVERY for UIs: given a business subject ("this lead's conversion"), return
        every attempt — the base id plus any ``-r2, -r3, …`` retries — each with its live
        status and ready-made action URLs. The id-construction and retry-suffix rules live
        HERE, server-side, so no client ever encodes them. ``current`` is the newest
        attempt (the only one a decision can still land on when RUNNING)."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        _who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if _auth_enforced() and not _who:
            return _problem(401, "Unauthorized",
                            "A verified identity is required to look up workflow runs.")
        if (entry := _LOOKUP_KINDS.get(kind)) is None:
            return _problem(422, "Validation failed",
                            f"Unknown kind '{kind}'; one of: "
                            f"{', '.join(sorted(_LOOKUP_KINDS))}.")
        prefix, decide = entry
        tenant = (request.headers.get("X-Tenant") or settings.register_tenant).strip()
        base = f"{prefix}-{_tenant_slug(tenant)}-{subject_id}"
        client: Client = request.app.state.temporal
        runs: list[dict[str, Any]] = []
        newest_desc: Any = None
        # Attempts are strictly sequential (base, -r2, -r3, …) with no gaps — the start
        # path guarantees it — so scan until the first miss. The cap is a safety bound,
        # far above any plausible retry count.
        for n in range(1, 51):
            candidate = base if n == 1 else f"{base}-r{n}"
            handle = client.get_workflow_handle(candidate)
            try:
                desc = await handle.describe()
            except RPCError:
                break
            row: dict[str, Any] = {
                "workflow_id": candidate,
                "run_id": desc.run_id,
                "status": desc.status.name if desc.status else "UNKNOWN",
                "started_at": desc.start_time.isoformat() if desc.start_time else None,
                "closed_at": desc.close_time.isoformat() if desc.close_time else None,
                **_action_urls(decide, candidate, subject_id),
            }
            if desc.status == WorkflowExecutionStatus.RUNNING:
                with contextlib.suppress(RPCError, TemporalError):
                    row["stage"] = await handle.query("status")
                # The business state — how a client learns a run was RETURNED (the
                # technical stage doesn't change when it is).
                with contextlib.suppress(RPCError, TemporalError):
                    st = await handle.query("state")
                    if isinstance(st, dict):
                        row["business_status"] = str(st.get("business_status") or "")
            runs.append(row)
            newest_desc = desc
        # Same read protection as the per-run status route, applied to the newest
        # attempt: its initiator or an approver-role holder may look the subject up.
        if runs and _auth_enforced():
            scope_err = await _status_scope_denied(
                request, runs[-1]["workflow_id"], newest_desc, _who)
            if scope_err is not None:
                return scope_err
        runs.reverse()
        return {"kind": kind, "subject_id": subject_id, "count": len(runs),
                "current": runs[0] if runs else None, "runs": runs}

    @app.get("/v1/workflows/{workflow_id}", tags=["Workflows"],
             summary="A run's live status (execution state + in-workflow stage)")
    async def describe(workflow_id: str, request: Request,
                       x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                       ) -> Any:
        if (resp := denied(x_api_key)) is not None:
            return resp
        # A run's status/result requires a VERIFIED identity (not just the shared
        # orchestrator key) and is readable ONLY within its own tenant. (Subject/assignment
        # level scoping is a further refinement tracked separately.)
        _who, err = await _verified_email(request, "")
        if err is not None:
            return err
        if _auth_enforced() and not _who:
            return _problem(401, "Unauthorized",
                            "A verified identity is required to read a workflow's status.")
        if (resp := _wf_tenant_denied(request, workflow_id)) is not None:
            return resp
        client: Client = request.app.state.temporal
        handle = client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            return _problem(404, "Not found", f"Workflow '{workflow_id}': {exc.message}")
        # SUBJECT SCOPE: under the production posture, only the INITIATOR (recorded in the
        # workflow memo at start) or an APPROVER-role holder for this vertical may read the
        # run — not any same-tenant caller who merely knows the id.
        if _auth_enforced():
            scope_err = await _status_scope_denied(request, workflow_id, desc, _who)
            if scope_err is not None:
                return scope_err
        out: dict[str, Any] = {
            "workflow_id": workflow_id,
            "run_id": desc.run_id,
            "status": desc.status.name if desc.status else "UNKNOWN",
            "workflow_type": desc.workflow_type,
            "started_at": desc.start_time.isoformat() if desc.start_time else None,
            "closed_at": desc.close_time.isoformat() if desc.close_time else None,
        }
        # The in-workflow stage query only answers while the run is open.
        if desc.status == WorkflowExecutionStatus.RUNNING:
            try:
                out["stage"] = await handle.query("status")
            except (RPCError, TemporalError):
                pass
            # Business status and technical stage, SEPARATELY (workflows that expose the
            # richer `state` query; older workflow types simply omit it).
            try:
                out["state"] = await handle.query("state")
            except (RPCError, TemporalError):
                pass
        elif desc.status == WorkflowExecutionStatus.COMPLETED:
            out["result"] = await handle.result()
        elif desc.status in (WorkflowExecutionStatus.FAILED,
                             WorkflowExecutionStatus.TIMED_OUT,
                             WorkflowExecutionStatus.TERMINATED):
            # WHY it failed, not just THAT it failed. A bare "FAILED" is a dead end for
            # whoever raised the request: they see the run stop and have nowhere to look.
            # Unwrap the cause chain the way the ?wait=true path does, so the message
            # names the real refusal ("Lead … has no entity_id — link it to a company").
            try:
                await handle.result()
            except Exception as exc:  # noqa: BLE001 - reporting, never re-raised
                chain: list[str] = []
                cur: BaseException | None = exc
                while cur is not None and len(chain) < 4:
                    msg = str(cur).strip() or cur.__class__.__name__
                    if msg not in chain:
                        chain.append(msg)
                    cur = cur.__cause__
                out["failure"] = " <- ".join(chain)
        return out

    @app.post("/v1/decisions/waiver", status_code=201, tags=["Decisions"],
              summary="Record a covenant-waiver decision (verified senior credit authority)")
    async def record_waiver_decision(payload: WaiverDecisionIn, request: Request,
                                     x_api_key: str | None = Header(default=None,
                                                                    alias="X-API-Key"),
                                     ) -> Any:
        """The waiver DECISION through the front door. The Register's single-winner
        decision store accepts writes only from the workflow service principal carrying a
        verified approver identity — so the human records it here: the caller is verified
        (bearer under require_auth; header trust in dev) and this service persists the
        decision under its principal with the approver's delegated, route-bound context.
        ``/v1/monitoring/{id}/waive`` then verifies that record — authority, subject
        binding, validity window — before any breach is excused."""
        if (resp := denied(x_api_key)) is not None:
            return resp
        decided_by, err = await _verified_email(request, payload.by)
        if err is not None:
            return err
        caller, verified = _caller_context(request, decided_by)
        if settings.internal_signing_secret and not verified:
            return _problem(403, "Forbidden",
                            "A verified, route-bound caller identity is required to "
                            "record a waiver decision.")
        extra = {"kind": "waiver", "subject_type": "Monitoring",
                 "subject_id": payload.subject_id, "valid_days": payload.valid_days}
        if settings.internal_signing_secret:
            record, perr = await _persist_decision(
                request, payload.reference, payload.decision, decided_by,
                payload.note or None, caller, None, extra=extra)
            if perr is not None:
                return perr
            return ORJSONResponse(status_code=201, content=record)
        # Dev (no signing): the Register's svc lane accepts header identity — write
        # directly so the decision row exists for /waive to verify, same as prod.
        body = {"workflow_id": payload.reference, "decision": payload.decision,
                "note": payload.note or None, **extra}
        try:
            resp2 = await request.app.state.http.post(
                f"{settings.register_base_url.rstrip('/')}/v1/internal/decisions",
                json={k: v for k, v in body.items() if v is not None},
                headers={"X-API-Key": settings.register_api_key,
                         "X-Tenant": caller.tenant,
                         "X-User-Email": decided_by,
                         "X-User-Roles": request.headers.get("X-User-Roles", "")})
        except httpx.HTTPError as exc:
            return _problem(502, "Upstream unavailable",
                            f"Could not record the decision (Register: {exc}).")
        if resp2.status_code == 409:
            return _problem(409, "Conflict",
                            "A different decision has already been recorded for this "
                            "reference; it cannot be changed.")
        if resp2.status_code >= 300:
            try:
                detail = (resp2.json().get("error") or {}).get("detail") or ""
            except ValueError:
                detail = ""
            status = resp2.status_code if resp2.status_code in (403, 422) else 502
            return _problem(status, "Register refused the decision",
                            detail or f"Register answered {resp2.status_code}.")
        return ORJSONResponse(status_code=201, content=resp2.json())

    # The CAM workbench (docs/LENDING_WORKFLOW_DESIGN.md §B) — the analyst's drafting
    # loop over the register's cam_reports lifecycle. Mounted last: it reuses the same
    # front-door key, verified identity and signed-context helpers as every other lane.
    from app.cam import mount_cam
    mount_cam(app, settings, denied=denied, verified_email=_verified_email,
              caller_context=_caller_context, problem=_problem)

    return app


app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    s = get_settings()
    uvicorn.run("app.api:app", host=s.api_host, port=s.api_port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
