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
    # Deletes — documents FIRST: removing a file from the Data Register is desk
    # housekeeping under upload_remove_documents (the Register adds the Verified
    # guard), not the Admin-only row purge. Without this carve-out the blanket
    # rule below stopped a Management remove at the door and the Register's own
    # delete gate never saw the request.
    ("DELETE", r"^/v1/documents/[^/]+$", "upload_remove_documents"),
    # Deletes — any other resource ("Delete a row — IRREVERSIBLE — Admin ONLY").
    ("DELETE", r"^/v1/(?!users|assignments|requests|documents)[^/]+/[^/]+$", "delete_row"),
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

    # ---- Capability routes on the FRONTED services (prefix included, since the gate runs
    # before the gateway strips the prefix). The binary gate rejects NONE at the door; each
    # backend still enforces its own final authorization (defence in depth). --------------
    # VocX — field touchpoint capture is an interaction write.
    ("POST",   r"^/vocx/v1/touchpoints$", "log_interaction"),
    # VOX conversations (the spec build) — recording, consenting, editing and approving
    # a conversation are all interaction writes; the register still enforces
    # recorder-or-authority on each row (defence in depth). Erasure is the
    # irreversible one, so it rides the delete gate (Admin).
    ("POST",   r"^/v1/vox/(conversations|consents)$", "log_interaction"),
    ("POST",   r"^/v1/vox/conversations/[^/]+/(edits|approve)$", "log_interaction"),
    # Erase is LIFECYCLE-dependent — a recorder deletes their own draft, only Admin
    # erases an approved record — and the split lives in the register handler, which
    # sees the row. Classifying it delete_row here 403'd every draft delete at the
    # gate before the register could rule, so it travels as log_interaction and the
    # register stays the enforcement point.
    ("POST",   r"^/v1/vox/conversations/[^/]+/erase$", "log_interaction"),
    ("POST",   r"^/v1/vox/conversations/[^/]+/regenerate$", "log_interaction"),
    ("POST",   r"^/vocx/v1/vox/process$", "log_interaction"),
    ("POST",   r"^/vocx/v1/vox/follow_up$", "log_interaction"),
    # Streamed capture: appending audio, finishing and discarding a take are all
    # interaction writes; the GET routes forward with identity, enforced at vocx.
    ("POST",   r"^/vocx/v1/vox/stream(/finish|/discard)?$", "log_interaction"),
    # PULSE — news radar: triggering a scan / filing items is the intel-scan capability.
    ("POST",   r"^/pulse/v1/scan$", "run_news_scan"),
    ("POST",   r"^/pulse/v1/items$", "run_news_scan"),
    # The DESK-FACING half of the radar — searching a name, emailing a digest, and the
    # recurring schedules — is the same capability. Without these lines `operation_for`
    # returns None and the gate does not run at all: any signed-in user could email a
    # digest to any address, or leave a recurring one pointed there. Searching is
    # harmless; sending mail as the firm is not, and both live behind the operation the
    # matrix already grants for exactly this feature.
    ("GET",    r"^/pulse/v1/news/(search|config|schedules)$", "run_news_scan"),
    ("POST",   r"^/pulse/v1/news/(email|email-digest|email-test)$", "run_news_scan"),
    ("POST",   r"^/pulse/v1/news/schedules(/(delete|run))?$", "run_news_scan"),
    # Orchestrator — starting/deciding workflows maps to the same operation the applied
    # change requires, so an unauthorized user is stopped before a durable workflow starts.
    ("POST",   r"^/orchestrator/v1/workflows/vox-touchpoints$", "log_interaction"),
    ("POST",   r"^/orchestrator/v1/workflows/lead-conversions$", "push_lead_to_deals"),
    ("POST",   r"^/orchestrator/v1/workflows/[^/]+/(approve|reject)$", "approve_stage_change"),
    # Business-lifecycle workflows: starting each maps to the capability its work exercises;
    # the Credit Committee decision maps to the stage-approval capability. The orchestrator still
    # re-checks fresh committee authority via Access before it persists/signals (defence in depth).
    ("POST",   r"^/orchestrator/v1/workflows/lead-qualifications$", "edit_lead"),
    ("POST",   r"^/orchestrator/v1/workflows/deal-structurings$", "change_lending_stage"),
    ("POST",   r"^/orchestrator/v1/workflows/document-collections$", "upload_remove_documents"),
    # Advaya handover — a money-movement authorization; the orchestrator re-checks Credit Head /
    # Management / Admin authority via Access before it starts the workflow (defence in depth).
    ("POST",   r"^/orchestrator/v1/workflows/advaya-handover$", "initiate_advaya_handover"),
    ("POST",   r"^/orchestrator/v1/workflows/advaya-handover/[^/]+/approve$", "approve_advaya_handover"),
    # CP/CS authoritative checklist maker-checker (business-facing via the orchestrator).
    ("POST",   r"^/orchestrator/v1/workflows/cpcs-checklists$", "prepare_cpcs_checklist"),
    ("POST",   r"^/orchestrator/v1/workflows/cpcs-checklists/[^/]+/approve$", "approve_cpcs_checklist"),
    ("POST",   r"^/orchestrator/v1/workflows/[^/]+/committee-decision$", "approve_stage_change"),
    ("POST",   r"^/orchestrator/v1/workflows/[^/]+/document-received$", "upload_remove_documents"),
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
