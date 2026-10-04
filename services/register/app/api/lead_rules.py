"""The client master follows the lead — link or create at the keyboard.

A new lead used to reach the client master only when it was pushed to deals:
until then the company existed as free text on the lead row, invisible to
Masters → Clients, to company-scoped views and to anything keyed by entity.
The desk's rule is that a company enters the master the moment it enters the
book — so lead CREATION now settles the link, with the same canonical
matching the conversion pre-flight and VOX use (evam_backend_core.
company_identity — one shared answer to "is this the same company?").

An explicitly supplied entity_id (the Add-lead dialog's Attach row, VOX's
already-linked leads, imports) is respected untouched. A matched master is
LINKED, never edited — the master outranks a lead's free text. Only a
genuinely new company creates a row, born lifecycle="Prospect" (the
documented start of the relationship journey) with register_status
"Pipeline", exactly as a VOX-discovered company is born. The hook runs
before the RBAC checks read the body, so a scoped creator's gate sees the
company the lead will actually carry.

``settle_company`` is the whole rule as a callable — the create hook rides
it per request, and the one-time backfill (app.scripts.backfill_lead_masters)
rides it per existing lead, so the live path and the catch-up path can never
drift apart.
"""

from __future__ import annotations

import uuid
from typing import Any

from evam_backend_core.company_identity import canonical_name, entity_code
from sqlalchemy import select


async def settle_company(session: Any, tenant_id: Any, name: str, *,
                         sector: str | None = None, lens: str | None = None,
                         actor: str = "register",
                         note: str | None = None,
                         identity: dict | None = None) -> tuple[str, uuid.UUID | None]:
    """Resolve a company NAME against the client master. Returns (outcome, id):

    - ("linked",  id)   — exactly one live master matches canonically;
    - ("created", id)   — genuinely new: a Prospect master row was added
                          (flushed, audit-logged) in this session;
    - ("ambiguous", None) — two or more same-named live masters: guessing
                          between them is the GREENPILLREN wound, a human
                          resolves it (Attach row / conversion dialog);
    - ("empty", None)   — no usable name.

    A matched master is never edited — it outranks a lead's free text.
    """
    from app.models import Entity

    name = str(name or "").strip()
    if not name or name == "(unknown)":
        return "empty", None
    wanted = canonical_name(name)
    if not wanted:
        return "empty", None

    rows = (await session.execute(
        select(Entity.id, Entity.code, Entity.legal_name, Entity.display_name).where(
            Entity.tenant_id == tenant_id, Entity.deleted_at.is_(None)))).all()
    matches = [row for row in rows
               if any(c and canonical_name(c) == wanted
                      for c in (row.legal_name, row.display_name))]
    if len(matches) == 1:
        return "linked", matches[0].id
    if matches:
        return "ambiguous", None

    # Genuinely new. The deterministic code is stable per name; if a LIVE row
    # somehow already holds it under a different canonical name (hash overlap),
    # suffix rather than refuse — the unique index would otherwise 500 the lead.
    code = entity_code(name)
    taken = {r.code for r in rows}
    if code in taken:
        n = 2
        while f"{code}-{n}" in taken:
            n += 1
        code = f"{code}-{n}"
    entity = Entity(
        tenant_id=tenant_id,
        code=code,
        legal_name=name,
        display_name=name,
        sector=sector or None,
        lens=lens or None,
        register_status="Pipeline",
        lifecycle="Prospect",
        notes=note or f"Created when lead for '{name}' was added to the register.",
    )
    # Identity the caller already knows (a lead's CIN/city, a prospect's
    # registrar data) seeds the NEWBORN master only — a matched master above
    # was returned untouched, because it outranks a lead's free text.
    for k in ("cin", "city", "state", "country", "address"):
        v = (identity or {}).get(k)
        if v:
            setattr(entity, k, str(v).strip())
    session.add(entity)
    await session.flush()
    # The trail must say a company was born here — the CRUD repository stamps its
    # own creates, but this row rides inside another operation.
    from evam_backend_core.logging import request_id_ctx

    from app.db.base import AuditLog
    session.add(AuditLog(
        tenant_id=tenant_id, actor=actor, action="create",
        resource_type="entity", resource_id=str(entity.id),
        request_id=request_id_ctx.get(),
        changes={"code": code, "legal_name": name, "via": "lead_create"}))
    return "created", entity.id


async def lead_company_to_master(ctx: Any, body: dict) -> None:
    if body.get("entity_id"):
        return
    _outcome, eid = await settle_company(
        ctx.session, ctx.tenant_id, str(body.get("company") or ""),
        sector=body.get("sector"), lens=body.get("lens"), actor=ctx.actor,
        identity={k: body.get(k)
                  for k in ("cin", "city", "state", "country", "address")})
    if eid is not None:
        body["entity_id"] = eid


async def lead_pre_delete(ctx: Any, obj_id: Any) -> None:
    """A Converted lead is the history its deal continues from — the Deals row,
    the tracker lines and the interactions ported from it all point back at it.
    Deleting it would orphan that story, so the register refuses (the row-lock
    only guarded edits; DELETE never asked). Drop or delete the DEAL instead."""
    from app.core.errors import ForbiddenError
    from app.models import Lead

    row = (await ctx.session.execute(
        select(Lead.status, Lead.lead_no).where(Lead.id == obj_id,
                                                Lead.tenant_id == ctx.tenant_id))).first()
    if row is not None and (row.status or "") == "Converted":
        raise ForbiddenError(
            f"Lead {row.lead_no or obj_id} is Converted — it is the history its deal "
            "continues from and cannot be deleted. Work on the deal instead.")


async def link_prospects_to_company(session: Any, tenant_id: Any, name: str,
                                    entity_id: Any, lead_id: Any = None) -> int:
    """Prospects that name this company get the client link (and the lead they
    spawned, when known). The prospect universe was a list beside the register —
    a company that was already a client or an open lead still read "uncontacted"
    there (B41). Matching is the shared canonical rule; a prospect already linked
    to another company is left alone. Returns how many rows were linked."""
    from evam_backend_core.company_identity import canonical_name

    from app.models import Prospect

    wanted = canonical_name(str(name or ""))
    if not wanted:
        return 0
    rows = (await session.execute(
        select(Prospect).where(Prospect.tenant_id == tenant_id,
                               Prospect.deleted_at.is_(None)))).scalars().all()
    n = 0
    for p in rows:
        if canonical_name(p.name or "") != wanted:
            continue
        if p.entity_id is not None and str(p.entity_id) != str(entity_id):
            continue
        changed = False
        if p.entity_id is None and entity_id is not None:
            p.entity_id = entity_id
            changed = True
        if lead_id is not None:
            ids = list(p.lead_ids or [])
            if str(lead_id) not in ids:
                ids.append(str(lead_id))
                p.lead_ids = ids
                changed = True
            if p.status in ("uncontacted", "contacted", "interested"):
                p.status = "lead_created"
                changed = True
        if changed:
            n += 1
    if n:
        await session.flush()
    return n


async def lead_post_write(ctx: Any, obj: Any, event: str, previous: Any = None) -> None:
    """ResourceSpec.post_write for leads: a created lead links the prospects that
    name its company (B41)."""
    if event != "create" or obj is None:
        return
    await link_prospects_to_company(ctx.session, ctx.tenant_id, obj.company,
                                    obj.entity_id, obj.id)
