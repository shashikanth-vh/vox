"""Stage 2 — Claude structuring against the schema contract.

Haiku for post-meeting notes; Sonnet for live-mode transcripts (long-context
quality). Both consume the same canonical prompt with the registry JSON and the
contract appended at runtime. The output is validated BEFORE any database write;
a validation failure earns one self-repair round (the model is shown its own
violations) and then becomes a processing failure with retry — never a partial
write, never a best-effort parse.
"""

from __future__ import annotations

import copy as _copy
import json
import logging
import os as _os
import re as _re
from datetime import date as _date
from typing import Any, Callable

from ..spec import (
    ContractError,
    build_tool_schema,
    compute_data_quality_flags,
    latest_prompt_version,
    latest_registry_version,
    load_prompt,
    load_registry,
    validate_report,
)

# Model routing per the spec's cost/quality split.
MODEL_NOTE = "claude-haiku-4-5-20251001"
MODEL_LIVE = "claude-sonnet-5"


def _structure_model(mode: str) -> str:
    """The model this box structures with — an EXCLUSIVE configuration switch.
    Default (unset / "anthropic"): Claude, Haiku for notes and Sonnet for live,
    exactly as production runs. "sarvam": Sarvam-M for everything, so a trial
    box compares like for like. Never both at once; the server's ask_model
    dispatches on the same variable, and the conversation log records whichever
    model actually structured the take."""
    provider = (_os.environ.get("VOCX_STRUCTURE_PROVIDER") or "anthropic").strip().lower()
    if provider == "sarvam":
        # sarvam-m was deprecated mid-trial. Of the two survivors, the
        # catalogue marks sarvam-105b "Always-on reasoning" — it burned its
        # whole 8192 completion budget thinking on the head call every run,
        # answer recovered only by salvage. The conversations tune carries no
        # such tag: direct answers, same price, same 128K context — the
        # better fit for single-turn temperature-0 extraction. SARVAM_MODEL
        # overrides when their catalogue moves again.
        return ((_os.environ.get("SARVAM_MODEL") or "").strip()
                or "sarvam-105b-conversations")
    return MODEL_LIVE if mode == "live" else MODEL_NOTE

log = logging.getLogger("vox.pipeline")


class StructuringError(RuntimeError):
    """The model could not produce a contract-valid report (after the repair
    round). The pipeline turns this into processing_failed with the detail."""


def build_prompt(registry_version: str | None = None) -> str:
    """The canonical prompt with the registry appended — assembled fresh so a
    registry bump flows through with zero code changes."""
    registry = load_registry(registry_version)
    return (
        load_prompt()
        + "\n\n--- REGISTRY (the field blocks to fill) ---\n"
        + json.dumps(registry, ensure_ascii=False)
        + "\n\n--- CONTRACT SHAPE ---\n"
        + "Every field is {\"value\": ..., \"confidence\": \"high|medium|low|n/a\"}. "
          "Top level: detected_use_cases, common, one block per detected use case, "
          "entity_candidates. Absent means absent. When you chose a subsector, also "
          "fill top-level \"subsector_details\" with THAT subsector's canonical data "
          "points from the registry (subsector_canonicals), same {value, confidence} "
          "shape; omit the block when subsector is null. Its keys are the canonical "
          "field KEYS themselves, never the subsector name — e.g. "
          "{\"subsector_details\": {\"operating_uc_capacity_mw\": "
          "{\"value\": \"40 MW\", \"confidence\": \"high\"}}}. "
          "In the syndication block, lender_updates.value is a LIST of "
          "{\"lender\", \"kind\": \"chase\"|\"reply\", \"note\"} objects — "
          "one per lender chased or responding; required whenever such an event "
          "was spoken, empty only when none was."
    )


# --------------------------------------------------------------------------
# Deterministic coercion vocabulary. Everything below serves ONE failure
# class, seen live with the Chikballapur bundle: the transcript's word for a
# thing is not the contract's token for it, the model echoes the speech, the
# strict validator refuses it — and the repair round echoes the speech again.
# Coercions are exact and isomorphic (a different spelling of the same fact);
# anything genuinely outside the vocabulary still fails validation.

# Spoken synonyms for closed enums: the word people say for the token we store.
_ENUM_SYN = {"party_role": {"seller": "owner", "selling": "owner", "vendor": "owner",
                            "purchaser": "buyer", "acquirer": "buyer", "buying": "buyer"}}

# Spoken names for the six locked sectors that no spelling rule can reach.
_SECTOR_SYN = {
    "green_energy": "Renewables", "clean_energy": "Renewables",
    "energy_storage": "BESS", "battery_storage": "BESS",
    "battery_energy_storage": "BESS", "battery_energy_storage_system": "BESS",
    "electric_mobility": "EV Mobility", "e_mobility": "EV Mobility",
    "emobility": "EV Mobility",
    "agriculture": "Climate Resilience", "agri": "Climate Resilience",
}

# Spoken names for subsectors whose registry names share no tokens with the
# way people actually say them. Values MUST be exact taxonomy strings.
_SUBSECTOR_SYN = {
    "charge_point_operator": "CPO", "charge_point_operators": "CPO",
    "ev_charging": "CPO",
    "cold_storage": "Post-harvest infrastructure (cold chain, warehousing, "
                    "processing, packaging)",
}

_CONF_MAP = {"high": "high", "hi": "high", "medium": "medium", "med": "medium",
             "moderate": "medium", "low": "low",
     "n_a": "n/a", "na": "n/a", "none": "n/a", "not_applicable": "n/a"}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

_NUM_RE = _re.compile(
    r"^[~₹$\s]*(?:rs\.?\s*|inr\s*)?([\d,]+(?:\.\d+)?)\s*"
    r"(cr|crore|crores|lakh|lakhs|lac|lacs|l)?\.?$", _re.IGNORECASE)
_DMY_RE = _re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_TEXTDATE_RE = _re.compile(
    r"^(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+)|([A-Za-z]+)\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?)(?:[,\s]+(\d{4}))?$")


def _canon(t: str) -> str:
    # z→s folds -ize/-ization spellings into the registry's -ise forms; both
    # sides of every comparison run through here, so only consistency matters.
    return _re.sub(r"[^a-z0-9]+", "_", t.strip().lower().replace("z", "s")).strip("_")


def _tokens(t: str) -> frozenset:
    # Trailing-s strip folds singular/plural ("Renewables"/"renewable energy").
    return frozenset(w[:-1] if len(w) > 2 and w.endswith("s") else w
                     for w in _canon(t).split("_") if w)


def _match_one(spoken: str, candidates: list) -> str | None:
    """The single candidate the spoken form names, or None. Exact canonical
    spelling first, then token containment either way round ("Cold chain" IS
    inside the post-harvest subsector's name; "Renewable Energy" CONTAINS
    Renewables) — and only when the match is unambiguous: "Solar" names four
    subsectors, so as a subsector it names none of them."""
    c = _canon(spoken)
    exact = [x for x in candidates if _canon(x) == c]
    if len(exact) == 1:
        return exact[0]
    st = _tokens(spoken)
    if not st:
        return None
    near = [x for x in candidates if st <= _tokens(x) or _tokens(x) <= st]
    return near[0] if len(near) == 1 else None


def _number_from(text: str) -> float | None:
    """"25 Cr" / "₹1,200" / "50 lakhs" as the float the register stores. The
    field is denominated in Cr, and 100 lakh is exactly 1 Cr — arithmetic, not
    interpretation. Anything else ("2-3 Cr", "USD 5mn") returns None and the
    validator keeps refusing it."""
    m = _NUM_RE.match(text.strip())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if (m.group(2) or "").lower() in ("lakh", "lakhs", "lac", "lacs", "l"):
        n = n / 100.0
    return n


