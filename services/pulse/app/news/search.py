"""Search the news for a term — the engine behind the News Radar screen.

WHY THIS LIVES SERVER-SIDE. The browser cannot call Google News or GDELT directly: no
CORS headers, so every request is blocked. The UI used to route around that through a
public proxy (allorigins.win), which put the desk's search terms — the names of companies
we are looking at — through a third party nobody vetted, and broke whenever that proxy
did. Fetching here removes both problems at once.

THREE SOURCES, merged. Google News RSS (honours a date range), GDELT (structured, but
rate-limits and can be turned off with PULSE_DISABLE_GDELT) and Bing News RSS (no date
filter, but stays up when the other two throttle). Any one of them failing is normal and
never fails the search — the others answer, and the shortfall is reported.

SEVERITY is the desk's own reading, not a source's: whole-word matching (``\\bword\\b``)
so "fir" never matches "firm" and "raid" never matches "afraid", hard-adverse beating a
positive, a positive beating a routine watch word.

Ported from the desk's atlas_serve.py, which proved this shape in the field; the mechanics
are async here (httpx + asyncio.gather) so one slow source never holds a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 - parsing responses from named sources
from collections import OrderedDict
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from evam_backend_core.logging import get_logger

from app.news.names import watch_names
from app.news.triage import classify, triage

log = get_logger("pulse.search")

UA = {"User-Agent": "Mozilla/5.0 (PRISM-NewsRadar/1.0)"}

GOOGLE_NEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
GDELT_TS = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
            "&mode=ArtList&maxrecords=25&format=json&sort=DateDesc&timespan=3m")
GDELT_RANGE = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
               "&mode=ArtList&maxrecords=25&format=json&sort=DateDesc"
               "&startdatetime=%s&enddatetime=%s")
BING_NEWS = "https://www.bing.com/news/search?q=%s&format=rss&count=25"

# The judgement lives in triage.py — four tiers, negation and word-sense guards, and the
# context flip that reads "raises fresh debt" differently for a borrower than a prospect.
# Re-exported here because this module is the one every caller already imports.
__all__ = ["NewsSearch", "classify", "triage"]


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else ""


def _valid_date(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""))


def _when_of(pub_date: str | None) -> str:
    if not pub_date:
        return ""
    try:
        return parsedate_to_datetime(pub_date).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return ""


def _unwrapped(link: str) -> str:
    """Bing's links go through a click-tracker (``bing.com/news/apiclick.aspx?…&url=``);
    the article's own address is in the ``url`` parameter. Unwrapped, the reader goes
    straight to the publisher and the source column can name it."""
    if "bing.com/news/apiclick" not in link:
        return link
    real = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url", [""])[0]
    return real if re.match(r"https?://", real) else link


def _rss_items(xml_text: str, via: str, *, require_http: bool = False) -> list[dict[str, Any]]:
    """The shared shape of both RSS sources. A feed that is not XML (an error page, a
    captcha) raises here and is handled by the caller as "that source had nothing"."""
    out: list[dict[str, Any]] = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link or (require_http and not link.startswith("http")):
            continue
        link = _unwrapped(link)
        source = ""
        # Google names the publisher in <source>; Bing in <News:Source>, under a
        # namespace that is the query URL itself — so match the local name, not the tag.
        for child in item:
            if (child.tag == "source" or child.tag.endswith("}Source")) and child.text:
                source = child.text.strip()
                break
        source = source or _domain(link)
        # Google appends " - Source" to every headline; the source has its own column.
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        # The snippet is what the relevance check reads besides the headline: Bing's
        # is a sentence of the article, Google's repeats the headline in markup.
        snippet = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        out.append({"title": title, "url": link, "source": source,
                    "when": _when_of(item.findtext("pubDate")), "via": via,
                    "snippet": re.sub(r"\s+", " ", snippet).strip()[:400]})
    return out


def _words(text: str) -> str:
    """Lower-case words separated by single spaces, padded — so a phrase test is a
    plain substring test that cannot match inside a longer word."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def mentions(article: dict[str, Any], name: str) -> bool:
    """Does the headline or snippet actually name ``name``, as whole words?

    A search engine answers an unquoted "Atlanta System" with a bus shooting in
    Atlanta; this is the check that keeps it out of the digest."""
    needle = _words(name)
    return needle.strip() != "" and needle in _words(
        f"{article.get('title', '')} {article.get('snippet', '')}")


