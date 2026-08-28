"""Fold imported lender-status spellings into the canonical vocabulary.

The Excel books wrote the same fact many ways — "IM in Prep", "IM under
preparation", "On hold" — and those rows sat LOCKED on the chase board (an
unknown status has no next steps) while the matrix export miscounted every one
of them as "Identified". The API and the UI now canonicalise at their
boundaries, so those rows already move; this one-time pass fixes the STORED
spelling too, so counts, filters and raw exports all speak one language.

Spelling folds only — never a semantic move: "IM in Prep" and "IM under
preparation" are the same fact in different letters. A value outside both the
vocabulary and the alias table is REPORTED, not guessed at.

DRY-RUN by default — prints what it would do. ``--apply`` executes, each fold
written to the audit log.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.normalize_lender_status
    docker exec compose-register-1 python -m app.maintenance.normalize_lender_status --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.normalize_lender_status"

# Canonical lender vocabulary + the spellings observed in the live books.
# Mirrors app.api.custom._LENDER_ALIASES — kept verbatim so the stored fold and
# the API-boundary fold can never disagree.
VOCAB = {"Identified", "IM Under Preparation", "IM Circulated", "Docs Pending",
         "Queries Received", "IP Received", "Sanctioned", "Disbursed",
         "Declined", "Dropped", "On Hold"}
ALIASES = {
    "im in prep": "IM Under Preparation", "im under prep": "IM Under Preparation",
    "im under preparation": "IM Under Preparation",
    "im in preparation": "IM Under Preparation", "im prep": "IM Under Preparation",
    "im preparation": "IM Under Preparation",
    "im sent": "IM Circulated", "im submitted": "IM Circulated",
    "im circulated": "IM Circulated",
    "on hold": "On Hold", "onhold": "On Hold", "hold": "On Hold",
    "dropped": "Dropped", "drop": "Dropped", "disbursed": "Disbursed",
    "approved": "Sanctioned", "sanctioned": "Sanctioned",
    "final sanction received": "Sanctioned",
    "rejected": "Declined", "declined": "Declined", "identified": "Identified",
    "queries received": "Queries Received", "ip received": "IP Received",
    "docs pending": "Docs Pending",
}


def canon(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    return ALIASES.get(text.lower(), text)


async def run(apply: bool) -> None:
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models.trackers import SyndicationLender

    init_engine()
    sm = get_sessionmaker()
    folded = clean = 0
    unknown: dict[str, int] = {}
    async with sm() as session:
        rows = (await session.execute(
            select(SyndicationLender).where(SyndicationLender.deleted_at.is_(None))
        )).scalars().all()
        for row in rows:
            current = (row.status or "").strip()
            if not current:
                clean += 1
                continue
            target = canon(current)
            if target == current:
                if current in VOCAB:
                    clean += 1
                else:
                    unknown[current] = unknown.get(current, 0) + 1
                continue
            print(f"{'FOLD ' if apply else 'would fold'} "
                  f"{row.lender_name!r:40s} {current!r} -> {target!r}")
            folded += 1
            if not apply:
                continue
            row.status = target
            row.updated_by = ACTOR
            session.add(AuditLog(
                tenant_id=row.tenant_id, actor=ACTOR,
                action="maintenance.lender_status_fold",
                resource_type="syndication_lenders", resource_id=str(row.id),
                changes={"lender": row.lender_name,
                         "status": {"from": current, "to": target}}))
        if apply:
            await session.commit()
    print(f"\n{'folded' if apply else 'would fold'}: {folded} · already clean: {clean}")
    for value, n in sorted(unknown.items()):
        print(f"UNKNOWN (left untouched — decide by hand): {value!r} x{n}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
