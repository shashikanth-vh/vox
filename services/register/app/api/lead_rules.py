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
                         note: str | None = None) -> tuple[str, uuid.UUID | None]:
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
        sector=body.get("sector"), lens=body.get("lens"), actor=ctx.actor)
    if eid is not None:
        body["entity_id"] = eid
