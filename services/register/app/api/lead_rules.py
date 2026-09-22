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
"""

from __future__ import annotations

from typing import Any

from evam_backend_core.company_identity import canonical_name, entity_code
from sqlalchemy import select


async def lead_company_to_master(ctx: Any, body: dict) -> None:
    from app.models import Entity

    if body.get("entity_id"):
        return
    name = str(body.get("company") or "").strip()
    if not name or name == "(unknown)":
        return

    wanted = canonical_name(name)
    if not wanted:
        return
    rows = (await ctx.session.execute(
        select(Entity.id, Entity.code, Entity.legal_name, Entity.display_name).where(
            Entity.tenant_id == ctx.tenant_id, Entity.deleted_at.is_(None)))).all()
    matches = [row for row in rows
               if any(c and canonical_name(c) == wanted
                      for c in (row.legal_name, row.display_name))]
    if len(matches) == 1:
        body["entity_id"] = matches[0].id
        return
    if matches:
        # SAME-NAMED SIBLINGS (the GREENPILLREN story): guessing between them is
        # exactly the wound this rule exists to close. The lead stays unlinked;
        # a human resolves it (the Attach row, or the conversion dialog).
        return

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
        tenant_id=ctx.tenant_id,
        code=code,
        legal_name=name,
        display_name=name,
        sector=body.get("sector") or None,
        lens=body.get("lens") or None,
        register_status="Pipeline",
        lifecycle="Prospect",
        notes=f"Created when lead for '{name}' was added to the register.",
    )
    ctx.session.add(entity)
    await ctx.session.flush()
    # The trail must say a company was born here — the CRUD repository stamps its
    # own creates, but this row rides inside the lead's request.
    from evam_backend_core.logging import request_id_ctx

    from app.db.base import AuditLog
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="create",
        resource_type="entity", resource_id=str(entity.id),
        request_id=request_id_ctx.get(),
        changes={"code": code, "legal_name": name, "via": "lead_create"}))
    body["entity_id"] = entity.id
