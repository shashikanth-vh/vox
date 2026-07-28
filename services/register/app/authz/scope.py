"""THE central scope evaluator — one definition of "in my scope", used everywhere.

RBAC 3.1's SCOPED is wider than "exactly my assignment". A row is in a user's scope
when ANY of these hold:

1. **Assignment** — an active assignment on that exact line.
2. **Connected company** — an active assignment on ANY line of the same company
   (assigned to EcoSoch Lending → you may READ EcoSoch's deals, dossier, documents,
   timeline; write still needs your own assignment).
3. **Own book** — rows the user created (``created_by`` = their e-mail; the Register
   stamps authenticated writes with the user's e-mail, not the spoofable X-Actor).
4. **Team** — rows in scope of anyone who reports to the user (the Access service
   resolves the reporting tree; the gateway forwards it as ``X-User-Reports`` /
   ``X-User-Report-Ids``). A Head sees their reporting team's book.
5. **Vertical default ownership** — an UNASSIGNED line belongs to its vertical Head
   (``DEFAULT_LINE_OWNER``): Credit Head for Lending, Syn Head for Syndication,
   AM Head for Asset Monetisation — operational, not just descriptive.

Every access path calls into this module — list filtering, direct GET-by-id, writes,
dossier, documents, timelines — so the answer can never differ between routes. That is
the whole point: scope evaluated in ONE place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.sql import ColumnElement

from app.authz.engine import UserContext
from app.authz.matrix import DEFAULT_LINE_OWNER
from app.core.security import RequestContext
from app.models import AssetMonetisation, Deal, Lead, LendingTracker, SyndicationTracker
from app.models.users import LineAssignment

# subject_type → the model whose rows carry that subject's entity linkage.
_SUBJECT_MODELS: dict[str, Any] = {
    "Lead": Lead,
    "Deal": Deal,
    "Lending": LendingTracker,
    "Syndication": SyndicationTracker,
    "AssetMonetisation": AssetMonetisation,
}


@dataclass
class UserScope:
    """Everything needed to answer "is this row mine?" — computed once per request."""

    user: UserContext
    # (subject_type, subject_id) pairs of the user's + their team's active assignments.
    assigned: set[tuple[str, uuid.UUID]] = field(default_factory=set)
    # Companies the user touches through ANY assignment (the connected-company rule).
    entity_ids: set[uuid.UUID] = field(default_factory=set)
    # e-mails whose created_by rows count as the user's own book (self + reports).
    own_emails: set[str] = field(default_factory=set)

    def assigned_ids(self, subject_type: str) -> set[uuid.UUID]:
        return {sid for st, sid in self.assigned if st == subject_type}

    def default_owner_of(self, subject_type: str) -> bool:
        """Does the user hold the vertical-Head role that owns UNASSIGNED lines?"""
        owner_role = DEFAULT_LINE_OWNER.get(subject_type)
        return owner_role is not None and owner_role in self.user.roles


async def build_scope(ctx: RequestContext, user: UserContext) -> UserScope:
    """Assemble the user's scope: own + team assignments, connected companies, own book.

    Two small queries per request (assignments, then entity linkage of those rows) —
    only on SCOPED paths, never for FULL/READ grants.
    """
    scope = UserScope(user=user, own_emails={user.email} | set(user.report_emails))
    user_ids = [user.id, *user.report_ids]

    rows = (
        await ctx.session.execute(
            select(LineAssignment.subject_type, LineAssignment.subject_id).where(
                LineAssignment.tenant_id == ctx.tenant_id,
                LineAssignment.user_id.in_(user_ids),
                LineAssignment.ended_at.is_(None),
                LineAssignment.deleted_at.is_(None),
            )
        )
    ).all()
    scope.assigned = {(r[0], r[1]) for r in rows}

    # Connected companies: resolve each assigned line to its entity_id.
    for subject_type, model in _SUBJECT_MODELS.items():
        ids = scope.assigned_ids(subject_type)
        if not ids:
            continue
        entity_ids = (
            await ctx.session.execute(
                select(model.entity_id).where(
                    model.tenant_id == ctx.tenant_id, model.id.in_(ids),
                    model.entity_id.is_not(None))
            )
        ).scalars().all()
        scope.entity_ids.update(entity_ids)

    # Vertical-Head default ownership at the COMPANY level: a Head also "connects" to any
    # company that has an UNASSIGNED line in their vertical. Computed once here so list
    # filtering, direct GET and company reads all agree cheaply.
    for subject_type, model in _SUBJECT_MODELS.items():
        if not scope.default_owner_of(subject_type):
            continue
        owned = (
            await ctx.session.execute(
                select(model.entity_id).where(
                    model.tenant_id == ctx.tenant_id,
                    model.entity_id.is_not(None),
                    _no_assignment_exists(subject_type, model))
            )
        ).scalars().all()
        scope.entity_ids.update(owned)
    return scope


def _no_assignment_exists(subject_type: str, model) -> ColumnElement:  # noqa: ANN001
    """SQL: the row has NO active assignment at all (→ vertical-Head default scope)."""
    return ~exists(
        select(LineAssignment.id).where(
            LineAssignment.subject_type == subject_type,
            LineAssignment.subject_id == model.id,
            LineAssignment.ended_at.is_(None),
            LineAssignment.deleted_at.is_(None),
        )
    )


def list_condition(scope: UserScope, subject_type: str) -> ColumnElement:
    """The scope disjunction as a SQL clause for list queries."""
    model = _SUBJECT_MODELS[subject_type]
    clauses: list[ColumnElement] = [
        model.id.in_(scope.assigned_ids(subject_type) or {uuid.UUID(int=0)}),
        model.created_by.in_(scope.own_emails),
    ]
    if scope.entity_ids:
        clauses.append(model.entity_id.in_(scope.entity_ids))
    if scope.default_owner_of(subject_type):
        clauses.append(_no_assignment_exists(subject_type, model))
    return or_(*clauses)


def company_scoped_condition(scope: UserScope, model) -> ColumnElement:  # noqa: ANN001
    """List clause for an entity-carrying resource (financials, contracts, intel,
    monitoring, documents, interactions): visible when the company is in scope, or the
    row is in the user's own/team book."""
    clauses: list[ColumnElement] = [model.created_by.in_(scope.own_emails)]
    if scope.entity_ids:
        clauses.append(model.entity_id.in_(scope.entity_ids))
    else:
        clauses.append(model.entity_id.in_({uuid.UUID(int=0)}))  # nothing in scope
    return or_(*clauses)


