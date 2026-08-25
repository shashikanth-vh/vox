"""LIFECYCLE policy — stage vocabularies, entry allowlists and ordered transitions.

Business-lifecycle policy for every subject (Lead funnel, Deal ORIGINATION FUNNEL,
Lending credit pipeline, Syndication mobilisation, Asset-Monetisation sale). Kept as a
dedicated module: this is business policy, not RBAC — it lives beside the mandatory-
field / field-lock / evidence rules in ``policy.py``, which layers on top of it.
"""

from __future__ import annotations

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
#   Sanctioned → CP/CS Completed → Ready for Disbursement → Disbursed
# "Disbursed" is PRISM's TERMINAL (the Evam MIS's own word): CP/CS + the executed agreement are
# complete, the proposed drawdown amount/date are fixed, and the maker-checker handover approval
# recorded the drawdown. Interactively the terminal is reached only through that approval (or by
# a governed historical import); under a future Advaya integration the dormant
# advaya_acknowledgement evidence kind lets the acknowledgement be verified against the
# downstream system instead of asserted.
_LENDING_STAGES = ("Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
                   "CP/CS Completed", "Ready for Disbursement", "Disbursed",
                   "Rejected", "On Hold")
_SYN_STATUSES = ("Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
                 "Queries Received", "IP Received", "Sanctioned", "Disbursed", "On Hold",
                 "Withdrawn", "Rejected", "Dropped")
_AM_STATUSES = ("Teaser Prepared", "Teaser Shared", "In Discussion", "NBO Received",
                "BO Received", "SPA / Documentation", "Closed", "Dropped")

# THE business stage of a Deal — the ORIGINATION FUNNEL, the Evam MIS's own vocabulary
# (deals.stage, verbatim from the Deals sheet). The two-layer model: a DEAL answers "how
# good is our origination?" (the commercial funnel: Inquiry → Screening → In Pipeline →
# Won/Lost — CRM layer), while the Indian bank/NBFC CREDIT lifecycle ("where is this
# exposure in the approval chain?") lives on the LENDING TRACKER line, the syndication
# lifecycle on the syndication tracker, and the asset-sale lifecycle on the AM tracker.
# The structuring workflow, evidence gates and committee governance are bound to the
# LENDING line's credit stages — a deal-level credit stage is DEPRECATED (the historical
# values are parked in deals.credit_stage_legacy until a later removal migration).
#
# THREE terminals, not two, because the desk's own ledger keeps three and the difference is
# the point of measuring a funnel at all:
#   Screened Out — never entered the pipeline; the screen did its job.
#   Dropped      — EVAM walked away from a deal it could have had. A judgement call.
#   Closed Lost  — Evam wanted the deal and did not get it. A competitive outcome.
# Collapsing Dropped into Closed Lost would answer "how many did we not close?" while making
# "and how many of those were our own decision?" unanswerable — which is the question a head
# of origination actually asks.
DEAL_FUNNEL_STAGES = ("New Inquiry", "In Screening", "In Pipeline", "On Hold",
                      "Screened Out", "Closed Won", "Closed Lost", "Dropped")

STAGE_VOCAB: dict[str, tuple[str, frozenset[str]]] = {
    "Lead":              ("status", frozenset(_LEAD_STATUSES)),
    "Deal":              ("stage",  frozenset(DEAL_FUNNEL_STAGES)),
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
    # A deal is BORN somewhere in the working funnel (an RM may well first log a deal already
    # in screening, or one committed straight into the pipeline — e.g. a lead conversion). The
    # funnel TERMINALS (Screened Out / Closed Won / Closed Lost / Dropped) are outcomes, never
    # a birth state.
    "Deal":              ("stage",  frozenset({"New Inquiry", "In Screening", "In Pipeline",
                                               "On Hold"})),
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

# The LENDING credit pipeline (a deal's credit lifecycle is DEPRECATED — credit execution is
# governed on the lending tracker line):
#   Data Awaited → Diligence → Note Circulated → Sanctioned → CP/CS Completed
#   → Ready for Disbursement → Disbursed (TERMINAL for the current product scope)
_CREDIT_PIPELINE: dict[str, set[str]] = {
    "Data Awaited":    {"Diligence", "On Hold", "Rejected"},
    "Diligence":       {"Note Circulated", "Data Awaited", "On Hold", "Rejected"},
    "Note Circulated": {"Sanctioned", "Diligence", "On Hold", "Rejected"},
    # Post-sanction, the conditions precedent / subsequent and the executed agreement are worked
    # to completion before the facility is prepared for disbursement. The CP approval alone
    # UNBLOCKS disbursement: 'Ready for Disbursement' is reachable straight from 'Sanctioned' —
    # gated by the cp_cs_completion evidence (policy.EVIDENCE_STAGE_GATES) and the proposed
    # drawdown fields — so the CS chase runs in parallel with the money. 'CP/CS Completed'
    # remains the milestone that closes BOTH halves.
    "Sanctioned":      {"CP/CS Completed", "Ready for Disbursement", "Note Circulated", "On Hold"},
    "CP/CS Completed": {"Ready for Disbursement", "Sanctioned", "On Hold"},
    # 'Ready for Disbursement' is the internal finalisation (proposed amount/date fixed); the
    # move to 'Disbursed' is senior-locked (ROW_LOCKS) and normally made by the maker-checker
    # handover approval.
    "Ready for Disbursement": {"Disbursed", "CP/CS Completed", "On Hold"},
    # 'Disbursed' is TERMINAL for the current product scope (see rbac header + FOUNDATION_SPEC §11).
    "Disbursed": {"On Hold"},
    "On Hold":         {"Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
                        "CP/CS Completed", "Ready for Disbursement", "Disbursed"},
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
    # The deal's COMMERCIAL funnel: forward one step, back one step for rework, On Hold and
    # resume anywhere working, and a screened-out deal may be re-opened into screening. The
    # CLOSED terminals are final (a revived opportunity is a NEW deal — the funnel measures
    # conversion, and resurrecting a closed row would silently rewrite history).
    ("Deal", "stage"): {
        # 'Dropped' — Evam walking away — is reachable from every WORKING stage, because the
        # decision can be taken the moment the desk learns whatever it learns. It is not
        # reachable from 'Screened Out': a deal that never entered the pipeline was not one
        # Evam walked away from, it was one the screen stopped.
        "New Inquiry":  {"In Screening", "Screened Out", "On Hold", "Closed Lost", "Dropped"},
        "In Screening": {"In Pipeline", "New Inquiry", "Screened Out", "On Hold", "Closed Lost",
                         "Dropped"},
        "In Pipeline":  {"Closed Won", "Closed Lost", "In Screening", "On Hold", "Dropped"},
        "On Hold":      {"New Inquiry", "In Screening", "In Pipeline", "Screened Out",
                         "Closed Lost", "Dropped"},
        "Screened Out": {"In Screening", "On Hold"},
        "Closed Won":   set(),
        "Closed Lost":  set(),
        # Final, like the other closed terminals: a deal Evam walked away from and later
        # revives is a NEW deal. Reopening this row would silently rewrite the conversion
        # history the funnel exists to measure.
        "Dropped":      set(),
    },
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
                {"Ready for Disbursement", "Disbursed"},
                {"Admin", "Management", "Credit Head"}),
}
