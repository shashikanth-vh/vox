"""Deterministic canonical correction of the transcript — before any model reads it.

The two-test diagnostic's decisive errors were born in speech-to-text: ICAC
Bank, Access Bank, Ajit Berla, AVM Finance, USD returns. The Claude path can
often repair these from the KNOWN NAMES block; the Regional path demonstrably
cannot — and neither should have to. This pass fixes the transcript the
STRUCTURING model receives, so both engines read clean input, while the stored
raw transcript remains untouched evidence and every replacement is flagged.

Two mechanisms, both conservative:

  * an ALIAS TABLE of known garble → canonical pairs. Seeded below with the
    pairs observed in the diagnostic and the staging A/B; extended WITHOUT a
    rebuild via config ``stt.canonical_aliases`` ({"heard": "canonical"}).
    This is the correction-mining store in embryo: every reviewer fix worth
    keeping goes here and compounds.
  * a FUZZY match of lender-shaped spans ("<TitleCase words> Bank|Finance|
    Capital") against the desk's lender roster, at a high similarity bar —
    catches fresh mishearings of known institutions (Kotak Menindra Bank)
    without ever touching an unknown name.

Never guessed: a span that matches nothing is left exactly as heard.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable

from .glossary import LENDER_GLOSSARY

# Observed garble → canonical. Word-boundary, case-insensitive, longest first.
# Every entry here was SEEN in a real transcript (diagnostic appendices B/D,
# staging A/Bs and the fresh take of 23 Sep) or is a trivial variant of one.
_SEED_ALIASES: dict[str, str] = {
    "ICAC Bank": "ICICI Bank",
    "ICIC Bank": "ICICI Bank",
    "Access Bank": "Axis Bank",
    "Ajit Berla": "Aditya Birla",
    "Ajit Birla": "Aditya Birla",
    "Aditya Berla": "Aditya Birla",
    "Egypt Burla": "Aditya Birla",
    "Egypt Berla": "Aditya Birla",
    "Kotak Menindra": "Kotak Mahindra",
    "Kotak Mahendra": "Kotak Mahindra",
    "Potak Mahendra": "Kotak Mahindra",
    "Potak Mahindra": "Kotak Mahindra",
    "AVM Finance": "Evam Finance",
    "AVM": "Evam",
    "Greenco": "Greenko",
    "Exact Climate Solutions": "Hexa Climate Solutions",
    "USD returns": "GST returns",
    # Dotted form only: the case-blind bare "us returns" occurs in ordinary
    # English ("gives us returns of 18%") and must never match.
    "U.S. returns": "GST returns",
    "dexindication": "debt syndication",
    "dex indication": "debt syndication",
    # "deteriorating" alone is ordinary English and must never be rewritten;
    # this exact bigram, twice observed as STT's rendering of the document-list
    # item "debtor ageing", cannot occur in a legitimate sentence.
    "bank statements, deteriorating": "bank statements, debtor ageing",
    "VARKS": "VOX",
    "performance by guarantee": "performance bank guarantee",
    "collateral curve": "collateral cover",
    "first order over": "first charge over",
    "land-tightened documents": "land-title documents",
    "land tightened documents": "land title documents",
    "reprimand obligations": "repayment obligations",
}

_LENDER_SUFFIX = ("Bank", "Finance", "Capital")
# "<Up to four TitleCase-ish words> <Bank|Finance|Capital>" — the shape of an
# Indian lender name as STT emits it.
_LENDER_SPAN_RE = re.compile(
    r"\b([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})\s+(Bank|Finance|Capital)\b")
# 0.78 with first-letter agreement: "Kotak Mahendru"→Kotak Mahindra (0.9) and
# "ICAC"→ICICI (0.84) clear it; "Jana Bank"→Canara Bank (0.80, J≠C) — a REAL
# small-finance bank that must never be rewritten into a different one — does not.
_FUZZY_BAR = 0.78


def _roster_names() -> list[str]:
    """The lender roster's primary names ("SBI (State Bank of India)" → both)."""
    out: list[str] = []
    for entry in LENDER_GLOSSARY:
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", entry)
        if m:
            out.extend((m.group(1).strip(), m.group(2).strip()))
        else:
            out.append(entry.strip())
    return [n for n in out if n]


_ROSTER = _roster_names()


def _alias_table(extra: dict[str, str] | None) -> list[tuple[str, str]]:
    table = dict(_SEED_ALIASES)
    for k, v in (extra or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            table[k.strip()] = v.strip()
    # Longest first so "AVM Finance" wins before the bare "AVM".
    return sorted(table.items(), key=lambda kv: -len(kv[0]))


def canonicalize_transcript(text: str,
                            extra_aliases: dict[str, str] | None = None,
                            ) -> tuple[str, list[str]]:
    """Return (corrected text, one flag sentence per distinct replacement).
    The correction is presentation for the STRUCTURING step; the caller keeps
    the raw text as evidence."""
    if not text:
        return text, []
    notes: list[str] = []
    seen: set[str] = set()

    def note(heard: str, canonical: str, how: str) -> None:
        key = f"{heard.lower()}→{canonical.lower()}"
        if key not in seen:
            seen.add(key)
            notes.append(f"canonical: ‘{heard}’ → ‘{canonical}’ ({how})")

    out = text
    for heard, canonical in _alias_table(extra_aliases):
        pattern = re.compile(r"\b" + re.escape(heard) + r"\b", re.IGNORECASE)

        def _sub(m: re.Match, _c: str = canonical, _h: str = heard) -> str:
            note(m.group(0), _c, "alias table")
            return _c
        out = pattern.sub(_sub, out)

    # Fuzzy lender spans — only spans whose suffix word matches a roster entry's,
    # and only when nothing in the roster matches exactly already.
    def _fuzz(m: re.Match) -> str:
        span = f"{m.group(1)} {m.group(2)}"
        low = span.lower()
        if any(low == r.lower() for r in _ROSTER):
            return span                       # already canonical
        best, best_score = None, 0.0
        for r in _ROSTER:
            rl = r.lower()
            if not rl.endswith(m.group(2).lower()):
                continue
            if rl[:1] != low[:1]:
                continue      # a different first letter is a different institution
            score = difflib.SequenceMatcher(None, low, rl).ratio()
            if score > best_score:
                best, best_score = r, score
        if best and best_score >= _FUZZY_BAR:
            note(span, best, f"lender roster, similarity {best_score:.2f}")
            return best
        return span

    out = _LENDER_SPAN_RE.sub(_fuzz, out)
    return out, notes
