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
    return {"name": name, "ok": False, "count": 0, "error": _reason(exc)}


class SourceGate:
    """Spacing and a breaker for ONE upstream — what the 13 Oct live run was missing.

    Six firms searched at once asked Google News for six feeds at once, over and over,
    and Google answered with its automated-traffic page (HTTP 503) for 365 of 489
    firms; GDELT says outright "one request every 5 seconds" and answered 429 for 389.
    Each source now gets requests no closer together than ``interval`` seconds,
    whatever the concurrency above it. A source that fails ``trip_after`` times in a
    row is paused for ``cooldown`` seconds instead of being hammered, and every search
    in that time reports it as not searched — never as searched and empty.

    ``enter(max_wait)``: wait for the next slot and return None, or return the reason
    the source is skipped (paused, or the slot is further off than ``max_wait``)."""

    def __init__(self, name: str, interval: float, *, trip_after: int = 4,
                 cooldown: float = 600.0) -> None:
        self.name, self.interval = name, interval
        self.trip_after, self.cooldown = trip_after, cooldown
        self._next = 0.0
        self._fails = 0
        self._paused_until = 0.0

    async def enter(self, max_wait: float | None = None) -> str | None:
        now = time.monotonic()
        if now < self._paused_until:
            return (f"paused after {self.trip_after} failures in a row; "
                    f"tried again in {int(self._paused_until - now) // 60 + 1} min")
        slot = max(now, self._next)
        if max_wait is not None and slot - now > max_wait:
            return f"not asked: it allows one request every {self.interval:g} s"
        # Claimed before sleeping (no await in between), so concurrent callers queue.
        self._next = slot + self.interval
        if slot > now:
            await asyncio.sleep(slot - now)
        return None

    def done(self, ok: bool) -> None:
        if ok:
            self._fails = 0
            return
        self._fails += 1
        if self._fails >= self.trip_after:
            self._paused_until = time.monotonic() + self.cooldown
            self._fails = 0
            log.warning("pulse_source_paused", extra={"source": self.name,
                                                     "seconds": self.cooldown})


# Request spacing per source, in seconds. Google News tolerates a steady trickle and
# blocks a burst; GDELT publishes its limit (one per 5 s); Bing took the whole live run.
GOOGLE_INTERVAL_S = 1.5
BING_INTERVAL_S = 0.25
GDELT_INTERVAL_S = 5.5


def _skipped(name: str, why: str) -> dict[str, Any]:
    return {"name": name, "ok": False, "count": 0, "error": why, "skipped": True}


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
                     call: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run one source call through its gate (spacing + breaker). ``gated=False``
        is for the diagnostics probe, which must report the network as it is."""
        gate = self._gates[name]
        if gated and (why := await gate.enter(max_wait)) is not None:
            return [], _skipped(name, why)
        items, status = await call()
        if gated:
            gate.done(bool(status.get("ok")))
        return items, status

    async def _google(self, client: httpx.AsyncClient, term: str,
                      dfrom: str, dto: str, *, gated: bool = True,
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
        return await self._gated("Google News", gated, None, call)

    async def _bing(self, client: httpx.AsyncClient, term: str,
                    dfrom: str, dto: str, *, gated: bool = True,
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
        return await self._gated("Bing News", gated, None, call)

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
                           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``bulk`` (a sweep or a digest — many terms in a row) asks GDELT only when it
        has a free slot rather than queueing every term behind its 5-second limit.

        ``strict`` is how a NAME is searched (a firm, a director) as opposed to a
        topic: each of the firm's names (``names``, the term when absent — see names.py)
        is searched in quotes on its own; ``context`` (a city) rides along unquoted to narrow a
        common name; and only articles that print one of the names, inside the window,
        are kept. A topic search ("solar tariff") stays loose."""
        client = await self._http()
        wanted = [n for n in (names or [term]) if n.strip()] or [term]
        ctx = f" {context}" if context else ""
        if not strict:
            queries = [term]
        else:
            # Bing answers ZERO results to any query with OR in it (checked live: '"Enerparc
            # Energy" OR "Enerparc"' -> 0 items, '"Enerparc"' -> 14), so each name is its
            # own query. A name containing another adds nothing — every article printing
            # "Enerparc Energy" prints "Enerparc" — so only the shortest forms are asked.
            low = [w.lower() for w in wanted]
            queries = [f'"{w}"{ctx}' for i, w in enumerate(wanted)
                       if not any(j != i and low[j] in low[i] and (low[j] != low[i] or j < i)
                                  for j in range(len(wanted)))]
        phrase = " OR ".join(f'"{n}"' for n in wanted)
        gq = (f"({phrase})" if len(wanted) > 1 else phrase) + ctx

        async def each(fetch: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            """One source over every query: the items merged, and the source counted as
            searched if any query reached it (else the first failure is its status)."""
            got = await asyncio.gather(*(fetch(client, q, dfrom, dto) for q in queries))
            items = [i for its, _ in got for i in its]
            ok = [st for _, st in got if st.get("ok")]
            status = ({**ok[0], "count": len(items)} if ok else got[0][1])
            return items, status

        (google, s_google), (gdelt, s_gdelt), (bing, s_bing) = await asyncio.gather(
            each(self._google),
            self._gdelt(client, term, dfrom, dto, gq.strip() if strict else "",
                        max_wait=0.0 if bulk or strict else None),
            each(self._bing))
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
                                                      strict, context, names, bulk)
            # Only a search that actually reached something is worth remembering: cache
            # a total outage for 15 minutes and the radar stays blind long after the
            # network comes back.
            if any(s["ok"] and not s.get("note") for s in sources):
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
                        names=watch_names(term) if strict else None, bulk=True)
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
