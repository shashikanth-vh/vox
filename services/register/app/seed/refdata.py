"""Reference-vocabulary seed data.

THE source of every dropdown in the UI: front-ends fetch these from the Register's
``/v1/ref`` rather than shipping their own copy, so a vocabulary change is a data change
and not a redeploy of the browser bundle.

Aligned with **ATLAS Forms & Validations v2.1** ("Reference lists"), including the fixes
that sheet calls out — the overlapping Tenor buckets, the duplicated Line of Lending
entry, and the Counterparty Type split into Lender Type / Investor Type.

TWO DELIBERATE DIVERGENCES from that sheet, both because PRISM enforces more than the
sheet describes:

* **Lending Stage.** The sheet lists "Documentation"; PRISM's credit pipeline names the
  post-sanction milestones for the governed work that actually happens —
  ``Sanctioned -> CP/CS Completed -> Ready for Disbursement -> Disbursed``. This
  vocabulary is ENFORCED (``evam_backend_core.lifecycle``) and cross-checked by a test,
  so it is not a free list; changing it changes what the platform will accept.
* **Entity Lifecycle.** The sheet's "Vistaar Journey" is carried as its own category;
  ``entities.lifecycle`` keeps the values already written to customer data.

NAMES ARE NEVER SEEDED HERE. The sheet is explicit — "Employees table drives role-based
lists (BDRM / Deal Analyst / Syn RM / AM RM / Heads). Do NOT hardcode names in the
frontend." ``GET /v1/ref`` merges those lists in live from the people directory; see
``app.api.custom.list_ref``.
"""

from __future__ import annotations

