"""Reference-vocabulary seed data.

Mirrors the ATLAS ``ref`` object so front-ends fetch identical dropdowns from the
Register's ``/v1/ref`` endpoint.
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
    "Source": ["RM", "DSA", "OEM", "Inbound", "Referral", "Event", "Website", "Direct", "Other"],
    "Product Type": [
        "Term Loan", "Working Capital", "Purchase Order Finance", "Mezzanine",
        "Co-lending", "Syndication", "Asset Monetisation Advisory",
        "OEM Bundle Equipment Finance",
    ],
    "Register Status": ["Pipeline", "Sanctioned", "Rejected", "Market Intelligence"],
    "Entity Type": ["Company", "Promoter", "Director", "Related Party"],
    "Person Role": ["Admin", "Management", "RM", "Analyst", "Ops"],
    # The Deal ORIGINATION-FUNNEL vocabulary (verbatim Evam MIS terms) — a separate
    # dimension from the credit pipeline below; see rbac.DEAL_FUNNEL_STAGES.
    "Deal Funnel Stage": [
        "New Inquiry", "In Screening", "In Pipeline", "On Hold",
        "Screened Out", "Closed Won", "Closed Lost",
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
    "Counterparty Type": [
        "Bank", "NBFC", "DFI", "AIF/Fund", "Investor", "Strategic", "Advisor", "Other",
    ],
    "Pending With": ["Client", "Lender", "RM", "Analyst", "Ops"],
    "Tenor": ["<12m", "3-36m", "12-36m", "36-60m", ">60m"],
    "Line of Lending": ["Referral", "Syndication", "Referral, Syndication"],
    "Mandate Status": [
        "To be sent by Evam", "Pending with Client", "In-principle approval from client",
        "Sent - pending signature", "Executed", "Not required",
    ],
    "IM in Place": ["Work not started", "In prep", "In place"],
    "Interaction Type": [
        "In-Person Meeting", "Virtual Meeting / Video Call", "Phone Call",
        "WhatsApp / Text message", "Email / Written Correspondence",
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
    # Generic + role pickers the ATLAS UI expects from /v1/ref (RM/Analyst can also be
    # served live from /v1/people?role=…; both are supported).
    "Yes/No": ["Yes", "No"],
    "Terminal (Lending)": ["Disbursed", "Rejected", "On Hold"],
    "RM": ["Chetan", "Shubh"],
    "Analyst": ["Archana", "Prateek", "Bhavana", "Nirmala", "Grishma"],
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
