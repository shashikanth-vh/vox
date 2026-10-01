"""Two readings of one transcript, compared field by field.

When both engines read a take, a fact they DISAGREE on cannot be high
confidence whatever either model claimed: the disagreement is the strongest
signal the pipeline has that the field needs a person. ``reconcile_readings``
caps such fields at medium on both reports and flags them, so they land in
the reviewer's "needs you" strip — and nothing else changes: agreed fields
keep their confidence, prose the two merely word differently is left alone.

Field kinds come from the registry: numbers compare as numbers, enums and
dates exactly, short strings after folding case and punctuation, lists of
names by count. Prose (summaries, remarks, discussion points) and judgement
fields are never compared.
"""

from __future__ import annotations

import re
from typing import Any

from ..spec.registry import load_registry

PROSE: frozenset[str] = frozenset({
    "meeting_summary", "key_discussion_points", "opportunity_assessment",
    "competitive_intelligence", "data_quality_flags", "remarks", "notes",
    "present_requirement", "offer_notes", "next_steps", "opportunity_score_override_reason",
    "action_items", "lender_updates",
})


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", str(s or "").lower())).strip()


def _absent(v: Any) -> bool:
    return v is None or v == [] or v == "" or v == "not_specified"


def _differs(fdef: dict, a: Any, b: Any) -> bool:
    if _absent(a) and _absent(b):
        return False
    if _absent(a) != _absent(b):
        return True
    ftype = fdef.get("type")
    if ftype in ("number", "int"):
        try:
            return abs(float(a) - float(b)) > 1e-9
        except (TypeError, ValueError):
            return _norm(a) != _norm(b)
    if ftype == "list":
        la = a if isinstance(a, list) else [a]
        lb = b if isinstance(b, list) else [b]
        if len(la) != len(lb):
            return True
        fa = sorted(_norm(x).split(" ")[0] for x in la if _norm(x))
        fb = sorted(_norm(x).split(" ")[0] for x in lb if _norm(x))
        return fa != fb
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return False
    # a long free-text field is prose in disguise: the same number inside both
    # (an amount, a size) is one fact worded twice
    if len(na) > 60 or len(nb) > 60:
        xa = re.findall(r"\d+(?:\.\d+)?", na)
        xb = re.findall(r"\d+(?:\.\d+)?", nb)
        return bool(xa and xb and xa != xb)
    return True


def _short(v: Any) -> str:
    s = "; ".join(str(x) for x in v[:3]) if isinstance(v, list) else str(v)
    return s if len(s) <= 40 else s[:37] + "…"


def reconcile_readings(primary: dict, alt: dict, registry_version: str | None = None,
                       names: tuple[str, str] = ("Default", "Regional")) -> list[str]:
    """Mutates both reports: a disagreeing fact field is capped at medium on
    each, and a flag naming both readings is appended to each report's
    data_quality_flags. Returns the flags (one list, same on both)."""
    reg = load_registry(registry_version)
    defs: list[tuple[str, dict]] = [("common", f) for f in reg["common"]]
    for b, spec in reg["blocks"].items():
        defs += [(b, f) for f in (spec.get("fields") or [])]
    flags: list[str] = []
    for block, fdef in defs:
        key = fdef["key"]
        if key in PROSE or fdef.get("judgement") or fdef.get("system"):
            continue
        ca = (primary.get(block) or {}).get(key) if isinstance(primary.get(block), dict) else None
        cb = (alt.get(block) or {}).get(key) if isinstance(alt.get(block), dict) else None
        if not isinstance(ca, dict) and not isinstance(cb, dict):
            continue
        va = ca.get("value") if isinstance(ca, dict) else None
        vb = cb.get("value") if isinstance(cb, dict) else None
        if not _differs(fdef, va, vb):
            continue
        for cell in (ca, cb):
            if isinstance(cell, dict) and cell.get("confidence") == "high" and not cell.get("user_override"):
                cell["confidence"] = "medium"
        label = fdef.get("label") or key
        flags.append(f"the two models disagree on {label}: {names[0]} read "
                     f"'{_short(va) if not _absent(va) else 'not captured'}', {names[1]} read "
                     f"'{_short(vb) if not _absent(vb) else 'not captured'}'")
    if flags:
        for rep in (primary, alt):
            common = rep.setdefault("common", {})
            cell = common.get("data_quality_flags") or {"value": [], "confidence": "n/a"}
            merged = list(dict.fromkeys([*(cell.get("value") or []), *flags]))
            common["data_quality_flags"] = {"value": merged, "confidence": "n/a"}
    return flags
