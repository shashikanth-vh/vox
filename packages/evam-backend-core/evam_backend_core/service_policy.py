"""SERVICE-PRINCIPAL capabilities — what each named machine identity may do.

Deliberately CODE, not database: a service's reach (which operations svc_workflows may
perform, which books svc_vox may read) is a security invariant of the architecture,
widened only through a reviewed pull request — never through a runtime admin surface.
"""

from __future__ import annotations

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
                      "attach_syndication_evidence", "attach_am_evidence",
                      "attach_document_evidence", "attach_qualification_evidence",
                      # CP/CS authoritative checklist maker-checker, and the Advaya handover
                      # (immutable package + stage advance). NOTE: attach_advaya_evidence is
                      # deliberately NOT here — it is granted only under an enabled Advaya
                      # integration (default off), so the dormant acknowledgement path is not
                      # executable in a normal deployment.
                      "prepare_cpcs_checklist", "approve_cpcs_checklist",
                      "record_handover_package", "approve_advaya_handover",
                      # Covenant sweep (recurring generation / overdue / waiver expiry)
                      # and EWS case plumbing (auto-escalation on a lapsed SLA).
                      "manage_covenants", "manage_ews"},
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
    # Beyond the entity/lead basics, VocX reads every book a touchpoint can LAND on:
    # deals for ref_type=Deal resolution, and lending / syndication / asset-monetisation
    # because an RM may log an interaction against any of those lines (the commit-time
    # log_to target). Read-only corpus context — the write is still only log_interaction.
    "svc_vox": {"/v1/entities", "/v1/leads", "/v1/people", "/v1/interactions",
                "/v1/deals", "/v1/lending", "/v1/syndication", "/v1/asset-monetisation"},
    "svc_pulse": {"/v1/entities", "/v1/external-intelligence"},
    "svc_workflows": {"/v1/entities", "/v1/leads", "/v1/deals", "/v1/lending",
                      "/v1/syndication", "/v1/asset-monetisation"},
    # Chitti — the chatbot VM — answers questions about anything in the book,
    # so it holds the WIDEST own-key read grant: every list resource. Two
    # lines still hold: the edge's machine window is GET-only (a write dies
    # at nginx, and svc_chitti has no entry in SERVICE_GRANTS, so it dies
    # again here), and the composite company capability below stays
    # human-delegated — this service included.
    "svc_chitti": {"/v1/entities", "/v1/leads", "/v1/people", "/v1/interactions",
                   "/v1/deals", "/v1/lending", "/v1/syndication",
                   "/v1/asset-monetisation", "/v1/syndication-lenders",
                   "/v1/financials", "/v1/contracts-assets", "/v1/counterparties",
                   "/v1/documents", "/v1/document-checklist",
                   "/v1/external-intelligence", "/v1/monitoring"},
}
# NOTE: the composite-company capability key ("company:composite") is deliberately in NO
# service's read grants — dossier/financial-history/timeline/documents/lender-matrix are
# reachable only by a DELEGATED (human) read, never an entity-matching service's own key.