def _date_from(text: str) -> str | None:
    """ISO datetimes lose their time part; 15/09/2026 reads day-first (this is
    an Indian book — a US-ordered date lands on an impossible month and fails
    honestly); "15th September 2026" and "September 15, 2026" spell out. An
    unparseable or impossible date returns None — never a guessed one."""
    t = text.strip()
    if _re.match(r"^\d{4}-\d{2}-\d{2}[T ]", t):
        return t[:10]
    m = _DMY_RE.match(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _TEXTDATE_RE.match(t)
        if not m:
            return None
        d = int(m.group(1) or m.group(4))
        name = (m.group(2) or m.group(3)).lower()
        hits = [i for mth, i in _MONTHS.items() if len(name) >= 3 and mth.startswith(name)]
        if len(hits) != 1:
            return None
        # A spoken date often omits the year ("14th September") — it means the
        # NEXT such date, never a past one.
        mo = hits[0]
        if m.group(5):
            y = int(m.group(5))
        else:
            today = _date.today()
            y = today.year
            try:
                if _date(y, mo, d) < today:
                    y += 1
            except ValueError:
                return None
    try:
        return _date(y, mo, d).isoformat()
    except ValueError:
        return None


def _coerce_cell_value(fdef: dict, cell: dict) -> None:
    """One cell's value into its field's type, when the two spell the same fact."""
    v = cell.get("value")
    ftype = fdef.get("type")
    if ftype == "enum" and fdef.get("options") and isinstance(v, str):
        vals = {o["value"] for o in fdef["options"]}
        if v in vals:
            return
        c = _canon(v)
        if c in vals:
            cell["value"] = c
            return
        by_label = {_canon(o.get("label", "")): o["value"] for o in fdef["options"]}
        if c in by_label:
            cell["value"] = by_label[c]
            return
        syn = _ENUM_SYN.get(fdef["key"], {})
        if c in syn:
            cell["value"] = syn[c]
    elif ftype == "number" and isinstance(v, str):
        n = _number_from(v)
        if n is not None:
            cell["value"] = n
    elif ftype == "int":
        # JSON's 4.0 is the same 4; "4" spoken as a string is too.
        if isinstance(v, float) and not isinstance(v, bool) and v.is_integer():
            cell["value"] = int(v)
        elif isinstance(v, str) and v.strip().lstrip("+-").isdigit():
            cell["value"] = int(v.strip())
    elif ftype == "date" and isinstance(v, str):
        d = _date_from(v)
        if d is not None and d != v:
            cell["value"] = d
    elif ftype == "list":
        # A list field spoken as one sentence is a one-item list — wrapped, never
        # split: comma-splitting a name like "Sharma, R." is interpretation.
        if isinstance(v, str) and v.strip():
            v = cell["value"] = [v]
        if fdef.get("item_shape") and isinstance(v, list):
            if fdef.get("key") == "lender_updates":
                # {lender, kind, note}: fold the model's spoken variants onto the
                # contract's tokens. Seen live: the model borrowing action_items'
                # {owner, action, deadline} — owner is the lender, action the note,
                # a deadline folds into the note, and the DIRECTION is read from
                # the note's verbs when no kind was given.
                cell["value"] = v = [item for item in v if isinstance(item, dict)]
                for item in v:
                    for alias in ("bank", "name", "lender_name", "owner"):
                        if not item.get("lender") and isinstance(item.get(alias), str):
                            item["lender"] = item.pop(alias)
                    for alias in ("remark", "remarks", "comment", "detail", "update",
                                  "action", "message", "text", "said", "details"):
                        if not item.get("note") and isinstance(item.get(alias), str):
                            item["note"] = item.pop(alias)
                    if isinstance(item.get("deadline"), str) and item["deadline"]:
                        item["note"] = (f"{item.get('note') or ''} "
                                        f"(by {item.pop('deadline')})").strip()
                    else:
                        item.pop("deadline", None)
                    kind = _canon(str(item.get("kind") or ""))
                    if kind in ("chase", "chased", "outbound", "follow_up", "followed_up",
                                "followup", "ping", "pinged"):
                        item["kind"] = "chase"
                    elif kind in ("reply", "replied", "response", "responded", "inbound",
                                  "revert", "reverted", "querie", "query"):
                        item["kind"] = "reply"
                    elif not item.get("kind"):
                        note_c = _canon(str(item.get("note") or ""))
                        if any(w in note_c for w in (
                                "raised", "reverted", "responded", "replied", "came_back",
                                "querie", "query", "declined", "sanctioned", "committed",
                                "confirmed", "agreed")):
                            item["kind"] = "reply"
                        else:
                            item["kind"] = "chase"
                return
            cell["value"] = v = [({"action": item} if isinstance(item, str) else item)
                                 for item in v]
            for item in v:
                if isinstance(item, dict) and "action" not in item:
                    for alias in ("task", "item", "description"):
                        if isinstance(item.get(alias), str):
                            item["action"] = item.pop(alias)
                            break
    elif ftype == "string":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cell["value"] = str(v)
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            joiner = "\n" if (fdef.get("judgement") or fdef.get("system")) else ", "
            cell["value"] = joiner.join(x for x in v if x.strip()) or None


def _scrub_cell(fdef: dict, cell: dict) -> None:
    """Bring one cell to {value, confidence(, user_override)}.

    A "unit" key on a number is NEVER just dropped — {"value": 25, "unit":
    "lakh"} dropped naively becomes 25 Cr, a 100× lie. Lakh folds by division,
    Cr/INR spellings fold away, and any other unit (mn, USD) stays put so the
    validator refuses the cell and the repair round sees it. Decorative extras
    (notes, reasoning) carry no meaning for the stored value and drop; the
    confidence's spelling folds to the four legal words."""
    if fdef.get("type") == "number" and isinstance(cell.get("value"), (int, float)) \
            and not isinstance(cell.get("value"), bool):
        u = _canon(cell["unit"]) if isinstance(cell.get("unit"), str) else None
        if u in ("lakh", "lakhs", "lac", "lacs", "l"):
            cell["value"] = cell["value"] / 100.0
            del cell["unit"]
        elif u in ("cr", "crore", "crores", "inr", "rs", "rupees", "inr_cr", "rs_cr"):
            del cell["unit"]
    keep = {"value", "confidence", "unit"}
    if fdef.get("key") == "opportunity_score":
        keep.add("user_override")
    for k in list(cell):
        if k not in keep:
            del cell[k]
    conf = cell.get("confidence")
    if isinstance(conf, str) and conf not in ("high", "medium", "low", "n/a"):
        cell["confidence"] = _CONF_MAP.get(_canon(conf), conf)
    if "confidence" not in cell and (fdef.get("judgement") or fdef.get("system")):
        cell["confidence"] = "n/a"  # the only value it may carry anyway
    _coerce_cell_value(fdef, cell)


def _fill_block(block_obj: dict, fields: list) -> None:
    """Every registry field present: a key the model omitted IS the contract's
    null ("missing values are null, never invented") — this generalises the
    additive-fields default that meeting_summary/follow_up_time shipped with.
    Present cells get scrubbed and type-coerced; extra keys the model invented
    stay for the validator to name (misfiled data is repair's job, not ours)."""
    for fdef in fields:
        if fdef["key"] not in block_obj:
            block_obj[fdef["key"]] = {"value": None, "confidence": "n/a"}
        cell = block_obj[fdef["key"]]
        if isinstance(cell, dict) and "value" in cell:
            _scrub_cell(fdef, cell)


def _normalize(obj: dict, registry_version: str | None = None) -> dict:
    """Deterministic shape aliases — NOT best-effort repair. Each one observed in
    the field or one exact spelling away from it, each isomorphic to the contract
    shape; anything genuinely broken still fails validation. The passes, in order:

    1. the use-case declaration (string→list, spoken spellings→registry keys,
       blocks-imply-tags, detected-but-missing block → all-null block);
    2. every block filled to the full registry key set, cells scrubbed and
       type-coerced (enum labels/synonyms, "25 Cr"→25.0, 4.0→4, datetime→date,
       sentence→[sentence], unit folding);
    3. judgement confidences pinned to "n/a";
    4. the TAXONOMY — all six sectors, every subsector: spoken spellings resolve
       against the locked lists; a subsector names its parent when the sector is
       missing or wrong; speech genuinely outside the taxonomy clears to null
       WITH a data-quality flag, because "not determinable" is the truthful
       answer and a whole take must not die for a filter chip;
    5. entity_candidates flattened to plain names, null→[];
    6. subsector_details unwrapped, label-spelled keys → canonical keys, bare
       values wrapped with the registry's own hi/md default, invented data
       points dropped (they have nowhere to render), orphans cleared."""
    registry = load_registry(registry_version)
    ucs = list(registry["use_cases"])
    blocks = registry.get("blocks", {})

    # -- 1. the use-case declaration ---------------------------------------
    detected = obj.get("detected_use_cases")
    if isinstance(detected, str):
        detected = obj["detected_use_cases"] = [detected]
    by_canon_uc = {_canon(u): u for u in ucs}
    if isinstance(detected, list):
        fixed = [by_canon_uc.get(_canon(u), u) if isinstance(u, str) else u
                 for u in detected]
        if all(isinstance(u, str) for u in fixed):
            fixed = list(dict.fromkeys(fixed))
        obj["detected_use_cases"] = detected = fixed
    known_top = {"detected_use_cases", "common", "entity_candidates",
                 "subsector_details", *ucs}
    for k in list(obj):
        if k not in known_top and isinstance(obj.get(k), dict):
            uc = by_canon_uc.get(_canon(k))
            if uc and uc not in obj:
                obj[uc] = obj.pop(k)
    if not (isinstance(detected, list) and detected):
        present = [uc for uc in ucs if isinstance(obj.get(uc), dict) and obj[uc]]
        if present:
            obj["detected_use_cases"] = detected = present
            for uc in ucs:
                if uc not in present and obj.get(uc) == {}:
                    del obj[uc]
    if isinstance(detected, list):
        for uc in detected:
            if isinstance(uc, str) and uc not in obj \
                    and (blocks.get(uc) or {}).get("fields"):
                obj[uc] = {}  # filled to all-null below: detected, nothing heard

    # -- 2. blocks to full shape, cells coerced -----------------------------
    common = obj.get("common")
    if isinstance(common, dict):
        _fill_block(common, registry.get("common") or [])
    for uc in ucs:
        if isinstance(obj.get(uc), dict) and (blocks.get(uc) or {}).get("fields"):
            _fill_block(obj[uc], blocks[uc]["fields"])

    # -- 3. judgement prose never grades itself -----------------------------
    if isinstance(common, dict):
        for fdef in registry.get("common", []):
            if not (fdef.get("judgement") or fdef.get("system")):
                continue
            cell = common.get(fdef["key"])
            if isinstance(cell, dict) and cell.get("confidence") in ("high", "medium", "low"):
                cell["confidence"] = "n/a"

    # -- 4. the taxonomy, all six sectors and every subsector ----------------
    if isinstance(common, dict):
        taxonomy = registry["taxonomy"]
        sectors = list(taxonomy)
        parent_of = {sub: sec for sec, subs in taxonomy.items() for sub in subs}
        notes: list[str] = []

        def _val(key: str):
            cell = common.get(key)
            return cell.get("value") if isinstance(cell, dict) else None

        def _set(key: str, value, conf: str | None = None) -> None:
            if not isinstance(common.get(key), dict):
                common[key] = {"value": None, "confidence": "n/a"}
            common[key]["value"] = value
            if value is None:
                common[key]["confidence"] = "n/a"
            elif conf and common[key].get("confidence") not in ("high", "medium", "low"):
                # A derived value (sector from its subsector) lands in a cell
                # holding "n/a" — it inherits the confidence of its evidence.
                common[key]["confidence"] = conf

        sector, subsector = _val("sector"), _val("subsector")
        if isinstance(sector, str) and sector not in taxonomy:
            fixed = _match_one(sector, sectors) or _SECTOR_SYN.get(_canon(sector))
            if not fixed:
                # "Solar" is not a sector — but every subsector that word names
                # lives under one roof, and the roof is the answer.
                fam = {parent_of[s] for s in parent_of if _tokens(sector) <= _tokens(s)}
                fixed = fam.pop() if len(fam) == 1 else None
            if fixed:
                sector = fixed
                _set("sector", sector)
        if isinstance(subsector, str):
            pool = taxonomy[sector] if isinstance(sector, str) and sector in taxonomy \
                else list(parent_of)
            if subsector not in pool:
                fixed = _match_one(subsector, pool)
                if not fixed:
                    syn = _SUBSECTOR_SYN.get(_canon(subsector))
                    fixed = syn if syn in pool else None
                if fixed:
                    subsector = fixed
                    _set("subsector", subsector)
            # The subsector is the more specific claim: it names its parent when
            # the sector is absent — or contradicts it.
            if subsector in parent_of and sector != parent_of[subsector]:
                if isinstance(sector, str) and sector in taxonomy:
                    notes.append(f"sector aligned to subsector "
                                 f"'{subsector}' (was '{sector}')")
                sector = parent_of[subsector]
                sub_cell = common.get("subsector")
                sub_conf = sub_cell.get("confidence") if isinstance(sub_cell, dict) else None
                _set("sector", sector,
                     sub_conf if sub_conf in ("high", "medium", "low") else "medium")
        if isinstance(sector, str) and sector not in taxonomy:
            notes.append(f"sector '{sector}' is outside the locked taxonomy — cleared")
            _set("sector", None)
            sector = None
        if isinstance(subsector, str) and subsector not in taxonomy.get(sector or "", []):
            notes.append(f"subsector '{subsector}' is outside the locked taxonomy — cleared")
            _set("subsector", None)
            subsector = None
            if isinstance(obj.get("subsector_details"), dict):
                obj["subsector_details"] = None  # orphaned with its subsector
        if notes:
            cell = common.get("data_quality_flags")
            if not isinstance(cell, dict):
                cell = common["data_quality_flags"] = {"value": [], "confidence": "n/a"}
            cell["value"] = list(dict.fromkeys([*(cell.get("value") or []), *notes]))

    # -- 5. entity_candidates: plain names ----------------------------------
    if obj.get("entity_candidates") is None:
        obj["entity_candidates"] = []
    cands = obj.get("entity_candidates")
    if isinstance(cands, list) and any(c is None for c in cands):
        cands = obj["entity_candidates"] = [c for c in cands if c is not None]
    if isinstance(cands, list) and any(isinstance(c, dict) for c in cands):
        flat: list = []
        ok = True
        for c in cands:
            if isinstance(c, str):
                flat.append(c)
            elif isinstance(c, dict):
                name = c.get("name") if isinstance(c.get("name"), str) else None
                if name is None:
                    strings = [v for v in c.values() if isinstance(v, str)]
                    name = strings[0] if len(strings) == 1 else None
                if name is None:
                    ok = False
                    break
                flat.append(name)
            else:
                ok = False
                break
        if ok:
            obj["entity_candidates"] = flat

    # -- 6. subsector_details ------------------------------------------------
    details = obj.get("subsector_details")
    subsector = ((common.get("subsector") or {}).get("value")
                 if isinstance(common, dict) and isinstance(common.get("subsector"), dict)
                 else None)
    if isinstance(details, dict) and subsector and set(details.keys()) == {subsector} \
            and isinstance(details[subsector], dict):
        obj["subsector_details"] = details = details[subsector]
    if isinstance(details, dict) and details and subsector:
        canon_fields = {f["key"]: f for f in
                        registry.get("subsector_canonicals", {}).get(subsector, [])}
        if canon_fields:
            by_alias: dict = {}
            for key, f in canon_fields.items():
                by_alias[_canon(key)] = key
                if f.get("label"):
                    by_alias[_canon(f["label"])] = key
            fixed_details: dict = {}
            for k, cell in details.items():
                key = k if k in canon_fields else by_alias.get(_canon(k))
                if key is None:
                    continue  # an invented data point has nowhere to render
                default = "high" if canon_fields[key].get("conf") == "hi" else "medium"
                if not isinstance(cell, dict) or "value" not in cell:
                    cell = {"value": cell, "confidence": default}
                elif "confidence" not in cell:
                    cell = {**cell, "confidence": default}
                conf = cell.get("confidence")
                if isinstance(conf, str) and conf not in ("high", "medium", "low", "n/a"):
                    cell["confidence"] = _CONF_MAP.get(_canon(conf), conf)
                fixed_details[key] = cell
            obj["subsector_details"] = fixed_details
    return obj


def _salvage(obj: dict, registry_version: str | None = None) -> dict | None:
    """The human can fix a field; nobody can fix a dead take.

    Last resort AFTER coercion and the repair round have both lost: force the
    skeleton right, then keep every cell that stands on its own and clear every
    cell that does not — null plus a data-quality flag naming what was heard,
    never an invented value. The reviewer updates or selects what they need on
    the report screen before approving, which is exactly what the flags point
    at. The validator itself is the oracle: each pass clears precisely the
    cells it names, so this never drifts from the contract. Returns a VALID
    report, or None when there is nothing usable to keep (the caller's
    StructuringError — and its retry — still own that case)."""
    registry = load_registry(registry_version)
    obj = _copy.deepcopy(obj)
    ucs = list(registry["use_cases"])
    blocks = registry.get("blocks", {})
    notes: list[str] = []

    # -- the skeleton, forced right ------------------------------------------
    if not isinstance(obj.get("common"), dict):
        obj["common"] = {}
        notes.append("the common block could not be read — cleared for review")
    _fill_block(obj["common"], registry.get("common") or [])

    raw_detected = obj.get("detected_use_cases")
    detected = list(dict.fromkeys(
        u for u in (raw_detected if isinstance(raw_detected, list) else [])
        if isinstance(u, str) and u in ucs))
    if not detected:
        detected = [uc for uc in ucs if isinstance(obj.get(uc), dict) and obj[uc]]
    if not detected:
        # A tag is a filter, not a fact: operations carries no fields, so
        # nothing is fabricated — the reviewer re-files it.
        detected = ["operations"]
        notes.append("use case not determinable — filed under operations for review")
    obj["detected_use_cases"] = detected
    known_top = {"detected_use_cases", "common", "entity_candidates",
                 "subsector_details", *ucs}
    for k in list(obj):
        if k not in known_top:
            del obj[k]
    for uc in ucs:
        fields = (blocks.get(uc) or {}).get("fields") or []
        if uc in detected:
            if not isinstance(obj.get(uc), dict):
                obj[uc] = {}
            if fields:
                _fill_block(obj[uc], fields)
            elif obj[uc]:
                obj[uc] = {}
        elif uc in obj:
            del obj[uc]
    cands = obj.get("entity_candidates")
    obj["entity_candidates"] = [c for c in cands if isinstance(c, str)] \
        if isinstance(cands, list) else []

    # -- the cells, validator-guided -----------------------------------------
    ok = False
    for _ in range(6):
        try:
            validate_report(obj, registry_version)
            ok = True
            break
        except ContractError as exc:
            progress = False
            for err in exc.errors:
                where = err.split(":", 1)[0].strip()
                if where.startswith("subsector_details"):
                    if obj.get("subsector_details") is not None:
                        obj["subsector_details"] = None
                        progress = True
                    continue
                m = _re.match(r"^([a-z_]+)\.([A-Za-z0-9_]+)", where)
                if not m:
                    continue
                blk, key = m.group(1), m.group(2)
                target = obj.get(blk)
                if not isinstance(target, dict):
                    continue
                if "unknown field" in err:
                    target.pop(key, None)
                    progress = True
                    continue
                cell = target.get(key)
                orig = cell.get("value") if isinstance(cell, dict) else cell
                if orig not in (None, "", []):
                    notes.append(f"{blk}.{key} {str(orig)[:80]!r} could not be "
                                 f"structured — cleared for review")
                target[key] = {"value": None, "confidence": "n/a"}
                progress = True
            if not progress:
                return None
    if not ok:
        return None

    flag_cell = obj["common"].get("data_quality_flags")
    if not isinstance(flag_cell, dict) or not isinstance(flag_cell.get("value"), list):
        flag_cell = obj["common"]["data_quality_flags"] = {"value": [], "confidence": "n/a"}
    flag_cell["value"] = list(dict.fromkeys([
        "structuring was salvaged — review the cleared fields before approving",
        *flag_cell["value"], *notes]))
    try:
        return validate_report(obj, registry_version)
    except ContractError:  # pragma: no cover — flags are plain strings
        return None


def _parse_strict(raw: str) -> dict:
    """The model was told: only the JSON object, no fences. Be tolerant of exactly
    one thing (fences it was told not to add), strict about everything else."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuringError(f"model returned non-JSON output: {exc}") from exc
    if not isinstance(obj, dict):
        raise StructuringError("model returned JSON that is not an object")
    return obj


# --------------------------------------------------------------------------
# Focused second pass for lender chases & replies. Proven live, twice: inside
# the full-contract call the main prompt's (correct) anti-hallucination
# pressure wins, and the note-taking model hedges spoken lender events into
# remarks while leaving lender_updates null — even when the prompt says not
# to. A call that asks for exactly one thing carries no such conflict. The
# pass still only extracts what was spoken, its output goes through the same
# folding as the main pass and the full contract validator, and any failure
# leaves the report exactly as it stood.

_LENDER_VERB_RE = _re.compile(
    r"chas(?:e|ed|ing)|follow(?:ed|ing)?[\s-]?up|revert|repli|respond|"
    r"reached out|wrote to|ping(?:ed)?|remind|quer(?:y|ies|ied)|sanction|"
    r"declin|came back|commit", _re.IGNORECASE)

_LENDER_PASS_SYSTEM = (
    'You extract lender chase/reply events from a syndication desk voice note. '
    'A "chase" is the desk reaching out to a lender/bank (chased, followed up, '
    'pinged, wrote to, reminded). A "reply" is a lender responding (reverted, '
    'came back, raised queries, committed to respond, sanctioned, declined). '
    'Return ONLY a JSON array — no prose, no code fences — with one object per '
    'spoken event: {"lender": the bank\'s name as spoken, "kind": "chase" or '
    '"reply", "note": the substance in one or two sentences including any '
    'promised date}. A KNOWN NAMES block may precede the transcript: when a '
    'spoken lender name is clearly an STT mangling of a name there, use the '
    'KNOWN spelling. Record only events actually spoken in the transcript; '
    'return [] when none were.')


def _backfill_lender_updates(report: dict, transcript: str,
                             ask: Callable[[str, str], str],
                             registry_version: str | None,
                             context: str = "") -> dict | None:
    """Returns the validated report with lender_updates filled, or None when
    the pass has nothing to add (the caller keeps the report it has)."""
    synd = report.get("syndication")
    if not isinstance(synd, dict):
        return None                              # no syndication block detected
    cell = synd.get("lender_updates")
    if isinstance(cell, dict) and cell.get("value"):
        return None                              # the main pass delivered
    if not _LENDER_VERB_RE.search(transcript):
        return None                              # nothing chase-shaped was spoken
    raw = ask(_LENDER_PASS_SYSTEM, f"{context}TRANSCRIPT:\n{transcript}").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    items = json.loads(raw)
    if not isinstance(items, list) or not items:
        return None
    fields = load_registry(registry_version)["blocks"]["syndication"]["fields"]
    fdef = next((f for f in fields if f.get("key") == "lender_updates"), None)
    if fdef is None:
        return None
    fresh = {"value": items, "confidence": "medium"}
    _coerce_cell_value(fdef, fresh)              # the same folding as the main pass
    kept = [it for it in (fresh.get("value") or [])
            if isinstance(it, dict) and str(it.get("lender") or "").strip()
            and str(it.get("note") or "").strip()]
    if not kept:
        return None
    previous = synd.get("lender_updates")
    synd["lender_updates"] = {"value": kept, "confidence": "medium"}
    try:
        return validate_report(report, registry_version)
    except ContractError:
        synd["lender_updates"] = previous        # a bonus pass never breaks a take
        return None


# --------------------------------------------------------------------------
# DECOMPOSED structuring for sarvam. The one contract-sized call defeated
# sarvam-105b live, twice: ~12K prompt tokens made it reason past any output
# budget and truncate. The desk's own prototype proved the same model handles
# PROTOTYPE-SIZED asks cleanly — so sarvam mode batches several small calls
# (detect + meeting basics, then one per detected product block), assembles
# the report, and everything downstream is IDENTICAL to the Claude path:
# _normalize, validate_report, _salvage, never-fabricate. Registry-driven,
# so a registry bump flows through with zero code changes.

_SARVAM_RULES = (
    "You extract structured fields from an Indian climate-finance desk "
    "voice-note transcript. NEVER fabricate: anything not spoken gets value "
    "null and confidence \"n/a\" — but classification fields (sector, "
    "subsector, chips) MAY be inferred at medium confidence when the "
    "business discussed plainly implies them (a solar project implies "
    "Renewables). An obvious speech-to-text garble you cannot resolve from "
    "the KNOWN NAMES block never rides into a summary or note — record it "
    "in data_quality_flags as 'transcription artifact: <term>' instead. "
    "The 'Recorded by' person is the narrator — the transcript's 'I'; refer "
    "to them by name alone and never echo the label ('Recorded by', 'the "
    "recorder') into a summary or note. Dates are YYYY-MM-DD; amounts are "
    "plain numbers denominated in crore (50 lakh = 0.5). Return ONLY one "
    "JSON object — no prose, no thinking, no code fences.")


# Field-shaped lessons from the live bake-off, keyed so they never leak onto
# other fields: the dialogue tune leaves these null unless told what the
# desk actually wants in them.
_SARVAM_FIELD_EXTRAS = {
    "deal_size": (" — FILL whenever anything is offered for sale: the rupee/EV "
                  "figure if spoken, otherwise the offered capacity (e.g. "
                  "'26.3 MW operational assets'); null only when no sale was "
                  "discussed"),
    "remarks": (" — a 1-2 sentence analyst note from THIS conversation: gaps, "
                "clarifications needed, follow-ups; null only when nothing "
                "needs noting. NEVER quote a garbled/artifact term here — "
                "those go only in data_quality_flags"),
    "meeting_summary": (" — a colleague's debrief of the business only: never "
                        "mention use cases, fields, extraction or "
                        "transcription artifacts"),
    "offer_notes": (" — FILL whenever a sale was discussed: what is offered, "
                    "the land/expansion picture, PPA and tenors, in the "
                    "seller's substance"),
    # "no evaluative language heard" left the score null on every clean run —
    # but the desk scores DEAL SUBSTANCE, not spoken sentiment, exactly as
    # the Claude path does.
    "requirement_nature": (" — funding to BUILD plants/projects (incl. land) "
                           "is project_finance; term_loan is general or "
                           "asset-backed corporate borrowing; working_capital "
                           "is operating cycle"),
    "requirement_quantum_cr": (" — when a split between the desk's own book "
                               "and a syndicated portion was spoken, this is "
                               "the own-book portion; otherwise the full "
                               "requirement"),
    "deal_size_cr": (" — the amount to syndicate: the syndicated portion "
                     "when a split was spoken, else the full requirement"),
    "asset_location": (" — the location as spoken, even shorthand or a site "
                       "code (e.g. 'AMPY (2.5 into 4 sites)')"),
    # Matches the glossary correction rules that ride in every call's
    # context: location is the venue lane; project/asset places each have
    # their own block field (asset_location, project_location).
    "location": (" — the MEETING VENUE only, as spoken ('met at their "
                 "Whitefield office' -> 'Whitefield'); project, plant and "
                 "asset places go to their own block fields, never here; "
                 "null when the venue was not spoken"),
    # HOWEVER the speaker phrased it, a follow-up must land as a concrete
    # date+time — that pair is what the Add-to-Calendar / Google Meet flow
    # consumes; a null here is a lost meeting.
    "follow_up_date": (" — resolve WHATEVER form was spoken against the "
                       "Capture timestamp: 'tomorrow', 'day after', 'next "
                       "Monday', 'the 16th', '16-09' (Indian day-month) all "
                       "become the concrete YYYY-MM-DD, confidence medium"),
    "follow_up_time": (" — the spoken time as 24-hour HH:MM ('11 am' -> "
                       "'11:00', 'at 4' in business context -> '16:00')"),
    "opportunity_score": (" — SUGGEST from the business substance, not only "
                          "spoken sentiment: 1-2 vague interest or no real "
                          "ask; 3 a concrete, actionable ask (a specific "
                          "asset or requirement with a named counterparty); "
                          "4 concrete plus sizeable/urgent/multiple threads; "
                          "5 exceptional. Null only when the conversation "
                          "offers nothing to judge"),
}


def _field_hint(f: dict) -> str:
    """The registry's own per-field guidance, folded into the brief. The Claude
    prompt renders these notes in full; the sarvam briefs dropping them is why
    the trial run left deal_size empty and invented free-form chips — the model
    was never told what each cell wants. First sentence, capped: guidance, not
    an essay."""
    notes = str(f.get("notes") or "").strip()
    if not notes:
        return ""
    head = notes.split(". ")[0].strip().rstrip(".")
    return f" — {head[:160]}" if head else ""


def _sarvam_field_brief(f: dict, registry: dict) -> str:
    t = f.get("type", "string")
    key = f["key"]
    label = f.get("label") or key
    hint = _field_hint(f) + _SARVAM_FIELD_EXTRAS.get(key, "")
    if f.get("options_from") == "taxonomy.sectors":
        return (f"- {key}: one of {list(registry['taxonomy'])} or null — infer "
                "from the business discussed when plainly implied (a solar "
                "project implies Renewables)")
    if f.get("options_from") == "taxonomy.subsectors_of_selected_sector":
        # Judged by activity, never by the company's name: "Chemenergy
        # Biofuels Limited" building CBG plants filed under Biofuels live —
        # compressed biogas is Biogas, whatever the letterhead says.
        return (f"- {key}: the subsector under the chosen sector, from "
                f"{json.dumps(registry['taxonomy'])}, or null — judge by the "
                "ACTIVITY discussed, never the company's name (a company "
                "named 'X Biofuels' building CBG / compressed biogas plants "
                "is Biogas, not Biofuels). Value-chain roles: OEM "
                "manufactures the equipment; EPC builds projects for "
                "others; Developer owns/operates projects; C&I serves "
                "commercial consumers on their site")
    if t == "enum" and f.get("options"):
        return f"- {key} ({label}): one of {[o['value'] for o in f['options']]} or null{hint}"
    if f.get("item_shape"):
        return f"- {key}: list of objects shaped {json.dumps(f['item_shape'])}{hint}"
    if t == "list" and f.get("closed_set"):
        # e.g. offer_components: the UI renders these tokens as chips — spoken
        # components must land as the canonical token, anything else spoken is
        # appended as free text in the SAME list, never invented.
        return (f"- {key} ({label}): list; use these exact tokens for what was "
                f"spoken: {f['closed_set']} ('selling the entire project / all "
                "assets' means entire_project); append any OTHER spoken "
                f"component as a short free-text string in the same list{hint}")
    if t == "list":
        return f"- {key} ({label}): list of short strings{hint}"
    if t in ("number", "int"):
        rng = ""
        if f.get("min") is not None and f.get("max") is not None:
            rng = f", integer {f['min']}-{f['max']},"
        return f"- {key} ({label}): number{rng} or null{hint}"
    if t == "date":
        return f"- {key} ({label}): date YYYY-MM-DD or null{hint}"
    return f"- {key} ({label}): text or null{hint}"


def _details_briefs(canon: list[dict]) -> str:
    """One brief line per canonical KEY DATA field. Dropdowns offer their
    options (portfolio_stage stayed empty until they were listed); text
    cells insist that a spoken plan WITHOUT numbers still files — 'Three
    CBG plants' was left null live because no capacity figure accompanied
    it, where the desk wants 'Three CBG plants (capacity not specified)'."""
    return "\n".join(
        (f"- {f['key']} ({f.get('label', f['key'])}): one of "
         f"{f['options']} or null" if f.get("options") else
         f"- {f['key']} ({f.get('label', f['key'])}): text or null — carry "
         "every spoken detail (capacities incl. planned, tenors, "
         "counterparties); a spoken plan WITHOUT numbers still fills the "
         "cell (e.g. 'Three CBG plants (capacity not specified)'); null "
         "only when nothing was said about it")
        for f in canon)


def _sarvam_parallelism() -> int:
    """How many sarvam calls may run at once (block calls, digest chunks).
    Default 3 — enough to collapse the wall-clock, gentle on their rate
    limits; SARVAM_MAX_PARALLEL=1 restores strictly sequential calls."""
    try:
        n = int(_os.environ.get("SARVAM_MAX_PARALLEL") or 3)
    except ValueError:
        n = 3
    return max(1, min(n, 8))


# The dialogue tune insists on DISCUSSING transcription artifacts in prose
# ("The transcript references SHIT and DA, which appears to be a
# speech-to-text artifact...") — told twice not to, it rephrased and did it
# again. Prompting lost; this is deterministic now: after validation, any
# sentence of a prose field that talks about the machinery (the transcript,
# artifacts, extraction, use cases) or quotes a flagged artifact term is
# dropped. Sentence-level, so the analyst substance in the same remark
# survives; data_quality_flags (system field) is the artifacts' one home and
# is never touched. Sarvam path only.

_META_SENTENCE_RE = _re.compile(
    r"transcript\s+(?:mentions|references|contains)|speech.to.text|"
    r"transcription\s+artifacts?|known.names\s+block|could\s+not\s+be\s+"
    r"resolved|use.cases?\s+(?:discussed|include)|noted\s+contextually",
    _re.IGNORECASE)


def _scrub_machinery(report: dict, registry_version: str | None) -> None:
    common_cells = report.get("common") or {}
    flags = ((common_cells.get("data_quality_flags") or {}).get("value")
             if isinstance(common_cells.get("data_quality_flags"), dict) else None) or []
    terms: list[str] = []
    for fl in flags:
        m = _re.match(r"\s*transcription artifacts?:\s*(.+)", str(fl), _re.IGNORECASE)
        if m:
            t = m.group(1).strip().strip("'\"").strip()
            if len(t) >= 3:
                terms.append(t.lower())

    def _dirty(sentence: str) -> bool:
        low = sentence.lower()
        return bool(_META_SENTENCE_RE.search(sentence)) or \
            any(t in low for t in terms)

    def _walk(cells: dict, fields: list[dict]) -> None:
        for f in fields:
            if f.get("system"):
                continue                     # flags keep their artifact entries
            cell = cells.get(f["key"])
            if not isinstance(cell, dict):
                continue
            v = cell.get("value")
            if isinstance(v, str) and v.strip():
                kept = [s for s in _re.split(r"(?<=[.!?])\s+", v) if not _dirty(s)]
                cleaned = " ".join(kept).strip()
                if cleaned != v:
                    cell["value"] = cleaned or None
                    if not cleaned:
                        cell["confidence"] = "n/a"
            elif (isinstance(v, list) and not f.get("item_shape")
                  and all(isinstance(x, str) for x in v)):
                kept_l = [x for x in v if not _dirty(x)]
                if kept_l != v:
                    cell["value"] = kept_l
                    if not kept_l:
                        cell["confidence"] = "n/a"

    registry = load_registry(registry_version)
    common_f = (registry["common"] if isinstance(registry["common"], list)
                else registry["common"]["fields"])
    _walk(common_cells, common_f)
    for uc, block in registry["blocks"].items():
        if isinstance(report.get(uc), dict):
            _walk(report[uc], block.get("fields") or [])


# ----------------------------------------------------- the 90-minute wall
# A 3-minute voice note (~2-3K chars) rides into every sarvam call whole. A
# 90-minute live take (~60-90K chars) cannot: sarvam-105b has no long-context
# headroom, and even where it fits, a 20K-token prompt is what drove the model
# into runaway reasoning live. So above a budget the transcript is condensed
# chunk by chunk into dense factual minutes (map), and the combined minutes
# stand in for the transcript in the head call, every block call and the
# lender pass (reduce). The minutes keep names, numbers, dates and lender
# events verbatim — extraction fodder, not a summary.

_SARVAM_DIGEST_SYSTEM = (
    "You condense one segment of an Indian climate-finance desk meeting "
    "transcript into dense factual minutes for a downstream extractor. Keep "
    "EVERY company and person name, every number with its unit, every date "
    "and time, every lender or bank mention with who chased or replied and "
    "what was said, every commitment and follow-up — verbatim where spoken. "
    "Do not interpret, do not drop specifics, do not add anything unspoken. "
    "Return ONLY one JSON object shaped {\"minutes\": [\"...\"]} — one string "
    "per fact, no prose, no thinking, no code fences.")


def _sarvam_transcript_budget() -> tuple[int, int]:
    try:
        budget = int(_os.environ.get("SARVAM_TRANSCRIPT_CHAR_BUDGET") or 14000)
    except ValueError:
        budget = 14000
    try:
        chunk = int(_os.environ.get("SARVAM_DIGEST_CHUNK_CHARS") or 9000)
    except ValueError:
        chunk = 9000
    return max(budget, 1000), max(chunk, 500)


def _split_speech(text: str, chunk_chars: int) -> list[str]:
    """Sentence-boundary chunking: every chunk ends at a sentence break (or,
    failing that, whitespace), so no amount, name or date is cut in half."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if n - i <= chunk_chars:
            out.append(text[i:])
            break
        window = text[i:i + chunk_chars]
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "),
                  window.rfind("\n"))
        if cut < chunk_chars // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = chunk_chars - 1
        out.append(text[i:i + cut + 1])
        i += cut + 1
    return [c.strip() for c in out if c.strip()]


