"""The COMPILED BASELINE of the approved ATLAS RBAC spec (v3.1) — a versioned reference.

AUTHORITY MODEL (what decides a production request):
  ATLAS (``ATLAS_RBAC_v3.1.xlsx``) is the approved DESIGN-TIME policy. PostgreSQL
  (``access_grants``, in the Access service) is the RUNTIME authority for human access.
  Access resolves it once per user, the Gateway issues a short-lived SIGNED authorization
  context carrying the live effective permissions, and downstream services verify and
  enforce THAT context. This file NEVER decides a production user request.

What this compiled baseline is for:
  * the explicit, versioned SEED (``python -m app.seed`` in the Access service) — insert
    missing baseline cells, provenance-tagged, never overwriting runtime overrides;
  * the DRIFT REPORT (``python -m app.seed --check``) — compare the live matrix against
    this approved version without writing anything;
  * DEV/legacy evaluation only, where requests arrive with bare identity headers and no
    signed context (never the case behind the production gateway posture).

Code (not the database) retains the non-editable pieces: the role/operation CATALOG
(``rbac_catalog.py`` — incl. ``POLICY_VERSION``), SERVICE-PRINCIPAL capabilities
(``service_policy.py``), LIFECYCLE policy (``lifecycle.py``) and the evaluation
algorithms (``engine.py`` per service). Access symbols map as:

    ✓ Full → FULL · ✎/◑ Scoped → SCOPED · 👁 Read → READ · ⚡ → APPROVE · — → NONE
"""

from __future__ import annotations



# --------------------------------------------------------------------------------------
# Compatibility re-exports. The catalog, service-principal and lifecycle policies now
# live in their own modules; every historical import site (`from evam_backend_core.rbac
# import STAGE_VOCAB`) keeps working unchanged through these re-exports.
# --------------------------------------------------------------------------------------
from evam_backend_core.lifecycle import (  # noqa: E402, F401
    ALLOWED_TRANSITIONS,
    DEAL_FUNNEL_STAGES,
    INITIAL_STATUS,
    ROW_LOCKS,
    STAGE_VOCAB,
    initial_status_error,
    stage_vocab_error,
    transition_error,
)
from evam_backend_core.rbac_catalog import (  # noqa: E402, F401
    POLICY_VERSION,
    ROLE_ALIASES,
    ROLES,
    Access,
    _ROLE_ORDER,
    canonical_roles,
)
from evam_backend_core.service_policy import (  # noqa: E402, F401
    SERVICE_GRANTS,
    SERVICE_READ_GRANTS,
)


def _row(cells: str) -> dict[str, Access]:
    """Build one matrix row from 12 space-separated symbols in _ROLE_ORDER order.
    Symbols: F=Full, S=Scoped, R=Read, A=Approve, -=None."""
    m = {"F": Access.FULL, "S": Access.SCOPED, "R": Access.READ, "A": Access.APPROVE,
         "-": Access.NONE}
    parts = cells.split()
    assert len(parts) == 12, cells
    return {role: m[sym] for role, sym in zip(_ROLE_ORDER, parts, strict=True)}


# ---- View Access sheet (column order: Admin Mgmt BDHead BDRM CreditHead DealAnalyst
#      SynHead SynRM AMHead AMRM) --------------------------------------------------
VIEW_ACCESS: dict[str, dict[str, Access]] = {
    "today":              _row("F F S S S S S S S S S S"),
    "dashboard":          _row("F F S S S S S S S S S S"),
    "leads":              _row("F F F S - - S S S S - -"),
    "deals":              _row("F F F S S S S S S S - -"),
    # LMS pair: whole-book READ (servicing is not assignment-scoped) — but the view
    # stops at read. Their writes are the SERVICING VERBS below (ledger, bookings,
    # covenants, classification), never the origination row itself: v3.6 closed the
    # gap where view-FULL let a generic line edit through outside the LOS screen.
    "lending":            _row("F F F S F S R R R R R R"),
    "syndication":        _row("F F F S S S F S R R - -"),
    "asset_monetisation": _row("F F F S S S R R F S - -"),
    "fi_master":          _row("F F F R S S F S R R - -"),
    "clients":            _row("F F F S R R S S S S R R"),
    "employees":          _row("F F R R R R R R R R - -"),
    "audit":              _row("F - - - - - - - - - - -"),
    "activity_log":       _row("F - - - - - - - - - - -"),
    "tools":              _row("F R R R R R R R R R - -"),
}