def company_row_in_scope(scope: UserScope, row) -> bool:  # noqa: ANN001
    """Direct-GET / write scope for an entity-carrying resource."""
    if getattr(row, "created_by", None) in scope.own_emails:
        return True
    eid = getattr(row, "entity_id", None)
    return eid is not None and eid in scope.entity_ids


def parent_company_condition(scope: UserScope, model, parent_model,  # noqa: ANN001
                             fk_attr: str) -> ColumnElement:
    """List clause for a child resource (e.g. a syndication lender) that has NO entity_id
    of its own: it is in scope when its PARENT line's company is in scope, or the row is in
    the user's own/team book. Keeps a scoped Syn RM from reading every lender tenant-wide
    via the flat route while still letting them see their own lines' lenders."""
    empty = {uuid.UUID(int=0)}
    parent_ids = select(parent_model.id).where(
        parent_model.entity_id.in_(scope.entity_ids or empty))
    return or_(getattr(model, fk_attr).in_(parent_ids),
               model.created_by.in_(scope.own_emails or {""}))


async def parent_company_row_in_scope(ctx: RequestContext, scope: UserScope, row,  # noqa: ANN001
                                      parent_model, fk_attr: str) -> bool:
    """Direct-GET scope for a child resource: resolve its parent line's entity and check."""
    if getattr(row, "created_by", None) in scope.own_emails:
        return True
    parent_id = getattr(row, fk_attr, None)
    if parent_id is None:
        return False
    entity_id = (
        await ctx.session.execute(
            select(parent_model.entity_id).where(
                parent_model.tenant_id == ctx.tenant_id, parent_model.id == parent_id))
    ).scalar_one_or_none()
    return entity_id is not None and entity_id in scope.entity_ids