def _sarvam_condense(transcript: str, ask: Callable[[str, str], str],
                     context: str) -> str:
    """Returns the transcript itself when it fits the budget (the 3-minute
    case, byte-identical behavior), else the CONDENSED MINUTES that stand in
    for it. Chunks condense in parallel; order is preserved on assembly. A
    failed chunk fails the take into the runner's retry path — the minutes are
    load-bearing, never best-effort."""
    budget, chunk_chars = _sarvam_transcript_budget()
    if len(transcript) <= budget:
        return transcript
    chunks = _split_speech(transcript, chunk_chars)

    def _one(idx: int, chunk: str) -> list[str]:
        raw = ask(_SARVAM_DIGEST_SYSTEM,
                  f"{context}SEGMENT {idx + 1} of {len(chunks)}:\n{chunk}").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            got = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StructuringError(
                f"sarvam digest call {idx + 1}/{len(chunks)} returned non-JSON: "
                f"{exc}") from exc
        mins = got.get("minutes") if isinstance(got, dict) else None
        if not isinstance(mins, list):
            raise StructuringError(
                f"sarvam digest call {idx + 1}/{len(chunks)} returned no minutes")
        return [str(m).strip() for m in mins if str(m).strip()]

    workers = min(_sarvam_parallelism(), len(chunks))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            per_chunk = list(pool.map(lambda ic: _one(*ic), enumerate(chunks)))
    else:
        per_chunk = [_one(i, c) for i, c in enumerate(chunks)]
    lines = [m for mins in per_chunk for m in mins]
    log.info("sarvam digest: %s chars condensed to %s minutes across %s chunks",
             len(transcript), len(lines), len(chunks))
    return ("CONDENSED MINUTES (machine-condensed, in spoken order, from a "
            "long recording — treat as the transcript):\n- " + "\n- ".join(lines))


