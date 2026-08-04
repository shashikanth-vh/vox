"""Turning a person's NAME into a person.

Leads, deals, trackers and VocX captures all store a human-readable string in `rm` /
`analyst` / `performed_by`. That is fine for reading and wrong for identifying: a name is
a label the firm chose, an identity is the mailbox the firm's SSO knows. The two are
related only by convention, and the convention has already failed twice in the field —
a conversion refused with "Unknown rm 'Priya'" while Priya Nair was sitting in the very
roster the picker had read, because one check looked at `full_name` and the picker had
offered `name`.

So this module is the ONE place a name is resolved, and it accepts every string that can
honestly denote a person on the roster:

    short handle   "Priya"                      what leads/deals/trackers store
    full name      "Priya Nair"                 what a picker shows and a human types
    e-mail         "priya.nair@evamfinance.com" the real identity, always unambiguous
    local part     "priya.nair"                 what VocX keys a capture under

and refuses — loudly, naming the candidates — when a string denotes MORE THAN ONE person.
Silently picking the first match is the failure mode worth engineering against: it files
one person's work against another's name and nothing ever reports it.

Namesakes: two people with the same *full* name cannot both be on the roster (people has
UNIQUE(tenant_id, full_name)), but two can share a short handle, and that is the case this
refuses rather than guesses. The e-mail is the way out, which is why it resolves here.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationAppError
from app.models.registry import Person


def _describe(p: Person) -> str:
    return f"{p.full_name or p.name}" + (f" <{p.email}>" if p.email else "")


async def find_people(session: AsyncSession, tenant_id, name: str) -> list[Person]:
    """Every person on this tenant's roster that ``name`` could denote."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return []
    handle = func.lower(func.trim(Person.name))
    full = func.lower(func.trim(Person.full_name))
    email = func.lower(func.trim(Person.email))
    # The local part is compared in SQL rather than in Python so the roster is not read
    # whole: split_part is exact, and the column may be NULL.
    local = func.lower(func.split_part(func.trim(Person.email), "@", 1))
    rows = (await session.execute(
        select(Person).where(
            Person.tenant_id == tenant_id,
            Person.deleted_at.is_(None),
            or_(handle == wanted, full == wanted, email == wanted, local == wanted),
        ).order_by(Person.full_name))).scalars().all()
    # One person matched by two of their own names is still one person.
    seen, out = set(), []
    for p in rows:
        if p.id not in seen:
            seen.add(p.id)
            out.append(p)
    return out


async def resolve_person(session: AsyncSession, tenant_id, name: str, *,
                         label: str = "person") -> Person | None:
    """The one person ``name`` denotes, or None when the roster holds nobody by it.

    Raises when the string is AMBIGUOUS. Two colleagues can share a short handle, and
    "Priya" then names neither of them in particular; the caller is told who the
    candidates are and that a full name or e-mail settles it.
    """
    found = await find_people(session, tenant_id, name)
    if not found:
        return None
    if len(found) > 1:
        raise ValidationAppError(
            f"'{name}' matches {len(found)} people on record "
            f"({', '.join(_describe(p) for p in found)}). Use the full name or the "
            f"e-mail address so the {label} is unambiguous.")
    return found[0]


async def require_person(session: AsyncSession, tenant_id, name: str, *,
                         label: str = "person") -> Person:
    """resolve_person, but "nobody by that name" is an error too — with the same wording
    the field has always used, because operators have learned it."""
    person = await resolve_person(session, tenant_id, name, label=label)
    if person is None:
        raise ValidationAppError(
            f"Unknown {label} '{name}' — not a person on record. Add them under "
            f"People (Employees) first, or pick a name from that list.")
    return person


def canonical_name(person: Person) -> str:
    """The name to hand to another service when binding this identity. The full name,
    because that is what Access holds; the handle only if the roster row has no full
    name."""
    return (person.full_name or person.name or "").strip()
