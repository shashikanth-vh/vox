"""The dynamic schema contract (Build Specification, Section 10).

``validate_report`` is the gate between the model and the database: the exact JSON
object the model must return and the preview must render. Validation failure is a
processing failure with retry — the parser never best-efforts a broken object into
the database, and a partial write never happens.

Contract rules enforced here, verbatim from the spec:
- confidence is a 3-level ordinal high|medium|low; prose-judgement fields carry "n/a";
- absent means absent: a use-case block appears only when detected/tagged, and a
  block that was not detected must not appear;
- missing values are null plus a data-quality flag, never invented;
- party_role = "both" renders owner and buyer blocks together; every AM field
  beyond party_role is optional;
- the opportunity-score override shape: {"value": n, "confidence": "n/a",
  "user_override": true}.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .registry import load_registry

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONFIDENCES = ("high", "medium", "low", "n/a")


class ContractError(ValueError):
    """The model's output violates the schema contract. Carries every violation,
    not just the first, so a retry prompt (or a human) sees the whole picture."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "contract violation")


def _is_date(v: Any) -> bool:
    if isinstance(v, date):
        return True
    if not isinstance(v, str) or not _DATE_RE.match(v):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False


def _field_defs(registry: dict, block: str) -> list[dict]:
    if block == "common":
        return registry["common"]
    return registry["blocks"][block]["fields"]


def _check_value(fdef: dict, value: Any, errors: list[str], where: str) -> None:
    """Type/enum discipline per field. Null is valid everywhere (D3 and friends) —
    typing is only enforced on values that are present."""
    if value is None:
        return
    ftype = fdef.get("type")
    if ftype == "enum":
        allowed = {o["value"] for o in fdef.get("options", [])} if fdef.get("options") else None
        if fdef.get("options_from"):  # sector/subsector — validated separately against the taxonomy
            return
        if not isinstance(value, str):
            errors.append(f"{where}: enum value must be a string, got {type(value).__name__}")
        elif allowed is not None and value not in allowed and value != "not_specified":
            errors.append(f"{where}: {value!r} is not one of {sorted(allowed)} (or 'not_specified')")
    elif ftype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{where}: expected a number, got {type(value).__name__}")
    elif ftype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where}: expected an integer, got {type(value).__name__}")
        else:
            lo, hi = fdef.get("min"), fdef.get("max")
            if lo is not None and value < lo or hi is not None and value > hi:
                errors.append(f"{where}: {value} outside [{lo}, {hi}]")
    elif ftype == "date":
        if not _is_date(value):
            errors.append(f"{where}: expected YYYY-MM-DD, got {value!r}")
    elif ftype == "list":
        if not isinstance(value, list):
            errors.append(f"{where}: expected a list, got {type(value).__name__}")
        elif fdef.get("item_shape"):  # action_items
            for i, item in enumerate(value):
                if not isinstance(item, dict) or "action" not in item:
                    errors.append(f"{where}[{i}]: each item needs at least an 'action'")
                elif item.get("deadline") is not None and not _is_date(item["deadline"]):
                    errors.append(f"{where}[{i}].deadline: expected YYYY-MM-DD or null")
        else:
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    errors.append(f"{where}[{i}]: list items must be strings")
    elif ftype == "string":
        if not isinstance(value, str):
            errors.append(f"{where}: expected a string, got {type(value).__name__}")


def _check_block(registry: dict, block: str, data: Any, errors: list[str]) -> None:
    defs = _field_defs(registry, block)
    if not isinstance(data, dict):
        errors.append(f"{block}: block must be an object")
        return
    keys = {f["key"] for f in defs}
    for extra in set(data) - keys:
        errors.append(f"{block}.{extra}: unknown field (not in registry)")
    for fdef in defs:
        key = fdef["key"]
        where = f"{block}.{key}"
        if key not in data:
            errors.append(f"{where}: missing (absent values are null, not omitted)")
            continue
        cell = data[key]
        if not isinstance(cell, dict) or "value" not in cell or "confidence" not in cell:
            errors.append(f"{where}: every field is {{value, confidence}}")
            continue
        extra_keys = set(cell) - {"value", "confidence", "user_override"}
        if extra_keys:
            errors.append(f"{where}: unexpected keys {sorted(extra_keys)}")
        if "user_override" in cell and key != "opportunity_score":
            errors.append(f"{where}: user_override belongs to opportunity_score only")
        conf = cell["confidence"]
        if conf not in _CONFIDENCES:
            errors.append(f"{where}: confidence {conf!r} not in {_CONFIDENCES}")
            continue
        judgement = bool(fdef.get("judgement") or fdef.get("system"))
        if judgement and conf != "n/a":
            errors.append(f"{where}: judgement fields carry confidence 'n/a'")
        # An empty list or string is semantically absent (the contract example carries
        # buyer_criteria [] with "n/a"), so only a REAL value demands a real confidence.
        absent = cell["value"] is None or cell["value"] == [] or cell["value"] == ""
        if not judgement and not absent and conf == "n/a" and not cell.get("user_override"):
            errors.append(f"{where}: a present model value needs high|medium|low (n/a is for judgement, nulls and overrides)")
        if key == "opportunity_score":
            if "user_override" in cell and not isinstance(cell["user_override"], bool):
                errors.append(f"{where}: user_override must be boolean")
            if cell.get("user_override") and conf != "n/a":
                errors.append(f"{where}: an overridden score carries confidence 'n/a'")
        _check_value(fdef, cell["value"], errors, where)


