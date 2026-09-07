"""Deal write rules (Field Rules sheet, deal slice).

One rule today: the conversion lineage is a locked first line. A converted deal's
``remarks`` begins with the stamp the conversion wrote — "Converted from lead
LD-### (Company). (approved by …)" — which is the relationship's provenance, not a
working note. The desk keeps ONE freely revisable remark on the line(s) BELOW it;
any update must carry the lineage line through unchanged. Admin (the platform's
correction lane) may rewrite the whole field, so a genuinely wrong stamp is still
fixable — audited like every other correction.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.errors import ValidationAppError
from app.models import Deal

_LINEAGE_PREFIX = "Converted from lead "


def lineage_line(remarks: str | None) -> str | None:
    """The locked first line, if this remarks value carries one."""
    first = (remarks or "").split("\n", 1)[0].strip()
    return first if first.startswith(_LINEAGE_PREFIX) else None


async def deal_pre_write(ctx: Any, body: dict, obj_id: Any) -> None:
    if obj_id is None or "remarks" not in body:
        return                                   # creates, and edits that skip remarks
    if ctx.user is not None and "Admin" in (ctx.user.roles or set()):
        return                                   # the correction lane
    current = (await ctx.session.execute(
        select(Deal.remarks).where(Deal.id == obj_id, Deal.tenant_id == ctx.tenant_id)
    )).scalar()
    locked = lineage_line(current)
    if locked is None:
        return                                   # no lineage — the field is free text
    new = body.get("remarks") or ""
    if new.split("\n", 1)[0].strip() == locked:
        return                                   # lineage carried through — the rest is theirs
    raise ValidationAppError(
        "The first line of a converted deal's remarks is its conversion record "
        f"({locked!r}) and cannot be changed. Add or revise your remark on the "
        "lines below it; an Admin can correct the record itself.")
