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


def _normalize(obj: dict, registry_version: str | None = None) -> dict:
    """Deterministic shape aliases — NOT best-effort repair. Each one observed in the
    field, each isomorphic to the contract shape; anything genuinely broken still
    fails validation.

    1. detected_use_cases empty/missing while use-case blocks are present: the
       blocks ARE the declaration — infer the tags from the non-empty blocks, and
       drop any empty stragglers that then declare nothing.
    2. subsector_details nested under the subsector's own name instead of keyed
       flat by canonical field: unwrap."""
    registry = load_registry(registry_version)
    detected = obj.get("detected_use_cases")
    if not (isinstance(detected, list) and detected):
        present = [uc for uc in registry["use_cases"]
                   if isinstance(obj.get(uc), dict) and obj[uc]]
        if present:
            obj["detected_use_cases"] = present
            for uc in registry["use_cases"]:
                if uc not in present and obj.get(uc) == {}:
                    del obj[uc]

    # 3. ENUM VALUES spoken as their labels or everyday synonyms: the transcript
    #    says "seller" and the contract says "owner"; the model echoes the speech and
    #    the strict enum refuses it — twice, because the repair round echoes it too
    #    (the Chikballapur bundle failed structuring exactly this way in the field).
    #    Deterministic, isomorphic coercion only: canonical form of the value, then
    #    the option's label, then a short spoken-synonym table. Anything genuinely
    #    outside the vocabulary still fails validation.
    _SYN = {"party_role": {"seller": "owner", "selling": "owner", "vendor": "owner",
                           "purchaser": "buyer", "acquirer": "buyer", "buying": "buyer"}}

    def _canon(t: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "_", t.strip().lower()).strip("_")

    def _coerce_block(block_obj: dict, fields: list) -> None:
        for fdef in fields:
            if fdef.get("type") != "enum" or not fdef.get("options"):
                continue
            cell = block_obj.get(fdef["key"])
            if not isinstance(cell, dict) or not isinstance(cell.get("value"), str):
                continue
            vals = {o["value"] for o in fdef["options"]}
            v = cell["value"]
            if v in vals:
                continue
            c = _canon(v)
            if c in vals:
                cell["value"] = c
                continue
            by_label = {_canon(o.get("label", "")): o["value"] for o in fdef["options"]}
            if c in by_label:
                cell["value"] = by_label[c]
                continue
            syn = _SYN.get(fdef["key"], {})
            if c in syn:
                cell["value"] = syn[c]

    for uc in registry["use_cases"]:
        if isinstance(obj.get(uc), dict):
            _coerce_block(obj[uc], (registry.get("blocks", {}).get(uc) or {}).get("fields", []))
    if isinstance(obj.get("common"), dict):
        # registry["common"] is the field list itself (blocks nest theirs under "fields").
        _coerce_block(obj["common"], registry.get("common") or [])

    # entity_candidates as objects instead of plain names: take the one string
    # each object unambiguously carries ("name" key, or a single string value).
    cands = obj.get("entity_candidates")
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

    # meeting_summary and follow_up_time were added to the registry after v1
    # shipped; an older model snapshot (or a cached stub) that omits them still
    # satisfies the contract as explicit nulls — additive fields default, they
    # never fail old outputs.
    common0 = obj.get("common")
    if isinstance(common0, dict):
        for added in ("meeting_summary", "follow_up_time"):
            if added not in common0:
                common0[added] = {"value": None, "confidence": "n/a"}

    # Judgement prose fields: the model keeps grading its own judgement ("medium"
    # on competitive_intelligence) or bulleting it as a list — both isomorphic to
    # the contract shape. Confidence coerces to the only legal value; a list of
    # strings joins to the newline-separated prose the UI already stores.
    if isinstance(common0, dict):
        for fdef in registry.get("common", []):
            if not (fdef.get("judgement") or fdef.get("system")):
                continue
            cell = common0.get(fdef["key"])
            if not isinstance(cell, dict):
                continue
            v = cell.get("value")
            if (fdef.get("type") == "string" and isinstance(v, list)
                    and all(isinstance(x, str) for x in v)):
                cell["value"] = "\n".join(x for x in v if x.strip()) or None
            if cell.get("confidence") in ("high", "medium", "low"):
                cell["confidence"] = "n/a"

    details = obj.get("subsector_details")
    common = obj.get("common")
    if isinstance(details, dict) and isinstance(common, dict):
        subsector = ((common.get("subsector") or {}).get("value")
                     if isinstance(common.get("subsector"), dict) else None)
        if subsector and set(details.keys()) == {subsector} \
                and isinstance(details[subsector], dict):
            obj["subsector_details"] = details[subsector]
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
