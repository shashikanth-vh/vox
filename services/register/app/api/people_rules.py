"""Invariants on the people directory that a column constraint cannot express.

The roster is the platform's answer to "who is this?", and every other service asks it in
a slightly different dialect: leads and deals store the short handle, a picker shows the
full name, Access knows the e-mail, VocX keys a capture under the e-mail's local part.
app.core.people resolves all four — but only if each one still denotes ONE person.

The schema guarantees that for the full name (UNIQUE(tenant_id, full_name)). It cannot
guarantee it for the e-mail, because e-mail is nullable and a partial unique index would
fail the deployment outright on a roster that already has a duplicate. So the rule is
enforced here, on the way in: a second row for the same mailbox is refused with an
explanation, existing rows are left alone, and the operator fixes them when they next
touch them.

Two people who share a short HANDLE is a different matter and is deliberately allowed —
"Priya" for Priya Nair and Priya Sharma is a real thing a firm does. The picker offers
those two under their full names instead (see _role_name_lists) and any string that still
denotes both is refused where it is used, naming the candidates.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.errors import ValidationAppError
from app.models.registry import Person


async def person_pre_write(ctx: Any, body: dict, obj_id: Any) -> None:
    """No two roster rows for one mailbox."""
    email = (body.get("email") or "").strip()
    if not email:
        return
    conds = [Person.tenant_id == ctx.tenant_id,
             Person.deleted_at.is_(None),
             func.lower(func.trim(Person.email)) == email.lower()]
    if obj_id is not None:
        conds.append(Person.id != obj_id)
    clash = (await ctx.session.execute(
        select(Person.full_name, Person.name).where(*conds).limit(1))).first()
    if clash is not None:
        raise ValidationAppError(
            f"{clash[0] or clash[1]} is already on record with the e-mail {email}. "
            f"One person, one mailbox — edit that entry instead of adding a second, or "
            f"give this person their own address.")
