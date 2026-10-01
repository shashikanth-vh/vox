"""Evidence and amount discipline on the OUTPUT of structuring.

Two guards, provider-independent because they read the report, not the model:

* ``evidence_guard`` — every fact field (a number, a name, a place, a date, an
  ask) must carry the transcript words it came from (prompt v3's ``evidence``),
  and those words must actually occur in the transcript. A value with no words
  behind it drops to LOW confidence and is flagged, so it lands in the
  reviewer's "needs you" strip instead of passing as a fact. The 1 Oct 2026
  bake-off take showed why: one model gave the syndication a ₹2 Cr deal size
  that no sentence in the transcript carried.

* ``amount_lane_guard`` — a rupee figure belongs to one product line. When the
  same number sits on lending.requirement_quantum_cr AND
  syndication.deal_size_cr, the syndication copy is cleared (the own-book ask
  is the one that was spoken as an ask) and flagged, unless its own evidence
  says that amount is being syndicated.

Both mutate cells in place, never raise, and return flag sentences for
common.data_quality_flags — the same contract as pipeline.guards.
"""

from __future__ import annotations

import re
from typing import Any

from ..spec.registry import load_registry

# Fact fields whose value needs words behind it. Enum classifications (sector,
# meeting_type, requirement_nature …) are inferred and stay out; prose and
# judgement fields are not facts; dates default from the capture and are
# flagged by their own path.
EVIDENCE_REQUIRED: frozenset[str] = frozenset({
    "location", "attendees_counterparty", "next_steps", "follow_up_date", "follow_up_time",
    "requirement_quantum_cr", "company_turnover_cr", "existing_bankers", "project_location",
    "present_requirement",
    "deal_size_cr", "existing_lenders", "probable_lenders",
    "deal_size", "offer_components", "asset_location", "target_project_size",
    "valuation_approach", "buyer_criteria",
})

_TOKEN_RE = re.compile(r"[\w₹]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 3 or t.isdigit()]


def quote_supported(evidence: str | None, transcript: str) -> bool:
    """Does the quote occur in the transcript? Exact substring first (after
    whitespace folding); otherwise most of its words must appear — STT and the
    canonical pass can change a letter or two, and a quote that is 80% there
    is a quote, while a quote the transcript never said is not."""
    if not evidence or not transcript:
        return False
    ev = re.sub(r"\s+", " ", evidence).strip().lower()
    tr = re.sub(r"\s+", " ", transcript).lower()
    if ev and ev in tr:
        return True
    toks = _tokens(ev)
    if not toks:
        return False
    have = set(_tokens(tr))
    hit = sum(1 for t in toks if t in have)
    return hit / len(toks) >= 0.6


def _absent(v: Any) -> bool:
    return v is None or v == [] or v == "" or v == "not_specified"


def _short(v: Any) -> str:
    if isinstance(v, list):
        s = "; ".join(str(x) for x in v[:3])
    else:
        s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _labels(registry_version: str | None) -> dict[tuple[str, str], str]:
    reg = load_registry(registry_version)
    out: dict[tuple[str, str], str] = {}
    for f in reg["common"]:
        out[("common", f["key"])] = f.get("label") or f["key"]
    for b, spec in reg["blocks"].items():
        for f in spec.get("fields") or []:
            out[(b, f["key"])] = f.get("label") or f["key"]
    return out


def carries_evidence(report: dict) -> bool:
    """Did this reading quote at all? A report from an older prompt, or a
    model that skipped the contract, has no evidence keys anywhere — judging
    every fact as unquoted would flood the reviewer, so the guard stands down."""
    for block, cells in report.items():
        if isinstance(cells, dict):
            for cell in cells.values():
                if isinstance(cell, dict) and isinstance(cell.get("evidence"), str):
                    return True
    return False


def evidence_guard(report: dict, transcript: str,
                   registry_version: str | None = None) -> list[str]:
    notes: list[str] = []
    if not carries_evidence(report):
        return notes
    try:
        labels = _labels(registry_version)
    except Exception:  # noqa: BLE001 — labels are cosmetic
        labels = {}
    for block, cells in report.items():
        if block == "subsector_details" or not isinstance(cells, dict):
            continue
        for key, cell in cells.items():
            if key not in EVIDENCE_REQUIRED or not isinstance(cell, dict):
                continue
            if _absent(cell.get("value")) or cell.get("user_override"):
                continue
            if cell.get("confidence") not in ("high", "medium"):
                continue
            ev = cell.get("evidence")
            if isinstance(ev, str) and quote_supported(ev, transcript):
                continue
            if ev is not None:
                # a quote the transcript never said is worse than none
                cell.pop("evidence", None)
            cell["confidence"] = "low"
            label = labels.get((block, key), key)
            notes.append(f"{label}: no transcript words behind '{_short(cell.get('value'))}' — confirm")
    return notes


_SYN_WORDS = re.compile(r"syndicat|consortium|arrang|club|participat|lenders?\b|banks?\b", re.IGNORECASE)


def amount_lane_guard(report: dict) -> list[str]:
    """The desk rule allows one ask on both lanes ("5 Cr working capital, taking
    it to syndication" files both) — so the guard only acts when the reading
    quotes, and the syndication copy's own quote does not tie the amount to a
    syndication. Without evidence it stands down, like evidence_guard."""
    lend = report.get("lending") if isinstance(report.get("lending"), dict) else None
    syn = report.get("syndication") if isinstance(report.get("syndication"), dict) else None
    if not lend or not syn or not carries_evidence(report):
        return []
    q = (lend.get("requirement_quantum_cr") or {}).get("value")
    d = (syn.get("deal_size_cr") or {}).get("value")
    if not isinstance(q, (int, float)) or not isinstance(d, (int, float)) or isinstance(q, bool):
        return []
    if abs(float(q) - float(d)) > 1e-9:
        return []
    ev = (syn.get("deal_size_cr") or {}).get("evidence") or ""
    if isinstance(ev, str) and _SYN_WORDS.search(ev) and re.search(r"\d", ev):
        return []                                    # the speaker tied this amount to the syndication
    syn["deal_size_cr"] = {"value": None, "confidence": "n/a"}
    amount = f"₹{q:g} Cr"
    return [f"{amount} was placed on both the lending ask and the syndication size; "
            f"kept as the lending ask — set the syndication size only if a separate amount was spoken"]
