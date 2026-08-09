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
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 - parsing responses from named sources
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from evam_backend_core.logging import get_logger

log = get_logger("pulse.search")

UA = {"User-Agent": "Mozilla/5.0 (PRISM-NewsRadar/1.0)"}

GOOGLE_NEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
GDELT_TS = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
            "&mode=ArtList&maxrecords=25&format=json&sort=DateDesc&timespan=3m")
GDELT_RANGE = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
               "&mode=ArtList&maxrecords=25&format=json&sort=DateDesc"
               "&startdatetime=%s&enddatetime=%s")
BING_NEWS = "https://www.bing.com/news/search?q=%s&format=rss&count=25"

# Whole-word lists. Phrases match as phrases; stems are spelled out as full words rather
# than truncated, because a substring match here mislabels a company for the whole desk.
RED = ["fraud", "frauds", "fraudulent", "defrauded", "default", "defaults", "defaulter",
       "wilful defaulter", "insolvency", "insolvent", "bankruptcy", "bankrupt", "arrest",
       "arrested", "raid", "raids", "raided", "scam", "npa", "npas", "money laundering",
       "cbi", "ed probe", "probe", "fir", "firs", "embezzlement", "embezzled",
       "irregularity", "irregularities", "misappropriation", "shell company",
       "tax evasion", "forgery", "fugitive", "fraud case", "loan fraud"]
AMBER = ["litigation", "lawsuit", "court", "penalty", "penalties", "fined", "fine",
         "downgrade", "downgraded", "delay", "delays", "delayed", "layoff", "layoffs",
         "strike", "dispute", "disputes", "show cause", "showcause", "investigation",
         "investigated", "shortfall", "recall", "recalled", "resignation", "resigns",
         "resigned", "warning", "summons"]
GOOD = ["wins", "win", "won", "bags", "bagged", "secures", "secured", "awarded", "awards",
        "award", "signs", "signed", "order", "orders", "contract", "contracts", "epc",
        "ppa", "mou", "commissions", "commissioned", "inaugurates", "inaugurated",
        "inauguration", "launches", "launched", "expansion", "expands", "funding",
        "funded", "raises", "raised", "investment", "invests", "profit", "profits",
        "record", "milestone", "partnership", "partners", "acquires", "acquisition",
        "ipo", "listing", "approval", "approved", "sanctioned", "disbursed", "growth",
        "surges", "jumps", "rises", "upgrade", "upgraded"]


