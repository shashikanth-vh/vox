"""The reader: connect, read, match, extract, file — and the queue for what it could
not place.

Everything that touches the outside world comes in as a callable so the tests can
hand in fakes: ``google`` (OAuth + Gmail), ``firms`` (the Register's companies with
their domains), ``subject`` (the lead, deal or company a conversation files
against), ``file`` (the interaction write), ``intel`` (the flagged-signal write),
``extractor`` (the model).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.mail.extract import ExtractError, Extractor
from app.mail.google import GoogleError, GoogleMail, TokenDeadError
from app.mail.match import Firm, Match, match_thread
from app.mail.store import MailStore
from app.mail.threads import PUBLIC_DOMAINS, Thread, parse_thread, skip_reason

log = logging.getLogger("pulse.mail")

INTERACTION_TYPE = "Email / Written Correspondence"
FirmsFn = Callable[[str], Awaitable[list[Firm]]]
SubjectFn = Callable[[str, str], Awaitable[tuple[str, str]]]
FileFn = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
IntelFn = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
DomainFn = Callable[[str, str, str], Awaitable[None]]


@dataclass
class MailConfig:
    internal_domains: set[str]
    excluded_labels: set[str]
    interval_s: int = 3600
    max_threads_per_run: int = 150
    backfill_pages_per_run: int = 3
    min_confidence: float = 0.6
    queue_days: int = 60
    engine_concurrency: int = 4


class MailError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _gmail_date(d: datetime) -> str:
    return d.strftime("%Y/%m/%d")


class MailReader:
    def __init__(self, store: MailStore, google: GoogleMail, extractor: Extractor,
                 firms: FirmsFn, subject: SubjectFn, file: FileFn, intel: IntelFn,
                 config: MailConfig, tenants: list[str], *, add_domain: DomainFn | None = None) -> None:
        self.store = store
        self.google = google
        self.extractor = extractor
        self._firms = firms
        self._subject = subject
        self._file = file
        self._intel = intel
        self._add_domain = add_domain
        self.config = config
        self.tenants = [t.upper() for t in tenants]
        self.running: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(max(1, config.engine_concurrency))

    # ------------------------------------------------------------------ connect
    async def connect(self, tenant: str, email: str, code: str, *, since_months: int = 12,
                      display_name: str = "") -> dict[str, Any]:
        """Exchange the consent code, check the token opens THIS person's mailbox (not
        some other account picked in the popup), and remember the connection."""
        email = email.lower().strip()
        tokens = await self.google.exchange_code(code)
        access = str(tokens["access_token"])
        profile = await self.google.profile(access)
        opened = str(profile.get("emailAddress") or "").lower()
        if opened != email:
            raise MailError(f"The mailbox you allowed is {opened or 'another account'}, "
                            f"but you are signed in as {email}. Connect your own work mailbox.")
        since_months = max(0, min(int(since_months), 36))
        since = (datetime.now(UTC) - timedelta(days=30 * since_months)).date() if since_months else None
        rec = {
            "email": email, "name": display_name, "token": self.store.seal(str(tokens["refresh_token"])),
            "connected_at": time.time(), "since": since.isoformat() if since else None,
            "history_id": str(profile.get("historyId") or ""),
            "backfill": {"done": since is None, "page": None, "pages": 0, "threads": 0},
            "status": "connected", "error": None, "last_run": None, "last_ok": None,
            "counts": {"seen": 0, "filed": 0, "queued": 0, "skipped": {}, "errors": 0},
        }
        self.store.put_mailbox(tenant, email, rec)
        self.store.save()
        log.info("pulse_mail_connected", extra={"tenant": tenant, "mailbox": email,
                                                "since": rec["since"]})
        return self.status(tenant, email)

    def disconnect(self, tenant: str, email: str) -> bool:
        gone = self.store.drop_mailbox(tenant, email)
        if gone:
            self.store.save()
            log.info("pulse_mail_disconnected", extra={"tenant": tenant, "mailbox": email})
        return gone

    def status(self, tenant: str, email: str) -> dict[str, Any]:
        rec = self.store.mailbox(tenant, email)
        if rec is None:
            return {"email": email.lower(), "connected": False, "configured": self.google.configured,
                    "engine": self.extractor.engine}
        out = {k: v for k, v in rec.items() if k != "token"}
        out["connected"] = True
        out["configured"] = self.google.configured
        out["engine"] = self.extractor.engine
        out["reading"] = f"{tenant}:{email.lower()}" in self.running
        return out

    def coverage(self, tenant: str) -> list[dict[str, Any]]:
        return [self.status(tenant, e) for e in sorted(self.store.mailboxes(tenant))]

    # ------------------------------------------------------------------ reading
    async def run_mailbox(self, tenant: str, email: str, *, force_backfill: bool = False) -> dict[str, Any]:
        """One pass over one mailbox: the backfill's next pages (if any are left) and
        everything new since the last pass. Returns the pass's counts."""
        email = email.lower()
        rec = self.store.mailbox(tenant, email)
        if rec is None:
            raise MailError("mailbox not connected")
        key = f"{tenant}:{email}"
        if key in self.running and not self.running[key].done():
            return {"note": "already running"}
        task = asyncio.current_task()
        if task is not None:
            self.running[key] = task
        stats: dict[str, Any] = {"seen": 0, "filed": 0, "queued": 0, "skipped": {}, "errors": 0, "note": ""}
        rec["last_run"] = time.time()
        try:
            refresh = self.store.open_token(rec["token"])
            if not refresh:
                raise TokenDeadError("stored token unreadable")
            access = await self.google.access_token(refresh)
            firms = await self._firms(tenant)
            budget = self.config.max_threads_per_run
            thread_ids: list[str] = []
            # 1. what is new since the last pass
            new_ids, new_hist = await self._new_threads(access, rec)
            thread_ids.extend(new_ids)
            # 2. the backfill, a few pages at a time
            bf = rec.setdefault("backfill", {"done": True})
            if (not bf.get("done") or force_backfill) and rec.get("since"):
                bf_ids = await self._backfill_page(access, rec, pages=self.config.backfill_pages_per_run)
                thread_ids.extend(x for x in bf_ids if x not in thread_ids)
            for tid in thread_ids[:budget]:
                stats["seen"] += 1
                try:
                    await self._one_thread(tenant, email, access, tid, firms, stats)
                except (GoogleError, ExtractError, MailError) as exc:
                    stats["errors"] += 1
                    log.warning("pulse_mail_thread_failed", extra={"tenant": tenant, "mailbox": email,
                                                                   "thread": tid, "error": str(exc)})
            if len(thread_ids) > budget:
                stats["note"] = f"{len(thread_ids) - budget} more threads wait for the next pass"
            if new_hist:
                rec["history_id"] = new_hist
            rec["status"] = "connected" if rec["backfill"].get("done") else "backfill"
            rec["error"] = None
            rec["last_ok"] = time.time()
        except TokenDeadError as exc:
            rec["status"] = "reconnect"
            rec["error"] = f"Google no longer accepts this mailbox's permission ({exc}). Reconnect it."
            log.warning("pulse_mail_token_dead", extra={"tenant": tenant, "mailbox": email})
        except GoogleError as exc:
            rec["error"] = f"Google could not be read: {exc}"
            stats["errors"] += 1
            log.warning("pulse_mail_run_failed", extra={"tenant": tenant, "mailbox": email,
                                                        "error": str(exc)})
        finally:
            c = rec.setdefault("counts", {"seen": 0, "filed": 0, "queued": 0, "skipped": {}, "errors": 0})
            for k in ("seen", "filed", "queued", "errors"):
                c[k] = c.get(k, 0) + stats[k]
            for why, n in stats["skipped"].items():
                c.setdefault("skipped", {})[why] = c["skipped"].get(why, 0) + n
            rec["last_pass"] = stats
            self.store.put_mailbox(tenant, email, rec)
            self.store.expire_queue(tenant, self.config.queue_days)
            with contextlib.suppress(Exception):
                self.store.save()
            self.running.pop(key, None)
        return stats

    async def _new_threads(self, access: str, rec: dict[str, Any]) -> tuple[list[str], str | None]:
        hist = rec.get("history_id")
        if hist:
            try:
                ids: set[str] = set()
                token = None
                newest = None
                for _ in range(20):
                    page, newest, token = await self.google.history(access, hist, page_token=token)
                    ids |= page
                    if not token:
                        break
                return sorted(ids), newest or hist
            except GoogleError as exc:
                if "not found" not in str(exc):
                    raise
                # history id too old (Gmail keeps about a week): list by date instead
        since = datetime.now(UTC) - timedelta(days=7)
        ids_l, _ = await self.google.list_threads(access, f"after:{_gmail_date(since)}", max_results=100)
        profile = await self.google.profile(access)
        return ids_l, str(profile.get("historyId") or "") or None

    async def _backfill_page(self, access: str, rec: dict[str, Any], *, pages: int) -> list[str]:
        bf = rec["backfill"]
        out: list[str] = []
        q = f"after:{rec['since'].replace('-', '/')}"
        for _ in range(max(1, pages)):
            ids, nxt = await self.google.list_threads(access, q, page_token=bf.get("page"), max_results=100)
            out.extend(ids)
            bf["pages"] = int(bf.get("pages") or 0) + 1
            bf["threads"] = int(bf.get("threads") or 0) + len(ids)
            bf["page"] = nxt
            if not nxt:
                bf["done"] = True
                break
        return out

    async def _one_thread(self, tenant: str, mailbox: str, access: str, thread_id: str,
                          firms: list[Firm], stats: dict[str, Any]) -> None:
        raw = await self.google.thread(access, thread_id)
        t = parse_thread(raw)
        if t is None:
            self._skip(stats, "no text")
            return
        why = skip_reason(t, self.config.internal_domains, self.config.excluded_labels)
        if why:
            self._skip(stats, why)
            return
        key = t.key
        if self.store.is_ignored(tenant, key):
            self._skip(stats, "ignored")
            return
        known = self.store.conversation(tenant, key)
        last_rfc = t.last.message_id or ""
        if known and known.get("last_message_id") == t.last.id and known.get("last_message_rfc") == last_rfc:
            self._skip(stats, "already filed")
            return
        if known and known.get("last_message_rfc") and known.get("last_message_rfc") == last_rfc:
            # same conversation seen from another mailbox, nothing newer
            self._skip(stats, "already filed")
            return
        match = match_thread(t, firms, self.config.internal_domains)
        if known and known.get("entity_id"):
            # a conversation that grew: keep the company it was filed under
            firm = next((f for f in firms if f.id == known["entity_id"]), None)
            if firm is not None:
                match = Match(firm, 1.0, "continued")
        if match is None or match.confidence < self.config.min_confidence:
            if self.store.queue(tenant).get(key) is None:
                self.store.enqueue(tenant, key, self._queue_entry(t, mailbox, match))
                stats["queued"] += 1
            return
        await self.file_conversation(tenant, mailbox, t, match, known)
        stats["filed"] += 1

    @staticmethod
    def _skip(stats: dict[str, Any], why: str) -> None:
        stats["skipped"][why] = stats["skipped"].get(why, 0) + 1

    def _queue_entry(self, t: Thread, mailbox: str, match: Match | None) -> dict[str, Any]:
        outsiders = t.outsiders(self.config.internal_domains)
        return {
            "key": t.key, "mailbox": mailbox, "thread_id": t.thread_id, "subject": t.subject,
            "first_at": _iso(t.first.date), "last_at": _iso(t.last.date), "messages": len(t.messages),
            "parties": [{"email": p.email, "name": p.name} for p in outsiders][:8],
            "excerpt": re.sub(r"\s+", " ", t.last.text)[:300],
            "text": t.text(4000),
            "suggested": ({"entity_id": match.firm.id, "name": match.firm.name,
                           "confidence": match.confidence, "how": match.how} if match else None),
            "queued_at": time.time(),
        }

    # ------------------------------------------------------------------ filing
    async def file_conversation(self, tenant: str, mailbox: str, t: Thread, match: Match,
                                known: dict[str, Any] | None) -> dict[str, Any]:
        firm = match.firm
        async with self._sem:
            note = await self.extractor.note(t, firm.name)
        subject_type, subject_id = await self._subject(tenant, firm.id)
        outsiders = t.outsiders(self.config.internal_domains)
        last = t.last
        direction = "Inbound" if last.sender.domain not in self.config.internal_domains else "Outbound"
        # The outside party, never one of ours: the model's name is kept unless it is
        # an insider's, else the first outsider's name or address.
        insiders = {p.name.lower() for p in t.insiders(self.config.internal_domains) if p.name}
        named = note.get("counterparty_name")
        contact = named if named and named.lower() not in insiders else (
            outsiders[0].name or outsiders[0].email if outsiders else None)
        continued = bool(known and known.get("interaction_ids"))
        lines = [note["summary"]]
        for label, k in (("Discussed", "discussed"), ("Decisions", "decisions"),
                         ("Asks", "asks"), ("Amounts and terms", "amounts")):
            if note.get(k):
                lines.append(f"{label}: " + "; ".join(note[k]))
        if note.get("evidence_quote"):
            lines.append(f'Quote: "{note["evidence_quote"]}"')
        if note.get("signal") and note["signal"] != "NONE":
            lines.append(f"Signal {note['signal']}: {note.get('signal_reason') or ''}".strip())
        lines.append(f"From the mailbox of {mailbox} · {len(t.messages)} message(s), "
                     f"{t.first.date.strftime('%d %b %Y')} to {last.date.strftime('%d %b %Y')}"
                     + (" · continues an earlier filing" if continued else ""))
        source_ref = f"mail:{t.key[:16]}:{(last.message_id or last.id)[:60]}"[:200]
        payload = {
            "subject_type": subject_type, "subject_id": subject_id,
            "interaction_type": INTERACTION_TYPE, "direction": direction,
            "occurred_at": _iso(last.date),
            "summary": (("Re: " if continued else "") + note["summary"])[:300],
            "notes": "\n".join(lines), "performed_by": mailbox, "contact_name": contact,
            "next_action": note.get("next_step"), "next_action_date": note.get("next_step_date"),
            "attendees": [{"name": p.name, "email": p.email} for p in t.parties()][:20],
            "key_intel": note, "source": "Email", "source_ref": source_ref,
            "meta": {"conversation": t.key, "thread_id": t.thread_id, "mailbox": mailbox,
                     "subject": t.subject, "matched_on": match.how, "confidence": match.confidence,
                     "engine": self.extractor.engine, "messages": [m.id for m in t.messages]},
        }
        tail = hashlib.sha256((last.message_id or last.id).encode()).hexdigest()[:16]
        idem = f"pulse:mail:{tenant}:{t.key}:{tail}"
        row = await self._file(tenant, idem, payload)
        if note.get("signal") in ("RED", "AMBER", "GREEN"):
            with contextlib.suppress(Exception):
                await self._intel(tenant, idem + ":intel", {
                    "entity_id": firm.id, "intel_type": "Email",
                    "source": f"PULSE:mail:{mailbox}", "signal": note["signal"],
                    "title": f"E-mail: {t.subject}"[:400],
                    "summary": note.get("signal_reason") or note["summary"],
                    "observed_at": _iso(last.date),
                    "payload": {"conversation": t.key, "interaction_id": row.get("id"),
                                "evidence": note.get("evidence_quote"), "mailbox": mailbox}})
        record = {
            "entity_id": firm.id, "entity_name": firm.name, "subject_type": subject_type,
            "subject_id": subject_id, "last_message_id": last.id,
            "last_message_rfc": last.message_id or "", "messages": len(t.messages),
            "interaction_ids": [*(known or {}).get("interaction_ids", []), row.get("id")],
            "filed_at": time.time(), "mailboxes": sorted({*((known or {}).get("mailboxes") or []), mailbox}),
            "signal": note.get("signal"), "matched_on": match.how,
        }
        self.store.put_conversation(tenant, t.key, record)
        self.store.dequeue(tenant, t.key)
        return row

    # ------------------------------------------------------------------ the queue
    def queue(self, tenant: str, *, mailbox: str | None = None) -> list[dict[str, Any]]:
        items = [dict(e) for e in self.store.queue(tenant).values()
                 if mailbox is None or e.get("mailbox") == mailbox.lower()]
        for e in items:
            e.pop("text", None)
        items.sort(key=lambda e: e.get("last_at") or "", reverse=True)
        return items

    async def attach(self, tenant: str, key: str, entity_id: str, *, by: str,
                     add_domain: bool = False) -> dict[str, Any]:
        """A person placed a queued conversation on a company: file it now, and if
        asked, teach the Register the outside domain so the next one files itself."""
        entry = self.store.queue(tenant).get(key)
        if entry is None:
            raise MailError("that conversation is no longer in the queue")
        firms = await self._firms(tenant)
        firm = next((f for f in firms if f.id == entity_id), None)
        if firm is None:
            raise MailError("no such company")
        rec = self.store.mailbox(tenant, entry["mailbox"])
        if rec is None:
            raise MailError("the mailbox this came from is no longer connected")
        refresh = self.store.open_token(rec["token"])
        if not refresh:
            raise MailError("the mailbox's permission is no longer readable; reconnect it")
        access = await self.google.access_token(refresh)
        t = parse_thread(await self.google.thread(access, entry["thread_id"]))
        if t is None:
            raise MailError("the conversation could not be read again from Gmail")
        if add_domain and self._add_domain is not None:
            outsiders = t.outsiders(self.config.internal_domains)
            for p in outsiders:
                d = p.domain
                if d and d not in firm.domains and d not in PUBLIC_DOMAINS:
                    await self._add_domain(tenant, firm.id, d)
                    firm.domains.append(d)
                    break
        row = await self.file_conversation(tenant, entry["mailbox"], t,
                                           Match(firm, 1.0, f"attached by {by}"), None)
        self.store.save()
        return row

    def ignore(self, tenant: str, key: str) -> bool:
        gone = self.store.dequeue(tenant, key) is not None
        self.store.ignore(tenant, key)
        self.store.save()
        return gone

    # ------------------------------------------------------------------ the loop
    async def run_tenant(self, tenant: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for email, rec in self.store.mailboxes(tenant).items():
            if rec.get("status") == "reconnect":
                continue
            out[email] = await self.run_mailbox(tenant, email)
        return out

    async def loop(self, *, tick_seconds: float = 60.0) -> None:
        last: dict[str, float] = {}
        while True:
            for tenant in self.tenants:
                now = time.time()
                due = [e for e, r in self.store.mailboxes(tenant).items()
                       if r.get("status") != "reconnect"
                       and now - float(r.get("last_run") or 0) >= self.config.interval_s]
                for email in due:
                    with contextlib.suppress(Exception):
                        await self.run_mailbox(tenant, email)
                last[tenant] = now
            await asyncio.sleep(tick_seconds)
