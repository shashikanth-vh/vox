"""
core.resolve — entity resolution: spoken company name -> ATLAS entity.

This is the make-or-break of VOX. An RM says "met the Bharti Waters folks";
the resolver has to land on lead LD-009 "Bharti Waters Private Limited" and not
open a duplicate. It MUST dedupe the same way ATLAS itself does when adding a
lead or pushing to deals — that logic keys off normName(), so we replicate
normName() byte-for-byte here (see norm_name below).

Matching cascade, highest wins, evaluated per candidate over every spoken form
(company_mentioned + aliases_heard):
    exact       normName equal ................................ 1.00
    containment one normName contains the other ............... 0.85
    fuzzy       token-set ratio >= VOX_FUZZY_MIN .............. 0.75
    phonetic    per-token sound-alike >= VOX_PHONETIC_MIN ..... 0.70
Own-client boost: +VOX_OWN_CLIENT_BOOST if the candidate's RM is the speaker.

The phonetic tier exists because the transcripts come from speech: a field
recording of "Suryodaya" routinely arrives as "Sarvodaya", and the token-set
ratio cannot see that they are the same word mis-heard. Its score (0.70) sits
deliberately BELOW the confident-match threshold and ABOVE the new-lead
ceiling, so a sound-alike is always surfaced as a suggestion for a human to
confirm — never auto-linked, never silently dropped to "new company".

Confidence gate (entity level):
    confident match  -> top >= VOX_ENTITY_MATCH_MIN AND gap-to-2nd >= gap
    confident new    -> top < VOX_NEW_LEAD_MAX (nothing even close)
    anything else    -> needs_approval (ambiguous / weak match)

Output is the entity_match contract:
    {code, canonical_name, match_score, alternatives[], is_new_lead,
     needs_approval, ...diagnostics}
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from app.vocx.core.atlas import AtlasStore, Candidate

# --- normName(): EXACT replica of ATLAS_EVAM_v15.html -------------------------
# function normName(s){return String(s||'').toLowerCase()
#   .replace(/\b(private|pvt|limited|ltd|llp|india|energy|energies|solutions?|
#     services?|technologies|technology|ventures?|renewables?)\b/g,'')
#   .replace(/[^a-z0-9]/g,'')}
_NORM_STOPWORDS = re.compile(
    r"\b(private|pvt|limited|ltd|llp|india|energy|energies|solutions?|services?|"
    r"technologies|technology|ventures?|renewables?)\b"
)
_NORM_NONALNUM = re.compile(r"[^a-z0-9]")


def norm_name(s: Any) -> str:
    """Canonical dedupe key — identical to ATLAS normName().

    Lowercases, strips the ATLAS stopword set (private/pvt/limited/ltd/llp/
    india/energy/energies/solution(s)/service(s)/technolog.../venture(s)/
    renewable(s)), then removes every non-alphanumeric character. The result
    has NO spaces, so it is a matching key, not a tokenizable string.
    """
    s = str(s if s is not None else "").lower()
    s = _NORM_STOPWORDS.sub("", s)
    return _NORM_NONALNUM.sub("", s)


# Token stopwords mirror the normName stopword set, but operate on whitespace
# tokens (normName removes spaces, so fuzzy token-set work has to tokenize the
# original string instead of the normName output).
_TOKEN_STOP = {
    "private", "pvt", "limited", "ltd", "llp", "india", "energy", "energies",
    "solution", "solutions", "service", "services", "technologies", "technology",
    "venture", "ventures", "renewable", "renewables", "the", "and",
}
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(s: Any) -> list[str]:
    """Meaningful lowercase tokens with the ATLAS stopwords removed."""
    raw = _TOKEN_SPLIT.split(str(s if s is not None else "").lower())
    return [t for t in raw if t and t not in _TOKEN_STOP]


def token_set_ratio(a: str, b: str) -> float:
    """A ratio in [0,1] tolerant of word reordering and small typos.

    max of:
      * Dice coefficient over the two token sets (word-level overlap), and
      * SequenceMatcher ratio over the sorted-token join (char-level, catches
        typos and singular/plural drift within a token).
    """
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    dice = (2.0 * len(inter)) / (len(ta) + len(tb))
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(dice, seq)


# Consonant skeleton: first character kept, later vowels dropped. STT mangles
# vowels far more often than consonants ("Suryodaya" -> "Sarvodaya"), so two
# skeletons agreeing is strong evidence of the same spoken word.
_LATER_VOWELS = re.compile(r"(?<=.)[aeiou]")


def _skeleton(tok: str) -> str:
    return _LATER_VOWELS.sub("", tok)


def _sound_alike(a: str, b: str) -> float:
    """How likely two single tokens are the same word mis-heard: the better of
    the raw character ratio and the consonant-skeleton ratio."""
    raw = SequenceMatcher(None, a, b).ratio()
    sk = SequenceMatcher(None, _skeleton(a), _skeleton(b)).ratio()
    return max(raw, sk)


def phonetic_ratio(spoken: str, cand_name: str) -> float:
    """Mean best sound-alike score of each SPOKEN token against the candidate's
    tokens. Averaged over the spoken side only, because a spoken form is often a
    fragment of the full legal name ("Sarvodaya" vs "Suryodaya EPC Pvt Ltd") and
    the extra candidate tokens must not dilute a clean per-word hit."""
    ts, tc = tokens(spoken), tokens(cand_name)
    if not ts or not tc:
        return 0.0
    per_token = sum(max(_sound_alike(s, c) for c in tc) for s in ts) / len(ts)
    # STT also splits or merges words ("Chikballapur" -> "Chip Balapur"), which
    # per-token pairing cannot see — so the space-stripped spoken join is scored
    # against every contiguous span of candidate tokens ("chikballapur", then
    # "chikballapursolar", ...) and the best span wins. Candidate names are a
    # handful of tokens, so the span set stays tiny.
    sj = "".join(ts)
    joined = max(_sound_alike(sj, "".join(tc[i:j]))
                 for i in range(len(tc)) for j in range(i + 1, len(tc) + 1))
    return max(per_token, joined)


class EntityResolver:
    def __init__(self, store: AtlasStore, config: dict[str, Any] | None = None):
        self.store = store
        cfg = config or {}
        th = cfg.get("thresholds", {})
        sc = cfg.get("scores", {})
        self.match_min = th.get("VOX_ENTITY_MATCH_MIN", 0.75)
        self.gap = th.get("VOX_ENTITY_AMBIGUITY_GAP", 0.10)
        self.new_lead_max = th.get("VOX_NEW_LEAD_MAX", 0.55)
        self.boost = th.get("VOX_OWN_CLIENT_BOOST", 0.10)
        self.fuzzy_min = th.get("VOX_FUZZY_MIN", 0.80)
        self.phonetic_min = th.get("VOX_PHONETIC_MIN", 0.78)
        self.s_exact = sc.get("exact", 1.0)
        self.s_contain = sc.get("containment", 0.85)
        self.s_fuzzy = sc.get("fuzzy", 0.75)
        self.s_phonetic = sc.get("phonetic", 0.70)
        self._cands = store.candidates()

    # ---- per-candidate scoring --------------------------------------------
    def _score_one(self, spoken_forms: Sequence[str], cand: Candidate) -> dict[str, Any]:
        """Best base score of a candidate over all spoken forms, plus RM boost."""
        cand_norm = norm_name(cand.name)
        best, how, raw_fuzzy = 0.0, "none", 0.0
        for form in spoken_forms:
            fn = norm_name(form)
            if not fn or not cand_norm:
                continue
            if fn == cand_norm:
                if self.s_exact > best:
                    best, how = self.s_exact, "exact"
                continue
            if len(fn) >= 3 and len(cand_norm) >= 3 and (fn in cand_norm or cand_norm in fn):
                if self.s_contain > best:
                    best, how = self.s_contain, "containment"
                continue
            tr = token_set_ratio(form, cand.name)
            raw_fuzzy = max(raw_fuzzy, tr)
            if tr >= self.fuzzy_min and self.s_fuzzy > best:
                best, how = self.s_fuzzy, "fuzzy"
                continue
            pr = phonetic_ratio(form, cand.name)
            if pr >= self.phonetic_min and self.s_phonetic > best:
                best, how = self.s_phonetic, "phonetic"
        return {"base": best, "how": how, "fuzzy_ratio": round(raw_fuzzy, 3)}

    # ---- public resolve ----------------------------------------------------
    def resolve(
        self,
        company_mentioned: str | None,
        aliases_heard: Sequence[str] | None = None,
        speaking_rm: str = "",
    ) -> dict[str, Any]:
        forms = [f for f in ([company_mentioned] + list(aliases_heard or [])) if f and str(f).strip()]
        scored: list[dict[str, Any]] = []
        for cand in self._cands:
            s = self._score_one(forms, cand)
            if s["base"] <= 0.0:
                continue
            boosted = s["base"]
            own = bool(cand.rm) and _same_rm(cand.rm, speaking_rm)
            if own:
                boosted = round(min(1.0, boosted + self.boost), 4)
            scored.append({
                "code": cand.ref_id,
                "canonical_name": cand.name,
                "kind": cand.kind,
                "ref_type": cand.ref_type,
                "rm": cand.rm,
                "match_score": boosted,
                "base_score": s["base"],
                "match_type": s["how"],
                "fuzzy_ratio": s["fuzzy_ratio"],
                "own_client_boost": own,
            })

        scored.sort(key=lambda x: (x["match_score"], x["base_score"]), reverse=True)

        if not forms:
            return self._empty_result(reason="no_company_mentioned")
        if not scored:
            return self._new_lead_result(forms, alternatives=[], reason="no_candidate_matched")

        top = scored[0]
        second_score = scored[1]["match_score"] if len(scored) > 1 else 0.0
        gap = round(top["match_score"] - second_score, 4)
        alternatives = _alternatives(scored)

        confident_match = top["match_score"] >= self.match_min and gap >= self.gap
        if confident_match:
            return {
                "code": top["code"],
                "canonical_name": top["canonical_name"],
                "kind": top["kind"],
                "ref_type": top["ref_type"],
                "rm": top["rm"],
                "match_score": top["match_score"],
                "match_type": top["match_type"],
                "own_client_boost": top["own_client_boost"],
                "alternatives": alternatives,
                "is_new_lead": False,
                "needs_approval": False,
                "gap_to_second": gap,
                "spoken_forms": forms,
                "reason": "confident_match",
            }

        # nothing even close -> confidently a brand-new company
        if top["match_score"] < self.new_lead_max:
            return self._new_lead_result(
                forms, alternatives=alternatives,
                reason="confident_new_lead", top=top, gap=gap,
            )

        # weak or ambiguous -> approval card (picker + "none -> new lead")
        return {
            "code": top["code"],
            "canonical_name": top["canonical_name"],
            "kind": top["kind"],
            "ref_type": top["ref_type"],
            "rm": top["rm"],
            "match_score": top["match_score"],
            "match_type": top["match_type"],
            "own_client_boost": top["own_client_boost"],
            "alternatives": alternatives,
            "is_new_lead": False,
            "needs_approval": True,
            "gap_to_second": gap,
            "spoken_forms": forms,
            "reason": "ambiguous" if gap < self.gap else "below_threshold",
        }

    # ---- result builders ---------------------------------------------------
    def _new_lead_result(self, forms, alternatives, reason, top=None, gap=None) -> dict[str, Any]:
        # A new lead is a register write -> critical. Auto-create only when we're
        # confident it's genuinely new (reason == confident_new_lead); a near-miss
        # still surfaces alternatives so the RM can pick an existing entity.
        confident_new = reason in ("confident_new_lead", "no_candidate_matched")
        proposed = forms[0] if forms else ""
        return {
            "code": None,
            "canonical_name": _titleish(proposed),
            "kind": "new_lead",
            "ref_type": "Lead",
            "rm": "",
            "match_score": (top["match_score"] if top else 0.0),
            "match_type": "none",
            "own_client_boost": False,
            "alternatives": alternatives,
            "is_new_lead": True,
            "needs_approval": not confident_new,
            "gap_to_second": gap,
            "spoken_forms": forms,
            "proposed_company": _titleish(proposed),
            "reason": reason,
        }

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "code": None, "canonical_name": None, "kind": "unknown",
            "ref_type": None, "rm": "", "match_score": 0.0, "match_type": "none",
            "own_client_boost": False, "alternatives": [], "is_new_lead": False,
            "needs_approval": True, "gap_to_second": None, "spoken_forms": [],
            "reason": reason,
        }


def _same_rm(a: str, b: str) -> bool:
    """RM equality tolerant of short vs full names ('Shubh' vs 'Shubh Dave')."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return a.split()[0] == b.split()[0]


def _alternatives(scored: list[dict[str, Any]], window: float = 0.25, cap: int = 4) -> list[dict[str, Any]]:
    """Runner-up candidates within `window` of the top score, for the picker."""
    if not scored:
        return []
    top = scored[0]["match_score"]
    out = []
    for s in scored[1:]:
        if top - s["match_score"] > window:
            break
        out.append({
            "code": s["code"],
            "canonical_name": s["canonical_name"],
            "kind": s["kind"],
            "ref_type": s["ref_type"],
            "match_score": s["match_score"],
            "rm": s["rm"],
        })
        if len(out) >= cap:
            break
    return out


def _titleish(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return s
    return " ".join(w if (w.isupper() and len(w) > 1) else w.capitalize() for w in s.split())


# --- config helper ------------------------------------------------------------
def load_config(path: str | None = None) -> dict[str, Any]:
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "config.json")  # app/vocx/config.json
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