def _structure_sarvam(transcript: str, ask: Callable[[str, str], str],
                      registry_version: str | None, context: str,
                      recorder: str | None, capture_ts: str | None) -> dict:
    """The batched sarvam path: returns the ASSEMBLED raw report object; the
    caller normalizes, validates and salvages it exactly like any other."""
    registry = load_registry(registry_version)
    blocks = registry["blocks"]
    common = registry["common"]
    common_f = common if isinstance(common, list) else common["fields"]
    ucs = list(registry["use_cases"])

    def _call(system: str, user: str) -> dict:
        raw = ask(system, user).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            got = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StructuringError(
                f"sarvam decomposed call returned non-JSON: {exc}") from exc
        if not isinstance(got, dict):
            raise StructuringError("sarvam decomposed call returned a non-object")
        return got

    def _wrap_bare(cells: dict, known: set[str]) -> dict:
        """The dialogue tune habitually answers bare values ("sector":
        "Renewables", "deal_size_cr": 5) instead of {value, confidence}
        cells — proven live on the first conversations run, where a bare
        sector crashed the validator and a bare number silently nulled.
        Wrap them at assembly: spoken facts are never lost to a missing
        wrapper. A missing confidence reads medium (n/a when nothing was
        said), and the judgement/system n/a forcing still runs after."""
        out: dict = {}
        for k, cell in cells.items():
            if k not in known:
                continue
            if isinstance(cell, dict) and "value" in cell:
                if "confidence" not in cell:
                    cell = {**cell, "confidence": "medium"}
            else:
                conf = "n/a" if cell in (None, "", []) else "medium"
                out[k] = {"value": cell, "confidence": conf}
                continue
            out[k] = cell
        return out

    # "admin held a call" read wrong in the live summary: an all-lowercase
    # recorder username is sentence-cased before the model ever sees it
    # (chetan malik -> Chetan Malik); a name already carrying capitals
    # passes through untouched. Sarvam path only — Claude's user message
    # stays byte-identical to production.
    nice_recorder = (" ".join(w.capitalize() if w.islower() else w
                              for w in recorder.split())
                     if recorder else recorder)
    by = f"Recorded by: {nice_recorder}\n" if recorder else ""
    user = (f"Capture timestamp: {capture_ts or 'unknown'}\n{by}\n"
            f"{context}TRANSCRIPT:\n{transcript}")

    head_system = (
        f"{_SARVAM_RULES}\n"
        "Shape: {\"detected_use_cases\": [...], \"entity_candidates\": [...], "
        "\"common\": {<key>: {\"value\": ..., \"confidence\": "
        "\"high\"|\"medium\"|\"low\"|\"n/a\"}}}\n"
        f"detected_use_cases: the subset of {ucs} the conversation is actually "
        "about — include one ONLY when that specific ask was spoken (an asset "
        "sale alone is asset_monetisation, not lending). A funding "
        "requirement being taken to syndication is BOTH lending and "
        "syndication: the desk may fund part from its own book and "
        "syndicate the rest. A borrowing ask with no syndication spoken is "
        "lending alone.\n"
        "entity_candidates: the company names the conversation is about.\n"
        "common keys:\n" + "\n".join(_sarvam_field_brief(f, registry)
                                      for f in common_f))
    head = _call(head_system, user)
    detected = [u for u in (head.get("detected_use_cases") or [])
                if isinstance(u, str) and u in ucs]
    if not detected:
        detected = ["operations"]
    known_common = {f["key"] for f in common_f}
    raw_common = head.get("common") if isinstance(head.get("common"), dict) else {}
    obj: dict = {
        "detected_use_cases": detected,
        "common": _wrap_bare(raw_common, known_common),
        "entity_candidates": [c for c in (head.get("entity_candidates") or [])
                              if isinstance(c, str)],
    }
    # Judgement/system fields carry confidence "n/a" by contract — the model
    # habitually stamps them anyway; correct it here, not in a repair round.
    for f in common_f:
        if f.get("judgement") or f.get("system"):
            cell = obj["common"].get(f["key"])
            if isinstance(cell, dict):
                cell["confidence"] = "n/a"

    def _block(uc: str) -> tuple[str, dict | None]:
        fields = (blocks.get(uc) or {}).get("fields") or []
        if not fields:
            return uc, {}
        label = (blocks.get(uc) or {}).get("label") or uc
        block_system = (
            f"{_SARVAM_RULES}\n"
            f"From the transcript, fill the {label} fields. Shape: "
            f"{{\"{uc}\": {{<key>: {{\"value\": ..., \"confidence\": "
            "\"high\"|\"medium\"|\"low\"|\"n/a\"}}}}}}\n"
            "keys:\n" + "\n".join(_sarvam_field_brief(f, registry)
                                   for f in fields))
        got = _call(block_system, user)
        cells = got.get(uc) if isinstance(got.get(uc), dict) else got
        known = {f["key"] for f in fields}
        return uc, (_wrap_bare(cells, known) if isinstance(cells, dict) else {})

    # The per-subsector canonical data points (Solar-Developer KEY DATA etc.)
    # ride inside Claude's one full-contract call; the batched path never
    # asked for them, so the panel's KEY DATA stayed empty live. Once the
    # head has chosen a subsector, one focused call fills them — alongside
    # the block calls, since they are independent.
    sub_cell = obj["common"].get("subsector")
    subsector = sub_cell.get("value") if isinstance(sub_cell, dict) else None
    canon = (registry.get("subsector_canonicals", {}).get(subsector)
             if isinstance(subsector, str) else None) or []

    def _details(_target: str) -> tuple[str, dict | None]:
        # Bonus data, same posture as the lender pass: a failure here logs
        # and skips — it never fails a take that the block calls carried.
        try:
            return "subsector_details", _details_cells()
        except Exception as exc:  # noqa: BLE001
            log.warning("subsector-details call skipped: %s", exc)
            return "subsector_details", None

    def _details_cells() -> dict | None:
        det_system = (
            f"{_SARVAM_RULES}\n"
            f"The company is a {subsector}. Fill its canonical data points "
            "from the transcript. Shape: {\"subsector_details\": "
            f"{{\"{subsector}\": {{<key>: {{\"value\": ..., \"confidence\": "
            "\"high\"|\"medium\"|\"low\"|\"n/a\"}}}}}}}\n"
            "keys:\n" + _details_briefs(canon))
        got = _call(det_system, user)
        inner = (got.get("subsector_details")
                 if isinstance(got.get("subsector_details"), dict) else got)
        cells = (inner.get(subsector)
                 if isinstance(inner, dict) and isinstance(inner.get(subsector), dict)
                 else inner)
        known = {f["key"] for f in canon}
        wrapped = _wrap_bare(cells, known) if isinstance(cells, dict) else {}
        spoken = {k: v for k, v in wrapped.items()
                  if isinstance(v, dict) and v.get("value") not in (None, "", [])}
        return {subsector: spoken} if spoken else None

    def _job(target: str) -> tuple[str, dict | None]:
        return _details(target) if target == "__details__" else _block(target)

    targets = list(detected)
    if subsector and canon:
        targets.append("__details__")

    # The calls are independent — running them concurrently collapses the
    # wall-clock from sum(calls) to max(calls). pool.map preserves order and
    # re-raises the first failure, exactly like the loop it replaces;
    # SARVAM_MAX_PARALLEL=1 restores sequential calls.
    workers = min(_sarvam_parallelism(), len(targets))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_job, targets))
    else:
        results = [_job(t) for t in targets]
    for key, cells in results:
        if cells is not None:
            obj[key] = cells
    return obj


