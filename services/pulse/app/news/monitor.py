"""The monitor — the news searched overnight, a slice an hour, and kept.

WHY. Google News lets a cloud host ask ~450 searches an hour, however politely they
are spaced. The digest used to ask all of them in the hour before the email — five
hundred firms and their directors — so it ran into that wall every morning, and every
fix (spacing, pauses, retries, a twenty-minute give-up) only decided how gracefully it
waited once it was over. Spread across the day the same allowance is ~10,000 searches:
room for every firm, its people and an adverse-words search, twice a day.

So the monitor wakes once an hour and searches the firms whose last COMPLETE search is
oldest, within a Google budget for the hour (``google_per_hour``, below Google's limit
so a desk's own searches still get through). A firm whose search had a gap is not
marked searched, so it is first in line next hour — no firm is left behind, and none
is chased for hours. Each batch also reads the official sources (SEBI orders, IBBI
insolvency announcements) once for the whole book.

ONLY AT NIGHT (``hours``, 00:00-06:00 on the desk's clock by default). The book is ~1,100
Google searches a pass — two a firm, one more for its people — so six batches of 300
cover it with a retry hour to spare, the day's searching stays out of the desk's way, and
the last batch ends an hour before the 7 AM digest reads what was filed. A firm is due
again after ``refresh_hours`` (20), so each one is searched once a night. "Scan all now"
and the history pass are asked for, and run whenever they are asked.

WHAT IT FINDS IS FILED IN THE REGISTER, as ``external-intelligence`` rows (News and
Regulatory), each once per firm and story. The Register is the source of truth and its
RBAC holds: the News Radar reads them as the signed-in user, and the all-firms digest
reads them instead of searching — so the 7 AM email takes seconds and is never late.

"Scan all now" is the same pass over every firm, ignoring the hourly budget (the
Google gate still paces it), in the background; its progress is the monitor's status.

State (which firm was searched when, what was filed, the official sources' last read)
is a JSON file on the PULSE volume, like the schedules: a few hundred rows a tenant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from evam_backend_core.logging import get_logger

from app.news import official
from app.news.evidence import read_evidence
from app.news.names import too_common, watch_names
from app.news.schedules import desk_tz, story_key
from app.news.search import in_window, mentions

log = get_logger("pulse.monitor")

GOOGLE = "Google News"
# How long the monitor remembers a story it filed for a firm. Longer than any digest or
# screen window, so a story is never filed twice for the same firm.
FILED_MEMORY_DAYS = 120
FILED_MEMORY_MAX = 50000
# Firms searched at once. The Google gate is the real pace; this only overlaps the
# Bing and Register round trips.
CONCURRENCY = 4
# A firm searched again looks back to a little before its last complete search: a
# source dates a story when IT saw it, often a few days late.
OVERLAP_DAYS = 3


def parse_hours(spec: str) -> tuple[int, int] | None:
    """``"0-6"`` -> (0, 6): batches START from 00:00 until (not at) 06:00, desk time.
    ``"22-5"`` wraps midnight. Blank, ``"all"`` or ``"0-24"`` -> None: all day. Anything
    else is refused — a typo must not quietly turn the night window into all day."""
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "0-24"):
        return None
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", spec)
    if not m or int(m[1]) > 23 or int(m[2]) > 24 or int(m[1]) == int(m[2]) % 24:
        raise ValueError(f"monitor hours {spec!r}: expected START-END, e.g. 0-6")
    return int(m[1]), int(m[2]) % 24


def in_hours(hours: tuple[int, int] | None, now: datetime) -> bool:
    if hours is None:
        return True
    start, end = hours
    return start <= now.hour < end if start < end else now.hour >= start or now.hour < end


def hours_label(hours: tuple[int, int] | None) -> str | None:
    return None if hours is None else f"{hours[0]:02d}:00–{hours[1]:02d}:00"


@dataclass
class MonitorConfig:
    enabled: bool = True
    google_per_hour: int = 300
    refresh_hours: float = 12.0
    window_days: int = 30
    batch_every_s: int = 3600
    # When hourly batches may START, on the desk's clock (see parse_hours); None = all day.
    hours: tuple[int, int] | None = None
    ibbi_pages: int = 3
    ibbi_history_pages: int = 100
    # The history pass ("📜 12-month history") searches this far back for every firm.
    history_days: int = 365
    sebi: bool = True
    ibbi: bool = True
    nse: bool = True
    adverse_query: bool = True


class MonitorStoreError(RuntimeError):
    pass


class MonitorStore:
    """The monitor's memory, persisted atomically, guarded by a lock (the loop and the
    API both touch it). One section per tenant."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as exc:
            # A corrupt file costs the monitor its memory (every firm is due again),
            # never the service.
            log.error("pulse_monitor_store_unreadable", extra={"path": self.path,
                                                               "error": str(exc)})
            self._data = {}

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.error("pulse_monitor_save_failed", extra={"error": str(exc)})

    def probe(self) -> tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path + ".probe", "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(self.path + ".probe")
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def tenant(self, tenant: str) -> dict[str, Any]:
        with self._lock:
            t = self._data.setdefault(tenant.upper(), {})
            t.setdefault("firms", {})
            t.setdefault("filed", {})
            t.setdefault("official", {})
            t.setdefault("run", {})
            t.setdefault("next_batch", 0.0)
            return t

    def was_filed(self, tenant: str, key: str) -> bool:
        with self._lock:
            return key in self.tenant(tenant)["filed"]

    def mark_filed(self, tenant: str, key: str) -> None:
        with self._lock:
            filed = self.tenant(tenant)["filed"]
            filed[key] = time.time()
            if len(filed) > FILED_MEMORY_MAX:
                cutoff = time.time() - FILED_MEMORY_DAYS * 86400
                for k in [k for k, at in filed.items() if at < cutoff]:
                    del filed[k]
                while len(filed) > FILED_MEMORY_MAX:
                    del filed[min(filed, key=filed.get)]

    def firm(self, tenant: str, entity_id: str) -> dict[str, Any]:
        with self._lock:
            return self.tenant(tenant)["firms"].setdefault(entity_id, {})