# ---- Operations sheet -------------------------------------------------------------
OPERATIONS: dict[str, dict[str, Access]] = {
    "sign_in":                        _row("F F F F F F F F F F F F"),
    "add_lead":                       _row("F F F F - - - - - - - -"),
    "edit_lead":                      _row("F F F S - - - - - - - -"),
    "reassign_lead":                  _row("F F F - - - - - - - - -"),
    "push_lead_to_deals":             _row("F F F S - - - - - - - -"),
    "create_client":                  _row("F F F S - - - - - - - -"),
    # Editing a company profile / its client-view records (contracts, intel, monitoring).
    # Mirrors the clients-view WRITE capability: FULL/SCOPED roles write, the READ-only
    # roles (Credit Head, Deal Analyst) may NOT — so a read-only viewer cannot PATCH.
    "edit_client":                    _row("F F F S - - S S S S - -"),
    "edit_contract":                  _row("F F F S - - S S S S - -"),
    "edit_intel":                     _row("F F F S - - S S S S - -"),
    "edit_monitoring":                _row("F F F S - - S S S S - -"),
    "edit_deal_profile":              _row("F F F S S S S S S S - -"),
    "edit_deal_ownership":            _row("F F F - - - S - S - - -"),
    "add_product_line":               _row("F F F S F - S - S - - -"),
    "add_company_note":               _row("F F F S F S S S S S - -"),
    "assign_analyst_lending":         _row("F F - - F - - - - - - -"),
    "assign_analyst_syndication":     _row("F F - - F - - - - - - -"),
    "assign_analyst_am":              _row("F F - - F - - - - - - -"),
    "change_lending_stage":           _row("F F F - F S - - - - - -"),
    "edit_lending_line":              _row("F F F S F S - - - - - -"),
    "assign_syn_rm":                  _row("F F F - - - F - - - - -"),
    "edit_syndication_line":          _row("F F F S F S F S - - - -"),
    "add_lender_to_mandate":          _row("F F F - - - F S - - - -"),
    "log_chase":                      _row("F F F - F S F S - - - -"),
    "log_response":                   _row("F F F - F S F S - - - -"),
    "advance_matrix_cell":            _row("F F F - F S F S - - - -"),
    "assign_am_rm":                   _row("F F F - - - - - F - - -"),
    "edit_am_record":                 _row("F F F S F S - - F S - -"),
    "log_interaction":                _row("F F F S F S F S F S - -"),
    "edit_fi_record":                 _row("F F F - - - F - - - - -"),
    "edit_employee":                  _row("F F - - - - - - - - - -"),
    "add_employee_assign_role":       _row("F F - - - - - - - - - -"),
    # Directory/reference maintenance (counterparties = banks; the document checklist =
    # config). Read is broad (see the tools view); mutation is restricted here.
    "manage_counterparty":            _row("F F - - F - F - - - - -"),
    "manage_checklist":               _row("F F - - - - - - - - - -"),
    "upload_remove_documents":        _row("F F F S F S S S S S - -"),
    "snooze_today_item":              _row("F F F S S S S S S S - -"),
    "delete_row":                     _row("F - - - - - - - - - - -"),
    "request_stage_change":           _row("- - F S S S S S S S - -"),
    "approve_stage_change":           _row("A A A - A - A - A - - -"),
    # Governance-evidence attachment, gated BY KIND (see evam_backend_core.evidence). These are the
    # authorities that may file each class of governance evidence — NOT any identified caller. A
    # committee outcome / sanction letter is reserved to the credit authority (Credit Head +
    # Management + Admin) and the designated workflow service; an RM/Analyst holds NONE of these.
    "attach_committee_evidence":      _row("F F - - F - - - - - - -"),
    "attach_sanction_evidence":       _row("F F - - F - - - - - - -"),
    # Executed documents — the document/OCR authority (mirrors upload_remove_documents' writers).
    "attach_document_evidence":       _row("F F F S F S S S S S - -"),
    # Lead qualification review — the BD authority.
    "attach_qualification_evidence":  _row("F F F S - - - - - - - -"),
    # Syndication mandate artefacts + sanction evidence (IM versions, allocation, the
    # syndication sanction record) — the syndication desk's authority, senior-gated.
    #                                        Adm Mgt BDH BDR CrH DA  SyH SyR AMH AMR
    "attach_syndication_evidence":    _row("F F - - - - F S - - - -"),
    # Asset-monetisation mandate artefacts + closure approval (teaser versions, NDA /
    # data-room records, offers, the closure decision's evidence) — the AM desk's
    # authority, senior-gated.
    "attach_am_evidence":             _row("F F - - - - - - F S - -"),
    # Advaya disbursement acknowledgement — recorded by the Advaya-handoff workflow service ONLY
    # under an enabled Advaya integration (default OFF; not in svc_workflows' baseline grant).
    "attach_advaya_evidence":         _row("F F - - - - - - - - - -"),
    # CP/CS checklist maker-checker: a maker prepares/completes the authoritative checklist; a
    # DIFFERENT senior credit authority approves it. Only an approved checklist can mint the
    # cp_cs_completion evidence (verified, not caller-attached).
    "prepare_cpcs_checklist":         _row("F F - - F S - - - - - -"),
    "approve_cpcs_checklist":         _row("F F - - F - - - - - - -"),
    # Advaya handover maker-checker: a maker PREPARES the package; a DIFFERENT checker APPROVES it,
    # which advances the line to 'Disbursed'. Both are senior credit authority.
    "initiate_advaya_handover":       _row("F F - - F - - - - - - -"),
    # The ANALYST sends the disbursement request (the CP approval already gated the
    # money movement) — so Deal Analyst holds this alongside the credit seniors.
    "record_handover_package":        _row("F F - - F F - - - - - -"),
    "approve_advaya_handover":        _row("F F - - F - - - - - - -"),
    "export_csv":                     _row("F F F F F F F F F F - -"),
    "backup_restore":                 _row("F - - - - - - - - - - -"),
    "run_news_scan":                  _row("F F F F F F F F F F - -"),
    # Increment 8 — covenant governance + early-warning surveillance.
    # Covenant DEFINITIONS (the schedule/thresholds) and covenant RESULTS are credit
    # governance: Credit Head owns them, the Deal Analyst works them scoped, Admin/Mgmt
    # oversee. RM desks neither define nor test covenants.
    "manage_covenants":               _row("F F - - F S - - - - F F"),
    # LMS servicing (maker/checker): the OPERATOR posts routine ledger events (EMI
    # receipts, computed interest accruals, charges); the AUTHORIZER holds the
    # hard-to-reverse verbs — classification (Standard/SMA/NPA), provisioning, closure.
    # v3.8: booking APPROVAL is the SERVICING desk's check, not credit's. The Credit
    # Head still RECORDS the manual attestation (the maker side of the seam) but can no
    # longer settle a booking — origination must not both create and book the exposure.
    # Admin/Management keep the oversight override.
    "record_ledger_entry":            _row("F F - - F S - - - - F F"),
    "authorize_loan_account":         _row("F F - - - - - - - - - F"),
    # EWS cases: the credit desk owns surveillance (Credit Head FULL); every RM desk can
    # OPEN and work a case on its own book (scoped) — a field RM spotting distress must
    # never be blocked from raising the flag.
    "manage_ews":                     _row("F F S S F S S S S S - -"),
}

