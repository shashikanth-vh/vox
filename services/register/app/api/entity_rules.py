"""Referential invariants on companies (entities) that a foreign key cannot express.

Everything in the register hangs off the company row: leads, deals, product lines, the
Data Register's documents. Rows reference it by ``entity_id`` WITHOUT a database-level
cascade (soft-delete means the FK stays satisfied) — so deleting a company out from
under a live book used to succeed, and every dependant kept pointing at a row that no
longer answers. The visible symptom was a lending line whose Data Register said
"Entity … not found" on every upload, with nothing on screen explaining why.

The client's own "is it in use?" check ran against its LOCAL cache, which is empty on a
fresh tab — this is the server-side truth that cannot be dodged. A refused delete names
what the company still carries; the operator either clears those rows first or leaves
the company in place. (A company deleted in error is recoverable: every resource keeps
its ``/restore`` route.)
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.errors import ConflictError


async def entity_pre_delete(ctx: Any, obj_id: uuid.UUID) -> None:
    from app.models import AssetMonetisation, Deal, Lead, LendingTracker, SyndicationTracker

    holds: list[str] = []
    for label, model in (("lead", Lead), ("deal", Deal),
                         ("lending line", LendingTracker),
                         ("syndication mandate", SyndicationTracker),
                         ("asset-monetisation mandate", AssetMonetisation)):
        n = (await ctx.session.execute(select(func.count(model.id)).where(
            model.tenant_id == ctx.tenant_id,
            model.entity_id == obj_id,
            model.deleted_at.is_(None)))).scalar_one()
        if n:
            holds.append(f"{n} {label}{'s' if n > 1 else ''}")
    if holds:
        raise ConflictError(
            "This company still carries " + ", ".join(holds) + " — delete or reassign "
            "those first. Removing the company row under a live book would leave every "
            "one of those records (and the company's Data Register) pointing at "
            "nothing.")