def entity_list_condition(scope: UserScope, model) -> ColumnElement:  # noqa: ANN001
    """Scope clause for the ENTITIES list (the clients view): connected companies +
    companies the user (or team) created."""
    clauses: list[ColumnElement] = [model.created_by.in_(scope.own_emails)]
    if scope.entity_ids:
        clauses.append(model.id.in_(scope.entity_ids))
    return or_(*clauses)


async def row_in_scope(ctx: RequestContext, scope: UserScope, subject_type: str,
                       row) -> bool:  # noqa: ANN001
    """Is this specific row readable under SCOPED access? (Direct-GET protection.)"""
    if (subject_type, row.id) in scope.assigned:
        return True
    if getattr(row, "created_by", None) in scope.own_emails:
        return True
    entity_id = getattr(row, "entity_id", None)
    if entity_id is not None and entity_id in scope.entity_ids:
        return True
    if scope.default_owner_of(subject_type):
        unassigned = (
            await ctx.session.execute(
                select(~exists(select(LineAssignment.id).where(
                    LineAssignment.tenant_id == ctx.tenant_id,
                    LineAssignment.subject_type == subject_type,
                    LineAssignment.subject_id == row.id,
                    LineAssignment.ended_at.is_(None),
                    LineAssignment.deleted_at.is_(None),
                )))
            )
        ).scalar_one()
        if unassigned:
            return True
    return False


async def entity_in_scope(ctx: RequestContext, scope: UserScope,
                          entity_id: uuid.UUID) -> bool:
    """May the user read THIS company (dossier / documents / timeline / summary)?

    True when the company is connected to any of their assignments, in their own/team
    book (they created it or any of its lines), or — for vertical Heads — when the
    company has an unassigned line in their vertical."""
    if entity_id in scope.entity_ids:
        return True
    # Own/team book: the company itself, or any of its lines, created by us.
    from app.models import Entity

    created = (
        await ctx.session.execute(
            select(Entity.id).where(Entity.tenant_id == ctx.tenant_id,
                                    Entity.id == entity_id,
                                    Entity.created_by.in_(scope.own_emails)).limit(1)
        )
    ).scalar_one_or_none()
    if created is not None:
        return True
    for subject_type, model in _SUBJECT_MODELS.items():
        conds = [model.tenant_id == ctx.tenant_id, model.entity_id == entity_id,
                 model.deleted_at.is_(None)]
        row_scope: list[ColumnElement] = [model.created_by.in_(scope.own_emails)]
        if scope.default_owner_of(subject_type):
            row_scope.append(_no_assignment_exists(subject_type, model))
        hit = (
            await ctx.session.execute(
                select(model.id).where(and_(*conds), or_(*row_scope)).limit(1)
            )
        ).scalar_one_or_none()
        if hit is not None:
            return True
    return False


async def can_write_row(ctx: RequestContext, scope: UserScope, subject_type: str,
                        subject_id: uuid.UUID) -> bool:
    """SCOPED write: own/team assignment on THIS line, or vertical-Head default
    ownership of an UNASSIGNED line. (Connected-company and own-book grant READ, not
    write — write stays assignment-driven, per the spec.)"""
    if (subject_type, subject_id) in scope.assigned:
        return True
    if scope.default_owner_of(subject_type):
        assigned_any = (
            await ctx.session.execute(
                select(LineAssignment.id).where(
                    LineAssignment.tenant_id == ctx.tenant_id,
                    LineAssignment.subject_type == subject_type,
                    LineAssignment.subject_id == subject_id,
                    LineAssignment.ended_at.is_(None),
                    LineAssignment.deleted_at.is_(None),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if assigned_any is None:
            return True
    return False
