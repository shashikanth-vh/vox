"""Give every company its group code — the identity the whole UI keys on.

An entity without a code splits into two on-screen companies: its deal opens
under the deal's number while its mandate opens under an S-number, and a
profile edit made on one side never shows on the other (nor lands anywhere —
the shadow record carries nothing to PATCH). Lead-born entities now get their
code at conversion; this one-time pass gives it to the ones already in the
book, using the seeder's own slug convention — stop-words out, uppercase,
12 characters, numeric suffix on a clash (KHURSHNAMI2-style).

DRY-RUN by default — prints what it would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.assign_entity_codes
    docker exec compose-register-1 python -m app.maintenance.assign_entity_codes --apply
"""
from __future__ import annotations

import asyncio
import re
import sys

from sqlalchemy import select

ACTOR = "maintenance.assign_entity_codes"
_STOP = {"private", "pvt", "limited", "ltd", "llp", "india", "the", "and", "co", "company"}


def _slug(name: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
             if w not in _STOP]
    return "".join(words).upper()[:12] or "ENTITY"


async def run(apply: bool) -> None:
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models import Entity

    init_engine()
    sm = get_sessionmaker()
    assigned = skipped = 0
    async with sm() as session:
        rows = (await session.execute(
            select(Entity).where(Entity.deleted_at.is_(None))
            .order_by(Entity.created_at.asc())
        )).scalars().all()
        taken = {(e.code or "").strip() for e in rows if (e.code or "").strip()}
        for ent in rows:
            if (ent.code or "").strip():
                skipped += 1
                continue
            base = _slug(ent.legal_name)
            code, i = base, 1
            while code in taken:
                i += 1
                code = f"{base[:10]}{i}"
            taken.add(code)
            print(f"{'ASSIGN' if apply else 'would assign'} {code!r:16s} "
                  f"to {ent.legal_name!r}")
            assigned += 1
            if not apply:
                continue
            ent.code = code
            ent.updated_by = ACTOR
            session.add(AuditLog(
                tenant_id=ent.tenant_id, actor=ACTOR,
                action="maintenance.entity_code_assigned",
                resource_type="entities", resource_id=str(ent.id),
                changes={"code": {"from": None, "to": code},
                         "legal_name": ent.legal_name}))
        if apply:
            await session.commit()
    print(f"\n{'assigned' if apply else 'would assign'}: {assigned} · "
          f"already coded: {skipped}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
