"""The ATLAS RBAC spec (v3.1) encoded verbatim — roles, view access, operations.

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
    "upload_remove_documents":        _row("F F F S F S S S S S"),
    "snooze_today_item":              _row("F F F S S S S S S S"),
    "delete_row":                     _row("F - - - - - - - - -"),
    "request_stage_change":           _row("- - F S S S S S S S"),
    "approve_stage_change":           _row("A A A - A - A - A -"),
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

# Assignment role expected per line for the "primary owner" (used for defaulting/display).
PRIMARY_ASSIGNMENT_ROLE: dict[str, str] = {
    "Lending": "Deal Analyst",
    "Syndication": "Syn RM",
    "AssetMonetisation": "AM RM",
    "Lead": "BDRM",
    "Deal": "BDRM",
}
