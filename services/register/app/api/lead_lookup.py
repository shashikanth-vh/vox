"""Is this company already on the book? — the Add-lead dialog's duplicate check.

The dialog used to answer from the browser's cache: whichever leads and clients
this tab happened to have loaded. In live mode that was often nothing (the grids
are server-paged and the cache fills only when the Dashboard or Today walks the
book), and always only the caller's OWN scope — so a BDRM typing a company
another BDRM already works never saw the warning (B08: LD-402 was in the cache,
LD-372 was not).

This route answers from the register, tenant-wide, with the SAME matching the
dialog applies (normalised containment, or bigram similarity on the distinctive
part of the name — "greenphill" must still find Greenpill). It is deliberately
NOT scoped: the whole point is to tell a desk member that someone else holds the
company. What it tells them is kept to what the warning needs — who, which row,
which status — never the lead's notes or the client's profile.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, Query
from sqlalchemy import exists, func, select

from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models import AssetMonetisation, Deal, Entity, Lead, LendingTracker, SyndicationTracker

router = api_router()

# Mirrors the UI's normName / distinct(): boilerplate words carry no identity.
_BOILER = re.compile(
    r"\b(private|pvt|limited|ltd|llp|india|energy|energies|solutions?|services?|"
    r"technologies|technology|ventures?|renewables?)\b")
_GENERIC = re.compile(
    r"\b(industries|industry|enterprises?|group|holdings?|corporation|corp|company|co|"
    r"infra|infrastructure|international)\b")


def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", _BOILER.sub("", str(s or "").lower()))


def distinct(s: str | None) -> str:
    return norm_name(_GENERIC.sub(" ", str(s or "").lower()))


def name_alike(a: str, b: str) -> float:
    """Bigram Dice similarity, 0..1, on the names as typed."""
    def grams(s: str) -> list[str]:
        t = " " + re.sub(r"\s+", " ", s.lower()).strip() + " "
        return [t[i:i + 2] for i in range(len(t) - 1)]
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    counts: dict[str, int] = {}
    for g in ga:
        counts[g] = counts.get(g, 0) + 1
    hit = 0
    for g in gb:
        if counts.get(g, 0) > 0:
            hit += 1
            counts[g] -= 1
    return 2 * hit / (len(ga) + len(gb))


def alike(typed: str, candidate: str | None) -> bool:
    q, qd = norm_name(typed), distinct(typed)
    an = norm_name(candidate)
    if not an or len(q) < 3:
        return False
    if q in an or an in q:
        return True
    ad = distinct(candidate)
    return bool(qd and ad) and name_alike(qd, ad) >= 0.62


async def live_deal_for(session: Any, tenant_id: Any, entity_id: Any) -> dict[str, Any] | None:
    """The company's most recent deal that still has a LIVE line (not closed, not
    dead), as {id, deal_no, rm} — or None. Shared by the Add-lead warning (B07)
    and the intake rule."""
    deals = (await session.execute(
        select(Deal.id, Deal.deal_no, Deal.rm).where(
            Deal.tenant_id == tenant_id, Deal.entity_id == entity_id,
            Deal.deleted_at.is_(None)).order_by(Deal.created_at.desc()))).all()
    for d in deals:
        live = bool((await session.execute(select(
            exists().where(LendingTracker.deal_id == d.id, LendingTracker.deleted_at.is_(None),
                           LendingTracker.stage.notin_(("Rejected", "Dropped", "Disbursed")))
            | exists().where(SyndicationTracker.deal_id == d.id, SyndicationTracker.deleted_at.is_(None),
                             SyndicationTracker.status.notin_(("Dropped", "Withdrawn", "Rejected", "Disbursed")))
            | exists().where(AssetMonetisation.deal_id == d.id, AssetMonetisation.deleted_at.is_(None),
                             AssetMonetisation.status.notin_(("Dropped", "Closed")))))).scalar())
        if live:
            return {"id": d.id, "deal_no": d.deal_no, "rm": d.rm}
    return None


@router.get("/v1/lead-lookup", tags=["Leads"],
            summary="Companies and open leads that look like this name (tenant-wide)")
async def lead_lookup(
    name: str = Query(..., min_length=1, max_length=300),
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    from app.authz.engine import view_access
    from app.authz.matrix import Access
    from app.core.errors import ForbiddenError

    if ctx.user is not None and view_access(ctx.user, "leads") is Access.NONE:
        raise ForbiddenError("No access to leads.")
    if len(norm_name(name)) < 3:
        return {"clients": [], "leads": []}

    ents = (await ctx.session.execute(
        select(Entity.id, Entity.code, Entity.legal_name, Entity.display_name)
        .where(Entity.tenant_id == ctx.tenant_id, Entity.deleted_at.is_(None)))).all()
    clients = [{"entity_id": str(e.id), "code": e.code,
                "name": e.display_name or e.legal_name}
               for e in ents
               if alike(name, e.display_name) or alike(name, e.legal_name)][:5]
    # Is the company already being WORKED? A deal with a line that is neither
    # closed nor dead means a second lead would only split the story (B07): the
    # dialog warns and offers Add product on the existing deal instead.
    for c in clients:
        eid = c["entity_id"]
        live = await live_deal_for(ctx.session, ctx.tenant_id, eid)
        if live is not None:
            c["deal_no"], c["deal_rm"] = live["deal_no"], live["rm"]
        c["live_deal"] = live is not None
        c["deals"] = int((await ctx.session.execute(
            select(func.count()).select_from(Deal).where(
                Deal.tenant_id == ctx.tenant_id, Deal.entity_id == eid,
                Deal.deleted_at.is_(None)))).scalar_one())

    rows = (await ctx.session.execute(
        select(Lead.lead_no, Lead.company, Lead.rm, Lead.status)
        .where(Lead.tenant_id == ctx.tenant_id, Lead.deleted_at.is_(None),
               Lead.status == "Active", Lead.converted_deal_id.is_(None)))).all()
    leads = [{"lead_no": r.lead_no, "company": r.company, "rm": r.rm, "status": r.status}
             for r in rows if alike(name, r.company)]
    return {"clients": clients, "leads": leads[:5]}
