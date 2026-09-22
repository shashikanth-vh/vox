"""Link the existing book's leads to client masters — the one-time catch-up.

Birth-linking (app.api.lead_rules) settles the client master the moment a
lead is CREATED — but every lead added before that rule shipped still carries
its company as free text, invisible to Masters → Clients and to everything
keyed by entity. This pass walks those leads, oldest first, and applies the
EXACT same shared rule (settle_company — canonical matching from
evam_backend_core.company_identity):

  - exactly one live master matches   → the lead is LINKED (master untouched);
  - genuinely new company             → a master row is created, born
                                        lifecycle="Prospect", audit-logged;
  - two same-named live masters       → REPORTED and left for a human — the
                                        GREENPILLREN lesson: never guess;
  - blank / "(unknown)" company       → reported, left untouched.

Idempotent: a linked lead is skipped, and a re-run after a partial apply
picks up exactly where things stand. DRY-RUN by default — prints what it
would do. ``--apply`` executes.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.backfill_lead_masters
    docker exec compose-register-1 python -m app.maintenance.backfill_lead_masters --apply
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

ACTOR = "maintenance.backfill_lead_masters"


async def run(apply: bool) -> None:
    from app.api.lead_rules import settle_company
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models import Lead

    init_engine()
    sm = get_sessionmaker()
    linked = created = ambiguous = empty = 0
    async with sm() as session:
        rows = (await session.execute(
            select(Lead).where(Lead.deleted_at.is_(None), Lead.entity_id.is_(None))
            .order_by(Lead.created_at.asc())
        )).scalars().all()
        for lead in rows:
            label = lead.lead_no or str(lead.id)
            outcome, eid = await settle_company(
                session, lead.tenant_id, lead.company,
                sector=lead.sector, lens=lead.lens, actor=ACTOR,
                note=f"Created by the lead→master backfill from lead {label}.")
            if outcome == "ambiguous":
                ambiguous += 1
                print(f"AMBIGUOUS (left for a human — same-named masters): "
                      f"{label}  {lead.company!r}")
                continue
            if outcome == "empty":
                empty += 1
                print(f"NO COMPANY NAME (left untouched): {label}")
                continue
            verb = ("LINK" if outcome == "linked" else "CREATE+LINK") if apply else \
                   ("would link" if outcome == "linked" else "would create+link")
            print(f"{verb:16s} {label:10s} {lead.company!r}")
            # The dry run must see its own creations too, or two leads for one
            # new company would both print CREATE — settle_company flushed the
            # master in-session, and the rollback below discards everything.
            lead.entity_id = eid
            if outcome == "linked":
                linked += 1
            else:
                created += 1
            if apply:
                lead.updated_by = ACTOR
                session.add(AuditLog(
                    tenant_id=lead.tenant_id, actor=ACTOR,
                    action="maintenance.lead_master_linked",
                    resource_type="leads", resource_id=str(lead.id),
                    changes={"entity_id": {"from": None, "to": str(eid)},
                             "company": lead.company, "outcome": outcome}))
        if apply:
            await session.commit()
        else:
            await session.rollback()
    mode = "" if apply else " (dry run — nothing written; --apply to execute)"
    print(f"\nlinked to existing masters: {linked} · masters created: {created} · "
          f"ambiguous (human): {ambiguous} · no name: {empty}{mode}")


if __name__ == "__main__":
    asyncio.run(run(apply="--apply" in sys.argv))
