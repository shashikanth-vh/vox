"""Lender names in the report, resolved against the names the desk knows.

Speech-to-text mangles proper nouns ("UN Finance", "Aditya Billan"). The
canonical pass fixes the transcript with a fixed alias table; this step works
on the OUTPUT fields that hold lender names and against the live list — the
tenant's FI master plus the common-lender glossary — so a near-miss becomes
the real name (flagged as resolved) and a name nobody knows is flagged for
the reviewer instead of being presented as a lender.

Pure and deterministic: no model call, never raises, returns flag sentences.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from ..core.resolve import norm_name, phonetic_ratio

# Fields that hold lender names as free text (one or several, comma/and separated).
_NAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("lending", "existing_bankers"),
    ("syndication", "existing_lenders"),
    ("syndication", "probable_lenders"),
)
_SPLIT_RE = re.compile(r"\s*(?:,|;|/|&|\band\b|\bor\b)\s*", re.IGNORECASE)
_GENERIC = re.compile(r"^(?:none|nil|na|n/a|not (?:stated|mentioned|disclosed)|unknown|tbd)$", re.IGNORECASE)
FUZZY_BAR = 0.82


def short_form(entry: str) -> str:
    """'SBI (State Bank of India)' -> 'SBI'; plain names pass through."""
    return re.sub(r"\s*\(.*\)\s*$", "", entry).strip()


def _aliases(entry: str) -> list[str]:
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", entry)
    return [m.group(1).strip(), m.group(2).strip()] if m else [entry.strip()]


def resolve_one(spoken: str, lenders: list[str]) -> tuple[str, str]:
    """Returns (name, how) with how in {'exact', 'resolved', 'unknown'}."""
    sp = spoken.strip()
    if not sp:
        return sp, "exact"
    key = norm_name(sp)
    best: tuple[float, str] | None = None
    for entry in lenders:
        canon = short_form(entry)
        for alias in _aliases(entry):
            if norm_name(alias) == key or alias.lower() == sp.lower():
                return canon, "exact" if canon.lower() == sp.lower() else "resolved"
            a, b = norm_name(alias), key
            if not a or not b:
                continue
            score = max(difflib.SequenceMatcher(None, a, b).ratio(), phonetic_ratio(sp, alias))
            # a bare substring the other way ("Kotak" inside "Kotak Mahindra Bank")
            if (b in a or a in b) and min(len(a), len(b)) >= 4:
                score = max(score, 0.9)
            if best is None or score > best[0]:
                best = (score, canon)
    if best and best[0] >= FUZZY_BAR and best[1][0].lower() == sp[0].lower():
        return best[1], ("exact" if best[1].lower() == sp.lower() else "resolved")
    return sp, "unknown"


def resolve_lender_names(report: dict, lenders: list[str] | None) -> list[str]:
    if not lenders:
        return []
    notes: list[str] = []

    def fix_text(text: str) -> str:
        parts = [p for p in _SPLIT_RE.split(text) if p and p.strip()]
        out: list[str] = []
        for p in parts:
            if _GENERIC.match(p.strip()):
                out.append(p.strip())
                continue
            name, how = resolve_one(p, lenders)
            if how == "resolved":
                notes.append(f"lender name resolved: '{p.strip()}' → '{name}'")
            elif how == "unknown":
                notes.append(f"'{p.strip()}' is not a lender the desk knows — confirm the name")
            out.append(name)
        return ", ".join(out)

    for block, key in _NAME_FIELDS:
        cells = report.get(block)
        if not isinstance(cells, dict):
            continue
        cell = cells.get(key)
        if isinstance(cell, dict) and isinstance(cell.get("value"), str) and cell["value"].strip():
            if not cell.get("user_override"):
                cell["value"] = fix_text(cell["value"])
    syn = report.get("syndication")
    if isinstance(syn, dict):
        lu = syn.get("lender_updates")
        items: Any = lu.get("value") if isinstance(lu, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("lender"), str) and item["lender"].strip():
                    name, how = resolve_one(item["lender"], lenders)
                    if how == "resolved":
                        notes.append(f"lender name resolved: '{item['lender'].strip()}' → '{name}'")
                        item["lender"] = name
                    elif how == "unknown":
                        notes.append(f"'{item['lender'].strip()}' is not a lender the desk knows — confirm the name")
    return list(dict.fromkeys(notes))
