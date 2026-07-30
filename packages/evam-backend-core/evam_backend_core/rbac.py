"""The ATLAS RBAC spec (v3.1) encoded verbatim — the PLATFORM policy artifact.

This is the single shared definition of roles, view access and operations that every
PRISM service reads: the Access service seeds its admin-editable tables from it, the
Gateway uses it as the compiled fallback, and the Register uses it for re-verification
and scoped enforcement.

Source of truth: ``ATLAS_RBAC_v3.1.xlsx`` (sheets: Legend & Roles, View Access,
Operations, Field Rules, Ownership Model). Keep this file a faithful transcription; the
*evaluation* logic lives in ``engine.py``. Access symbols map as:

    ✓ Full → FULL · ✎/◑ Scoped → SCOPED · 👁 Read → READ · ⚡ → APPROVE · — → NONE
"""

from __future__ import annotations

from enum import IntEnum


class Access(IntEnum):
    """Ordered so that role stacking = max() across held roles."""

    NONE = 0
    READ = 1
    SCOPED = 2   # read-write on rows in the user's own scope (book / vertical / assignment)
    FULL = 3     # read + write, no scope restriction within the module
    APPROVE = 4  # not a data write — an approve/reject decision (operations matrix only)


# The 10 catalogue roles (tier, vertical) — spec "Legend & Roles".
ROLES: dict[str, dict[str, str]] = {
    "Admin":       {"tier": "Leadership", "vertical": "System"},
    "Management":  {"tier": "Leadership", "vertical": "All"},
    "BD Head":     {"tier": "Head",       "vertical": "BD"},
    "Credit Head": {"tier": "Head",       "vertical": "Credit"},
    "Syn Head":    {"tier": "Head",       "vertical": "Syndication"},
    "AM Head":     {"tier": "Head",       "vertical": "Asset Monetisation"},
    "BDRM":        {"tier": "IC",         "vertical": "BD"},
    "Deal Analyst": {"tier": "IC",        "vertical": "Credit"},
    "Syn RM":      {"tier": "IC",         "vertical": "Syndication"},
    "AM RM":       {"tier": "IC",         "vertical": "Asset Monetisation"},
}

_ROLE_ORDER = ["Admin", "Management", "BD Head", "BDRM", "Credit Head", "Deal Analyst",
               "Syn Head", "Syn RM", "AM Head", "AM RM"]


def _row(cells: str) -> dict[str, Access]:
    """Build one matrix row from 10 space-separated symbols in _ROLE_ORDER order.
    Symbols: F=Full, S=Scoped, R=Read, A=Approve, -=None."""
    m = {"F": Access.FULL, "S": Access.SCOPED, "R": Access.READ, "A": Access.APPROVE,
         "-": Access.NONE}
    parts = cells.split()
    assert len(parts) == 10, cells
    return {role: m[sym] for role, sym in zip(_ROLE_ORDER, parts, strict=True)}


# ---- View Access sheet (column order: Admin Mgmt BDHead BDRM CreditHead DealAnalyst
#      SynHead SynRM AMHead AMRM) --------------------------------------------------
VIEW_ACCESS: dict[str, dict[str, Access]] = {
    "today":              _row("F F S S S S S S S S"),
    "dashboard":          _row("F F S S S S S S S S"),
    "leads":              _row("F F F S - - S S S S"),
    "deals":              _row("F F F S S S S S S S"),
    "lending":            _row("F F F S F S R R R R"),
    "syndication":        _row("F F F S S S F S R R"),
    "asset_monetisation": _row("F F F S S S R R F S"),
    "fi_master":          _row("F F F R S S F S R R"),
    "clients":            _row("F F F S R R S S S S"),
    "employees":          _row("F F R R R R R R R R"),
    "audit":              _row("F - - - - - - - - -"),
    "activity_log":       _row("F - - - - - - - - -"),
    "tools":              _row("F R R R R R R R R R"),
}

