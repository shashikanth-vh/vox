"""Default Data Register checklist template (Evam's standard document requirements).

Seeded per-tenant into ``document_checklist`` so a fresh Register renders the same
checklist ATLAS shows: 6 sections, 24 slots, 17 of them required. Configurable afterwards
via ``/v1/document-checklist``. Idempotent: upserts on (tenant, applies_to, slot_key).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocumentChecklistItem

# (section, section_order, slot_key, label, is_required)
DOCUMENT_CHECKLIST: list[tuple[str, int, str, str, bool]] = [
    # 1 — KYC & Constitutional
    ("KYC & Constitutional", 1, "coi", "Certificate of Incorporation", True),
    ("KYC & Constitutional", 1, "moa_aoa", "MOA & AOA", True),
    ("KYC & Constitutional", 1, "company_pan", "Company PAN", True),
    ("KYC & Constitutional", 1, "gst_registration", "GST Registration", True),
    ("KYC & Constitutional", 1, "shareholding_pattern", "Shareholding pattern", True),
    ("KYC & Constitutional", 1, "promoter_director_kyc", "Promoter / Director KYC", True),
    # 2 — Financials
    ("Financials", 2, "audited_financials", "Audited financials — last 3 FYs", True),
    ("Financials", 2, "provisional_financials", "Provisional financials — current FY", True),
    ("Financials", 2, "itr_acknowledgements", "ITR acknowledgements — 3 years", True),
    ("Financials", 2, "projections_cma", "Projections / CMA data", True),
    # 3 — Banking & Debt
    ("Banking & Debt", 3, "bank_statements", "Bank statements — 12 months", True),
    ("Banking & Debt", 3, "loan_sanction_letters", "Existing loan sanction letters", True),
    ("Banking & Debt", 3, "loan_outstanding_soa", "Loan outstanding / SOA", True),
    ("Banking & Debt", 3, "repayment_track_record", "Repayment track record", False),
    # 4 — Compliance & Bureau
    ("Compliance & Bureau", 4, "gst_returns", "GST returns — 12 months", True),
    ("Compliance & Bureau", 4, "cibil_consent", "CIBIL consent letter", True),
    ("Compliance & Bureau", 4, "statutory_dues", "Statutory dues confirmation", False),
    # 5 — Project & Technical
    ("Project & Technical", 5, "project_report_dpr", "Project report / DPR", True),
    ("Project & Technical", 5, "ppa_offtake", "PPA / offtake agreement", False),
    ("Project & Technical", 5, "land_documents", "Land documents", False),
    ("Project & Technical", 5, "environmental_clearances", "Environmental clearances", False),
    # 6 — Deal Documents
    ("Deal Documents", 6, "mandate_letter", "Mandate letter", True),
    ("Deal Documents", 6, "information_memorandum", "Information Memorandum", False),
    ("Deal Documents", 6, "term_sheet", "Term sheet", False),
]


async def seed_document_checklist(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert any missing default checklist items for this tenant. Returns how many added."""
    existing = {
        key
        for (key,) in (
            await session.execute(
                select(DocumentChecklistItem.slot_key).where(
                    DocumentChecklistItem.tenant_id == tenant_id,
                    DocumentChecklistItem.applies_to == "*",
                )
            )
        ).all()
    }
    added = 0
    for sort_order, (section, section_order, slot_key, label, required) in enumerate(
        DOCUMENT_CHECKLIST
    ):
        if slot_key in existing:
            continue
        session.add(
            DocumentChecklistItem(
                tenant_id=tenant_id, applies_to="*", section=section,
                section_order=section_order, slot_key=slot_key, label=label,
                is_required=required, sort_order=sort_order,
                created_by="seed", updated_by="seed",
            )
        )
        added += 1
    await session.flush()
    return added
