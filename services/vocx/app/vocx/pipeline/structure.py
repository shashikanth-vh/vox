"""Stage 2 — Claude structuring against the schema contract.

Haiku for post-meeting notes; Sonnet for live-mode transcripts (long-context
quality). Both consume the same canonical prompt with the registry JSON and the
contract appended at runtime. The output is validated BEFORE any database write;
a validation failure earns one self-repair round (the model is shown its own
violations) and then becomes a processing failure with retry — never a partial
write, never a best-effort parse.
"""

from __future__ import annotations

import json
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
          "{\"value\": \"40 MW\", \"confidence\": \"high\"}}}."
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
    r"^(?:(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)|([A-Za-z]+)\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?)[,\s]+(\d{4})$")


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
        mo, y = hits[0], int(m.group(5))
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
                sub_conf = (common.get("subsector") or {}).get("confidence")
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
    model = MODEL_LIVE if mode == "live" else MODEL_NOTE
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

    raw = _ask(user)
    try:
        report = validate_report(_normalize(_parse_strict(raw), registry_version), registry_version)
    except (ContractError, StructuringError) as first:
        # One self-repair round: the model sees its own violations, verbatim.
        detail = "; ".join(first.errors) if isinstance(first, ContractError) else str(first)
        repair = (f"{user}\n\nYour previous output violated the contract:\n{detail}\n"
                  f"Return the corrected single JSON object only.")
        raw = _ask(repair)
        try:
            report = validate_report(_normalize(_parse_strict(raw), registry_version), registry_version)
        except (ContractError, StructuringError) as second:
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
