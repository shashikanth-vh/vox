"""Silence-hallucination control (Build Specification 13.1) — mandatory, testable.

whisper-1 and faster-whisper alike fabricate text over silence and noise. Before
structuring, every segment is examined and the suspect ones are MARKED (never
silently dropped — the transcript is verbatim evidence): high ``no_speech_prob``,
degenerate repetition inside a segment, and the same text looping across
consecutive segments. The structuring prompt (rule 3) forbids extracting a fact
solely from a suspect segment.
"""

from __future__ import annotations

import re

# Above this the ASR itself says "probably nobody was speaking".
NO_SPEECH_THRESHOLD = 0.6
# A phrase repeated this many times inside one segment is a filler loop.
_REPEAT_RUN = 3


def _degenerate_repetition(text: str) -> bool:
    words = re.findall(r"\S+", text.lower())
    if len(words) < 6:
        return False
    # any word or short phrase (1-4 words) repeated >= _REPEAT_RUN times in a row
    for size in (1, 2, 3, 4):
        run = 1
        for i in range(size, len(words) - size + 1, size):
            if words[i:i + size] == words[i - size:i]:
                run += 1
                if run >= _REPEAT_RUN:
                    return True
            else:
                run = 1
    return False


def mark_suspect_segments(segments: list[dict]) -> list[dict]:
    """Return the segments with ``suspect: True/False`` (and a ``suspect_reason``)
    stamped on each. Input items carry at least ``text``; ``no_speech_prob``,
    ``start``/``end`` ride along when the ASR provides them."""
    out: list[dict] = []
    prev_text = None
    prev_repeats = 0
    for seg in segments:
        text = (seg.get("text") or "").strip()
        reason = None
        nsp = seg.get("no_speech_prob")
        if isinstance(nsp, (int, float)) and nsp >= NO_SPEECH_THRESHOLD:
            reason = f"no_speech_prob {nsp:.2f}"
        elif _degenerate_repetition(text):
            reason = "degenerate repetition"
        elif text and text.lower() == (prev_text or "").lower():
            prev_repeats += 1
            if prev_repeats >= _REPEAT_RUN - 1:
                reason = "same text looping across segments"
        if text.lower() != (prev_text or "").lower():
            prev_repeats = 0
        prev_text = text
        marked = {**seg, "suspect": reason is not None}
        if reason:
            marked["suspect_reason"] = reason
        out.append(marked)
    return out


def transcript_for_structuring(segments: list[dict]) -> str:
    """The text handed to the model: suspect segments stay IN (rule 3 tells the
    model how to treat them) but are wrapped in explicit markers so the rule has
    something concrete to bite on."""
    parts = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if seg.get("suspect"):
            parts.append(f"[SUSPECT SEGMENT — {seg.get('suspect_reason', 'unreliable')}] "
                         f"{text} [/SUSPECT]")
        else:
            parts.append(text)
    return "\n".join(parts)