def _check_taxonomy(registry: dict, common: dict, errors: list[str]) -> None:
    taxonomy = registry["taxonomy"]
    sector = (common.get("sector") or {}).get("value")
    subsector = (common.get("subsector") or {}).get("value")
    if sector is not None and sector not in taxonomy:
        errors.append(f"common.sector: {sector!r} is not one of the six locked sectors")
        return
    if subsector is not None:
        if sector is None:
            errors.append("common.subsector: a subsector needs its parent sector")
        elif subsector not in taxonomy.get(sector, []):
            errors.append(f"common.subsector: {subsector!r} is not under {sector!r}")


def validate_report(obj: Any, registry_version: str | None = None) -> dict:
    """Validate the model's structured report against the contract. Returns the
    object unchanged on success; raises ContractError listing EVERY violation."""
    registry = load_registry(registry_version)
    errors: list[str] = []

    if not isinstance(obj, dict):
        raise ContractError(["the report must be a JSON object"])

    detected = obj.get("detected_use_cases")
    allowed_ucs = set(registry["use_cases"])
    if not isinstance(detected, list) or not detected:
        errors.append("detected_use_cases: at least one use case is required")
        detected = []
    else:
        # type discipline FIRST — a dict in this list once crashed the duplicate
        # check with a raw TypeError instead of a named violation
        for uc in detected:
            if not isinstance(uc, str):
                errors.append("detected_use_cases: entries must be plain use-case "
                              "strings, e.g. \"lending\"")
            elif uc not in allowed_ucs:
                errors.append(f"detected_use_cases: {uc!r} not in the allowed enum")
        detected = [uc for uc in detected if isinstance(uc, str)]
        if len(detected) != len(set(detected)):
            errors.append("detected_use_cases: duplicates")

    known_top = {"detected_use_cases", "common", "entity_candidates",
                 "subsector_details"} | allowed_ucs
    for extra in set(obj) - known_top:
        errors.append(f"{extra}: unknown top-level key")

    if "common" not in obj:
        errors.append("common: block is required on every conversation")
    else:
        _check_block(registry, "common", obj["common"], errors)
        if isinstance(obj["common"], dict):
            _check_taxonomy(registry, obj["common"], errors)

    for uc in allowed_ucs:
        present = uc in obj
        wanted = uc in detected
        has_fields = bool(registry["blocks"][uc]["fields"])
        if present and not wanted:
            errors.append(f"{uc}: block present but the use case was not detected/tagged (absent means absent)")
        elif wanted and has_fields and not present:
            errors.append(f"{uc}: detected but its block is missing")
        elif present and has_fields:
            _check_block(registry, uc, obj[uc], errors)
        elif present and not has_fields and obj[uc] not in ({}, None):
            if not isinstance(obj[uc], dict) or obj[uc]:
                errors.append(f"{uc}: v1 carries the common field set only — block must be empty")

    # The per-subsector canonical data points (9.8) live under "subsector_details" —
    # only when a subsector is chosen, only that subsector's registered keys, and the
    # renderer shows them under Additional details with no code change per subsector.
    details = obj.get("subsector_details")
    if details not in (None, {}):
        subsector = (((obj.get("common") or {}).get("subsector")) or {}).get("value") \
            if isinstance(obj.get("common"), dict) else None
        if not isinstance(details, dict):
            errors.append("subsector_details: must be an object")
        elif subsector is None:
            errors.append("subsector_details: present without a chosen subsector")
        else:
            canon = {f["key"]: f for f in registry["subsector_canonicals"].get(subsector, [])}
            for key, cell in details.items():
                if key not in canon:
                    errors.append(f"subsector_details.{key}: not a canonical data point "
                                  f"of {subsector!r}")
                    continue
                if not isinstance(cell, dict) or "value" not in cell or "confidence" not in cell:
                    errors.append(f"subsector_details.{key}: every field is {{value, confidence}}")
                elif cell["confidence"] not in _CONFIDENCES:
                    errors.append(f"subsector_details.{key}: confidence "
                                  f"{cell['confidence']!r} not in {_CONFIDENCES}")

    cands = obj.get("entity_candidates")
    if cands is None:
        errors.append("entity_candidates: required (may be an empty list)")
    elif not isinstance(cands, list) or any(not isinstance(c, str) for c in cands):
        errors.append("entity_candidates: a flat JSON array of name strings, "
                      "e.g. [\"Suryodaya EPC\", \"SBI\"]")

    if errors:
        raise ContractError(errors)
    return obj


