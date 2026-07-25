"""Matching — which Register entities does a news item talk about, and how bad is it?

Two deliberately simple, explainable steps (a human can always answer "why did this
alert fire?"):

1. **Name match** — an item matches an entity when the entity's legal name, display
   name, or code appears in the item's title+summary (case-insensitive, whole-word-ish).
   Corporate suffixes ("Pvt", "Ltd", "Private", "Limited", ...) are stripped from the
   name first so "EcoSoch Solar Pvt Ltd" matches "EcoSoch Solar commissions ...".
2. **Signal** — RED if any configured red word appears (insolvency, fraud, default, …),
   GREEN if a green word does (commissioned, awarded, raises, …), otherwise AMBER
   ("look at this, human"). Word lists are runtime config, not code.

This is intentionally not an ML model: for a lending desk's adverse-media radar the
cost of a false negative is high and the volume is low, so an auditable keyword rule
beats a black box. If you later want smarter matching, this module is the seam —
swap ``match_entities``/``classify_signal`` and nothing else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.providers import NewsItem

# Common Indian corporate suffixes that appear in legal names but never in headlines.
_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|india|energy|energies|co|company)\b\.?", re.IGNORECASE)


@dataclass(frozen=True)
class WatchEntity:
    """The slice of a Register entity the matcher needs (id + the names to look for)."""

    id: str
    code: str
    legal_name: str
    display_name: str | None = None


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _match_terms(entity: WatchEntity) -> list[str]:
    """The search terms for one entity: display name, then legal name minus corporate
    suffixes. Terms shorter than 4 characters are dropped — they match everything."""
    terms = []
    for raw in (entity.display_name, _SUFFIXES.sub(" ", entity.legal_name)):
        if not raw:
            continue
        term = _normalise(raw)
        if len(term) >= 4:
            terms.append(term)
    return terms


def match_entities(item: NewsItem, watchlist: list[WatchEntity]) -> list[WatchEntity]:
    """Every watchlist entity mentioned in the item (usually 0 or 1)."""
    haystack = _normalise(f"{item.title} {item.summary}")
    return [e for e in watchlist if any(term in haystack for term in _match_terms(e))]


def classify_signal(item: NewsItem, red_words: list[str], green_words: list[str]) -> str:
    """RED beats GREEN beats AMBER: adverse words win when a headline has both."""
    haystack = _normalise(f"{item.title} {item.summary}")
    if any(w in haystack for w in red_words):
        return "RED"
    if any(w in haystack for w in green_words):
        return "GREEN"
    return "AMBER"
