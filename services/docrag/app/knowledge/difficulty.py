"""Per-page difficulty scoring: decide whether a page's local text extraction
is trustworthy, or whether it should be routed to Sarvam Vision instead.

This is the "clean page vs difficult page" fork from the architecture:
most pages in a real corpus have a clean embedded text layer and gain
nothing from a Vision API call (slower, costs money, no better result).
A minority -- scans, photos of documents, pages with heavy stylized
tables -- have weak or garbled local extraction and are worth the Vision
call. This module is the judgment call in between.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import get_settings

_JUNK_CHAR_RE = re.compile(r"[^\w\s.,;:()/\-₹$%]")


@dataclass
class PageDifficulty:
    page_number: int
    char_count: int
    garbled_ratio: float
    is_difficult: bool
    reason: str


def score_page(page_text: str, page_number: int) -> PageDifficulty:
    settings = get_settings()
    stripped = page_text.strip()
    char_count = len(stripped)

    if char_count < settings.min_chars_per_page:
        return PageDifficulty(
            page_number=page_number,
            char_count=char_count,
            garbled_ratio=0.0,
            is_difficult=True,
            reason=f"only {char_count} chars extracted locally (below {settings.min_chars_per_page})",
        )

    junk = len(_JUNK_CHAR_RE.findall(stripped))
    garbled_ratio = junk / max(char_count, 1)
    if garbled_ratio > settings.max_garbled_ratio:
        return PageDifficulty(
            page_number=page_number,
            char_count=char_count,
            garbled_ratio=garbled_ratio,
            is_difficult=True,
            reason=f"{garbled_ratio:.0%} non-standard characters (possible OCR garble/encoding issue)",
        )

    return PageDifficulty(
        page_number=page_number,
        char_count=char_count,
        garbled_ratio=garbled_ratio,
        is_difficult=False,
        reason="clean local text layer",
    )