def structure_transcript(
    transcript: str,
    *,
    mode: str,
    ask_model: Callable[[str, str, str], str],
    capture_ts: str | None = None,
    registry_version: str | None = None,
    known_names: str | None = None,
    recorder: str | None = None,
) -> dict[str, Any]:
    """Run the structuring stage. ``ask_model(model, system, user)`` is injected so
    the pipeline is testable without a network and swappable without a rewrite.

    ``known_names`` is the rendered KNOWN NAMES glossary block (see
    pipeline.glossary): runtime context that lets the model repair STT-mangled
    proper nouns. It rides in the user message so the canonical prompt — and
    prompt_version — stay untouched.

    Returns {"report", "prompt_version", "registry_version", "model"}."""
    model = _structure_model(mode)
    system = build_prompt(registry_version)
    context = f"{known_names}\n\n" if known_names else ""
    # The narrator has a name: summaries should read "Ananda H met R. Sharma",
    # not "the BDM met" — the transcript's "I"/"the BDM"/"the RM" is this person.
    by = f"Recorded by: {recorder}\n" if recorder else ""
    user = (f"Capture timestamp: {capture_ts or 'unknown'}\n{by}\n"
            f"{context}TRANSCRIPT:\n{transcript}")

    # The forced-tool-call schema: callables that accept it get the API's own
    # server-side validation (the outer wall); plain callables run text-only.
    import inspect
    schema = build_tool_schema(registry_version)
    params = None
    try:
        params = inspect.signature(ask_model).parameters
    except (TypeError, ValueError):
        params = None
    takes_schema = bool(params) and ("schema" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))

    def _ask(u: str) -> str:
        return ask_model(model, system, u, schema=schema) if takes_schema \
            else ask_model(model, system, u)

    provider = (_os.environ.get("VOCX_STRUCTURE_PROVIDER") or "anthropic").strip().lower()
    # What the extraction calls actually read: the transcript itself, or — on
    # the sarvam path, above the char budget — its condensed minutes. The
    # Claude path never condenses (200K context; production behavior is
    # byte-identical), so this stays `transcript` there.
    work_transcript = transcript
    if provider == "sarvam":
        # The batched path (see _structure_sarvam). No repair round — each
        # small call either parses or fails; salvage still stands behind the
        # validator, exactly as on the Claude path.
        work_transcript = _sarvam_condense(
            transcript, lambda sys2, u2: ask_model(model, sys2, u2), context)
        obj0 = _structure_sarvam(work_transcript,
                                 lambda sys2, u2: ask_model(model, sys2, u2),
                                 registry_version, context, recorder, capture_ts)
        norm = _normalize(obj0, registry_version)
        try:
            report = validate_report(norm, registry_version)
        except ContractError as first:
            report = _salvage(norm, registry_version)
            if report is None:
                detail = "; ".join(first.errors)
                raise StructuringError(
                    f"sarvam decomposed structuring failed: {detail}") from first
        _scrub_machinery(report, registry_version)
    else:
        raw = _ask(user)
        obj1: dict | None = None
        try:
            obj1 = _normalize(_parse_strict(raw), registry_version)
            report = validate_report(obj1, registry_version)
        except (ContractError, StructuringError) as first:
            # One self-repair round: the model sees its own violations, verbatim.
            detail = "; ".join(first.errors) if isinstance(first, ContractError) else str(first)
            repair = (f"{user}\n\nYour previous output violated the contract:\n{detail}\n"
                      f"Return the corrected single JSON object only.")
            raw = _ask(repair)
            obj2: dict | None = None
            try:
                obj2 = _normalize(_parse_strict(raw), registry_version)
                report = validate_report(obj2, registry_version)
            except (ContractError, StructuringError) as second:
                # The take must not die for a field: salvage what stands (repair
                # round first — it saw the violations), flag what was cleared, and
                # let the reviewer update or select the rest. Only output with
                # nothing usable in it (no JSON object at all) still fails here,
                # into the runner's retry path.
                report = None
                for cand in (obj2, obj1):
                    if isinstance(cand, dict):
                        report = _salvage(cand, registry_version)
                        if report is not None:
                            break
                if report is None:
                    detail2 = "; ".join(second.errors) if isinstance(second, ContractError) else str(second)
                    raise StructuringError(f"contract violation after repair round: {detail2}") from second


    # A post-meeting note is recorded when the meeting just happened: if the
    # model still left meeting_date null (nothing spoken, older prompt), the
    # capture date fills it at medium confidence — flagged for a one-tap
    # confirm, never silently invisible to date filters.
    md = (report.get("common") or {}).get("meeting_date") or {}
    if md.get("value") in (None, "") and capture_ts:
        cap_date = str(capture_ts)[:10]
        if len(cap_date) == 10 and cap_date[4] == "-":
            report["common"]["meeting_date"] = {"value": cap_date, "confidence": "medium"}

    # Lender chases & replies: when the main pass left the field empty but the
    # transcript speaks of chasing/replying, ask once more — for that one field
    # only (see _backfill_lender_updates).
    try:
        report = _backfill_lender_updates(
            report, work_transcript, lambda s, u: ask_model(model, s, u),
            registry_version, context=context) or report
    except Exception as exc:  # noqa: BLE001 — a bonus pass never fails the take
        log.warning("lender-updates second pass skipped: %s", exc)

    # Server-side data-quality nudges merge into the model's own flags (deduplicated,
    # order preserved) — flags never block, they steer the review.
    server_flags = compute_data_quality_flags(report, registry_version)
    cell = report["common"].get("data_quality_flags") or {"value": [], "confidence": "n/a"}
    merged = list(dict.fromkeys([*(cell.get("value") or []), *server_flags]))
    report["common"]["data_quality_flags"] = {"value": merged, "confidence": "n/a"}

    return {
        "report": report,
        "prompt_version": latest_prompt_version(),
        "registry_version": registry_version or latest_registry_version(),
        "model": model,
    }