# A source's date is when IT first saw the piece, often a few days after publication;
# a week's grace keeps that lag in, and keeps a resurfaced 2016 story out.
WINDOW_GRACE_DAYS = 7


def in_window(article: dict[str, Any], dfrom: str, dto: str) -> bool:
    """Inside the asked window (with the grace above). Undated items are kept: no
    date is not evidence of an old story."""
    when = re.sub(r"\D", "", article.get("when") or "")[:8]
    if len(when) != 8:
        return True
    if _valid_date(dfrom):
        floor = (datetime.strptime(dfrom, "%Y-%m-%d")
                 - timedelta(days=WINDOW_GRACE_DAYS)).strftime("%Y%m%d")
        if when < floor:
            return False
    return not (_valid_date(dto) and when > dto.replace("-", ""))


def _reason(exc: Any) -> str:
    """A sentence a desk can act on, from an exception a desk cannot read.

    ``ConnectError('[Errno -3] Temporary failure in name resolution')`` and
    ``ReadTimeout('')`` are the two that matter most in a container, and the second
    stringifies to nothing at all — so the exception TYPE has to carry the meaning
    when the message is empty."""
    if isinstance(exc, str):
        return exc[:200]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from the source"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot reach the source ({str(exc)[:120] or 'connection refused'})"
    if isinstance(exc, httpx.TimeoutException):
        return "the source did not answer in time"
    return (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:200]


def _ok(name: str, count: int) -> dict[str, Any]:
    return {"name": name, "ok": True, "count": count}


def _failed(name: str, exc: Any) -> dict[str, Any]:
    out = {"name": name, "ok": False, "count": 0, "error": _reason(exc)}
    # 503 (Google's automated-traffic page) and 429 mean "slower", not "broken".
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
        out["throttled"] = True
    return out