def compute_data_quality_flags(obj: dict, registry_version: str | None = None) -> list[str]:
    """Server-side nudges (never blocks): null numerics in detected blocks and the
    lending-with-no-sector case raise flags for the reviewer."""
    registry = load_registry(registry_version)
    flags: list[str] = []
    detected = obj.get("detected_use_cases") or []
    sector = ((obj.get("common") or {}).get("sector") or {}).get("value")
    if "lending" in detected and sector is None:
        flags.append("sector not determinable")
    for uc in detected:
        block = obj.get(uc)
        if not isinstance(block, dict):
            continue
        for fdef in registry["blocks"][uc]["fields"]:
            if fdef.get("type") == "number":
                cell = block.get(fdef["key"]) or {}
                if cell.get("value") is None:
                    flags.append(f"{fdef['label']} not mentioned")
    return flags


def build_tool_schema(registry_version: str | None = None) -> dict:
    """A JSON Schema mirror of the contract, for a FORCED tool call — the same
    pattern the legacy extraction path has run in production: the API validates the
    model's output against this before we ever see it, so structural drift (cells,
    arrays, enums) cannot reach the validator at all. validate_report stays the
    final authority; this schema is the outer wall."""
    registry = load_registry(registry_version)

    def value_schema(fdef: dict) -> dict:
        ftype = fdef.get("type")
        if ftype == "enum" and fdef.get("options"):
            return {"enum": [o["value"] for o in fdef["options"]] + ["not_specified", None]}
        if ftype == "number":
            return {"type": ["number", "null"]}
        if ftype == "int":
            sch: dict = {"type": ["integer", "null"]}
            if fdef.get("min") is not None:
                sch["minimum"] = fdef["min"]
            if fdef.get("max") is not None:
                sch["maximum"] = fdef["max"]
            return sch
        if ftype == "list":
            if fdef.get("item_shape"):
                return {"type": ["array", "null"], "items": {
                    "type": "object",
                    "properties": {"action": {"type": "string"},
                                   "owner": {"type": ["string", "null"]},
                                   "deadline": {"type": ["string", "null"]}},
                    "required": ["action"]}}
            return {"type": ["array", "null"], "items": {"type": "string"}}
        return {"type": ["string", "null"]}

    def cell(fdef: dict) -> dict:
        props: dict = {"value": value_schema(fdef),
                       "confidence": {"enum": ["high", "medium", "low", "n/a"]}}
        if fdef["key"] == "opportunity_score":
            props["user_override"] = {"type": "boolean"}
        return {"type": "object", "properties": props,
                "required": ["value", "confidence"], "additionalProperties": False}

    def block(defs: list[dict]) -> dict:
        return {"type": "object",
                "properties": {f["key"]: cell(f) for f in defs},
                "required": [f["key"] for f in defs],
                "additionalProperties": False}

    generic_cell = {"type": "object",
                    "properties": {"value": {},
                                   "confidence": {"enum": ["high", "medium", "low", "n/a"]}},
                    "required": ["value", "confidence"], "additionalProperties": False}

    properties: dict = {
        "detected_use_cases": {"type": "array", "minItems": 1,
                               "items": {"enum": registry["use_cases"]}},
        "common": block(registry["common"]),
        "subsector_details": {"type": "object", "additionalProperties": generic_cell},
        "entity_candidates": {"type": "array", "items": {"type": "string"}},
    }
    for uc in registry["use_cases"]:
        fields = registry["blocks"][uc]["fields"]
        properties[uc] = block(fields) if fields else {"type": "object",
                                                       "additionalProperties": False}
    return {"type": "object", "properties": properties,
            "required": ["detected_use_cases", "common", "entity_candidates"],
            "additionalProperties": False}