# ---- Operations sheet -------------------------------------------------------------
OPERATIONS: dict[str, dict[str, Access]] = {
    "sign_in":                        _row("F F F F F F F F F F"),
    "add_lead":                       _row("F F F F - - - - - -"),
    "edit_lead":                      _row("F F F S - - - - - -"),
    "reassign_lead":                  _row("F F F - - - - - - -"),
    "push_lead_to_deals":             _row("F F F S - - - - - -"),
    "create_client":                  _row("F F F S - - - - - -"),
    # Editing a company profile / its client-view records (contracts, intel, monitoring).
    # Mirrors the clients-view WRITE capability: FULL/SCOPED roles write, the READ-only
    # roles (Credit Head, Deal Analyst) may NOT — so a read-only viewer cannot PATCH.
    "edit_client":                    _row("F F F S - - S S S S"),
    "edit_contract":                  _row("F F F S - - S S S S"),
    "edit_intel":                     _row("F F F S - - S S S S"),
    "edit_monitoring":                _row("F F F S - - S S S S"),
    "edit_deal_profile":              _row("F F F S S S S S S S"),
    "edit_deal_ownership":            _row("F F F - - - S - S -"),
    "add_product_line":               _row("F F F S F - S - S -"),
    "add_company_note":               _row("F F F S F S S S S S"),
    "assign_analyst_lending":         _row("F F - - F - - - - -"),
    "assign_analyst_syndication":     _row("F F - - F - - - - -"),
    "assign_analyst_am":              _row("F F - - F - - - - -"),
    "change_lending_stage":           _row("F F F - F S - - - -"),
    "edit_lending_line":              _row("F F F S F S - - - -"),
    "assign_syn_rm":                  _row("F F F - - - F - - -"),
    "edit_syndication_line":          _row("F F F S F S F S - -"),
    "add_lender_to_mandate":          _row("F F F - - - F S - -"),
    "log_chase":                      _row("F F F - F S F S - -"),
    "log_response":                   _row("F F F - F S F S - -"),
    "advance_matrix_cell":            _row("F F F - F S F S - -"),
    "assign_am_rm":                   _row("F F F - - - - - F -"),
    "edit_am_record":                 _row("F F F S F S - - F S"),
    "log_interaction":                _row("F F F S F S F S F S"),
    "edit_fi_record":                 _row("F F F - - - F - - -"),
    "edit_employee":                  _row("F F - - - - - - - -"),
    "add_employee_assign_role":       _row("F F - - - - - - - -"),
    # Directory/reference maintenance (counterparties = banks; the document checklist =
    # config). Read is broad (see the tools view); mutation is restricted here.
    "manage_counterparty":            _row("F F - - F - F - - -"),
    "manage_checklist":               _row("F F - - - - - - - -"),
    "upload_remove_documents":        _row("F F F S F S S S S S"),
    "snooze_today_item":              _row("F F F S S S S S S S"),
    "delete_row":                     _row("F - - - - - - - - -"),
    "request_stage_change":           _row("- - F S S S S S S S"),
    "approve_stage_change":           _row("A A A - A - A - A -"),
    # Governance-evidence attachment, gated BY KIND (see evam_backend_core.evidence). These are the
    # authorities that may file each class of governance evidence — NOT any identified caller. A
    # committee outcome / sanction letter is reserved to the credit authority (Credit Head +
    # Management + Admin) and the designated workflow service; an RM/Analyst holds NONE of these.
    "attach_committee_evidence":      _row("F F - - F - - - - -"),
    "attach_sanction_evidence":       _row("F F - - F - - - - -"),
    # Executed documents — the document/OCR authority (mirrors upload_remove_documents' writers).
    "attach_document_evidence":       _row("F F F S F S S S S S"),
    # Lead qualification review — the BD authority.
    "attach_qualification_evidence":  _row("F F F S - - - - - -"),
    # Advaya disbursement acknowledgement — recorded by the Advaya-handoff workflow service ONLY
    # under an enabled Advaya integration (default OFF; not in svc_workflows' baseline grant).
    "attach_advaya_evidence":         _row("F F - - - - - - - -"),
    # CP/CS checklist maker-checker: a maker prepares/completes the authoritative checklist; a
    # DIFFERENT senior credit authority approves it. Only an approved checklist can mint the
    # cp_cs_completion evidence (verified, not caller-attached).
    "prepare_cpcs_checklist":         _row("F F - - F S - - - -"),
    "approve_cpcs_checklist":         _row("F F - - F - - - - -"),
    # Advaya handover maker-checker: a maker PREPARES the package; a DIFFERENT checker APPROVES it,
    # which advances the line to 'Handed Over to Advaya'. Both are senior credit authority.
    "initiate_advaya_handover":       _row("F F - - F - - - - -"),
    "record_handover_package":        _row("F F - - F - - - - -"),
    "approve_advaya_handover":        _row("F F - - F - - - - -"),
    "export_csv":                     _row("F F F F F F F F F F"),
    "backup_restore":                 _row("F - - - - - - - - -"),
    "run_news_scan":                  _row("F F F F F F F F F F"),
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

# Service principals — a machine caller authenticated by a NAMED service API key may only
# perform the operations on its allowlist (least privilege), regardless of enforce_rbac. A
# generic/unnamed API key keeps the legacy compatibility behaviour (governed by enforce_rbac).
SERVICE_GRANTS: dict[str, set[str]] = {
    "svc_pulse":     {"run_news_scan", "edit_intel"},
    "svc_vox":       {"create_client", "add_lead", "edit_lead", "log_interaction",
                      "add_company_note", "add_employee_assign_role"},
    "svc_workflows": {"create_client", "add_lead", "edit_lead", "push_lead_to_deals",
                      "add_product_line", "log_interaction", "add_employee_assign_role",
                      "add_company_note",
                      # The governance workflows (qualification / structuring / document
                      # collection) file the evidence their milestones require — as the DESIGNATED
                      # service, bound to the authoritative workflow/decision at attach time.
                      "attach_committee_evidence", "attach_sanction_evidence",
                      "attach_document_evidence", "attach_qualification_evidence",
                      # CP/CS authoritative checklist maker-checker, and the Advaya handover
                      # (immutable package + stage advance). NOTE: attach_advaya_evidence is
                      # deliberately NOT here — it is granted only under an enabled Advaya
                      # integration (default off), so the dormant acknowledgement path is not
                      # executable in a normal deployment.
                      "prepare_cpcs_checklist", "approve_cpcs_checklist",
                      "record_handover_package", "approve_advaya_handover"},
    "svc_atlas":     set(),  # read-only BFF — no write operations
    # The gateway's OWN key is a pure delegation TRANSPORT: it carries no authority of its
    # own. Every gateway-forwarded request rides a signed USER context (production refuses
    # anonymous), and the user governs — so a stolen gateway key WITHOUT a context can
    # neither write (empty allowlist here) nor read (empty read grant below) anything.
    "svc_gateway":   set(),
}

# What each service may READ **on its own key alone** (no forwarded user context), keyed by
# the resource's URL prefix. This is DISTINCT from write grants: having a write grant no
# longer implies tenant-wide read of every table. A service that carries a signed USER
# context is a DELEGATE — its reads are governed by that user's view/row scope instead, so
# these own-key grants are the *floor* a service is trusted with by itself.
#   * svc_atlas: EMPTY — a pure BFF must always delegate (forward the user's context); its
#     own key never reads the data plane.
#   * svc_vox: the interaction/company-resolution context it needs to file captures.
#   * svc_pulse: the intelligence context it matches against and writes.
#   * svc_workflows: the deal/lead subjects a conversion workflow reads.
SERVICE_READ_GRANTS: dict[str, set[str]] = {
    "svc_atlas": set(),
    "svc_gateway": set(),  # pure delegation transport — reads only via a forwarded user
    "svc_vox": {"/v1/entities", "/v1/leads", "/v1/people", "/v1/interactions"},
    "svc_pulse": {"/v1/entities", "/v1/external-intelligence"},
    "svc_workflows": {"/v1/entities", "/v1/leads", "/v1/deals", "/v1/lending",
                      "/v1/syndication", "/v1/asset-monetisation"},
}
# NOTE: the composite-company capability key ("company:composite") is deliberately in NO
# service's read grants — dossier/financial-history/timeline/documents/lender-matrix are
# reachable only by a DELEGATED (human) read, never an entity-matching service's own key.

# --------------------------------------------------------------------------- #
# Lifecycle vocabularies — the AUTHORITATIVE stage/status values per product line.
# These mirror the ATLAS reference dropdowns (services/register .../seed/refdata.py
# REF_VALUES) served from /v1/ref; a register-side test cross-checks them so the two cannot
# drift. A lifecycle field may hold ONLY a value in its vocabulary — an unknown/free-text value
# is rejected on every interactive write path (create / PATCH / approval). subject → (field,
# frozenset of legal values).
# --------------------------------------------------------------------------- #
_LEAD_STATUSES = ("Active", "On Hold", "Dropped", "Converted")
# Deal and Lending share the credit pipeline vocabulary ("Lending Stage" in REF_VALUES).
# The post-sanction chain names each milestone for the real-world work it represents:
#   Sanctioned → CP/CS Completed → Ready for Disbursement → Handed Over to Advaya
# "Handed Over to Advaya" is PRISM's TERMINAL for the current product scope: CP/CS + the executed
# agreement are complete, the proposed drawdown amount/date are fixed, an immutable handover package
# is on file, and the facility has been handed to Advaya (the downstream loan-management system).
# PRISM does NOT advance a loan on its own authority past that point. The onward states
# ('Accepted by Advaya' → 'Disbursement Pending' → 'Disbursed') exist ONLY under a future Advaya
# integration and are added to the vocabulary only when that integration mode is enabled — so with
# no integration nothing can reach them and nothing is ever synthetically disbursed.
_LENDING_STAGES = ("Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
                   "CP/CS Completed", "Ready for Disbursement", "Handed Over to Advaya",
                   "Rejected", "On Hold")
_SYN_STATUSES = ("Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
                 "Queries Received", "IP Received", "Sanctioned", "Disbursed", "On Hold",
                 "Withdrawn", "Rejected", "Dropped")
_AM_STATUSES = ("Teaser Prepared", "Teaser Shared", "In Discussion", "NBO Received",
                "BO Received", "SPA / Documentation", "Closed", "Dropped")

STAGE_VOCAB: dict[str, tuple[str, frozenset[str]]] = {
    "Lead":              ("status", frozenset(_LEAD_STATUSES)),
    "Deal":              ("stage",  frozenset(_LENDING_STAGES)),
    "Lending":           ("stage",  frozenset(_LENDING_STAGES)),
    "Syndication":       ("status", frozenset(_SYN_STATUSES)),
    "AssetMonetisation": ("status", frozenset(_AM_STATUSES)),
}

# Valid CREATION states per subject — a resource may be born ONLY at a genuine ENTRY stage (the
# very start of its lifecycle). Every later stage — including working states like Note Circulated
# or IM Circulated, and every governance/terminal outcome — is reached only by stepping through
# the ordered graph below (or, for historical data, a separately-audited import). subject →
# (field, allowed initial values).
INITIAL_STATUS: dict[str, tuple[str, frozenset[str]]] = {
    "Lead":              ("status", frozenset({"Active", "On Hold", "Dropped"})),
    "Deal":              ("stage",  frozenset({"Data Awaited", "Diligence"})),
    "Lending":           ("stage",  frozenset({"Data Awaited", "Diligence"})),
    "Syndication":       ("status", frozenset({"Deal Sourced", "Docs Pending", "IM in Prep"})),
    "AssetMonetisation": ("status", frozenset({"Teaser Prepared", "Teaser Shared",
                                               "In Discussion"})),
}

# The ORDERED business lifecycle per product line — a step may only advance to the NEXT stage
# (no skipping document/diligence/appraisal/committee/sanction gates), step BACK one stage for
# refer-back/rework, go On Hold and resume, or move to a terminal outcome (Rejected/Withdrawn/
# Dropped). Reaching a governance stage (Sanctioned, Disbursed, Closed, …) still additionally
# requires that stage's mandatory data; the deeper "workflow-generated evidence" gates
# (document completeness, CIPHER appraisal, Credit Committee outcome, Advaya acknowledgement)
# are layered on by each product line's workflow as those workflows are built.

# Deal & Lending share the credit pipeline:
#   Data Awaited → Diligence → Note Circulated → Sanctioned → CP/CS Completed
#   → Ready for Disbursement → Handed Over to Advaya (TERMINAL for the current product scope)
_CREDIT_PIPELINE: dict[str, set[str]] = {
    "Data Awaited":    {"Diligence", "On Hold", "Rejected"},
    "Diligence":       {"Note Circulated", "Data Awaited", "On Hold", "Rejected"},
    "Note Circulated": {"Sanctioned", "Diligence", "On Hold", "Rejected"},
    # Post-sanction, the conditions precedent / subsequent and the executed agreement are worked
    # to completion before the facility is prepared for disbursement.
    "Sanctioned":      {"CP/CS Completed", "Note Circulated", "On Hold"},
    "CP/CS Completed": {"Ready for Disbursement", "Sanctioned", "On Hold"},
    # 'Ready for Disbursement' is the internal finalisation (proposed amount/date fixed); from there
    # the facility can only be handed OVER to Advaya — PRISM never self-disburses.
    "Ready for Disbursement": {"Handed Over to Advaya", "CP/CS Completed", "On Hold"},
    # 'Handed Over to Advaya' is TERMINAL: PRISM asserts nothing past it. Onward disbursement states
    # exist only under a future Advaya integration (see rbac header + FOUNDATION_SPEC §11).
    "Handed Over to Advaya": {"On Hold"},
    "On Hold":         {"Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
                        "CP/CS Completed", "Ready for Disbursement", "Handed Over to Advaya"},
    "Rejected":        {"Data Awaited", "Diligence"},   # refer-back / reopen
}

# Syndication mobilisation pipeline:
#   Deal Sourced → Docs Pending → IM in Prep → IM Circulated → Queries Received → IP Received
#   → Sanctioned → Disbursed
_SYN_PIPELINE: dict[str, set[str]] = {
    "Deal Sourced":     {"Docs Pending", "On Hold", "Withdrawn", "Rejected", "Dropped"},
    "Docs Pending":     {"IM in Prep", "Deal Sourced", "On Hold", "Withdrawn", "Rejected",
                         "Dropped"},
    "IM in Prep":       {"IM Circulated", "Docs Pending", "On Hold", "Withdrawn", "Rejected",
                         "Dropped"},
    "IM Circulated":    {"Queries Received", "IM in Prep", "On Hold", "Withdrawn", "Rejected",
                         "Dropped"},
    "Queries Received": {"IP Received", "IM Circulated", "On Hold", "Withdrawn", "Rejected",
                         "Dropped"},
    "IP Received":      {"Sanctioned", "Queries Received", "On Hold", "Withdrawn", "Rejected",
                         "Dropped"},
    "Sanctioned":       {"Disbursed", "On Hold"},
    "Disbursed":        {"On Hold"},
    "On Hold":          {"Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
                         "Queries Received", "IP Received", "Sanctioned"},
    "Withdrawn":        set(),
    "Rejected":         set(),
    "Dropped":          set(),
}

# Asset-monetisation pipeline:
#   Teaser Prepared → Teaser Shared → In Discussion → NBO Received → BO Received
#   → SPA / Documentation → Closed
_AM_PIPELINE: dict[str, set[str]] = {
    "Teaser Prepared":     {"Teaser Shared", "Dropped"},
    "Teaser Shared":       {"In Discussion", "Teaser Prepared", "Dropped"},
    "In Discussion":       {"NBO Received", "Teaser Shared", "Dropped"},
    "NBO Received":        {"BO Received", "In Discussion", "Dropped"},
    "BO Received":         {"SPA / Documentation", "NBO Received", "Dropped"},
    "SPA / Documentation": {"Closed", "BO Received", "Dropped"},
    "Closed":              set(),
    "Dropped":             set(),
}

# Allowed status/stage transitions per (subject_type, field). A source value maps to the set of
# targets reachable from it; a move not listed is rejected (422). Same-value (no-op) is always
# allowed, and a move from an UNSET (NULL) stage is an initial set — governed by the ENTRY-stage
# allowlist in the policy engine, not by this graph. Converting a Lead is deliberately absent — it
# must go through /convert.
ALLOWED_TRANSITIONS: dict[tuple[str, str], dict[str, set[str]]] = {
    ("Lead", "status"): {
        "Active":  {"Dropped", "On Hold"},
        "On Hold": {"Active", "Dropped"},
        "Dropped": {"Active"},
    },
    ("Deal", "stage"): dict(_CREDIT_PIPELINE),
    ("Lending", "stage"): dict(_CREDIT_PIPELINE),
    ("Syndication", "status"): dict(_SYN_PIPELINE),
    ("AssetMonetisation", "status"): dict(_AM_PIPELINE),
}


def stage_vocab_error(subject_type: str, data: dict) -> str | None:
    """Reject an UNKNOWN / free-text lifecycle value on ANY write path. If the change sets the
    subject's lifecycle field to a non-null value outside its authoritative vocabulary, return an
    error (else None). This closes the gap where an arbitrary string could be introduced at
    creation, or as the FIRST stage of a row whose stage was still NULL (which the transition
    graph exempts)."""
    rule = STAGE_VOCAB.get(subject_type)
    if rule is None:
        return None
    field, vocab = rule
    value = data.get(field)
    if value is not None and value not in vocab:
        return (f"{subject_type}.{field} has an unknown value {value!r}; it must be one of the "
                f"authoritative lifecycle values {sorted(vocab)}.")
    return None


def initial_status_error(subject_type: str, data: dict) -> str | None:
    """Reject an invalid INITIAL lifecycle state at creation (a Lead created as ``Converted``, a
    Lending line created as ``Disbursed``), so creation obeys the same lifecycle a later edit
    does. First rejects an unknown value (authoritative vocabulary), then enforces the
    per-subject creation ALLOWLIST (INITIAL_STATUS). Returns an error string when the create is
    forbidden, else None. A create that omits the field (the model default applies) passes."""
    verr = stage_vocab_error(subject_type, data)
    if verr is not None:
        return verr
    allow_rule = INITIAL_STATUS.get(subject_type)
    if allow_rule is not None:
        field, allowed = allow_rule
        value = data.get(field)
        if value is not None and value not in allowed:
            return (f"{subject_type}.{field} cannot be created as {value!r} — a resource is born "
                    f"only in a working state (allowed: {sorted(allowed)}); a governance or "
                    "terminal state is reached only through its workflow or an approved transition.")
    return None


def transition_error(subject_type: str, field: str,
                     from_value, to_value) -> str | None:
    """The SINGLE transition validator both the direct PATCH and the change-request
    approval path call, so they can never disagree. Returns an error string when the move
    is forbidden, else None.

    FAIL-CLOSED: when a transition graph exists for this (subject, field), an UNKNOWN
    current state (not a node in the graph — e.g. free-text or a value the workflow never
    defined) is rejected, not waved through. A same-value no-op is always allowed. A
    (subject, field) with no graph is unconstrained here (other gates still apply)."""
    if from_value == to_value:
        return None
    # An UNSET (NULL/empty) current value is not a transition — it is the FIRST time the stage
    # is set, which the creation rules (initial_status_error) and mandatory-field policy govern,
    # not the transition graph. Exempt it so a freshly created row can enter its first stage.
    if from_value in (None, ""):
        return None
    graph = ALLOWED_TRANSITIONS.get((subject_type, field))
    if graph is None:
        return None
    targets = graph.get(from_value)
    if targets is None:
        return (f"{subject_type}.{field} is in an unrecognised state {from_value!r}; "
                f"no transition to {to_value!r} is permitted from it.")
    if to_value not in targets:
        return (f"{subject_type}.{field} may not move {from_value!r} → {to_value!r} "
                f"(allowed: {sorted(targets) or ['<none>']}).")
    return None

# Field Rules sheet, first operational slice: ROW LOCKS. When a row's field holds one
# of the listed values, further edits require one of the listed roles. (The full
# per-field policy engine — mandatory fields, per-stage field locks — is layered on
# this same structure later.)
ROW_LOCKS: dict[str, tuple[str, set[str], set[str]]] = {
    # subject → (field, locking values, roles still allowed to edit)
    "Lead":    ("status", {"Converted"}, {"Admin", "Management", "BD Head"}),
    # Finalising for disbursement and handing a facility OVER to Advaya are the money-movement
    # authorization steps — senior credit authority only.
    "Lending": ("stage",
                {"Ready for Disbursement", "Handed Over to Advaya"},
                {"Admin", "Management", "Credit Head"}),
}