class SourceGate:
    """Pacing for ONE upstream, that slows down when told to and never gives up on a
    search because the source said "not so fast".

    What the live runs taught: Google News answers a steady trickle (150 searches 2.5 s
    apart from a cloud host, all 200) and blocks a burst with its automated-traffic
    page (HTTP 503) — 335 of 525 firms in the 14 Oct production digest at 1.5 s. The
    block lifts on its own. So a throttle (503 / 429) is not a failure of the search:

      * ``throttled()`` pauses the whole lane (60 s, doubling to 15 min), and widens the
        spacing (x1.5, up to ``max_interval``); the caller then RETRIES the same search.
      * ``done(True)`` counts successes; after 25 in a row the spacing eases back toward
        ``interval`` and the pause step resets.
      * Any other failure (no route, a timeout) is a real one: ``trip_after`` of those
        in a row pause the lane for ``cooldown`` and searches report it as not searched.

    ``enter(max_wait)`` waits for the next slot — through a throttle pause too — and
    returns None, or the reason the source is skipped when the wait would exceed
    ``max_wait`` (an interactive search does not sit out a 15-minute pause)."""

    def __init__(self, name: str, interval: float, *, trip_after: int = 4,
                 cooldown: float = 600.0, max_interval: float | None = None,
                 pause: float = 60.0, max_pause: float = 900.0) -> None:
        self.name, self.base, self.interval = name, interval, interval
        self.max_interval = max_interval if max_interval is not None else interval * 4
        self.trip_after, self.cooldown = trip_after, cooldown
        self.pause_base, self.max_pause, self._pause = pause, max_pause, pause
        self._next = 0.0
        self._fails = 0
        self._wins = 0
        self._broken_until = 0.0     # real failures: skip until then
        self._throttled_until = 0.0  # a throttle: wait until then
        self._last_throttle = -1.0   # when the last throttle was taken (monotonic)
        self._refusing_since: float | None = None  # first throttle with no success since
        self.give_up_after = GIVE_UP_AFTER_S
        self.throttles = 0
        # Requests let through, ever: the monitor reads it before and after a batch to
        # spend Google's hourly allowance by measurement, not by estimate.
        self.sent = 0

    async def enter(self, max_wait: float | None = None) -> str | None:
        now = time.monotonic()
        if (self._refusing_since is not None
                and now - self._refusing_since > self.give_up_after):
            # Refusing for longer than any pause should take: stop waiting on it.
            self._broken_until = now + self.give_up_after
            self._refusing_since = None
            log.warning("pulse_source_given_up", extra={"source": self.name,
                                                       "seconds": self.give_up_after})
        if now < self._broken_until:
            return (f"refusing or failing for a long stretch; tried again in "
                    f"{int(self._broken_until - now) // 60 + 1} min")
        slot = max(now, self._next, self._throttled_until)
        if max_wait is not None and slot - now > max_wait:
            if now < self._throttled_until:
                return "asking too fast for it right now; not asked for this search"
            return f"not asked: it allows one request every {self.interval:g} s"
        # Claimed before sleeping (no await in between), so concurrent callers queue.
        self._next = slot + self.interval
        if slot > now:
            await asyncio.sleep(slot - now)
        # A throttle may have landed while this caller waited for its slot: nothing goes
        # out during a pause (cipher-test, 15 Oct: requests queued before Google said
        # "stop" went out anyway, each read as a new refusal, and the pause climbed from
        # 60 s to 15 min in under three minutes).
        while (wait := self._throttled_until - time.monotonic()) > 0:
            if max_wait is not None:
                return "asking too fast for it right now; not asked for this search"
            await asyncio.sleep(wait)
        self.sent += 1
        return None

    def throttled(self, sent_at: float | None = None) -> float:
        """The source said slow down: pause the lane, widen the spacing. Returns the
        pause, for the log.

        ``sent_at`` is when the refused request went out. One refusal episode reaches
        every request already in flight; escalating once per refusal would double the
        pause six times in a second. A request sent BEFORE the last throttle therefore
        only honours the current pause — it does not escalate it."""
        if sent_at is not None and sent_at < self._last_throttle:
            return max(0.0, self._throttled_until - time.monotonic())
        self.throttles += 1
        self._last_throttle = time.monotonic()
        if self._refusing_since is None:
            self._refusing_since = self._last_throttle
        pause = self._pause
        self._throttled_until = max(self._throttled_until, time.monotonic() + pause)
        self._pause = min(self._pause * 2, self.max_pause)
        self.interval = min(self.interval * 1.5, self.max_interval)
        self._wins = 0
        log.warning("pulse_source_throttled", extra={"source": self.name, "pause_s": pause,
                                                    "interval_s": round(self.interval, 2)})
        return pause

    def done(self, ok: bool) -> None:
        if ok:
            self._fails = 0
            self._refusing_since = None
            self._wins += 1
            if self._wins >= 25:
                self._wins = 0
                self._pause = self.pause_base
                self.interval = max(self.base, self.interval * 0.85)
            return
        self._fails += 1
        if self._fails >= self.trip_after:
            self._broken_until = time.monotonic() + self.cooldown
            self._fails = 0
            log.warning("pulse_source_paused", extra={"source": self.name,
                                                     "seconds": self.cooldown})


# Request spacing per source, in seconds. Google News limits a cloud host to roughly
# 450-500 searches in about an hour, whatever the spacing (cipher-test: 2.5 s ran clean
# for ~460 searches, then refused; 1.5 s in production was refused after ~190). 9 s is
# ~400 an hour: a 500-firm digest takes ~75 minutes and the scheduler starts it that
# much early. GDELT publishes its limit (one per 5 s) and is not used for bulk.
GOOGLE_INTERVAL_S = 9.0
BING_INTERVAL_S = 0.25
GDELT_INTERVAL_S = 5.5
# How many times a bulk search retries a throttled source before reporting it. With the
# pause doubling from 60 s, five retries span ~30 minutes of the source saying "wait".
THROTTLE_RETRIES = 5
# A source that has refused every request for this long is given up on for the rest of
# the run (reported per firm) — live, Google refused for 2.5 hours straight and the run
# sat out fifteen-minute pauses for the last nine firms.
GIVE_UP_AFTER_S = 20 * 60
# The screen sweep asks in batches the browser waits up to 5 minutes for: two retries
# (pauses of 60 s and 120 s) keep a throttled batch inside that.
SWEEP_THROTTLE_RETRIES = 2


