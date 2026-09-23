"""Deterministic guards on structured output — code, not prompt obedience.

The two-test diagnostic (21 Sep 2026) showed the failure class that matters:
a *convincing* report carrying a few commercially decisive errors — a stage
quietly upgraded (indicative proposal → in-principle approval), a negation
reversed (unwilling → willing), a score presented though never spoken. Prompt
rules ask the model to behave; these guards make the behaviour a property of
the pipeline, identically for every structuring provider (Claude or Sarvam —
they run AFTER structuring, on the output).

Three guards:

- ``negation_flags``     — every transcript sentence carrying a negation word
                           becomes a review flag. A single missed "not" can
                           reverse a credit conclusion; the reviewer's eye is
                           forced to each one.
- ``stage_guard``        — the syndication stage ladder (indicative proposal →
                           in-principle approval → sanction) can never be
                           climbed silently: a field wording a HIGHER rung than
                           the transcript evidences is downgraded to the
                           evidenced rung and flagged. "Not a sanction" is
                           negation, not evidence.
- ``score_suggestion_flag`` — an opportunity score that was never spoken is
                           legitimate as an AI *suggestion*, never as a fact;
                           the flag says so where the reviewer approves.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Negations
# ---------------------------------------------------------------------------
_NEGATION_RE = re.compile(
    r"\b(not|no|never|unwilling|cannot|can't|won't|declined?|declines|rejected?|"
    r"rejects|refused?|refuses|unable|without)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def negation_sentences(transcript: str, cap: int = 6) -> list[str]:
    """The transcript sentences that carry a negation — trimmed, deduped, capped.
    These are the sentences where one mis-heard word reverses the meaning."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT.split(transcript or ""):
        s = raw.strip()
        if not s or len(s) > 240 or not _NEGATION_RE.search(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def negation_flags(transcript: str, cap: int = 6) -> list[str]:
    return [f"negation check: “{s}”" for s in negation_sentences(transcript, cap)]


# ---------------------------------------------------------------------------
# Stage ladder
# ---------------------------------------------------------------------------
# Rung 1: indicative proposal · rung 2: in-principle approval · rung 3: sanction.
_IND_RE = re.compile(r"\bindicative\s+(proposal|offer|terms?)\b", re.IGNORECASE)
_INP_RE = re.compile(r"\bin[\s-]?principle\s+(approval|sanction)\b", re.IGNORECASE)
_SAN_RE = re.compile(r"\bsanction(?:ed)?\b", re.IGNORECASE)
_SAN_NEG_RE = re.compile(
    r"\b(?:not?\s+(?:a\s+|yet\s+|been\s+)?sanction(?:ed)?|no\s+(?:formal\s+)?sanction|"
    r"sanction\s+(?:is\s+)?(?:not|pending|awaited))\b", re.IGNORECASE)
# ASPIRATION IS NOT EVIDENCE: "would like the loan to be sanctioned within three
# weeks" is a request; counting it as rung-3 evidence once unlocked an invented
# "indicative proposal stage" remark on the two-test transcript. These forms
# neither evidence a sanction in the transcript nor claim one in a field value.
_SAN_ASPIRATIONAL_RE = re.compile(
    r"\b(?:to\s+be\s+sanction(?:ed)?|sanction(?:ed)?\s+within|"
    r"sanction\s+(?:is\s+)?(?:required|requested|expected|targeted|sought)|"
    r"(?:requested?|seeking|want(?:s|ed)?|hope[sd]?)\s+(?:a\s+)?sanction)\b",
    re.IGNORECASE)
# An explicit correction caps the ladder: "this is ONLY an indicative proposal"
# (typically right after an STT-garbled "in-principle approval") is the speaker
# saying the lower rung is the truth — the correction is the controlling
# statement, exactly the sentence the two-test diagnostic saw ignored.
_ONLY_INDICATIVE_RE = re.compile(
    r"\bonly\s+(?:an?\s+)?indicative\b", re.IGNORECASE)
_LADDER_WORDS = ["indicative proposal", "in-principle approval", "sanction"]


def stage_evidence_level(transcript: str) -> int:
    """The highest rung the transcript actually evidences. A negated sanction
    ("this is not a sanction", "no formal sanction has been received") is
    evidence AGAINST rung 3, not for it."""
    t = transcript or ""
    level = 0
    if _IND_RE.search(t):
        level = 1
    if _INP_RE.search(t):
        level = 2
    if _SAN_RE.search(t):
        # Every sanction mention must be non-negated AND non-aspirational to count.
        mentions = len(_SAN_RE.findall(t))
        discounted = (len(_SAN_NEG_RE.findall(t)) + len(_INP_RE.findall(t))
                      + len(_SAN_ASPIRATIONAL_RE.findall(t)))
        if mentions > discounted:
            level = 3
    # The controlling correction: "this is ONLY an indicative proposal" caps the
    # ladder at rung 1 whatever else was (mis)heard earlier in the transcript.
    if level > 1 and _ONLY_INDICATIVE_RE.search(t):
        level = 1
    return level


def _value_level(text: str) -> int:
    """The highest rung a FIELD VALUE claims."""
    if not text:
        return 0
    if (_SAN_RE.search(text) and not _SAN_NEG_RE.search(text)
            and not _SAN_ASPIRATIONAL_RE.search(text) and not _INP_RE.search(text)):
        return 3
    if _INP_RE.search(text):
        return 2
    if _IND_RE.search(text):
        return 1
    return 0


def stage_guard_text(value: str, evidence: int) -> tuple[str, str | None]:
    """Downgrade a value's stage wording to the evidenced rung. Returns the
    (possibly rewritten) value and a flag sentence when something changed.
    With NO ladder evidence at all, nothing is rewritten (there is no evidenced
    wording to rewrite TO) — the mismatch is flagged instead."""
    claimed = _value_level(value)
    if claimed == 0 or claimed <= evidence:
        return value, None
    if evidence >= 1:
        target = _LADDER_WORDS[evidence - 1]
        new = value
        if claimed == 3:
            new = _SAN_RE.sub(target, new)
        if claimed >= 2:
            new = _INP_RE.sub(target, new)
        # Collapse an accidental doubled phrase from the substitution.
        new = re.sub(re.escape(target) + r"(\s+" + re.escape(target) + r")+",
                     target, new, flags=re.IGNORECASE)
        return new, (f"stage wording downgraded to “{target}” — the transcript "
                     f"does not evidence “{_LADDER_WORDS[claimed - 1]}”")
    return value, (f"stage check: the value says “{_LADDER_WORDS[claimed - 1]}” "
                   "but the transcript does not evidence any stage — confirm before approving")


# ---------------------------------------------------------------------------
# Opportunity score
# ---------------------------------------------------------------------------
_SPOKEN_SCORE_RE = re.compile(
    r"\b(out\s+of\s+(?:5|five)|rate\s+this|rating\s+of|score\s+(?:of|it|this)|"
    r"(?:[1-5]|one|two|three|four|five)\s*(?:/|out of)\s*(?:5|five))\b", re.IGNORECASE)


def score_was_spoken(transcript: str) -> bool:
    return bool(_SPOKEN_SCORE_RE.search(transcript or ""))


def score_suggestion_flag(score, transcript: str) -> str | None:
    """A score the recorder never spoke is an AI suggestion and must say so."""
    if score in (None, "", 0) or score_was_spoken(transcript):
        return None
    return (f"opportunity score {score} is an AI suggestion — no score was spoken; "
            "confirm or clear it")


# ---------------------------------------------------------------------------
# Application to the two report shapes
# ---------------------------------------------------------------------------
_SPEC_GUARDED_BLOCKS = ("lending", "syndication", "asset_monetisation")
_SPEC_GUARDED_COMMON = ("meeting_summary", "key_discussion_points", "remarks")


def _guard_cell_value(value, evidence: int, notes: list[str]):
    """Stage-guard a cell's value in place-shape: strings and lists of strings."""
    if isinstance(value, str):
        new, note = stage_guard_text(value, evidence)
        if note:
            notes.append(note)
        return new
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                new, note = stage_guard_text(item, evidence)
                if note:
                    notes.append(note)
                out.append(new)
            else:
                out.append(item)
        return out
    return value


def apply_spec_guards(report: dict, transcript: str) -> list[str]:
    """The registry-contract report (blocks of {value, confidence} cells).
    Rewrites over-claimed stage wording IN the cells and returns the flag
    sentences to merge into common.data_quality_flags. Never raises — a guard
    that cannot read a malformed corner simply leaves it alone."""
    notes: list[str] = []
    evidence = stage_evidence_level(transcript)
    for block in _SPEC_GUARDED_BLOCKS:
        cells = report.get(block)
        if not isinstance(cells, dict):
            continue
        for cell in cells.values():
            if isinstance(cell, dict) and "value" in cell:
                cell["value"] = _guard_cell_value(cell["value"], evidence, notes)
    common = report.get("common")
    if isinstance(common, dict):
        for key in _SPEC_GUARDED_COMMON:
            cell = common.get(key)
            if isinstance(cell, dict) and "value" in cell:
                cell["value"] = _guard_cell_value(cell["value"], evidence, notes)
        score_cell = common.get("opportunity_score") or {}
        flag = score_suggestion_flag(
            score_cell.get("value") if isinstance(score_cell, dict) else None, transcript)
        if flag:
            notes.append(flag)
    notes.extend(negation_flags(transcript))
    return list(dict.fromkeys(notes))


def apply_extract_guards(extraction: dict, transcript: str) -> None:
    """The notes-capture shape (core.extract). Adds ``report.quality_flags``
    (created if absent) with the same review steers; rewrites over-claimed
    stage wording in the free-text report fields; leaves the pipeline_stage
    chip alone but flags a Sanctioned claim the transcript does not carry."""
    report = extraction.get("report")
    if not isinstance(report, dict):
        return
    notes: list[str] = []
    evidence = stage_evidence_level(transcript)
    for key in ("summary", "key_intel", "nuances"):
        if key in report:
            report[key] = _guard_cell_value(report.get(key), evidence, notes)
    stage = report.get("pipeline_stage")
    if isinstance(stage, str) and stage.strip().lower() == "sanctioned" and evidence < 3:
        notes.append("stage check: pipeline_stage says Sanctioned but the transcript "
                     "does not evidence a sanction — confirm before approving")
    flag = score_suggestion_flag(report.get("opportunity_score"), transcript)
    if flag:
        notes.append(flag)
    notes.extend(negation_flags(transcript))
    if notes:
        existing = report.get("quality_flags") or []
        report["quality_flags"] = list(dict.fromkeys([*existing, *notes]))
