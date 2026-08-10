"""Recurring news digests — daily or weekly, emailed without anyone asking.

State is a JSON file on a mounted volume (``PULSE_SCHEDULE_FILE``). A database would be
the reflex, but PULSE is deliberately stateless with respect to the Register, and a
handful of schedules per tenant does not earn a schema, a migration and a connection.

THE STARTUP RULE, learned in the field and kept: a schedule whose slot passed while the
service was down is NOT run on boot. It is re-anchored to its next future slot. Otherwise
every restart fires a catch-up sweep at whoever is on the recipient list — the fastest way
to teach a desk to ignore its own alerts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from evam_backend_core.logging import get_logger

log = get_logger("pulse.schedules")


def next_run(cadence: str, hour: int, weekday: int, base: datetime | None = None) -> float:
    """The next slot as a POSIX timestamp. weekday: 0=Mon … 6=Sun (weekly only)."""
    now = base or datetime.now()
    run = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if cadence == "weekly":
        days = (int(weekday) - run.weekday()) % 7
        run = run + timedelta(days=days)
        if run <= now:
            run = run + timedelta(days=7)
    else:
        if run <= now:
            run = run + timedelta(days=1)
    return run.timestamp()


class ScheduleStore:
    """The schedule list, persisted. Guarded by a lock because the scheduler loop and the
    API both touch it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._items = json.load(fh)
        except (OSError, ValueError):
            self._items = []          # no file yet, or an unreadable one: start empty

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._items, fh, indent=2)
            os.replace(tmp, self.path)     # atomic: a crash mid-write cannot truncate it
        except OSError as exc:
            log.error("pulse_schedule_save_failed", extra={"error": str(exc)})

    @staticmethod
    def _key(tenant: str | None) -> str:
        """Tenant codes are compared case-INSENSITIVELY. A schedule filed as 'evam' and
        listed as 'EVAM' would otherwise vanish from the screen while sitting in the
        file, still firing — a silent failure that reads as data loss."""
        return (tenant or "").strip().casefold()

    def all(self, tenant: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if tenant is None:
                return list(self._items)
            want = self._key(tenant)
            return [s for s in self._items if self._key(s.get("tenant")) == want]

    def add(self, schedule: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(schedule)
            self.save()

    def remove(self, sid: str, tenant: str | None = None) -> bool:
        with self._lock:
            before = len(self._items)
            self._items[:] = [s for s in self._items
                              if s.get("id") != sid
                              or (tenant is not None
                                  and self._key(s.get("tenant")) != self._key(tenant))]
            if len(self._items) != before:
                self.save()
            return len(self._items) < before

    def get(self, sid: str, tenant: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            for s in self._items:
                if s.get("id") == sid and (tenant is None
                                           or self._key(s.get("tenant")) == self._key(tenant)):
                    return s
        return None

    def touch(self, sid: str, **fields: Any) -> None:
        with self._lock:
            for s in self._items:
                if s.get("id") == sid:
                    s.update(fields)
                    self.save()
                    return

    def rearm_stale(self) -> int:
        """Re-anchor every past-due schedule to its next FUTURE slot. Called once at
        startup so a restart never fires a catch-up digest."""
        now = time.time()
        moved = 0
        with self._lock:
            for s in self._items:
                try:
                    if float(s.get("next_run") or 0) <= now:
                        s["next_run"] = next_run(s.get("cadence", "daily"),
                                                 int(s.get("hour", 8)),
                                                 int(s.get("weekday", 0)))
                        moved += 1
                except (TypeError, ValueError):
                    continue
            if moved:
                self.save()
        return moved


async def run_schedule(schedule: dict[str, Any], *, search: Callable, send: Callable,
                       digest: Callable) -> tuple[bool, str]:
    """Search every term on the schedule, then email one digest. Firms with nothing to
    report are LEFT OUT rather than listed as empty — a digest of silence is noise."""
    dto = datetime.now().strftime("%Y-%m-%d")
    window = int(schedule.get("window_days", 7))
    dfrom = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d")
    terms = [t.strip() for t in re.split(r"[,\n]", schedule.get("q", "")) if t.strip()]
    adverse_only = bool(schedule.get("adverse_only"))

    log.info("pulse_schedule_running", extra={"id": schedule.get("id"),
                                              "terms": len(terms), "from": dfrom, "to": dto,
                                              "adverse_only": adverse_only})
    t0 = time.time()
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for term in terms:
        arts = await search(term, dfrom, dto)
        if adverse_only:
            arts = [a for a in arts if a.get("severity") in ("UGLY", "BAD")]
        if arts:
            groups.append((term, arts))
    total = sum(len(a) for _, a in groups)

    head = ('<div style="font-family:Segoe UI,Arial,sans-serif;max-width:720px;margin:8px auto;'
            f'color:#5F6E76;font-size:13px">{len(groups)} firm(s) with '
            f'{"adverse" if adverse_only else "matching"} news &middot; {dfrom} to {dto}</div>')
    body = head + "".join(digest(term, arts, dfrom, dto) + "<br>" for term, arts in groups)
    label = "PRISM " + ("risk (adverse)" if adverse_only else "news") + " digest"
    subject = schedule.get("subject") or (
        f"{label} — {len(groups)} firms, {total} items" if groups
        else f"{label} — nothing to report")

    ok, msg = await asyncio.to_thread(
        send, schedule.get("recipients", []), subject,
        body or "<p>No matching news in this window.</p>")
    log.info("pulse_schedule_done", extra={"id": schedule.get("id"),
                                           "seconds": round(time.time() - t0, 1),
                                           "firms": len(groups), "items": total,
                                           "result": msg})
    return ok, msg


async def scheduler_loop(store: ScheduleStore, runner: Callable, *,
                         tick_seconds: int = 60) -> None:
    """Wake once a minute, run whatever is due, re-arm it. Cancelled on shutdown."""
    moved = store.rearm_stale()
    if moved:
        log.info("pulse_schedules_rearmed", extra={"count": moved,
                                                   "note": "past-due slots moved forward; "
                                                           "no catch-up run on startup"})
    while True:
        try:
            await asyncio.sleep(tick_seconds)
            now = time.time()
            for s in store.all():
                if not s.get("active", True):
                    continue
                try:
                    due = float(s.get("next_run") or 0)
                except (TypeError, ValueError):
                    continue
                if due > now:
                    continue
                # Re-arm BEFORE running: a slow or failing run must not be retried in a
                # tight loop against the recipients' inboxes.
                store.touch(s["id"], next_run=next_run(s.get("cadence", "daily"),
                                                       int(s.get("hour", 8)),
                                                       int(s.get("weekday", 0))),
                            last_run=now)
                try:
                    await runner(s)
                except Exception as exc:  # noqa: BLE001 - one bad schedule keeps the loop
                    log.error("pulse_schedule_failed", extra={"id": s.get("id"),
                                                              "error": str(exc)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop itself must never die
            log.error("pulse_scheduler_tick_failed", extra={"error": str(exc)})
