"""The desk's calendar: which DAY a register stamp lands on.

The register runs on UTC and the desk works in India. ``datetime.now(UTC).date()``
and ``date.today()`` (the container's clock, also UTC) both answer the UTC day,
which from 00:00 to 05:30 IST is still YESTERDAY: a stage moved at 9 am IST was
dated correctly, a stage moved at 1 am IST (a late night before a committee) was
dated the day before, and "last touch" on a lead logged after midnight read as
the previous day. Every date the register derives from "now" or from a
timestamp goes through here instead.

``tenant_timezone`` (REGISTER_TENANT_TIMEZONE) names the zone; the default is
Asia/Kolkata. An unknown zone name falls back to UTC rather than refusing to
boot — a wrong date is recoverable, a dead register is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import get_settings


@lru_cache(maxsize=4)
def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def tenant_zone() -> tzinfo:
    return _zone(get_settings().tenant_timezone or "UTC")


def tenant_now() -> datetime:
    """The current instant, tz-aware, in the desk's zone."""
    return datetime.now(UTC).astimezone(tenant_zone())


def tenant_today() -> date:
    """The desk's calendar day right now."""
    return tenant_now().date()


def tenant_date(moment: datetime | date | None) -> date | None:
    """The desk's calendar day of ``moment``.

    A tz-aware timestamp is converted; a NAIVE one is taken as UTC (which is how
    the database hands back ``timestamp without time zone`` columns written from
    ``datetime.now(UTC)``); a bare date is already a day and passes through.
    """
    if moment is None:
        return None
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(tenant_zone()).date()
    return moment
