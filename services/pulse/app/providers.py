"""News providers — where PULSE gets its raw items from.

A provider is anything with an async ``fetch()`` returning ``NewsItem`` rows. Three are
built in:

* ``SampleProvider`` — a handful of hard-coded items so the whole pipeline runs offline
  (dev, tests, demos). No network, no keys.
* ``RSSProvider``    — any RSS/Atom feed (most news sites and Google News queries).
* ``JSONProvider``   — a JSON endpoint returning ``[{"title": ..., "summary": ...,
  "url": ..., "published_at": ...}, ...]`` — the shape a paid news API adapter or an
  internal scraper would expose.

Adding a new source kind is deliberately a beginner-sized task: subclass, implement
``fetch()``, add one line to ``build_providers()``. Failures never crash a scan — a
provider that errors is logged and skipped, and its error is reported in the scan
result so the operator can see it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own configured feeds
from dataclasses import dataclass

import httpx
from evam_backend_core.logging import get_logger

log = get_logger("pulse.providers")


@dataclass(frozen=True)
class NewsItem:
    """One raw news item, before matching. ``url`` (or title as a fallback) is the
    identity used for exactly-once writes downstream."""

    source: str
    title: str
    summary: str = ""
    url: str = ""
    published_at: str | None = None  # ISO timestamp when the feed provides one


class Provider:
    """Base class. ``name`` shows up in scan stats and in the intel row's ``source``."""

    kind = "base"

    def __init__(self, name: str, url: str = "", timeout_s: float = 10.0) -> None:
        self.name = name
        self.url = url
        self.timeout_s = timeout_s

    async def fetch(self) -> list[NewsItem]:  # pragma: no cover - abstract
        raise NotImplementedError


class SampleProvider(Provider):
    """Offline demo items. Deterministic, so scans are idempotent end-to-end."""

    kind = "sample"

    async def fetch(self) -> list[NewsItem]:
        return [
            NewsItem(source=self.name, url="https://example.com/pulse-sample/1",
                     title="EcoSoch Solar commissions 12.5 MW rooftop portfolio ahead of schedule",
                     summary="The Bengaluru EPC completed its C&I portfolio with two "
                             "marquee offtakers signed."),
            NewsItem(source=self.name, url="https://example.com/pulse-sample/2",
                     title="NCLT admits insolvency plea against Sunrise Green Power",
                     summary="Operational creditor moves NCLT over unpaid EPC dues."),
            NewsItem(source=self.name, url="https://example.com/pulse-sample/3",
                     title="State discom announces new open-access solar policy",
                     summary="Policy expected to widen the C&I rooftop market."),
        ]


class RSSProvider(Provider):
    """Minimal RSS/Atom reader using only the standard library parser.

    We read ``<item>`` (RSS) and ``<entry>`` (Atom) elements and take title / link /
    description. Deliberately tolerant: a malformed element is skipped, never fatal.
    """

    kind = "rss"

    async def fetch(self) -> list[NewsItem]:
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
        root = ET.fromstring(resp.text)  # noqa: S314 - operator-configured feed
        items: list[NewsItem] = []
        ns_atom = "{http://www.w3.org/2005/Atom}"
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            title = (el.findtext("title") or el.findtext(f"{ns_atom}title") or "").strip()
            if not title:
                continue
            link = (el.findtext("link") or "").strip()
            if not link:  # Atom puts the URL in <link href="...">
                link_el = el.find(f"{ns_atom}link")
                link = (link_el.get("href") if link_el is not None else "") or ""
            summary = (el.findtext("description") or el.findtext(f"{ns_atom}summary") or "").strip()
            published = (el.findtext("pubDate") or el.findtext(f"{ns_atom}updated") or None)
            items.append(NewsItem(source=self.name, title=title, summary=summary,
                                  url=link, published_at=published))
        return items


class JSONProvider(Provider):
    """A JSON list endpoint — the adapter shape for paid news APIs and internal scrapers."""

    kind = "json"

    async def fetch(self) -> list[NewsItem]:
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
        items: list[NewsItem] = []
        for row in resp.json():
            title = (row.get("title") or "").strip()
            if not title:
                continue
            items.append(NewsItem(source=self.name, title=title,
                                  summary=(row.get("summary") or "").strip(),
                                  url=(row.get("url") or "").strip(),
                                  published_at=row.get("published_at")))
        return items


_KINDS: dict[str, type[Provider]] = {
    "sample": SampleProvider,
    "rss": RSSProvider,
    "json": JSONProvider,
}


def build_providers(source_configs: list[dict], timeout_s: float) -> list[Provider]:
    """Turn the PULSE_SOURCES config into provider instances. Unknown kinds are logged
    and skipped rather than failing startup — a typo in one source must not take the
    whole radar down."""
    providers: list[Provider] = []
    for cfg in source_configs:
        kind = str(cfg.get("kind", "")).lower()
        cls = _KINDS.get(kind)
        if cls is None:
            log.warning("pulse_unknown_source_kind", extra={"kind": kind, "cfg": cfg})
            continue
        providers.append(cls(name=str(cfg.get("name") or kind),
                             url=str(cfg.get("url") or ""), timeout_s=timeout_s))
    return providers