def filed_key(tenant: str, entity_id: str, article: dict[str, Any]) -> str:
    """One story about one firm, however many sources and URLs carry it."""
    ident = story_key(article)
    return f"pulse:{tenant}:{entity_id}:{hashlib.sha256(ident.encode()).hexdigest()[:32]}"


SIGNAL_OF = {"UGLY": "RED", "BAD": "AMBER", "GOOD": "GREEN", "POLICY": "BLUE"}


def intel_row(firm: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    """The Register row for one finding. The payload keeps everything the screen and
    the digest need to show it as the radar does — severity, why, who it is about,
    which name it was found under — plus the evidence reading."""
    when = article.get("when") or ""
    observed = None
    if len(when) >= 8 and when[:8].isdigit():
        observed = f"{when[:4]}-{when[4:6]}-{when[6:8]}T00:00:00+05:30"
    official_rec = bool(article.get("official"))
    return {
        "entity_id": firm["id"],
        "intel_type": "Regulatory" if official_rec else "News",
        "source": f"PULSE:{article.get('source') or article.get('via') or ''}"[:120],
        "signal": SIGNAL_OF.get(str(article.get("severity") or ""), "AMBER"),
        "title": str(article.get("title") or "")[:400],
        "summary": (article.get("snippet") or None),
        "url": article.get("url") or None,
        "observed_at": observed,
        "payload": {k: v for k, v in {
            "severity": article.get("severity"), "category": article.get("category"),
            "reason": article.get("reason"), "matched": article.get("matched"),
            "about": article.get("about"), "via": article.get("via"),
            "publisher": article.get("source"), "when": when,
            "evidence": article.get("evidence"), "story_key": story_key(article),
            "record_type": article.get("record_type"), "monitor": True,
        }.items() if v not in (None, "")},
    }


def with_evidence(article: dict[str, Any]) -> dict[str, Any]:
    """Adverse stories carry the evidence reading; a win needs none."""
    if article.get("evidence") or article.get("severity") not in ("UGLY", "BAD"):
        return article
    return {**article, "evidence": read_evidence(article.get("title", ""),
                                                 article.get("snippet", ""))}


# The designations that make a lead's contact someone whose news is the firm's news.
# Managers, consultants, aggregators and BD people are not; a contact with no
# designation is not guessed at.
LEADER_ROLE = re.compile(
    r"\b(director|md|managing|ceo|cfo|coo|founder|co[- ]?founder|promot[eo]r|chairman|"
    r"chairperson|chair|owner|proprietor|partner|president|whole[- ]?time|wtd|"
    r"chief executive|chief financial|board)\b", re.I)


def lead_people(leads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Promoters and directors per entity, from the desk's leads: the contact, when the
    designation is a leadership role and the name has two words or more ("Prashant"
    alone names thousands). ``source: lead`` tells the monitor to hold these to a
    stricter test than people an analyst listed (see Monitor._search_firm)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        eid = str(lead.get("entity_id") or "")
        name = re.sub(r"\s+", " ", str(lead.get("contact") or "")).strip()
        role = re.sub(r"\s+", " ", str(lead.get("designation") or "")).strip()
        words = re.findall(r"[A-Za-z]{2,}", name)
        if not eid or len(words) < 2 or len(words) != len(name.split()) \
                or not LEADER_ROLE.search(role):
            continue
        people = out.setdefault(eid, [])
        if all(p["name"].casefold() != name.casefold() for p in people):
            people.append({"name": name.title() if name.islower() else name,
                           "role": role[:60], "city": str(lead.get("city") or "").strip(),
                           "source": "lead"})
    return out


def merge_people(curated: list[dict[str, Any]], from_leads: list[dict[str, Any]],
                 ) -> list[dict[str, Any]]:
    """An analyst's list first; a lead contact only when it is not already on it."""
    seen = {str(p.get("name") or "").strip().casefold() for p in curated}
    return [*curated, *[p for p in from_leads if p["name"].casefold() not in seen]]


def article_of(row: dict[str, Any]) -> dict[str, Any]:
    """A stored intel row back in the article shape the digest and the screen render."""
    p = row.get("payload") or {}
    return {k: v for k, v in {
        "title": row.get("title") or "", "url": row.get("url") or "",
        "source": p.get("publisher") or str(row.get("source") or "").replace("PULSE:", ""),
        "when": p.get("when") or "", "via": p.get("via"),
        "severity": p.get("severity") or {"RED": "UGLY", "AMBER": "BAD", "GREEN": "GOOD",
                                          "BLUE": "POLICY"}.get(row.get("signal") or "", "BAD"),
        "category": p.get("category"), "reason": p.get("reason"),
        "about": p.get("about"), "matched": p.get("matched"),
        "evidence": p.get("evidence"), "snippet": row.get("summary") or "",
        "official": row.get("intel_type") == "Regulatory" or None,
    }.items() if v not in (None, "")}


def findings_digest(rows: list[dict[str, Any]], firms: list[dict[str, Any]],
                    state: dict[str, Any], dfrom: str, dto: str,
                    ) -> tuple[list[tuple[str, list[dict[str, Any]]]], dict[str, Any]]:
    """(firm name, articles) per watched firm, and the coverage line, from the rows the
    monitor filed. A story is in when it was published inside the window (with the
    search's grace) — a first full pass files a month of history, and a weekly digest
    must not send all of it as this week's news.

    Coverage is honest about the monitor: a firm not COMPLETELY searched since the
    window opened is counted against the source that failed it (or "not reached yet"
    when it is still in the queue), and an official source that has not been read in a
    day is named too."""
    by_id = {str(f["id"]): f for f in firms}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        firm = by_id.get(str(r.get("entity_id")))
        if firm is None or r.get("is_dismissed"):
            continue
        a = article_of(r)
        if in_window(a, dfrom, dto):
            groups.setdefault(str(firm["id"]), []).append(a)
    pairs = [(str(by_id[i].get("name") or ""), arts) for i, arts in groups.items()]
    try:
        opened = datetime.strptime(dfrom, "%Y-%m-%d").replace(tzinfo=desk_tz()).timestamp()
    except ValueError:
        opened = time.time() - 7 * 86400
    down: dict[str, int] = {}
    why: dict[str, str] = {}
    for f in firms:
        st = (state.get("firms") or {}).get(str(f["id"])) or {}
        if float(st.get("last_ok") or 0) >= opened:
            continue
        reasons = st.get("down") or {"News search": "not reached yet — still in the "
                                                    "monitor's queue"}
        for src, because in reasons.items():
            down[src] = down.get(src, 0) + 1
            why.setdefault(src, str(because or "no answer"))
    for src, o in (state.get("official") or {}).items():
        if not o.get("ok") or time.time() - float(o.get("at") or 0) > 86400:
            down[src] = len(firms)
            why[src] = (str(o.get("detail") or "") if not o.get("ok")
                        else "not read in the last day")
    return pairs, {"firms": len(firms), "down": down, "why": why}


FirmsFn = Callable[[str], Awaitable[list[dict[str, Any]]]]
FileFn = Callable[[str, str, dict[str, Any]], Awaitable[None]]   # tenant, key, row


class Monitor:
    """Runs the hourly batches and the on-demand full pass. ``firms(tenant)`` answers the
    tenant's watched firms (id, name, legal_name, aliases, people, city, group);
    ``file(tenant, row)`` writes one intel row to the Register."""

    def __init__(self, store: MonitorStore, search: Any, firms: FirmsFn, file: FileFn,
                 config: MonitorConfig, tenants: list[str]) -> None:
        self.store, self.search, self.firms, self.file = store, search, firms, file
        self.config = config
        self.tenants = [t.upper() for t in tenants if t.strip()]
        self.running: dict[str, asyncio.Task] = {}
        # The last firm list read per tenant, so a status poll does not re-read the
        # whole register every few seconds while a pass runs.
        self.last_firms: dict[str, list[dict[str, Any]]] = {}

    async def watched(self, tenant: str) -> list[dict[str, Any]]:
        firms = [f for f in await self.firms(tenant.upper()) if f.get("watch", True)]
        self.last_firms[tenant.upper()] = firms
        return firms

    async def scan_firm(self, tenant: str, entity_id: str) -> int | None:
        """Search one firm now (the screen's per-firm Scan). None when it is not a
        watched firm of this tenant."""
        tenant = tenant.upper()
        firm = next((f for f in await self.watched(tenant) if str(f["id"]) == entity_id),
                    None)
        if firm is None:
            return None
        self.store.firm(tenant, entity_id)["name"] = firm.get("name", "")
        n = await self._search_firm(tenant, firm)
        self.store.save()
        return n

    # ------------------------------------------------------------------ hours
    def in_hours(self, now: datetime | None = None) -> bool:
        return in_hours(self.config.hours, now or datetime.now(desk_tz()))

    def opens_at(self, now: datetime | None = None) -> float:
        """When the next hourly batch may start: now inside the window, else the hour
        it next opens."""
        now = now or datetime.now(desk_tz())
        if self.in_hours(now) or self.config.hours is None:
            return now.timestamp()
        opens = now.replace(hour=self.config.hours[0], minute=0, second=0, microsecond=0)
        if opens <= now:
            opens += timedelta(days=1)
        return opens.timestamp()

    # ------------------------------------------------------------------ status
    def status(self, tenant: str, firms: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        t = self.store.tenant(tenant)
        now = time.time()
        fresh_cut = now - self.config.refresh_hours * 3600
        day_cut = now - 86400
        rows = t["firms"]
        if firms is None:
            firms = self.last_firms.get(tenant.upper())
        known = firms if firms is not None else [
            {"id": k, "name": v.get("name", "")} for k, v in rows.items()]
        searched_fresh = searched_day = never = 0
        gaps: list[dict[str, Any]] = []
        for f in known:
            st = rows.get(str(f["id"])) or {}
            ok_at = float(st.get("last_ok") or 0)
            if ok_at >= fresh_cut:
                searched_fresh += 1
            if ok_at >= day_cut:
                searched_day += 1
            if not ok_at:
                never += 1
            if st.get("down") or not ok_at or ok_at < day_cut:
                gaps.append({"id": f["id"], "name": f.get("name") or st.get("name", ""),
                             "last_ok": ok_at or None, "down": st.get("down") or {},
                             "last_try": st.get("last_try") or None})
        gaps.sort(key=lambda g: g["last_ok"] or 0)
        run = dict(t["run"])
        run["running"] = tenant.upper() in self.running
        # Whose promoters are searched, and where their names came from: a firm with
        # nobody on file is searched by its name alone, and the screen says how many.
        with_people = [f for f in known if f.get("people")]
        people = {
            "firms_with_people": len(with_people),
            "from_leads": sum(1 for f in with_people
                              if any(p.get("source") == "lead" for p in f["people"])),
            "people": sum(len(f["people"]) for f in with_people),
        }
        return {
            "enabled": self.config.enabled, "people": people,
            "firms": len(known), "searched_fresh": searched_fresh,
            "searched_24h": searched_day, "never_searched": never,
            "refresh_hours": self.config.refresh_hours,
            "google_per_hour": self.config.google_per_hour,
            "hours": hours_label(self.config.hours),
            # Outside the window the next batch is when it opens, not an hour from now.
            "next_batch": (max(float(t["next_batch"] or 0), self.opens_at())
                           if self.config.enabled else None),
            "run": run, "gaps": gaps[:100], "gap_count": len(gaps),
            "official": t["official"],
            "not_covered": official.NOT_COVERED,
        }

    # ------------------------------------------------------------------ loop
    async def loop(self, tick_seconds: int = 60) -> None:
        """Wake once a minute; start a tenant's batch when it is due and the window is
        open. Cancelled on shutdown. A restart does not fire a catch-up storm: the next
        batch is simply due, and it is one hour's budget like any other."""
        while True:
            try:
                await asyncio.sleep(tick_seconds)
                if not self.config.enabled or not self.in_hours():
                    continue
                for tenant in self.tenants:
                    t = self.store.tenant(tenant)
                    if tenant in self.running or time.time() < float(t["next_batch"] or 0):
                        continue
                    self.start(tenant, full=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop itself must never die
                log.error("pulse_monitor_tick_failed", extra={"error": str(exc)})

    def start(self, tenant: str, *, full: bool, history: bool = False,
              official_only: bool = False) -> bool:
        """Start a pass in the background. False when one is already running.
        ``official_only`` reads the official sources' full history and matches every
        firm — minutes, and no Google at all."""
        tenant = tenant.upper()
        if tenant in self.running:
            return False
        task = asyncio.create_task(
            self.run_official(tenant) if official_only
            else self.run(tenant, full=full, history=history))
        self.running[tenant] = task

        def finished(_: asyncio.Task) -> None:
            self.running.pop(tenant, None)

        task.add_done_callback(finished)
        return True

    # ------------------------------------------------------------------ one pass
    async def run_official(self, tenant: str) -> dict[str, Any]:
        """The official sources only, with their history: no Google, a few minutes."""
        tenant = tenant.upper()
        t = self.store.tenant(tenant)
        started = time.time()
        t["run"] = {"kind": "official", "started": started, "done": 0, "total": 0,
                    "filed": 0, "note": "reading the official records' history…"}
        self.store.save()
        try:
            firms = await self.watched(tenant)
            filed = await self._official(tenant, firms, history=True)
            o = t["official"]
            note = (f"{filed} new findings from official records; "
                    + "; ".join(f"{k}: {v.get('detail')}" for k, v in o.items()))
        except Exception as exc:  # noqa: BLE001 - reported on the status
            filed, note = 0, f"could not read the register: {exc}"
        t["run"].update(finished=time.time(), filed=filed, note=note,
                        seconds=round(time.time() - started, 1))
        self.store.save()
        return t["run"]

    async def run(self, tenant: str, *, full: bool = False,
                  history: bool = False) -> dict[str, Any]:
        tenant = tenant.upper()
        t = self.store.tenant(tenant)
        started = time.time()
        # Re-armed FIRST: a pass that crashes must not be retried in a tight loop.
        t["next_batch"] = started + self.config.batch_every_s
        kind = "history" if history else "full" if full else "hourly"
        t["run"] = {"kind": kind, "started": started, "done": 0, "total": 0,
                    "filed": 0, "note": "reading the register…"}
        self.store.save()
        try:
            firms = await self.watched(tenant)
        except Exception as exc:  # noqa: BLE001 - reported on the status, retried next hour
            log.warning("pulse_monitor_firms_failed", extra={"tenant": tenant,
                                                             "error": str(exc)})
            t["run"].update(finished=time.time(), note=f"could not read the register: {exc}")
            self.store.save()
            return t["run"]
        for f in firms:
            self.store.firm(tenant, str(f["id"]))["name"] = f.get("name", "")
        filed = 0
        filed += await self._official(tenant, firms, history=history)
        # Which firms: all of them (full), or those whose last complete search is older
        # than the refresh interval, oldest first (never-searched first of all).
        def last_ok(f: dict[str, Any]) -> float:
            return float(self.store.firm(tenant, str(f["id"])).get("last_ok") or 0)
        due = sorted(firms, key=last_ok)
        if not full:
            cut = time.time() - self.config.refresh_hours * 3600
            due = [f for f in due if last_ok(f) < cut]
        t["run"].update(total=len(due), note=f"searching {len(due)} firms")
        self.store.save()
        budget = None if full else self.config.google_per_hour
        g0 = self.search.sent(GOOGLE)
        gate = asyncio.Semaphore(CONCURRENCY)
        stopped_for_budget = 0

        async def one(f: dict[str, Any]) -> int:
            nonlocal stopped_for_budget
            async with gate:
                if budget is not None and self.search.sent(GOOGLE) - g0 >= budget:
                    stopped_for_budget += 1
                    return 0
                n = await self._search_firm(
                    tenant, f, self.config.history_days if history else None)
                t["run"]["done"] = int(t["run"].get("done", 0)) + 1
                t["run"]["filed"] = int(t["run"].get("filed", 0)) + n
                # Saved now and then, not per firm: the filed-story memory runs to a few
                # MB, and a crash costs at most a few firms searched again.
                if t["run"]["done"] % 25 == 0:
                    self.store.save()
                return n

        filed += sum(await asyncio.gather(*(one(f) for f in due)))
        used = self.search.sent(GOOGLE) - g0
        took = round(time.time() - started, 1)
        note = (f"{t['run']['done']} of {len(due)} firms searched, {filed} new findings, "
                f"{used} Google searches in {int(took // 60)} min")
        if stopped_for_budget:
            note += (f"; {stopped_for_budget} firms wait for the next hour "
                     f"(Google budget {budget}/h)")
        t["run"].update(finished=time.time(), filed=filed, google=used, seconds=took,
                        note=note, skipped_for_budget=stopped_for_budget)
        if full:
            t["next_batch"] = time.time() + self.config.batch_every_s
        self.store.save()
        log.info("pulse_monitor_pass", extra={"tenant": tenant, "kind": kind,
                                              "firms": len(due), "done": t["run"]["done"],
                                              "filed": filed, "google": used,
                                              "seconds": took,
                                              "waiting": stopped_for_budget})
        return t["run"]

    async def _search_firm(self, tenant: str, firm: dict[str, Any],
                           lookback_days: int | None = None) -> int:
        """Search one firm (its names, with the adverse words; its people in one more
        query), file what is new, and record whether the search was complete.
        ``lookback_days`` (the history pass) searches that far back whatever was
        searched before."""
        st = self.store.firm(tenant, str(firm["id"]))
        st["last_try"] = time.time()
        today = datetime.now(desk_tz())
        last_ok = float(st.get("last_ok") or 0)
        if lookback_days:
            start = today - timedelta(days=lookback_days)
        else:
            start = (datetime.fromtimestamp(last_ok, desk_tz())
                     - timedelta(days=OVERLAP_DAYS)
                     if last_ok else today - timedelta(days=self.config.window_days))
            start = max(start, today - timedelta(days=self.config.window_days))
        dfrom, dto = start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
        down: dict[str, str] = {}
        found: list[dict[str, Any]] = []
        name = str(firm.get("name") or "")
        names = [n for n in watch_names(name, firm.get("aliases")) if not too_common(n)]
        if not names:
            # Not searched rather than searched badly: the gap is named on the screen and
            # in the digest until someone sets the firm's news names.
            down["News search"] = (f"“{name}” is a common word, so a search finds "
                                   "everything but this firm — set its news names")
        else:
            try:
                arts, sources = await self.search.search_detail(
                    name, dfrom, dto, 80, strict=True, names=names, bulk=True,
                    adverse=self.config.adverse_query)
                found.extend(arts)
                down.update({s["name"]: s.get("error", "") for s in sources
                             if not s.get("ok") and s.get("name") != "GDELT"})
            except Exception as exc:  # noqa: BLE001 - one firm never ends a pass
                down["all sources"] = str(exc)[:200]
        people = [p for p in (firm.get("people") or []) if str(p.get("name") or "").strip()]
        if people:
            pnames = [str(p["name"]).strip() for p in people]
            # A person is searched with their city — or, for a lead contact with none,
            # the firm's — to narrow a common name to the one who runs this firm.
            ctx = {str(p["name"]).strip(): str(p.get("city") or "").strip()
                   or (str(firm.get("city") or "").strip() if p.get("source") == "lead"
                       else "")
                   for p in people}
            ctx = {k: v for k, v in ctx.items() if v}
            # What else a story must print to be about a LEAD contact rather than a
            # namesake: the firm, or the city. An analyst's own key people are trusted
            # as listed; a name read off a lead is not.
            firm_words = [n for n in [name, str(firm.get("legal_name") or ""),
                                      *names] if n.strip()]
            try:
                parts, sources = await self.search.search_detail(
                    f"people of {name}", dfrom, dto, 60, strict=True, names=pnames,
                    bulk=True, contexts=ctx)
                by_name = {n.casefold(): p for n, p in zip(pnames, people, strict=True)}
                for a in parts:
                    p = by_name.get(str(a.get("matched") or "").casefold()) or {}
                    if p.get("source") == "lead":
                        anchors = [*firm_words, ctx.get(str(p.get("name")), "")]
                        if not any(mentions(a, w) for w in anchors if w.strip()):
                            continue
                    role = str(p.get("role") or "").strip()
                    who = str(a.get("matched") or "")
                    found.append({**a, "about": f"{role} · {who}" if role else who})
                for s in sources:
                    if not s.get("ok") and s.get("name") != "GDELT":
                        down.setdefault(s["name"], s.get("error", ""))
            except Exception as exc:  # noqa: BLE001
                down.setdefault("all sources", str(exc)[:200])
        n = await self._file_all(tenant, firm, found)
        if down:
            st["down"] = down
        else:
            st["last_ok"] = time.time()
            st["down"] = {}
        st["found"] = len(found)
        return n

    async def _file_all(self, tenant: str, firm: dict[str, Any],
                        articles: list[dict[str, Any]]) -> int:
        n = 0
        seen: set[str] = set()
        for a in articles:
            key = filed_key(tenant, str(firm["id"]), a)
            if key in seen or self.store.was_filed(tenant, key):
                continue
            seen.add(key)
            try:
                await self.file(tenant, key, intel_row(firm, with_evidence(a)))
            except Exception as exc:  # noqa: BLE001 - retried on the next pass
                log.warning("pulse_monitor_file_failed", extra={"tenant": tenant,
                                                                "entity": firm.get("id"),
                                                                "error": str(exc)})
                continue
            self.store.mark_filed(tenant, key)
            n += 1
        return n

    async def _official(self, tenant: str, firms: list[dict[str, Any]], *,
                        history: bool) -> int:
        """Read SEBI and IBBI once, match every firm, file what is new."""
        t = self.store.tenant(tenant)
        client = await self.search.http()
        records: list[dict[str, Any]] = []
        sources: list[tuple[str, Callable[[], Awaitable[tuple[list[dict[str, Any]], str]]]]] = []

        async def sebi() -> tuple[list[dict[str, Any]], str]:
            recs = await official.fetch_sebi(client)
            return recs, f"{len(recs)} enforcement items in the feed"

        async def ibbi() -> tuple[list[dict[str, Any]], str]:
            # The history once a day, unasked: ~100 polite requests, a few minutes, no
            # Google — a firm added today is checked against ~19 months of insolvencies
            # by tomorrow (100 pages reached back to March 2025 on 16 Oct 2026). Every
            # other batch reads the newest pages.
            deep = history or time.time() - float(t.get("ibbi_deep_at") or 0) > 86400
            pages = self.config.ibbi_history_pages if deep else self.config.ibbi_pages
            recs, read = await official.fetch_ibbi(client, pages)
            if deep and read:
                t["ibbi_deep_at"] = time.time()
            return recs, f"{len(recs)} announcements from {read} of {pages} pages"

        async def nse() -> tuple[list[dict[str, Any]], str]:
            # The last two days each hour; a fortnight with the daily deep read.
            days = 14 if (history or time.time() - float(t.get("nse_deep_at") or 0)
                          > 86400) else 2
            recs = await official.fetch_nse(client, days)
            if days > 2:
                t["nse_deep_at"] = time.time()
            return recs, f"{len(recs)} adverse filings in the last {days} days"

        if self.config.sebi:
            sources.append(("SEBI", sebi))
        if self.config.ibbi:
            sources.append(("IBBI", ibbi))
        if self.config.nse:
            sources.append(("NSE", nse))
        for name, fetch in sources:
            try:
                recs, detail = await fetch()
                records.extend(recs)
                t["official"][name] = {"at": time.time(), "ok": True, "detail": detail}
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                log.warning("pulse_official_failed", extra={"source": name,
                                                            "error": str(exc)})
                t["official"][name] = {"at": time.time(), "ok": False,
                                       "detail": str(exc)[:200] or type(exc).__name__}
        n = 0
        matched: dict[str, int] = {}
        for f in firms:
            hits = official.match_official(records, f)
            for h in hits:
                matched[h["source"]] = matched.get(h["source"], 0) + 1
            if hits:
                n += await self._file_all(tenant, f, hits)
        for name, _ in sources:
            if t["official"].get(name, {}).get("ok"):
                # Records about watched firms in what was read — old ones included;
                # what is NEW is the run's "filed".
                t["official"][name]["matched"] = matched.get(name, 0)
        return n
