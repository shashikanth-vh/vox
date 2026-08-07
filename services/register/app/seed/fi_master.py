"""Default FI Master — the lender counterparties every fresh Register starts with.

The Platform Deals build works a known market: the same ~37 banks and NBFCs appear
on mandate after mandate, so an empty lender master on day one just means someone
retypes the market by hand (with typos that then split the by-bank rollups). This
seeds that list once, as ordinary ``counterparties`` rows (``counterparty_type``
set), after which the runtime API owns them completely — add, edit, deactivate or
delete through ``/v1/counterparties`` like any other row.

Idempotency contract (the part that matters in production):

* A name already present — live OR soft-deleted — is never touched. Renames,
  reclassifications and deletions made at runtime survive every re-run of
  bootstrap; a deleted default does not resurrect.
* Only names this tenant has never seen are inserted, so extending the list below
  in a later release adds just the newcomers.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import Counterparty

# name -> counterparty_type. An explicit table (not name-pattern guessing) so the
# classification is reviewable and each entry independently correctable at runtime.
DEFAULT_FI_MASTER: dict[str, str] = {
    # Scheduled commercial banks
    "Axis Bank": "Bank",
    "Kotak Mahindra": "Bank",
    "ICICI Bank": "Bank",
    "HDFC Bank": "Bank",
    "SBI": "Bank",
    "Bank of Maharashtra": "Bank",
    "IndusInd Bank": "Bank",
    "Bandhan Bank": "Bank",
    "IDFC First": "Bank",
    "HSBC": "Bank",
    "Federal Bank": "Bank",
    "RBL Bank": "Bank",
    # Small finance banks
    "ESAF Small Finance Bank": "Small Finance Bank",
    "AU Small Finance Bank": "Small Finance Bank",
    # NBFCs / specialty credit
    "Bajaj Finance": "NBFC",
    "Tata Capital": "NBFC",
    "Orix": "NBFC",
    "Oxyzo Financial Services": "NBFC",
    "Poonawalla Fin": "NBFC",
    "Piramal": "NBFC",
    "Northern Arc": "NBFC",
    "Jio Credit": "NBFC",
    "Aseem Infra": "NBFC",
    "Axis Finance": "NBFC",
    "AK Capital": "NBFC",
    "GetVantage": "NBFC",
    "Credable": "NBFC",
    "Mufin Green Finance": "NBFC",
    "Aditya Birla Capital Limited": "NBFC",
    "JSW One Finance": "NBFC",
    "Vivriti Capital": "NBFC",
    "Mas Financial": "NBFC",
    "Western Cap": "NBFC",
    "Hero Fincorp": "NBFC",
    "Strides One": "NBFC",
    "Mizuho Capsave": "NBFC",
    "Satin Finserve": "NBFC",
}


async def seed_fi_master(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Insert the default lenders this tenant has never had. Returns the count added."""
    seen = {
        (name or "").strip().lower()
        for name in (
            await session.execute(
                select(Counterparty.name).where(Counterparty.tenant_id == tenant_id)
            )
        ).scalars()
    }
    added = 0
    for name, fi_type in DEFAULT_FI_MASTER.items():
        if name.strip().lower() in seen:
            continue
        session.add(Counterparty(
            tenant_id=tenant_id, name=name, counterparty_type=fi_type,
            is_active=True, created_by="bootstrap", updated_by="bootstrap",
        ))
        added += 1
    await session.flush()
    return added
