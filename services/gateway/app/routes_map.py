"""Route → operation map: which RBAC operation each Register route exercises.

The binary gate looks an incoming (method, path) up here and checks the user's cached
access for that operation — NONE is rejected at the gateway, FULL/SCOPED forward with a
decision header. A route NOT in this map forwards with identity headers but no decision;
the Register then applies its own checks (scoped write enforcement, delete gate, and the
authority checks on assignments/requests). Grow this map as routes are classified —
unmapped is safe (enforced downstream), mapped is fast (rejected at the door).
"""

from __future__ import annotations

import re

# (HTTP method, compiled path regex) → operation key from the matrix.
_RAW: list[tuple[str, str, str]] = [
    # Deletes — any resource ("Delete a row — IRREVERSIBLE — Admin ONLY").
    ("DELETE", r"^/v1/(?!users|assignments|requests)[^/]+/[^/]+$", "delete_row"),
    # Leads.
    ("POST",   r"^/v1/leads$", "add_lead"),
    ("PATCH",  r"^/v1/leads/[^/]+$", "edit_lead"),
    # Deals.
    ("PATCH",  r"^/v1/deals/[^/]+$", "edit_deal_profile"),
    # Lines.
    ("PATCH",  r"^/v1/lending/[^/]+$", "edit_lending_line"),
    ("PATCH",  r"^/v1/syndication/[^/]+$", "edit_syndication_line"),
    ("PATCH",  r"^/v1/asset-monetisation/[^/]+$", "edit_am_record"),
    ("POST",   r"^/v1/syndication/[^/]+/lenders$", "add_lender_to_mandate"),
    # Interactions (timeline + nested).
    ("POST",   r"^/v1/interactions$", "log_interaction"),
    ("POST",   r"^/v1/[^/]+/[^/]+/interactions$", "log_interaction"),
    # Documents (Data Register uploads).
    ("POST",   r"^/v1/documents(/upload)?$", "upload_remove_documents"),
    ("POST",   r"^/v1/[^/]+/[^/]+/documents(/upload)?$", "upload_remove_documents"),
    # Request → approve flow.
    ("POST",   r"^/v1/requests$", "request_stage_change"),
    ("POST",   r"^/v1/requests/[^/]+/(approve|reject)$", "approve_stage_change"),
    # Exports.
    ("GET",    r"^/v1/export/.*$", "export_csv"),
]

ROUTE_OPERATIONS: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), op) for method, pattern, op in _RAW
]


def operation_for(method: str, path: str) -> str | None:
    """The matrix operation this route exercises, or None (forward; enforce downstream)."""
    for m, rx, op in ROUTE_OPERATIONS:
        if m == method and rx.match(path):
            return op
    return None