# ---- Ownership Model sheet --------------------------------------------------------
# Any unassigned line's owner defaults to its vertical Head — deals never sit ownerless.
DEFAULT_LINE_OWNER: dict[str, str] = {
    "Lending": "Credit Head",
    "Syndication": "Syn Head",
    "AssetMonetisation": "AM Head",
}

# Which assignment roles may be placed on which line, and who has the authority to do it.
# (Credit Head owns the Deal Analyst pool — including cross-assignment to Syn/AM lines,
# NEW in v2.1; each vertical Head assigns their own RM; Mgmt/Admin can always override.)
ASSIGNMENT_AUTHORITY: dict[tuple[str, str], set[str]] = {
    ("Lending", "Deal Analyst"):          {"Credit Head", "Management", "Admin"},
    ("Syndication", "Deal Analyst"):      {"Credit Head", "Management", "Admin"},
    ("AssetMonetisation", "Deal Analyst"): {"Credit Head", "Management", "Admin"},
    ("Syndication", "Syn RM"):            {"Syn Head", "BD Head", "Management", "Admin"},
    ("AssetMonetisation", "AM RM"):       {"AM Head", "BD Head", "Management", "Admin"},
    ("Lead", "BDRM"):                     {"BD Head", "Management", "Admin"},
    ("Deal", "BDRM"):                     {"BD Head", "Management", "Admin"},
}