def _skipped(name: str, why: str) -> dict[str, Any]:
    return {"name": name, "ok": False, "count": 0, "error": why, "skipped": True}


# The words an adverse search adds to a firm's names. Google answers a name with its
# hundred most RELEVANT stories, and for a large firm those are results and orders —
# the one about the raid is story 140. Asked alongside these, it is story 1.
ADVERSE_TERMS = ("fraud OR default OR insolvency OR NCLT OR SEBI OR raid OR arrest OR "
                 "probe OR penalty OR scam OR ED OR CBI OR lawsuit OR liquidation")
# Names per Google OR query: a firm's people go in one query, not one each, and a URL
# that long still stays far inside what Google accepts.
GOOGLE_OR_CHUNK = 8


def strict_queries(wanted: list[str], context_of: dict[str, str] | None = None,
                   adverse: bool = False) -> tuple[list[str], list[str]]:
    """(Google queries, Bing queries) for a NAME search.

    GOOGLE TAKES OR, so a firm's names — or its people — are one query: Google allows a
    cloud host ~450 searches an hour, and a firm searched under three names cost three
    of them. BING ANSWERS ZERO to any query with OR in it (checked live: '"Enerparc
    Energy" OR "Enerparc"' -> 0 items, '"Enerparc"' -> 14), so Bing keeps one per name;
    Bing has no such budget. Either way a name containing another adds nothing — every
    article printing "Enerparc Energy" prints "Enerparc" — so only the shortest forms
    are asked. ``context_of`` (a person's city) narrows a common name on both."""
    ctx = context_of or {}
    low = [w.lower() for w in wanted]
    short = [w for i, w in enumerate(wanted)
             if not any(j != i and low[j] in low[i] and (low[j] != low[i] or j < i)
                        for j in range(len(wanted)))]
    bing = [f'"{w}"' + (f" {ctx[w]}" if ctx.get(w) else "") for w in short]
    google: list[str] = []
    for i in range(0, len(short), GOOGLE_OR_CHUNK):
        chunk = short[i:i + GOOGLE_OR_CHUNK]
        parts = [f'"{w}" {ctx[w]}' if ctx.get(w) else f'"{w}"' for w in chunk]
        if len(parts) == 1:
            google.append(parts[0])
        else:
            # A name with its city is grouped, so OR binds the pairs, not the words.
            google.append(" OR ".join(f"({p})" if ctx.get(w) else p
                                      for p, w in zip(parts, chunk, strict=True)))
        if adverse:
            names = " OR ".join(f'"{w}"' for w in chunk)
            google.append(f"({names}) ({ADVERSE_TERMS})")
    return google, bing


