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
from typing import Any, Callable

from ..spec import (
    ContractError,
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
          "shape; omit the block when subsector is null."
    )


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
) -> dict[str, Any]:
    """Run the structuring stage. ``ask_model(model, system, user)`` is injected so
    the pipeline is testable without a network and swappable without a rewrite.

    Returns {"report", "prompt_version", "registry_version", "model"}."""
    model = MODEL_LIVE if mode == "live" else MODEL_NOTE
    system = build_prompt(registry_version)
    user = (f"Capture timestamp: {capture_ts or 'unknown'}\n\nTRANSCRIPT:\n{transcript}")

    raw = ask_model(model, system, user)
    try:
        report = validate_report(_parse_strict(raw), registry_version)
    except (ContractError, StructuringError) as first:
        # One self-repair round: the model sees its own violations, verbatim.
        detail = "; ".join(first.errors) if isinstance(first, ContractError) else str(first)
        repair = (f"{user}\n\nYour previous output violated the contract:\n{detail}\n"
                  f"Return the corrected single JSON object only.")
        raw = ask_model(model, system, repair)
        try:
            report = validate_report(_parse_strict(raw), registry_version)
        except (ContractError, StructuringError) as second:
            detail2 = "; ".join(second.errors) if isinstance(second, ContractError) else str(second)
            raise StructuringError(f"contract violation after repair round: {detail2}") from second

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
