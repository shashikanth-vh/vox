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

from app.news.names import watch_names

log = get_logger("pulse.schedules")

# A failed send is retried a few times, spaced out, then conceded until the next
# regular slot — never a tight loop against the recipients' inboxes.
MAX_RETRIES = 3
# How long, and how many, sent stories a schedule remembers so it does not send them
# again. Ninety days covers any digest window with room to spare.
SENT_MEMORY_DAYS = 90
SENT_MEMORY_MAX = 5000
RETRY_DELAY_S = 600
MAX_HISTORY = 10          # run outcomes kept per schedule, newest last


def desk_tz():
    """The timezone schedule hours are SPOKEN in. The desk says "7 AM" meaning its own
    clock; the container runs UTC, so without this a 7 o'clock digest lands at 12:30 in
    the afternoon. PULSE_DIGEST_TZ overrides; an unknown zone falls back to server time
    (and says so once) rather than refusing to schedule anything."""
    name = os.environ.get("PULSE_DIGEST_TZ", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad zone name / missing tzdata
        log.warning("pulse_digest_tz_unavailable", extra={"tz": name})
        return None


class ScheduleStoreError(RuntimeError):
    """The schedule could not be PERSISTED. Raised to the API caller so a schedule
    that exists only in memory — gone on the next restart — is never reported as
    created. (The silent-swallow version of this is exactly how the desk's digests
    vanished after an upgrade.)"""


def next_run(cadence: str, hour: int, weekday: int, base: datetime | None = None) -> float:
    """The next slot as a POSIX timestamp, computed on the DESK's clock (see desk_tz).
    weekday: 0=Mon … 6=Sun (weekly only)."""
    now = base or datetime.now(desk_tz())
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

    def save(self, strict: bool = True) -> None:
        """Persist, atomically. strict (the default, used by every API mutation) RAISES
        on failure — a schedule that cannot reach disk must fail the request, not
        survive as a phantom until the next restart. The scheduler loop's run-stamps
        pass strict=False: a full disk must not kill the loop."""
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._items, fh, indent=2)
            os.replace(tmp, self.path)     # atomic: a crash mid-write cannot truncate it
        except OSError as exc:
            log.error("pulse_schedule_save_failed", extra={"error": str(exc)})
            if strict:
                raise ScheduleStoreError(
                    f"could not persist the schedule file ({exc}) — check that the "
                    "pulse data volume is writable by the service user") from exc

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
                    self.save(strict=False)   # a run-stamp must never kill the loop
                    return

    def update(self, sid: str, tenant: str | None, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Edit a schedule in place, keeping its identity (id, tenant, run history)."""
        with self._lock:
            for s in self._items:
                if s.get("id") == sid and (tenant is None
                                           or self._key(s.get("tenant")) == self._key(tenant)):
                    s.update(fields)
                    self.save()
                    return dict(s)
        return None

    def record_run(self, sid: str, *, ok: bool, note: str,
                   firms: int = 0, items: int = 0, seconds: float | None = None) -> None:
        """Append one run outcome to the schedule's history — the desk's answer to
        "did Monday's digest actually go out?". Best-effort persist: a history write
        must never kill the loop."""
        with self._lock:
            for s in self._items:
                if s.get("id") == sid:
                    hist = s.setdefault("history", [])
                    hist.append({"at": time.time(), "ok": bool(ok),
                                 "note": str(note)[:300], "firms": firms, "items": items,
                                 **({"seconds": round(seconds, 1)} if seconds else {})})
                    del hist[:-MAX_HISTORY]
                    self.save(strict=False)
                    return

    def sent_keys(self, sid: str) -> set[str]:
        """The stories this schedule already sent (see run_schedule's ONLY NEW)."""
        with self._lock:
            for s in self._items:
                if s.get("id") == sid:
                    return set((s.get("sent") or {}).keys())
        return set()

    def remember_sent(self, sid: str, keys: list[str]) -> None:
        """Note what a digest just sent. Bounded twice — by age and by count — so the
        file stays small however long a schedule runs: a story older than the memory
        is outside any digest window anyway."""
        now = time.time()
        with self._lock:
            for s in self._items:
                if s.get("id") == sid:
                    sent = {k: t for k, t in (s.get("sent") or {}).items()
                            if now - float(t) < SENT_MEMORY_DAYS * 86400}
                    sent.update({k: now for k in keys if k})
                    if len(sent) > SENT_MEMORY_MAX:
                        sent = dict(sorted(sent.items(), key=lambda kv: kv[1])
                                    [-SENT_MEMORY_MAX:])
                    s["sent"] = sent
                    self.save(strict=False)
                    return

    def probe(self) -> tuple[bool, str]:
        """Can the store reach disk RIGHT NOW? Rewrites the file through the same
        save() every mutation uses, so the answer is the truth, not a guess. The
        permissions failure this exists for announced itself as data loss weeks
        after the fact; now it announces itself on boot and in the dialog."""
        try:
            with self._lock:
                self.save()
            return True, ""
        except ScheduleStoreError as exc:
            return False, str(exc)

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


# The name the press actually prints — "Acme Solar India Pvt. Ltd." searches as
# "Acme Solar India". Mirrors the UI's tradingName() so an all-firms digest built
# server-side covers the same names the dialog would have snapshotted.
_CORP_SUFFIX = re.compile(
    r"\s*[,.]?\s*\b(private limited|pvt\.?\s?ltd\.?|pvt\.?\s?limited|public limited"
    r"|limited|ltd\.?|llp|inc\.?|incorporated|corporation|corp\.?|plc|pte\.?\s?ltd\.?"
    r"|gmbh|s\.?a\.?|b\.?v\.?|&\s?co\.?|company)\b\.?\s*$", re.IGNORECASE)


def trading_name(name: str) -> str:
    original = (name or "").strip()
    s = original
    for _ in range(2):
        if not _CORP_SUFFIX.search(s):
            break
        s = _CORP_SUFFIX.sub("", s).strip()
    s = s.rstrip(", \t").strip()
    return s if len(s) >= 3 else original


# Searches in flight at once. The scheduled run used to ask one name at a time — a
# 400-firm digest was 400 serial waits on three news sources. The bound is modest on
# purpose: Google News and Bing answer a polite client and throttle a greedy one (the
# search engine's own upstream pool caps it again underneath).
DIGEST_CONCURRENCY = 6
# Extra passes over firms whose search had a gap, before the email goes out.
RETRY_PASSES = 2
# No retry pass starts once the run has been searching this long.
RETRY_PASS_BUDGET_S = 45 * 60


class Watch:
    """One firm on the watch list: its trading name, the aliases an analyst set, its
    key people, and whether the record is watched at all."""

    __slots__ = ("name", "aliases", "people", "watched")

    def __init__(self, name: str, aliases: list[str] | None = None,
                 people: list[dict[str, Any]] | None = None, watched: bool = True) -> None:
        self.name, self.aliases = name, aliases or []
        self.people, self.watched = people or [], watched


def _watch_item(entry: Any) -> Watch:
    """A firm from the register: a bare name, or ``{name, aliases, people, watch}``."""
    if isinstance(entry, dict):
        people = [p for p in (entry.get("people") or [])
                  if isinstance(p, dict) and str(p.get("name") or "").strip()]
        aliases = [str(a).strip() for a in (entry.get("aliases") or []) if str(a).strip()]
        return Watch(trading_name(str(entry.get("name") or "")), aliases, people,
                     entry.get("watch") is not False)
    return Watch(trading_name(str(entry or "")))


def _person_label(person: dict[str, Any]) -> str:
    role = str(person.get("role") or "").strip()
    name = str(person.get("name") or "").strip()
    return f"{role} · {name}" if role else name


def story_key(article: dict[str, Any]) -> str:
    """One story, however many sources carry it: the normalised headline. Used to say
    an article was already sent, and to spot two firms reporting the same story."""
    return re.sub(r"[^a-z0-9]+", "", str(article.get("title") or "").lower())[:80] \
        or str(article.get("url") or "")


async def _search_firm(watch: Watch, search: Callable, dfrom: str, dto: str,
                       gate: asyncio.Semaphore,
                       ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The firm's own news plus its key people's, as ONE list for one card. The firm
    is searched under all its watch names (full name, aliases or the automatic short
    name); a person by their name, with their city. A person's article carries
    ``about`` (role · name) so the card can say who it is about; an article that
    names both the firm and the person appears once.

    Also answers which sources were NOT searched for the firm (source → reason): a
    search may hand back ``(articles, sources)`` and every source that failed or was
    skipped for the firm's own name is named, so the email can say so."""
    down: dict[str, str] = {}

    async def one(term: str, names: list[str], context: str = "",
                  firm: bool = False) -> list[dict[str, Any]]:
        async with gate:
            try:
                got = await search(term, dfrom, dto, strict=True, context=context,
                                   names=names)
            except Exception as exc:  # noqa: BLE001 — one name never ends a digest
                log.warning("pulse_schedule_term_failed", extra={"term": term,
                                                                  "error": str(exc)})
                if firm:
                    down["all sources"] = str(exc)[:200]
                return []
        if isinstance(got, tuple):
            got, sources = got
            if firm:
                down.update({str(s.get("name")): str(s.get("error") or "")
                             for s in sources or [] if not s.get("ok")})
        return list(got)

    found = await asyncio.gather(
        one(watch.name, watch_names(watch.name, watch.aliases), firm=True),
        *(one(str(p["name"]).strip(), [str(p["name"]).strip()],
              str(p.get("city") or "").strip()) for p in watch.people))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for person, arts in zip([None, *watch.people], found, strict=True):
        for a in arts:
            key = story_key(a)
            if key in seen:
                continue
            seen.add(key)
            out.append({**a, "about": _person_label(person)} if person else a)
    return out, down


def merge_shared(groups: list[tuple[str, list[dict[str, Any]]]],
                 ) -> list[tuple[str, list[dict[str, Any]]]]:
    """Two records reporting the same stories are one firm to the reader ("Euroteck"
    and "Euroteck Environmental"): when half or more of the smaller card's stories are
    on the other card, the two become one card under both names."""
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for name, arts in groups:
        keys = {story_key(a) for a in arts}
        for i, (other, oarts) in enumerate(out):
            okeys = {story_key(a) for a in oarts}
            if len(keys & okeys) * 2 >= min(len(keys), len(okeys)):
                merged = oarts + [a for a in arts if story_key(a) not in okeys]
                out[i] = (f"{other} · {name}", merged)
                break
        else:
            out.append((name, arts))
    return out


def _counts_line(groups: list[tuple[str, list[dict[str, Any]]]], total: int, new: int,
                 window: int) -> str:
    """"2 new · 5 in the last 7 days" — or "nothing in the last 7 days"."""
    if not groups:
        return f"nothing in the last {window} days"
    return (f"{new} new · {total} item{'s' if total != 1 else ''} "
            f"in the last {window} days")


def _subject(label: str, groups: list[tuple[str, list[dict[str, Any]]]], total: int,
             new: int = 0, window: int = 7) -> str:
    """Lead with who needs attention, not with a count: the reader decides from the
    subject line whether to open it. Firms with something NEW are named first."""
    if not groups:
        return f"{label} — nothing in the last {window} days"
    rank = {"UGLY": 0, "BAD": 1}
    top = sorted(groups, key=lambda g: (not any(a.get("new") for a in g[1]),
                                        min((rank.get(a.get("severity", ""), 9)
                                             for a in g[1]), default=9), -len(g[1])))
    names = [n for n, _ in top[:2]]
    more = len(groups) - len(names)
    return (f"{label} — {', '.join(names)}"
            + (f" and {more} more firm{'s' if more != 1 else ''}" if more else "")
            + f" ({_counts_line(groups, total, new, window)})")


async def run_schedule(schedule: dict[str, Any], *, search: Callable, send: Callable,
                       digest: Callable,
                       firm_terms: Callable | None = None,
                       already_sent: set[str] | None = None,
                       on_sent: Callable[[list[str]], None] | None = None,
                       send_at: float | None = None,
                       stored: Callable | None = None,
                       ) -> tuple[bool, str, int, int]:
    """Search every term on the schedule, then email one digest. Firms with nothing to
    report are LEFT OUT rather than listed as empty — a digest of silence is noise.

    ``stored`` (an async ``(dfrom, dto) -> (pairs, coverage)``): when given, an
    all-firms schedule reads the monitor's findings instead of searching (see
    monitor.py) — ``pairs`` is (firm name, articles) per firm.

    An all-firms schedule covers the register AS OF SEND TIME: ``firm_terms`` (an async
    callable answering the current firms, each a name or ``{name, aliases, people,
    watch}``) is merged with the terms stored on the schedule — the stored snapshot
    keeps the desk's typed watch terms and stands alone as the fallback when the
    register cannot be reached, so a register outage costs freshness, never the digest
    itself. A record marked not watched is left out even if the snapshot names it. A
    firm's key people (directors, promoters) are searched with it and reported on its
    card.

    ONLY NEW (the default): a story this schedule already sent is not sent again —
    ``already_sent`` is what went out before, ``on_sent`` is told what went out now,
    once the send succeeded. The same Satyendar Jain arrest used to arrive every week.

    ``send_at`` (a POSIX time): the scheduler starts the search early and the email is
    held until then, so careful pacing never makes a digest late."""
    dto = datetime.now(desk_tz()).strftime("%Y-%m-%d")
    window = int(schedule.get("window_days", 7))
    dfrom = (datetime.now(desk_tz()) - timedelta(days=window)).strftime("%Y-%m-%d")
    watches = [Watch(t.strip()) for t in re.split(r"[,\n]", schedule.get("q", ""))
               if t.strip()]
    if schedule.get("scope") == "all-firms" and firm_terms is not None:
        try:
            fresh = [_watch_item(t) for t in await firm_terms()]
            by_name = {w.name.casefold(): w for w in fresh if w.name}
            off = {k for k, w in by_name.items() if not w.watched}
            # A stored term that is now a register firm takes the firm's details.
            watches = [by_name.get(w.name.casefold(), w) for w in watches]
            seen = {w.name.casefold() for w in watches}
            added = [w for k, w in by_name.items() if k not in seen]
            watches = [w for w in watches + added if w.name.casefold() not in off]
            log.info("pulse_schedule_firms_refreshed",
                     extra={"id": schedule.get("id"), "added": len(added),
                            "not_watched": len(off),
                            "with_people": sum(1 for w in watches if w.people),
                            "with_aliases": sum(1 for w in watches if w.aliases)})
        except Exception as exc:  # noqa: BLE001 — freshness lost, digest kept
            log.warning("pulse_schedule_firms_refresh_failed",
                        extra={"id": schedule.get("id"), "error": str(exc)})
    adverse_only = bool(schedule.get("adverse_only"))
    only_new = schedule.get("only_new", True) is not False
    before = already_sent if only_new and already_sent else set()

    log.info("pulse_schedule_running", extra={"id": schedule.get("id"),
                                              "terms": len(watches), "from": dfrom,
                                              "to": dto, "adverse_only": adverse_only,
                                              "only_new": only_new})
    t0 = time.time()
    retried = 0
    if schedule.get("scope") == "all-firms" and stored is not None:
        # THE MONITOR HAS ALREADY SEARCHED. Its findings are in the Register, filed
        # over the day; the digest reads them rather than asking Google for five
        # hundred firms in the hour before the email. Coverage is the monitor's: which
        # firms it could not completely search in the window, and why.
        pairs, coverage = await stored(dfrom, dto)
        schedule["_search_seconds"] = round(time.time() - t0, 1)
        return await _send_digest(schedule, pairs, coverage, send=send, digest=digest,
                                  before=before, on_sent=on_sent, send_at=send_at,
                                  dfrom=dfrom, dto=dto, adverse_only=adverse_only,
                                  t0=t0, retried=0)
    gate = asyncio.Semaphore(DIGEST_CONCURRENCY)
    found = list(await asyncio.gather(*(_search_firm(w, search, dfrom, dto, gate)
                                        for w in watches)))
    # NO FIRM LEFT BEHIND: a firm whose search had a gap (a source throttled past its
    # retries, a timeout) is searched again, up to RETRY_PASSES more times, before the
    # email goes out. Only what is still missing after that is reported as such.
    for _ in range(RETRY_PASSES):
        gaps = [i for i, (_, down) in enumerate(found) if down]
        if not gaps:
            break
        if time.time() - t0 > RETRY_PASS_BUDGET_S:
            # A source that is down for the morning is reported, not chased for hours.
            log.warning("pulse_schedule_retry_budget_spent",
                        extra={"id": schedule.get("id"), "firms": len(gaps)})
            break
        retried += len(gaps)
        log.info("pulse_schedule_retry_pass", extra={"id": schedule.get("id"),
                                                     "firms": len(gaps)})
        again = await asyncio.gather(*(_search_firm(watches[i], search, dfrom, dto, gate)
                                       for i in gaps))
        for i, res in zip(gaps, again, strict=True):
            found[i] = res
    # The search time (not the hold below) is what the next lead time is learnt from.
    schedule["_search_seconds"] = round(time.time() - t0, 1)
    # Which sources were not searched, for how many firms, and why (first reason seen).
    down_count: dict[str, int] = {}
    down_why: dict[str, str] = {}
    for _, down in found:
        for src, why in down.items():
            down_count[src] = down_count.get(src, 0) + 1
            down_why.setdefault(src, why)
    coverage = {"firms": len(watches), "down": down_count, "why": down_why}
    pairs = [(w.name, arts) for w, (arts, _) in zip(watches, found, strict=True)]
    return await _send_digest(schedule, pairs, coverage, send=send, digest=digest,
                              before=before, on_sent=on_sent, send_at=send_at,
                              dfrom=dfrom, dto=dto, adverse_only=adverse_only,
                              t0=t0, retried=retried)


async def _send_digest(schedule: dict[str, Any],
                       pairs: list[tuple[str, list[dict[str, Any]]]],
                       coverage: dict[str, Any], *, send: Callable, digest: Callable,
                       before: set[str], on_sent: Callable[[list[str]], None] | None,
                       send_at: float | None, dfrom: str, dto: str, adverse_only: bool,
                       t0: float, retried: int) -> tuple[bool, str, int, int]:
    """Filter what was found (adverse only), mark what is new, compose and send — the
    same for a digest that searched and one that read the monitor's findings.

    The email covers the WHOLE window every time. It used to drop every story sent
    before ("only new"), so a "7-day" digest on a quiet morning said "0 items" while
    four adverse stories sat inside the week (production, 20–23 Oct). Now a story sent
    before stays in, marked as such, and what is new since the last email is marked NEW
    and counted in the subject. Old stories resurfacing (the weekly Satyendar Jain
    arrest) are still kept out by the window, which reads each story's own date."""
    if send_at is not None and send_at > time.time():
        # Searched early so the email lands at the scheduled minute, not after the run.
        await asyncio.sleep(send_at - time.time())
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    repeats = 0
    down_count: dict[str, int] = coverage.get("down") or {}
    n_firms = int(coverage.get("firms") or len(pairs))
    for name, arts in pairs:
        if adverse_only:
            arts = [a for a in arts if a.get("severity") in ("UGLY", "BAD")]
        marked = [{**a, "new": story_key(a) not in before} for a in arts]
        repeats += sum(1 for a in marked if not a["new"])
        if marked:
            groups.append((name, marked))
    groups = merge_shared(groups)
    total = sum(len(a) for _, a in groups)
    new_total = sum(1 for _, arts in groups for a in arts if a.get("new"))

    # One page for the whole run: the masthead once, then a card per firm — and the
    # coverage, even when nothing was found: "nothing new" with Google down for most
    # firms is not the same email as "nothing new".
    body = digest(groups, dfrom, dto, adverse_only=adverse_only, coverage=coverage)
    label = "PRISM " + ("adverse news" if adverse_only else "news") + " digest"
    window = int(schedule.get("window_days", 7) or 7)
    # A desk's own subject line keeps its words and gains the counts, so "0 new" is
    # never read as "nothing this week".
    subject = (f"{schedule['subject']} — {_counts_line(groups, total, new_total, window)}"
               if schedule.get("subject") else _subject(label, groups, total, new_total, window))

    ok, msg = await asyncio.to_thread(
        send, schedule.get("recipients", []), subject,
        body or "<p>No matching news in this window.</p>")
    if ok and on_sent is not None and new_total:
        on_sent([story_key(a) for _, arts in groups for a in arts if a.get("new")])
    log.info("pulse_schedule_done", extra={"id": schedule.get("id"),
                                           "seconds": round(time.time() - t0, 1),
                                           "firms": len(groups), "items": total,
                                           "new": new_total, "sent_before": repeats,
                                           "retried_firms": retried,
                                           "sources_down": down_count, "result": msg})
    if not down_count:
        msg += f" — all {n_firms} firms searched on every source"
    else:
        msg += " — not searched: " + ", ".join(
            f"{src} for {n} of {n_firms} firms" for src, n in down_count.items())
    return ok, msg, len(groups), total


# Lead time for a schedule with no history yet: Google is paced to ~400 requests an
# hour, so an all-firms digest of ~500 firms takes 75-80 minutes.
DEFAULT_LEAD_S = 90 * 60
MAX_LEAD_S = 3 * 3600


def lead_seconds(schedule: dict[str, Any]) -> float:
    """How long before its slot a schedule's search should start: the slowest of its
    last three searches, plus a quarter and five minutes; the default when unknown."""
    took = [float(h["seconds"]) for h in (schedule.get("history") or [])[-3:]
            if isinstance(h, dict) and h.get("seconds")]
    if not took:
        return DEFAULT_LEAD_S
    return min(max(took) * 1.25 + 300, MAX_LEAD_S)


def _permanent(msg: str) -> bool:
    """A failure no retry can fix. Email that is not configured stays not configured;
    retrying it re-ran the whole all-firms search three more times each morning on
    cipher-test (four Google sweeps in 40 minutes) for an email that could not go."""
    return msg.startswith("Email is not configured")


async def scheduler_loop(store: ScheduleStore, runner: Callable, *,
                         tick_seconds: int = 60,
                         lead: Callable[[dict[str, Any]], float] | None = None) -> None:
    """Wake once a minute, run whatever is due, re-arm it. Cancelled on shutdown.
    ``lead`` overrides how early a schedule starts (``lead_seconds`` by default) — a
    digest that reads the monitor's findings needs minutes, not an hour and a half."""
    lead_of = lead or lead_seconds
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
                # Start EARLY by the time the search takes, and hold the email until the
                # slot: pacing that keeps Google answering must not make a digest late.
                # (A retry after a failed send keeps its own short delay: no lead.)
                early = 0.0 if int(s.get("retry") or 0) else lead_of(s)
                if due - early > now:
                    continue
                # Re-arm BEFORE running: a slow or failing run must not be retried in a
                # tight loop against the recipients' inboxes.
                # The slot AFTER this one: computed from "now", an early start (01:10 for
                # a 01:30 slot) found 01:30 still ahead and re-armed the same slot — the
                # digest ran twice (cipher-test, 16 Oct: 01:10 and again at 04:09).
                after = datetime.fromtimestamp(max(due, now) + 60, desk_tz())
                store.touch(s["id"], next_run=next_run(s.get("cadence", "daily"),
                                                       int(s.get("hour", 8)),
                                                       int(s.get("weekday", 0)), after),
                            last_run=now)
                try:
                    res = await runner(s, send_at=due)
                    ok, msg, firms, items = (res if isinstance(res, tuple) and len(res) == 4
                                             else (True, "", 0, 0))
                except Exception as exc:  # noqa: BLE001 - one bad schedule keeps the loop
                    ok, msg, firms, items = False, f"run crashed: {exc}", 0, 0
                    log.error("pulse_schedule_failed", extra={"id": s.get("id"),
                                                              "error": str(exc)})
                attempt = int(s.get("retry") or 0)
                if ok:
                    if attempt:
                        store.touch(s["id"], retry=0)
                    store.record_run(s["id"], ok=True, note=msg, firms=firms, items=items,
                                     seconds=s.pop("_search_seconds", None))
                elif attempt < MAX_RETRIES and not _permanent(msg):
                    # The day's digest is not lost to one SMTP hiccup: come back soon,
                    # a bounded number of times, then concede until the next slot.
                    store.touch(s["id"], next_run=time.time() + RETRY_DELAY_S,
                                retry=attempt + 1)
                    store.record_run(s["id"], ok=False, firms=firms, items=items,
                                     note=f"{msg} — retry {attempt + 1} of {MAX_RETRIES} "
                                          f"in {RETRY_DELAY_S // 60} min")
                else:
                    store.touch(s["id"], retry=0)
                    store.record_run(s["id"], ok=False, firms=firms, items=items,
                                     note=f"{msg} — " + ("not retried: fix the setting first"
                                                         if _permanent(msg) else
                                                         "giving up until the next slot"),
                                     seconds=s.pop("_search_seconds", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop itself must never die
            log.error("pulse_scheduler_tick_failed", extra={"error": str(exc)})