class NewsSearch:
    """One search engine per process: a bounded upstream pool, a shared TTL cache and
    request coalescing, so twenty desks asking about the same company at once cost one
    fetch rather than twenty."""

    def __init__(self, *, disable_gdelt: bool = False, timeout_s: float = 12.0,
                 cache_ttl_s: int = 900, cache_max: int = 500,
                 upstream_concurrency: int = 8, pace: bool = True) -> None:
        self.disable_gdelt = disable_gdelt
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_max = cache_max
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(upstream_concurrency)
        self._gdelt_warned = False
        # pace=False (tests that measure overlap) keeps the breakers, drops the spacing.
        k = 1.0 if pace else 0.0
        self._gates = {"Google News": SourceGate("Google News", GOOGLE_INTERVAL_S * k),
                       "Bing News": SourceGate("Bing News", BING_INTERVAL_S * k),
                       "GDELT": SourceGate("GDELT", GDELT_INTERVAL_S * k)}
        # ONE client for the process, built on first use.
        #
        # A sweep of four hundred firms used to open four hundred clients — three fresh
        # TLS handshakes each, to the same three hosts, thrown away after one request.
        # The handshakes, not the searching, were most of the sweep's wall-clock. A
        # shared pool keeps the connections alive between terms.
        #
        # Built lazily rather than in __init__ so a test that patches
        # ``app.news.search.httpx.AsyncClient`` still gets its stub — construction has
        # to happen after the patch, not before it.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    def sent(self, source: str) -> int:
        """Requests ``source`` has been sent by this process (see SourceGate.sent)."""
        return self._gates[source].sent

    async def http(self) -> httpx.AsyncClient:
        """The shared pool, for the official-source fetches that ride along with it."""
        return await self._http()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        headers=UA, follow_redirects=True, timeout=self.timeout_s,
                        limits=httpx.Limits(max_keepalive_connections=20,
                                            max_connections=40,
                                            keepalive_expiry=60.0))
        return self._client

    async def aclose(self) -> None:
        """Close the shared pool — called from the app's shutdown hook."""
        client, self._client = self._client, None
        if client is not None:
            # A socket that will not close politely must not hold up shutdown.
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        async with self._sem:
            resp = await client.get(url, headers=UA, timeout=self.timeout_s,
                                    follow_redirects=True)
            resp.raise_for_status()
            return resp.text

    async def _gated(self, name: str, gated: bool, max_wait: float | None,
                     call: Any, retries: int = 0,
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run one source call through its gate. A throttled answer pauses the lane and,
        for a bulk search (``retries`` > 0), the SAME search is asked again once the
        pause is over — a digest waits for Google rather than leaving a firm unsearched.
        ``gated=False`` is for the diagnostics probe, which reports the network as is."""
        gate = self._gates[name]
        for attempt in range(retries + 1):
            if gated and (why := await gate.enter(max_wait)) is not None:
                return [], _skipped(name, why)
            sent_at = time.monotonic()
            items, status = await call()
            if not gated:
                return items, status
            if status.get("throttled"):
                gate.throttled(sent_at)
                if attempt < retries:
                    continue
                return items, status
            gate.done(bool(status.get("ok")))
            return items, status
        return [], _skipped(name, "no answer")  # unreachable: the loop always returns

    @staticmethod
    def _patience(patient: bool, retries: int | None = None) -> tuple[float | None, int]:
        """(max_wait, retries). A bulk search waits out a throttle and retries — the
        digest THROTTLE_RETRIES times, the screen sweep fewer (``retries``) so a batch
        fits the browser's wait; a person at the search box waits 20 s at most."""
        if not patient:
            return 20.0, 0
        return None, THROTTLE_RETRIES if retries is None else retries

    async def _google(self, client: httpx.AsyncClient, term: str,
                      dfrom: str, dto: str, *, gated: bool = True, patient: bool = False,
                      retries: int | None = None,
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        q = term
        if _valid_date(dfrom):
            q += " after:" + dfrom
        if _valid_date(dto):
            q += " before:" + dto

        async def call() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            try:
                xml = await self._get(client, GOOGLE_NEWS % urllib.parse.quote(q))
                items = _rss_items(xml, "Google News")
                return items, _ok("Google News", len(items))
            except Exception as exc:  # noqa: BLE001 - a best-effort source
                log.warning("pulse_google_news_failed", extra={"error": str(exc)})
                return [], _failed("Google News", exc)
        max_wait, tries = self._patience(patient, retries)
        return await self._gated("Google News", gated, max_wait, call, retries=tries)

    async def _bing(self, client: httpx.AsyncClient, term: str,
                    dfrom: str, dto: str, *, gated: bool = True, patient: bool = False,
                    retries: int | None = None,
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Ignores the date range — Bing's RSS has no reliable date filter — but stays
        up when the other two throttle, which is exactly when it earns its place."""
        async def call() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            try:
                xml = await self._get(client, BING_NEWS % urllib.parse.quote(term))
                items = _rss_items(xml, "Bing News", require_http=True)
                return items, _ok("Bing News", len(items))
            except Exception as exc:  # noqa: BLE001
                log.warning("pulse_bing_news_failed", extra={"error": str(exc)})
                return [], _failed("Bing News", exc)
        max_wait, tries = self._patience(patient, retries)
        return await self._gated("Bing News", gated, max_wait, call, retries=tries)

    async def _gdelt(self, client: httpx.AsyncClient, term: str,
                     dfrom: str, dto: str, query: str = "", *, gated: bool = True,
                     max_wait: float | None = None,
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """``query`` is a ready-made GDELT query (a strict name search); without it the
        term is searched as one quoted phrase. GDELT allows one request every five
        seconds, so a bulk search asks it only when a slot is free (``max_wait=0``)
        and the rest are reported as not asked; there is no quick retry, which is
        exactly what earned the 429s."""
        if self.disable_gdelt:
            return [], {"name": "GDELT", "ok": True, "count": 0, "note": "off"}
        q = urllib.parse.quote(query or f'"{term}"')
        url = (GDELT_RANGE % (q, dfrom.replace("-", "") + "000000",
                              dto.replace("-", "") + "235959")
               if _valid_date(dfrom) and _valid_date(dto) else GDELT_TS % q)

        async def call() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            try:
                raw = (await self._get(client, url)).strip()
                if not raw or raw[0] not in "{[":
                    # A throttled GDELT answers 200 with a plain-text notice, not JSON.
                    return [], _failed("GDELT", "non-JSON response (usually rate-limited)")
                import json as _json
                out = []
                for a in (_json.loads(raw).get("articles") or []):
                    u = a.get("url") or ""
                    if u:
                        out.append({"title": (a.get("title") or "").strip(), "url": u,
                                    "source": a.get("domain") or _domain(u),
                                    "when": (a.get("seendate") or "")[:8], "via": "GDELT"})
                return out, _ok("GDELT", len(out))
            except Exception as exc:  # noqa: BLE001
                # Log the friendly reason ONCE. Google News + Bing already cover the
                # search, so a permanently rate-limited GDELT must not fill the log.
                if not self._gdelt_warned:
                    self._gdelt_warned = True
                    log.warning("pulse_gdelt_unavailable", extra={
                        "error": str(exc),
                        "note": "results come from Google News + Bing; "
                                "set PULSE_DISABLE_GDELT=1 to silence"})
                return [], _failed("GDELT", exc)
        return await self._gated("GDELT", gated, max_wait, call)

    @staticmethod
    def _norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:60]

    async def _fetch_merge(self, term: str, dfrom: str, dto: str, limit: int,
                           strict: bool = False, context: str = "",
                           names: list[str] | None = None, bulk: bool = False,
                           retries: int | None = None, adverse: bool = False,
                           contexts: dict[str, str] | None = None,
                           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``bulk`` (a sweep or a digest — many terms in a row) asks GDELT only when it
        has a free slot rather than queueing every term behind its 5-second limit.

        ``strict`` is how a NAME is searched (a firm, a director) as opposed to a
        topic: each of the firm's names (``names``, the term when absent — see names.py)
        is quoted; ``context`` (a city) rides along unquoted to narrow a common name —
        or ``contexts``, one per name, for a firm's people asked together; and only
        articles that print one of the names, inside the window, are kept. Google gets
        the names in one OR query, Bing one query each (see ``strict_queries``);
        ``adverse`` adds a Google query of the names with the adverse words. A topic
        search ("solar tariff") stays loose."""
        client = await self._http()
        wanted = [n for n in (names or [term]) if n.strip()] or [term]
        if not strict:
            g_queries = b_queries = [term]
        else:
            ctx_of = dict(contexts or {})
            if context:
                ctx_of = {w: ctx_of.get(w) or context for w in wanted}
            g_queries, b_queries = strict_queries(wanted, ctx_of, adverse)

        async def each(fetch: Any, queries: list[str],
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            """One source over every query: the items merged, and the source counted as
            searched if any query reached it (else the first failure is its status)."""
            got = await asyncio.gather(*(fetch(client, q, dfrom, dto, patient=bulk or strict,
                                               retries=retries) for q in queries))
            items = [i for its, _ in got for i in its]
            ok = [st for _, st in got if st.get("ok")]
            status = ({**ok[0], "count": len(items)} if ok else got[0][1])
            return items, status

        async def gdelt_call() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            # Bulk searches leave GDELT out: at one request per 5 s it covered a quarter
            # of a digest at best and never added a story Google and Bing lacked.
            if bulk or strict:
                return [], {"name": "GDELT", "ok": True, "count": 0, "note": "bulk: not used"}
            return await self._gdelt(client, term, dfrom, dto, "")

        (google, s_google), (gdelt, s_gdelt), (bing, s_bing) = await asyncio.gather(
            each(self._google, g_queries), gdelt_call(), each(self._bing, b_queries))
        sources = [s_google, s_gdelt, s_bing]
        log.info("pulse_search_sources", extra={"term": term, "google": len(google),
                                                "gdelt": len(gdelt), "bing": len(bing),
                                                "failed": [s["name"] for s in sources
                                                           if not s["ok"]]})
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for h in google + gdelt + bing:
            if not h["title"] or not re.match(r"https?://", h["url"]):
                continue
            # De-duplicate on the NORMALISED headline: the same story reaches us from
            # three sources under three URLs, and the desk should read it once.
            key = self._norm(h["title"]) or h["url"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)
        if strict:
            kept = []
            for h in merged:
                hit = next((n for n in wanted if mentions(h, n)), None)
                if hit is not None and in_window(h, dfrom, dto):
                    # Which name it was found under: a card matched only on a short name
                    # says so, so a namesake ("nSure.ai" for NSure Energy) is seen as one.
                    kept.append({**h, "matched": hit})
            log.info("pulse_search_strict", extra={"term": term, "fetched": len(merged),
                                                   "kept": len(kept)})
            merged = kept
        merged.sort(key=lambda h: h.get("when") or "0", reverse=True)
        return merged[:limit], sources

    @staticmethod
    def _judged(articles: list[dict[str, Any]], live: bool) -> list[dict[str, Any]]:
        """Colour the articles for THIS caller.

        Triage is applied on the way out rather than baked into the cache, because the
        same story is a different colour depending on whether the firm owes us money —
        and one fetch has to serve both readings."""
        return [{**a, **triage(a.get("title", ""), live).as_dict()} for a in articles]

    async def search(self, term: str, dfrom: str = "", dto: str = "",
                     limit: int = 40, live: bool = False, *, strict: bool = False,
                     context: str = "", names: list[str] | None = None,
                     ) -> list[dict[str, Any]]:
        """Just the articles — what the digest and the schedules want."""
        return (await self.search_detail(term, dfrom, dto, limit, live, strict=strict,
                                         context=context, names=names))[0]

    async def search_detail(self, term: str, dfrom: str = "", dto: str = "",
                            limit: int = 40, live: bool = False, *,
                            strict: bool = False, context: str = "",
                            names: list[str] | None = None, bulk: bool = False,
                            retries: int | None = None, adverse: bool = False,
                            contexts: dict[str, str] | None = None,
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Articles AND how each source fared.

        An empty list means two very different things — "this company is not in the
        news" and "nothing could be reached" — and a desk sweeping 400 firms cannot
        tell them apart from the count alone. The second value says which is which."""
        term = (term or "").strip()
        if not term:
            return [], []
        context = (context or "").strip()
        key = (f"{term.lower()}|{dfrom or ''}|{dto or ''}"
               + (f"|strict|{context.lower()}|{'|'.join(names or []).lower()}"
                  f"|{'adverse' if adverse else ''}"
                  f"|{'|'.join(f'{k}={v}' for k, v in sorted((contexts or {}).items()))}"
                  if strict else ""))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            self._cache.move_to_end(key)
            return self._judged(hit[1], live), hit[2]
        # COALESCING: an all-firms sweep asks for 300 companies at once and the same
        # company can appear on several desks' screens. Everyone waits on ONE fetch.
        inflight = self._inflight.get(key)
        if inflight is not None:
            raw, sources = await asyncio.shield(inflight)
            return self._judged(raw, live), sources
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            merged, sources = await self._fetch_merge(term, dfrom, dto, limit,
                                                      strict, context, names, bulk, retries,
                                                      adverse, contexts)
            # Only a COMPLETE answer is worth remembering. Cache a total outage and the
            # radar stays blind long after the network is back; cache a partial one
            # (Google throttled, Bing fine) and a digest's retry pass gets the same gap
            # back from the cache instead of asking Google again. GDELT is optional.
            core_failed = any(not s["ok"] for s in sources if s["name"] != "GDELT")
            if not core_failed and any(s["ok"] and not s.get("note") for s in sources):
                self._cache[key] = (time.time(), merged, sources)
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_max:
                    self._cache.popitem(last=False)
            if not fut.done():
                fut.set_result((merged, sources))
            return self._judged(merged, live), sources
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

    async def sweep(self, terms: list[str], *, dfrom: str = "", dto: str = "",
                    limit: int = 40, live_terms: set[str] | None = None,
                    concurrency: int = 6, strict: bool = False) -> list[dict[str, Any]]:
        """Search MANY terms at once, server-side.

        The sweep is what the radar is for — four hundred firms, asked in one go — and
        it used to run in the BROWSER: one request per firm per watch term, strictly
        one after another, each crossing the gateway. Four hundred round trips of a few
        seconds each is half an hour of a spinner, and every one of them paid its own
        TLS handshakes. None of that work was ever parallel, because a `for` loop with
        an `await` in it cannot be.

        Here the fan-out happens next to the upstreams instead: `concurrency` terms in
        flight at a time, sharing the pool, the cache and the coalescing that already
        exist. The bound is deliberate and modest — Google News and Bing answer a
        polite client and rate-limit a greedy one, and a sweep that gets itself
        throttled finishes later than one that waits its turn.

        Returns one row per term IN THE ORDER GIVEN, each carrying its own articles and
        its own per-source outcome, so a term that found nothing can be told apart from
        a term whose sources were unreachable — per term, not just for the sweep.
        """
        wanted = [t.strip() for t in (terms or []) if t and t.strip()]
        if not wanted:
            return []
        live = live_terms or set()
        gate = asyncio.Semaphore(max(1, concurrency))

        async def one(term: str) -> dict[str, Any]:
            async with gate:
                try:
                    articles, sources = await self.search_detail(
                        term, dfrom, dto, limit, term in live, strict=strict,
                        names=watch_names(term) if strict else None, bulk=True,
                        retries=SWEEP_THROTTLE_RETRIES)
                except Exception as exc:  # noqa: BLE001 — one bad term never ends a sweep
                    log.warning("pulse_sweep_term_failed",
                                extra={"term": term, "error": str(exc)})
                    return {"term": term, "articles": [], "count": 0, "sources": [],
                            "error": _reason(exc)}
                row: dict[str, Any] = {"term": term, "articles": articles,
                                       "count": len(articles), "sources": sources}
                down = [s for s in sources if not s["ok"]]
                if down and not articles:
                    row["error"] = ("no news source could be reached — " + "; ".join(
                        f"{s['name']}: {s['error']}" for s in down))
                return row

        t0 = time.time()
        rows = await asyncio.gather(*(one(t) for t in wanted))
        log.info("pulse_sweep", extra={
            "terms": len(wanted), "seconds": round(time.time() - t0, 1),
            "articles": sum(r["count"] for r in rows),
            "failed_terms": sum(1 for r in rows if r.get("error"))})
        return list(rows)

    async def probe(self) -> dict[str, Any]:
        """Can this container reach the news at all?

        The one question a deployment cannot answer from the outside. A search that
        returns nothing looks identical whether the desk picked a quiet company or the
        container has no egress, and on a locked-down host it is ALWAYS the second —
        which is a five-minute fix once somebody knows, and a fortnight of blaming the
        radar until they do.

        Uses a fixed, deliberately well-covered term so "no articles" cannot be
        mistaken for "no connectivity", bypasses the cache so it reports the network as
        it is NOW, and times each source separately.
        """
        term = "reserve bank of india"
        client = await self._http()
        out: list[dict[str, Any]] = []

        async def timed(name: str, coro: Any) -> None:
            t0 = time.time()
            items, status = await coro
            out.append({**status, "name": name, "count": len(items),
                        "ms": int((time.time() - t0) * 1000)})

        await asyncio.gather(
            timed("Google News", self._google(client, term, "", "", gated=False)),
            timed("GDELT", self._gdelt(client, term, "", "", gated=False)),
            timed("Bing News", self._bing(client, term, "", "", gated=False)))
        out.sort(key=lambda s: str(s.get("name")))
        reachable = [s for s in out if s["ok"] and not s.get("note")]
        return {
            "ok": bool(reachable),
            "term": term,
            "sources": out,
            "summary": (f"{len(reachable)} of {len(out)} sources answered"
                        if reachable else
                        "NO source could be reached from this container — the radar "
                        "cannot work until outbound HTTPS to news.google.com, "
                        "www.bing.com and api.gdeltproject.org is allowed"),
        }