REF_VALUES: dict[str, list[str]] = {
    "Sector": [
        "Solar - EPC", "Solar - Developer", "Solar - Rooftop", "Solar - OEM",
        "Solar - General", "BESS / Energy Storage", "EV Mobility",
        "Biofuels / Biogas / CBG", "Industrial Decarbonisation", "Industrial Water",
        "Water Treatment / WASH", "Climate Data & IoT", "Industrial Efficiency",
        "Agri / Drone", "Other",
    ],
    "Lens": ["Mitigation", "Adaptation"],
    "Temperature": ["Hot", "Warm", "Cold"],
    "Priority": ["High", "Medium", "Low"],
    "Lead Status": ["Active", "Converted", "Dropped"],
    "Source": ["BDRM", "DSA", "Inbound", "Referral", "Event", "Other"],
    "Product Type": [
        "Term Loan", "Working Capital", "Purchase Order Finance", "Mezzanine",
        "Co-lending", "Syndication", "Asset Monetisation Advisory",
        "OEM Bundle Equipment Finance",
    ],
    "Register Status": ["Pipeline", "Sanctioned", "Rejected", "Market Intelligence"],
    # The client RELATIONSHIP journey (ATLAS "Vistaar journey") — the vocabulary the
    # prototype's Lifecycle dropdown uses, adopted verbatim as entities.lifecycle.
    "Entity Lifecycle": ["Prospect", "Onboarded", "Active", "Serviced",
                         "Vistaar — Expansion", "Dormant"],
    "Entity Type": ["Company", "Promoter", "Director", "Related Party"],
    # v2.1 "Employee.Role" — mandatory on an employee record and the thing RBAC keys on.
    # Same catalogue as "RBAC Role"; role stacking (multi-select) is allowed.
    "Person Role": [
        "Admin", "Management", "BD Head", "BDRM", "Credit Head", "Deal Analyst",
        "Syn Head", "Syn RM", "AM Head", "AM RM",
    ],
    # The Deal ORIGINATION-FUNNEL vocabulary (verbatim Evam MIS terms) — a separate
    # dimension from the credit pipeline below; see rbac.DEAL_FUNNEL_STAGES.
    "Deal Funnel Stage": [
        "New Inquiry", "In Screening", "In Pipeline", "On Hold",
        # Three terminals: the screen stopped it, Evam walked away, or Evam lost it.
        "Screened Out", "Closed Won", "Closed Lost", "Dropped",
    ],
    "Lending Stage": [
        "Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
        "CP/CS Completed", "Ready for Disbursement", "Disbursed",
        "Rejected", "On Hold",
    ],
    "Status of Proposal": [
        "Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
        "Queries Received", "IP Received", "Sanctioned", "Disbursed",
        "On Hold", "Withdrawn", "Rejected", "Dropped",
    ],
    "Asset Mon Status": [
        "Teaser Prepared", "Teaser Shared", "In Discussion", "NBO Received",
        "BO Received", "SPA / Documentation", "Closed", "Dropped",
    ],
    # v2.1 SPLIT the single "Counterparty Type" in two. Both are seeded; the union stays
    # for the counterparties table, whose rows predate the split.
    "Lender Type": ["Bank", "NBFC", "DFI", "AIF / Fund", "Multilateral", "Other"],
    "Investor Type": [
        "Strategic", "Financial Investor", "Family Office", "Corporate", "Advisor", "Other",
    ],
    "Counterparty Type": [
        "Bank", "NBFC", "DFI", "AIF/Fund", "Investor", "Strategic", "Advisor", "Other",
    ],
    # v2.1 EXPANDED to the parties a real file actually waits on.
    "Pending With": [
        "Client", "Lender", "BDRM", "Deal Analyst", "Syn RM", "AM RM",
        "Credit Committee", "Legal", "CFO",
    ],
    # v2.1 FIX: the old buckets overlapped (3-36m and 12-36m both covered 12-36 months),
    # so the same tenor could be filed under either.
    "Tenor": ["<12m", "12-24m", "24-36m", "36-60m", ">60m"],
    # v2.1 FIX: "Referral, Syndication" was a combined value masquerading as a third one.
    "Line of Lending": ["Referral", "Syndication"],
    "Mandate Status": [
        "To be sent by Evam", "Pending with Client", "In-principle approval from client",
        "Sent - pending signature", "Executed", "Not required",
    ],
    "IM in Place": ["Work not started", "In prep", "In place"],
    "Interaction Type": [
        "In-Person Meeting", "Virtual Meeting / Video Call", "Phone Call",
        "WhatsApp / Text Message", "Email / Written Correspondence",
        "Site Visit / Due Diligence", "Management Presentation",
        "Term Sheet Negotiation", "Internal Review / Credit Committee",
    ],
    "Statement Type": [
        "Audited", "Provisional", "Projection", "Bank Statement Extract",
        "GST Return", "Account Aggregator Pull", "CMA Spread",
    ],
    "Asset Type": ["PPA", "EPC", "O&M", "Offtake", "Project SPV", "Security Charge", "Other"],
    "Intel Type": [
        "CIBIL", "Account Aggregator", "MCA", "Probe42", "GST", "PULSE",
        "Court Case", "Sector Benchmark", "Comparable Deal", "News",
    ],
    "Signal": ["RED", "AMBER", "GREEN"],
    "Monitoring Record Type": [
        "Covenant Compliance", "Security Creation", "Periodic Submission",
        "Behavioural Score", "MIS Snapshot", "Report Register",
    ],
    # Syndication dropdowns that previously had backing fields but no vocabulary.
    "Syndication Type": [
        "Fee will be paid by customer", "Fee to be collected from lender",
    ],
    "Mandate Status 3": ["Yes", "No", "Under discussion"],
    "Yes/No": ["Yes", "No"],
    "Terminal (Lending)": ["Disbursed", "Rejected", "On Hold"],
    # v2.1 "Vistaar Journey" — Evam's own name for the client relationship lifecycle.
    # Reporting vocabulary; entities.lifecycle keeps its own (enforced) values.
    "Vistaar Journey": ["Prospect", "Engaged", "Documented", "Under Review",
                        "Committed", "Live", "Wound Down"],
    # NO "RM" / "Analyst" NAME LISTS. They were seeded with the prototype's five people,
    # so every deployment offered names its own people table had never heard of and the
    # mismatch only surfaced at conversion ("Unknown rm 'Shubh' — not a person on
    # record"). /v1/ref now merges these in from the people directory, by role.
    # Backing for the new financials / covenant fields.
    "Financial Section": ["P&L", "Balance Sheet", "Cash Flow", "Ratios"],
    "Scale": ["Absolute", "Thousand", "Lakh", "Million", "Crore"],
    "Waiver Status": ["None", "Requested", "Granted", "Rejected", "Expired"],
    "Covenant Frequency": ["OneTime", "Monthly", "Quarterly", "SemiAnnual", "Annual"],
    "EWS Severity": ["Amber", "Red"],
    "EWS Status": ["Open", "UnderInvestigation", "Escalated", "Closed"],
    "EWS Disposition": ["Resolved", "Downgraded", "FalseAlarm", "LossMitigated",
                        "Restructured"],
    # Data Register (documents) dropdowns.
    "Document Section": [
        "KYC & Constitutional", "Financials", "Banking & Debt",
        "Compliance & Bureau", "Project & Technical", "Deal Documents",
    ],
    "Document Status": ["On File", "Pending", "Verified", "Waived", "Rejected",
                        "Superseded", "Expired"],
    "Calendar Event Status": ["Scheduled", "Completed", "Cancelled"],
    "Notification Severity": ["info", "warning", "critical"],
    # RBAC (ATLAS RBAC v3.1) — role catalogue + assignment capacities.
    "RBAC Role": [
        "Admin", "Management", "BD Head", "Credit Head", "Syn Head", "AM Head",
        "BDRM", "Deal Analyst", "Syn RM", "AM RM",
    ],
    "Assignment Role": ["BDRM", "Deal Analyst", "Syn RM", "AM RM"],
    "Request Status": ["Pending", "Approved", "Rejected", "Cancelled"],
}