def _wre(words: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


_RED_RE, _AMBER_RE, _GOOD_RE = _wre(RED), _wre(AMBER), _wre(GOOD)


def classify(headline: str) -> str:
    """UGLY / BAD / GOOD — in the desk's own vocabulary, and explainable: whichever list
    the headline hits, hard-adverse first."""
    h = headline or ""
    if _RED_RE.search(h):        # hard-adverse always wins
        return "UGLY"
    if _GOOD_RE.search(h):       # a clear positive outranks a routine watch word
        return "GOOD"
    if _AMBER_RE.search(h):
        return "BAD"
    return "GOOD"


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


def _rss_items(xml_text: str, via: str, *, require_http: bool = False) -> list[dict[str, Any]]:
    """The shared shape of both RSS sources. A feed that is not XML (an error page, a
    captcha) raises here and is handled by the caller as "that source had nothing"."""
    out: list[dict[str, Any]] = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link or (require_http and not link.startswith("http")):
            continue
        source = ""
        se = item.find("source")
        if se is not None and se.text:
            source = se.text.strip()
        source = source or _domain(link)
        # Google appends " - Source" to every headline; the source has its own column.
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        out.append({"title": title, "url": link, "source": source,
                    "when": _when_of(item.findtext("pubDate")), "via": via})
    return out


class NewsSearch:
    """One search engine per process: a bounded upstream pool, a shared TTL cache and
    request coalescing, so twenty desks asking about the same company at once cost one
    fetch rather than twenty."""

    def __init__(self, *, disable_gdelt: bool = False, timeout_s: float = 12.0,
                 cache_ttl_s: int = 900, cache_max: int = 500,
                 upstream_concurrency: int = 8) -> None:
        self.disable_gdelt = disable_gdelt
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_max = cache_max
        self._cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(upstream_concurrency)
        self._gdelt_warned = False

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        async with self._sem:
            resp = await client.get(url, headers=UA, timeout=self.timeout_s,
                                    follow_redirects=True)
            resp.raise_for_status()
            return resp.text

    async def _google(self, client: httpx.AsyncClient, term: str,
                      dfrom: str, dto: str) -> list[dict[str, Any]]:
        q = term
        if _valid_date(dfrom):
            q += " after:" + dfrom
        if _valid_date(dto):
            q += " before:" + dto
        try:
            xml = await self._get(client, GOOGLE_NEWS % urllib.parse.quote(q))
            return _rss_items(xml, "Google News")
        except Exception as exc:  # noqa: BLE001 - a best-effort source
            log.warning("pulse_google_news_failed", extra={"error": str(exc)})
            return []

    async def _bing(self, client: httpx.AsyncClient, term: str,
                    dfrom: str, dto: str) -> list[dict[str, Any]]:
        """Ignores the date range — Bing's RSS has no reliable date filter — but stays
        up when the other two throttle, which is exactly when it earns its place."""
        try:
            xml = await self._get(client, BING_NEWS % urllib.parse.quote(term))
            return _rss_items(xml, "Bing News", require_http=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("pulse_bing_news_failed", extra={"error": str(exc)})
            return []

    async def _gdelt(self, client: httpx.AsyncClient, term: str,
                     dfrom: str, dto: str) -> list[dict[str, Any]]:
        if self.disable_gdelt:
            return []
        q = urllib.parse.quote('"%s"' % term)
        url = (GDELT_RANGE % (q, dfrom.replace("-", "") + "000000",
                              dto.replace("-", "") + "235959")
               if _valid_date(dfrom) and _valid_date(dto) else GDELT_TS % q)
        for attempt in (0, 1):                     # one retry on 429 / transient
            try:
                raw = (await self._get(client, url)).strip()
                if not raw or raw[0] not in "{[":
                    return []
                import json as _json
                out = []
                for a in (_json.loads(raw).get("articles") or []):
                    u = a.get("url") or ""
                    if u:
                        out.append({"title": (a.get("title") or "").strip(), "url": u,
                                    "source": a.get("domain") or _domain(u),
                                    "when": (a.get("seendate") or "")[:8], "via": "GDELT"})
                return out
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if attempt == 0 and ("429" in msg or "timeout" in msg.lower()):
                    await asyncio.sleep(1.0)
                    continue
                # Log the friendly reason ONCE. Google News + Bing already cover the
                # search, so a permanently rate-limited GDELT must not fill the log.
                if not self._gdelt_warned:
                    self._gdelt_warned = True
                    log.warning("pulse_gdelt_unavailable", extra={
                        "error": msg,
                        "note": "results come from Google News + Bing; "
                                "set PULSE_DISABLE_GDELT=1 to silence"})
                return []
        return []

    @staticmethod
    def _norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:60]

    async def _fetch_merge(self, term: str, dfrom: str, dto: str,
                           limit: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            google, gdelt, bing = await asyncio.gather(
                self._google(client, term, dfrom, dto),
                self._gdelt(client, term, dfrom, dto),
                self._bing(client, term, dfrom, dto))
        log.info("pulse_search_sources", extra={"term": term, "google": len(google),
                                                "gdelt": len(gdelt), "bing": len(bing)})
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
            h["severity"] = classify(h["title"])
            merged.append(h)
        merged.sort(key=lambda h: h.get("when") or "0", reverse=True)
        return merged[:limit]

    async def search(self, term: str, dfrom: str = "", dto: str = "",
                     limit: int = 40) -> list[dict[str, Any]]:
        term = (term or "").strip()
        if not term:
            return []
        key = f"{term.lower()}|{dfrom or ''}|{dto or ''}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            self._cache.move_to_end(key)
            return hit[1]
        # COALESCING: an all-firms sweep asks for 300 companies at once and the same
        # company can appear on several desks' screens. Everyone waits on ONE fetch.
        inflight = self._inflight.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            merged = await self._fetch_merge(term, dfrom, dto, limit)
            self._cache[key] = (time.time(), merged)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)
            if not fut.done():
                fut.set_result(merged)
            return merged
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)