# Approval routing: who (besides Admin/Management) approves a change on which line.
APPROVER_FOR_SUBJECT: dict[str, set[str]] = {
    "Lending":           {"Credit Head", "Admin", "Management"},
    "Syndication":       {"Syn Head", "Admin", "Management"},
    "AssetMonetisation": {"AM Head", "Admin", "Management"},
    "Lead":              {"BD Head", "Admin", "Management"},
    "Deal":              {"BD Head", "Admin", "Management"},
}

# Assignment role expected per line for the "primary owner". Doubly operational:
# creating a line while holding this role AUTO-ASSIGNS the creator (spec: "BDRM
# automatically owns a newly created lead"), and unassigned lines default to the
# vertical Head (DEFAULT_LINE_OWNER above).
PRIMARY_ASSIGNMENT_ROLE: dict[str, str] = {
    "Lending": "Deal Analyst",
    "Syndication": "Syn RM",
    "AssetMonetisation": "AM RM",
    "Lead": "BDRM",
    "Deal": "BDRM",
}

# Which operation gates EDITING (PATCH) each line resource — used to bind a machine caller
# to its service allowlist on writes (a read-only service like svc_atlas can edit nothing).
WRITE_OPERATION_FOR_SUBJECT: dict[str, str] = {
    "Entity": "edit_client",
    "Lead": "edit_lead",
    "Deal": "edit_deal_profile",
    "Lending": "edit_lending_line",
    "Syndication": "edit_syndication_line",
    "AssetMonetisation": "edit_am_record",
}

# Which operation gates CREATING each line resource (the Register's fallback check —
# the gateway maps the same routes to the same operations at the front door).
CREATE_OPERATION_FOR_SUBJECT: dict[str, str] = {
    "Entity": "create_client",
    "Lead": "add_lead",
    "Deal": "push_lead_to_deals",
    "Lending": "add_product_line",
    "Syndication": "add_product_line",
    "AssetMonetisation": "add_product_line",
}


def policy_fingerprint() -> str:
    """SHA-256 over the compiled baseline (version + both matrices) — stamped on seeds,
    drift reports and audit events, so "which policy produced this grant?" is answerable."""
    import hashlib
    import json
    payload = {
        "version": POLICY_VERSION,
        "views": {v: {r: a.name for r, a in row.items()} for v, row in VIEW_ACCESS.items()},
        "operations": {o: {r: a.name for r, a in row.items()} for o, row in OPERATIONS.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
