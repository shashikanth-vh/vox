"""Sync the people roster FROM Access — so the two halves of a person cannot drift.

A person lives in two places for good reasons (identity/RBAC in Access, the business
roster here), but the seam between them was maintained by convention: every path that
created an Access user was supposed to also create the roster row, and the paths that
didn't (Postman, scripts, an older UI) produced people who could sign in yet appeared
in no dropdown, could not be named on a lead, and could not be cited by a conversion.

This endpoint makes the register RECONCILE instead of trust: it reads Access's own user
list and upserts roster rows keyed by e-mail. A PULL, deliberately — the register
already depends on Access (assignee verification), so syncing this way keeps the
dependency pointing one way and leaves Access knowing nothing about business tables.

Semantics, chosen to be safe to run any time:
* keyed by e-mail (lowercased) — the one identity string both halves share;
* creates missing rows (short handle from Access's short_name, else the e-mail local
  part — the same value VocX keys captures by);
* updates role / full_name / inactive on existing rows — Access is authoritative for
  who a person IS and what they hold; roster-only fields (geography, sectors,
  reporting line, notes) are never touched;
* never deletes — a person cited by old deals must survive losing their sign-in;
  Access-inactive marks them inactive here;
* a full-name collision with a DIFFERENT mailbox is skipped AND REPORTED, never
  guessed around (full_name is unique per tenant).

The Employees screen calls this on open, so a user added through Postman appears on the
roster the next time anyone looks. It is also a plain endpoint for folder 01 / scripts.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.registry import Person

router = api_router()


async def _list_access_users(tenant_code: str) -> list[dict[str, Any]]:
    """Every active-or-not user Access holds for this tenant. Raises
    ServiceUnavailableError when Access is not configured or not answering —
    a sync that cannot READ must never look like 'nothing to sync'."""
    import httpx

    settings = get_settings()
    if not settings.access_url:
        raise ServiceUnavailableError(
            "Access is not configured (REGISTER_ACCESS_URL) — there is no user list "
            "to sync the roster from.")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{settings.access_url.rstrip('/')}/v1/users",
                params={"include_inactive": "true"},
                headers={"X-API-Key": settings.access_api_key, "X-Tenant": tenant_code})
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError(
            f"Access is not answering ({exc}) — the roster was not touched.") from exc
    if r.status_code >= 300:
        raise ServiceUnavailableError(
            f"Access refused the user list (HTTP {r.status_code}) — the roster was "
            "not touched.")
    rows = r.json()
    return rows if isinstance(rows, list) else rows.get("items", [])


def _handle_of(user: dict[str, Any]) -> str:
    short = (user.get("short_name") or "").strip()
    if short:
        return short
    return (user.get("email") or "").split("@")[0].strip()


@router.post("/v1/internal/people/sync-access", tags=["Internal"],
             summary="Upsert the people roster from Access's user list")
async def sync_people_from_access(ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "edit_employee")
    users = await _list_access_users(ctx.tenant_code)

    existing = (await ctx.session.execute(select(Person).where(
        Person.tenant_id == ctx.tenant_id,
        Person.deleted_at.is_(None)))).scalars().all()
    by_email = {(p.email or "").strip().lower(): p for p in existing if p.email}
    by_full = {(p.full_name or "").strip().lower(): p for p in existing}

    created: list[str] = []
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    unchanged = 0

    for u in users:
        email = (u.get("email") or "").strip()
        full = (u.get("full_name") or "").strip() or email
        if not email:
            skipped.append({"user": str(u.get("id") or "?"),
                            "reason": "no e-mail on the Access user"})
            continue
        role = ", ".join(u.get("roles") or []) or "—"
        inactive = not bool(u.get("is_active", True))
        row = by_email.get(email.lower())
        if row is None:
            clash = by_full.get(full.lower())
            if clash is not None and (clash.email or "").strip().lower() not in ("", email.lower()):
                skipped.append({"user": email,
                                "reason": f"full name {full!r} already belongs to "
                                          f"{clash.email!r} on the roster"})
                continue
            if clash is not None:
                # Same person, roster row without an e-mail (or same mailbox): claim it.
                row = clash
            else:
                row = Person(tenant_id=ctx.tenant_id, name=_handle_of(u), full_name=full,
                             role=role, email=email, inactive=inactive,
                             created_by=ctx.actor)
                ctx.session.add(row)
                await ctx.session.flush()
                by_email[email.lower()] = row
                by_full[full.lower()] = row
                created.append(email)
                continue
        # Access is authoritative for identity + roles; roster-only fields stay ours.
        changes = {}
        if (row.email or "").strip().lower() != email.lower():
            changes["email"] = email
        if (row.role or "") != role:
            changes["role"] = role
        if (row.full_name or "") != full:
            if by_full.get(full.lower()) not in (None, row):
                skipped.append({"user": email,
                                "reason": f"cannot rename to {full!r} — that full name "
                                          "belongs to another roster row"})
                changes = {k: v for k, v in changes.items() if k != "full_name"}
            else:
                changes["full_name"] = full
        if bool(row.inactive) != inactive:
            changes["inactive"] = inactive
        if not changes:
            unchanged += 1
            continue
        for k, v in changes.items():
            setattr(row, k, v)
        row.updated_by = ctx.actor
        updated.append(email)

    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="people.sync_access",
        resource_type="people", resource_id=None, request_id=request_id_ctx.get(),
        changes={"created": len(created), "updated": len(updated),
                 "unchanged": unchanged, "skipped": len(skipped)}))
    total = (await ctx.session.execute(select(func.count(Person.id)).where(
        Person.tenant_id == ctx.tenant_id, Person.deleted_at.is_(None)))).scalar_one()
    return {"created": created, "updated": updated, "unchanged": unchanged,
            "skipped": skipped, "roster_total": int(total)}
