"""Separate OpenAI SDK calls for Chitti's model-backed reasoning stages."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

from evam_backend_core.logging import get_logger
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.contracts import (
    AUDIENCE_GROUNDING,
    AUDIENCE_PLANNING,
    PRESENTATION_OPERATORS,
    ROLE_INVARIANT,
    ROLE_SEMANTIC,
)
from app.executor import PlanExecutor
from app.qualitative import AUTHORIZED_QUALITATIVE_FIELDS, validate_qualitative_findings
from app.register_access import (
    CONTROLLED_REFERENCE_FIELDS,
    GOVERNED_COMPOSITE_FIELDS,
    REGISTER_OPAQUE_IDENTIFIER_FIELDS,
    REPORT_UNASSESSABLE_FILTER_FIELDS,
    RESOURCE_FIELDS,
    RESOURCE_SPECS,
    canonical_controlled_value,
    controlled_field_values_match,
)
from app.semantic import load_passages
from app.stage_models import (
    AnswerabilityResult,
    AnswerabilityResultDraft,
    AnswerGenerationResult,
    ConversationInput,
    ConversationResolution,
    ConversationResolutionDraft,
    EvidenceObligation,
    FilterArguments,
    FilterOperation,
    MissingDataRequirement,
    PlannedOperation,
    PreservedIntent,
    QualitativeAnalysisDraft,
    QualitativeAnalysisResult,
    QualitativeCorpus,
    QueryPlan,
    QueryPlanDraft,
    QuestionInterpretation,
    ResultEvidence,
    SemanticRetrievalResult,
    ValueGroundingResult,
    VerifiedFilter,
    VerifiedQuestion,
)

log = get_logger("chitti.model_stages")

_MARKDOWN_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_OMITTED_RESULT_TABLE = "[Host-rendered result table omitted from conversation history.]"
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(slots=True)
class ModelCallUsage:
    repair: bool
    response_received: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    provider: str | None = None
    validation_errors: list[dict[str, Any]] | None = None


class ModelResponseTerminationError(RuntimeError):
    """A legal provider outcome that must not be sent through structured-output repair."""

    def __init__(self, kind: Literal["refusal", "length", "content_filter"], detail: str) -> None:
        self.kind = kind
        super().__init__(detail)


class ExecutableProvenanceError(ValueError):
    """Answerability attempted an executable predicate absent from Grounding."""


def _compact_conversation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for message in messages:
        canonical = dict(message)
        content = canonical.get("content")
        if canonical.get("role") != "assistant" or not isinstance(content, str):
            compacted.append(canonical)
            continue

        lines = content.splitlines()
        output: list[str] = []
        index = 0
        while index < len(lines):
            if not _MARKDOWN_TABLE_LINE.fullmatch(lines[index]):
                output.append(lines[index])
                index += 1
                continue
            end = index + 1
            while end < len(lines) and _MARKDOWN_TABLE_LINE.fullmatch(lines[end]):
                end += 1
            block = lines[index:end]
            if any(_MARKDOWN_TABLE_SEPARATOR.fullmatch(line) for line in block):
                output.append(_OMITTED_RESULT_TABLE)
            else:
                output.extend(block)
            index = end
        canonical["content"] = "\n".join(output)
        compacted.append(canonical)
    return compacted


_usage_sink: ContextVar[list[ModelCallUsage] | None] = ContextVar("chitti_usage_sink", default=None)


def strict_output_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    """Derive the provider's strict JSON-Schema subset from the Pydantic contract."""

    schema = deepcopy(output_type.model_json_schema())
    verified_draft = schema.get("$defs", {}).get("VerifiedQuestionDraft")
    if isinstance(verified_draft, dict):
        properties = verified_draft.get("properties")
        if isinstance(properties, dict):
            properties.pop("missing_data_requirements", None)
        required = verified_draft.get("required")
        if isinstance(required, list):
            verified_draft["required"] = [name for name in required if name != "missing_data_requirements"]

    def close(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                close(item)
            return
        if not isinstance(node, dict):
            return
        if "const" in node:
            node["enum"] = [node.pop("const")]
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["type"] = "object"
            node["additionalProperties"] = False
            node["required"] = list(properties)
        for value in node.values():
            close(value)

    close(schema)
    return schema


def _strict_response_format(stage: str, output_type: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": stage,
            "strict": True,
            "schema": strict_output_schema(output_type),
        },
    }


def _response_provider(response: Any) -> str | None:
    provider = getattr(response, "provider", None)
    if isinstance(provider, str):
        return provider
    extra = getattr(response, "model_extra", None)
    candidate = extra.get("provider") if isinstance(extra, dict) else None
    return candidate if isinstance(candidate, str) else None


def _response_content(response: Any, *, stage: str, repaired: bool) -> str:
    choice = response.choices[0]
    message = choice.message
    refusal = getattr(message, "refusal", None)
    position = "repair" if repaired else "primary"
    if refusal:
        raise ModelResponseTerminationError("refusal", f"{stage} {position} response was refused: {refusal}")
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise ModelResponseTerminationError("length", f"{stage} {position} response was truncated")
    if finish_reason == "content_filter":
        raise ModelResponseTerminationError(
            "content_filter", f"{stage} {position} response was content-filtered"
        )
    content = message.content
    if not content:
        raise ValueError(f"{stage} returned empty {'repaired ' if repaired else ''}model content")
    return content


def _extract_structured_json(content: str) -> str:
    """Return the last complete JSON object from provider-added response framing."""
    stripped = content.strip().lstrip("\ufeff")
    try:
        if isinstance(json.loads(stripped), dict):
            return stripped
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\{", stripped):
        start = match.start()
        try:
            value, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((end, end - start, stripped[start:end]))
    if not candidates:
        return stripped
    # Prefer the object ending latest; for nested objects sharing an end, prefer
    # the widest one. This accepts XML reasoning blocks and Markdown fences while
    # leaving schema validation to Pydantic.
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _ontology_invariants(audience: str) -> list[dict[str, Any]]:
    return [
        {
            "passage_id": passage["id"],
            "source": passage["source"],
            "content": passage["content"],
            "metadata": dict(passage.get("metadata") or {}),
        }
        for passage in load_passages()["passages"]
        if (passage.get("metadata") or {}).get("role") == ROLE_INVARIANT
        and audience in (passage.get("metadata") or {}).get("audience", [])
    ]


def _project_retrieval_matches(
    retrieval: SemanticRetrievalResult, *, audience: str = AUDIENCE_GROUNDING
) -> list[dict[str, Any]]:
    source_matches = (
        retrieval.planning_matches
        if audience == AUDIENCE_PLANNING and retrieval.planning_matches
        else retrieval.matches
    )
    matches = [
        {"passage_id": match.passage_id, "source": match.source, "content": match.content}
        for match in source_matches
        if audience
        in (
            match.metadata.get("audience")
            or ([AUDIENCE_PLANNING] if match.metadata.get("planning") else [AUDIENCE_GROUNDING])
        )
        and match.metadata.get("role", ROLE_SEMANTIC) != ROLE_INVARIANT
    ]
    return [*_ontology_invariants(audience), *matches]


def _reachable_reference_values(values: Any) -> dict[str, list[Any]]:
    if not isinstance(values, dict):
        return {}
    return {
        category: values[category]
        for mapping in CONTROLLED_REFERENCE_FIELDS.values()
        for category in set(mapping.values())
        if category in values and values[category]
    }


def _canonicalize_grounding_controlled_values(
    result: ValueGroundingResult,
    reference_values: dict[str, list[Any]],
) -> list[str]:
    """Rewrite controlled values through one resource-qualified semantic binding."""

    errors: list[str] = []
    meanings = [result.established_meaning]
    if result.issue is not None:
        meanings.extend(alternative.meaning for alternative in result.issue.alternatives)
    for meaning in meanings:
        controlled_bindings: dict[str, list[Any]] = {}
        for binding in meaning.semantic_bindings:
            resource = binding.resource
            field = binding.field
            if resource is None or field is None:
                continue
            if field not in CONTROLLED_REFERENCE_FIELDS.get(resource, {}):
                continue
            controlled_bindings.setdefault(field, []).append(binding)
            normalized: list[Any] = []
            for value in binding.canonical_values:
                canonical = canonical_controlled_value(resource, field, value, reference_values)
                if canonical is None:
                    errors.append(
                        f"Grounding controlled binding {resource}.{field} has unknown or "
                        f"ambiguous canonical value {value!r}."
                    )
                elif canonical not in normalized:
                    normalized.append(canonical)
            binding.canonical_values = normalized
            if binding.binding_kind == "lifecycle" and not normalized:
                errors.append(
                    f"Grounding controlled lifecycle binding {resource}.{field} must have a "
                    "non-empty caller-visible canonical expansion."
                )

        for grounded_value in meaning.grounded_values:
            possible = [
                binding
                for binding in controlled_bindings.get(grounded_value.field, [])
                if grounded_value.resource is None or binding.resource == grounded_value.resource
            ]
            owner_pairs = {(str(binding.resource), str(binding.field)) for binding in possible}
            if len(owner_pairs) > 1:
                proposed = grounded_value.canonical_value
                matching_pairs = {
                    (str(binding.resource), str(binding.field))
                    for binding in possible
                    if any(
                        controlled_field_values_match(
                            str(binding.resource),
                            str(binding.field),
                            value,
                            proposed,
                        )
                        for value in binding.canonical_values
                    )
                }
                if len(matching_pairs) == 1:
                    owner_pairs = matching_pairs
            if len(owner_pairs) != 1:
                potential = {
                    (resource, grounded_value.field)
                    for resource in meaning.resources
                    if grounded_value.field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                }
                if possible or potential:
                    choices = ", ".join(
                        f"{resource}.{field}" for resource, field in sorted(owner_pairs or potential)
                    )
                    errors.append(
                        f"Grounding controlled field '{grounded_value.field}' must have exactly "
                        "one resource-qualified semantic binding. Set grounded_value.resource and "
                        "retain only the binding that owns the user's requested lifecycle or "
                        f"classification. Candidate owners: {choices or 'none'}."
                    )
                continue
            resource, field = next(iter(owner_pairs))
            grounded_value.resource = resource
            original = grounded_value.canonical_value
            canonical = canonical_controlled_value(resource, field, original, reference_values)
            if canonical is None:
                errors.append(
                    f"Grounding controlled field {resource}.{field} must use a unique "
                    "caller-visible canonical value."
                )
                continue
            grounded_value.canonical_value = canonical
            grounded_value.candidate_label = canonical
            grounded_value.exact_match = str(original) == canonical
            grounded_value.normalized_match = str(original) != canonical
            matching_bindings = [
                binding
                for binding in possible
                if (str(binding.resource), str(binding.field)) == (resource, field)
            ]
            if not any(canonical in binding.canonical_values for binding in matching_bindings):
                errors.append(
                    f"Grounding controlled value {resource}.{field}={canonical!r} disagrees "
                    "with its semantic binding canonical_values."
                )
    return errors


def _stable_candidate_value(value: Any) -> Any:
    """Canonicalize question-independent candidate data for byte-stable prefixes."""

    if isinstance(value, dict):
        return {key: _stable_candidate_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [_stable_candidate_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    return value


def _split_grounding_candidates(candidates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    stable_keys = ("people", "counterparties", "reference_values")
    variable_keys = ("entities", "governed_composite_values")
    completeness = candidates.get("completeness")
    stable = {
        key: _stable_candidate_value(candidates.get(key, {} if key == "reference_values" else []))
        for key in stable_keys
    }
    variable = {key: candidates.get(key, []) for key in variable_keys}
    if isinstance(completeness, dict):
        stable["completeness"] = {
            key: completeness[key] for key in ("people", "counterparties") if key in completeness
        }
        variable["completeness"] = {
            key: value for key, value in completeness.items() if key not in {"people", "counterparties"}
        }
    return stable, variable


PROMPTS = {
    "conversation_resolution": (
        "Resolve the current conversation into one standalone Ledger business question. Use "
        "history only to resolve references; never treat a prior answer as current evidence. "
        "Resolution is meaning-preserving: do not add a lifecycle, time, status, book, entity, "
        "inclusion, exclusion, comparison, or aggregation restriction that the user did not state; "
        "do not remove one they did state; do not reverse polarity; and do not merge distinct "
        "entities or cohorts. Preserve requests to keep a cohort separate as separate treatment, "
        "not exclusion. Build intent_ledger with every material entity, scope, temporal/status "
        "qualifier, polarity/inclusion/exclusion, separate cohort, metric, and relationship retained "
        "from user messages. Each entry records the source message index and a concise resolved "
        "phrase. "
        "introduced_constraints must "
        "be empty."
    ),
    "question_interpretation": (
        "Extract only the surface meaning explicitly present in the standalone question. Return "
        "terms with their exact source text, explicit literals and operators, requested answer "
        "shape, grouping/ranking language, time expressions, relationship language, and unresolved "
        "terms. Emit atomic terms, splitting a compound phrase into separate, possibly overlapping "
        "terms when it carries more than one role. Classify explicit business-book, product or "
        "process qualifiers as book_scope; "
        "status, stage, live/active/terminal, ageing or time conditions as lifecycle; categorical "
        "attributes or values used to slice, filter or group as dimension; and other predicates as "
        "filter. Named participants are actors; relationship is the linking predicate rather than "
        "the participant name. Separation, comparison-layout and display instructions are "
        "presentation, never dimension. These are surface retrieval roles only, not canonical "
        "business definitions. Do "
        "not select or name Ledger resources, fields, lifecycle values, business "
        "definitions, default thresholds, null policies, joins, filters, or Register operations. "
        "Do not resolve ambiguity or expand a term using model knowledge. Canonical business "
        "meaning is grounded only after Semantic Retrieval."
        " Set qualitative_analysis=true whenever answering requires interpreting, grouping, or "
        "summarizing free-text reasons, remarks, notes, concerns, explanations, or themes, even "
        "when requested_shape is ranked or list. The flag describes the requested analysis, not a "
        "Ledger resource or field."
    ),
    "value_grounding": (
        "Resolve the complete question into one coherent ontology-supported meaning. Read all "
        "surface terms, qualifiers, compound phrases, relationships, literals, grouping, and "
        "ranking together; do not ground isolated words independently. Construct complete candidate "
        "interpretations from semantic_material and discard any candidate that contradicts an "
        "explicit qualifier, relationship phrase, field meaning, literal, or another material part "
        "of the question, or cannot bind every explicit actor, role, predicate, relationship, and "
        "requested record grain using its proposed resources. A more specific phrase constrains "
        "less-specific words inside it. Treat ontology-stated selector and ambiguity conditions as "
        "binding; do not select a meaning when its required qualifier is absent. Ambiguity "
        "exists only when at least two complete interpretations still explain the entire question. "
        "Return RESOLVED with the one established meaning, or NEEDS_CLARIFICATION with shared "
        "established meaning and exactly one primary issue. When several issues exist, prioritize an "
        "issue that changes the resource, cohort, or metric over a display preference. An AMBIGUOUS "
        "issue contains at least two "
        "complete alternative meanings. A NOT_FOUND issue precisely names the missing definition "
        "and classifies missing_kind as IDENTITY when the client can clarify a live name, or "
        "AUTHORITATIVE_DATA when the governed relationship, definition, or classification is not "
        "held. Do not block on words that do not change the Register snapshot, predicates, metric, "
        "grouping, ranking, or answer. Ground live entity, person, and counterparty terms only from "
        "caller_visible_candidates and question_specific_candidates. Use caller-visible "
        "controlled-value candidates when supplied; "
        "State candidates come from Chitti's explicit local state vocabulary while other controlled "
        "candidates come from caller-visible Register reference categories. Every controlled value "
        "must belong to one resource-qualified semantic binding. Every semantic binding must cite "
        "retrieved passage_ids. Person identity resolves a canonical value, not a relationship. For "
        "informal ownership language, a caller-visible directory role may select a relationship only "
        "when retrieved ontology supports that field on the selected resource and leaves one clear "
        "meaning; explicit relationship wording wins, otherwise clarify. A shared People candidate "
        "source or API filter availability never makes relationship fields interchangeable. Apply "
        "candidate_field_contracts exactly: identifier fields use the "
        "caller-visible candidate id, while stored assignment and lender-name fields use the "
        "candidate's declared canonical name value and retain candidate_id only as identity "
        "provenance. Never substitute an id for a stored name field or a display label for an id "
        "field. Do not invent definitions, "
        "Every grounded controlled value must set its owning resource and have exactly one matching "
        "resource-qualified semantic binding. When ontology assigns a lifecycle term to one resource, "
        "do not also bind that term to another resource merely because it has a same-named field. "
        "When the user explicitly keeps two names separate, never reuse one candidate_id for both; "
        "leave an unmatched name ungrounded instead of collapsing identities. "
        "resources, fields, identifiers, values, thresholds, "
        "or null policies. Canonical resources and fields are not controlled values. Explicit units, "
        "dates, numbers, and comparison operators remain user literals. Caller-visible candidates "
        "establish only current stored identities and canonical values; their collection names, "
        "labels, groupings, or apparent categories never define business meaning, lifecycle families, "
        "metrics, or semantic alternatives and never override semantic_material. Never invent a "
        "stored value. Never emit Register "
        "reads or operations. When the main cohort is supported "
        "but an auxiliary check, field, or classification is not held, keep Grounding RESOLVED and "
        "record it in established_meaning.unavailable_obligations with prevents_main_answer=false. "
        "Use NOT_FOUND only when the unavailable authority prevents the main requested answer. "
        "When retrieved ontology says a "
        "missing related value is unassessable and must be counted, record "
        "details.missing_policy=report_unassessable on that resource/field binding."
    ),
    "answerability": (
        "Decide whether the grounded question is ANSWERABLE, CLARIFICATION_REQUIRED, or "
        "OUT_OF_SCOPE. Ledger mutation requests are OUT_OF_SCOPE. Only ANSWERABLE may emit a "
        "typed verified_question. It is the only canonical question passed to planning. Construct "
        "resources, fields, lifecycle meanings, relationships, defaults, and null policies only "
        "from grounding.established_meaning; do not add a binding or ontology citation directly from "
        "model knowledge. The only permitted ontology passage_ids are those already present in the "
        "established semantic bindings. Preserve grounded live values. Preserve user literals and "
        "their source "
        "text without ontology grounding. Emit explicit evidence_obligations for the answer shape, "
        "metrics, requested row fields, human-readable display evidence, completeness checks, and "
        "material definitions. Every user-provenance evidence obligation must include source_text "
        "copied from the user question. Preserve every list-back field and missing-data disclosure "
        "explicitly established by Grounding. Keep verified_question token-bounded: retain all "
        "grounded_values, filters, relationships, metrics, and material qualifiers, but do not copy "
        "redundant semantic_bindings or emit duplicate evidence obligations. Use the smallest set "
        "of obligations that covers the requested answer and established completeness requirements. "
        "Preserve any missing-value disclosure supplied by Grounding. The host derives typed "
        "missing_data_requirements from governed filters, measures, and qualitative sources; do "
        "not duplicate that plumbing. A filter-level missing_policy copied from Grounding is "
        "accepted and normalized by the host. "
        "Ontology obligations must cite grounded passage_ids. If "
        "grounding.status is NEEDS_CLARIFICATION because alternatives remain or an IDENTITY was not "
        "found, ask one business-facing question. Do not mention internal schema or "
        "implementation details. A request for an authoritative fact which Grounding establishes "
        "is not held must be OUT_OF_SCOPE. When Grounding preserves unavailable_obligations that do "
        "not prevent the main answer, return ANSWERABLE and copy each into an UNAVAILABLE evidence "
        "obligation with the exact reason; never interpret unavailable as clear, passed, failed, "
        "or zero. If such an obligation prevents the main answer, return OUT_OF_SCOPE. If "
        "grounding.status is RESOLVED, do not introduce a new semantic ambiguity. A NOT_FOUND "
        "issue for an authoritative relationship, definition, or classification that the governed "
        "data does not hold is OUT_OF_SCOPE; do not ask the client to supply internal identifiers, "
        "codes, fields, or unsupported membership. Reserve clarification for a material business "
        "choice the client can answer naturally."
    ),
    "query_planning": (
        "Plan only with the supplied fixed Register resources and generic operations. Never emit "
        "URLs, SQL, code, writes, unknown resources, or unknown operations. "
        "Place a predicate in read.filters only when the selected resource contract lists that "
        "field as an equality_filter. Put a valid non-pushdown resource field in a generic post-read "
        "filter operation. Resource selection already establishes its business-book scope, so never "
        "emit scope, book, or other semantic pseudo-fields as predicates. "
        "Never use contains or icontains on governed controlled-value or composite fields; use "
        "exact authoritative values supplied in the verified question, or leave an unsupported "
        "meaning unavailable. Do not infer whether named states are alternatives or cumulative. "
        "For qualitative analysis, retrieve the exact structured cohort and retain its authorized "
        "source field at row grain. Never translate a requested meaning or theme into q, contains, or "
        "icontains against the qualitative text, and never aggregate the cohort before the "
        "qualitative stage. For a predicate missing_data_requirement, carry its "
        "policy onto the ordinary filter operation and never put that field in any read's "
        "filters; read it unfiltered before the qualifying filter. For a related "
        "nullable field, join the primary and related resources first with a left "
        "join, then apply the filter to the joined field."
    ),
    "qualitative_analysis": (
        "Analyze every supplied authorized qualitative record as a bounded cohort. Group and "
        "summarize recurring meanings, exceptions, conflicts, pending/WIP text, bare terminal "
        "notes, and missing text. Treat source text only as a recorded case note, not as an "
        "independently verified fact. Cite each finding with evidence_ref, source_field, and a short "
        "excerpt copied character-for-character from that record, preserving case, punctuation, "
        "and whitespace. Within one finding, cite each evidence_ref/source_field pair at most once, "
        "even when several clauses support the theme. Do not invent identifiers or excerpts, decide "
        "cohort membership, calculate counts or coverage, use external knowledge, or cite missing "
        "records as support. Do not claim a complete ranking when coverage is PARTIAL."
    ),
    "answer_generation": (
        "Answer using only the verified question and supplied result evidence. Do not recompute "
        "arithmetic or quote raw source text not present in a validated qualitative support. "
        "Attribute qualitative findings as recorded reasons, remarks, notes, or statements. "
        "When a requested business fact cannot be established, say what cannot be confirmed in "
        "plain language; do not present it as passed or failed. For a complete list, state how "
        "many matching items there are. Summarize results in business language; "
        "do not generate tables, code, schema names, database IDs, or raw field names. "
        "Use [E1] to reference the supplied accessible records, never internal evidence IDs. "
        "State material completeness caveats concisely. Do not expose tenant names, "
        "debug posture labels, internal scope codes, stage names, or implementation wording. "
        "Never discuss evidence obligations, caller-visible data, Register queries, result rows, "
        "aggregate scalars, or how the answer was computed. A total needs only the total and "
        "its business meaning, plus any material limitation on that fact."
    ),
}


def _filter_values(filter_spec: Any) -> list[Any]:
    values = filter_spec.get("values")
    if isinstance(values, list):
        return values
    value = filter_spec.get("value")
    return [] if value is None else [value]


def _planning_verified_question_payload(verified: VerifiedQuestion) -> dict[str, Any]:
    """Expose host-assigned filter ids only after Answerability has completed."""

    payload = verified.model_dump(mode="json")
    for filter_payload, filter_spec in zip(payload["filters"], verified.filters, strict=True):
        filter_payload["filter_id"] = filter_spec.filter_id
    return payload


def _canonical_filter_operator(value: Any) -> str:
    rendered = str(value or "eq")
    return "eq" if rendered in {"eq", "equals", "="} else rendered


def _filter_shape_matches(
    verified_filter: Any,
    *,
    resources_and_fields: set[tuple[str, str]],
    operator: Any,
    value: Any = None,
    values: Any = None,
) -> bool:
    pair = (
        str(verified_filter.get("resource") or ""),
        str(verified_filter.get("field") or ""),
    )
    if pair not in resources_and_fields:
        return False
    if _canonical_filter_operator(verified_filter.get("operator")) != _canonical_filter_operator(operator):
        return False
    planned_values = values if isinstance(values, list) else ([] if value is None else [value])
    verified_values = _filter_values(verified_filter)
    if len(verified_values) != len(planned_values):
        return False
    resource, field = pair
    if field in CONTROLLED_REFERENCE_FIELDS.get(resource, {}):
        unmatched = list(planned_values)
        for canonical in verified_values:
            match = next(
                (
                    candidate
                    for candidate in unmatched
                    if controlled_field_values_match(resource, field, canonical, candidate)
                ),
                None,
            )
            if match is None:
                return False
            unmatched.remove(match)
        return not unmatched
    return verified_values == planned_values


def _validate_filter_correspondence(plan: QueryPlan, verified: VerifiedQuestion) -> None:
    """Require complete and truthful references to host-assigned verified filters."""

    expected = {
        item.filter_id: item
        for item in verified.filters
        if item.get("operator") not in PRESENTATION_OPERATORS
    }
    referenced: set[str] = set()

    def accept_references(ids: list[str], matches: list[str], location: str) -> list[str]:
        effective = ids or matches
        if not ids:
            ids.extend(matches)
        for filter_id in effective:
            if filter_id not in expected:
                raise ValueError(f"{location} references unknown verified filter id '{filter_id}'")
            if filter_id not in matches:
                raise ValueError(
                    f"{location} claims verified filter id '{filter_id}' but implements a "
                    "different resource, field, operator, or value"
                )
            referenced.add(filter_id)
        return effective

    for read in plan.reads:
        for item in read.filters.root:
            matches = [
                filter_id
                for filter_id, verified_filter in expected.items()
                if _filter_shape_matches(
                    verified_filter,
                    resources_and_fields={(item.resource, item.field)},
                    operator="eq",
                    value=item.value,
                )
            ]
            accepted = accept_references(
                item.verified_filter_ids,
                matches,
                f"Read '{read.name}' filter {item.resource}.{item.field}",
            )
            if len(accepted) == 1 and item.field in CONTROLLED_REFERENCE_FIELDS.get(item.resource, {}):
                canonical = _filter_values(expected[accepted[0]])
                if len(canonical) == 1:
                    item.value = canonical[0]

    origins = _plan_field_origins(plan)
    for step in plan.operations:
        if step.operation != "filter":
            continue
        field = str(step.arguments.get("field") or "")
        field_origins = origins.get(step.input, {}).get(field, set())
        matches = [
            filter_id
            for filter_id, verified_filter in expected.items()
            if _filter_shape_matches(
                verified_filter,
                resources_and_fields=field_origins,
                operator=step.arguments.get("operator"),
                value=step.arguments.get("value"),
                values=step.arguments.get("values"),
            )
        ]
        accepted = accept_references(
            step.arguments.verified_filter_ids,
            matches,
            f"Operation '{step.name}'",
        )
        if len(accepted) == 1:
            verified_filter = expected[accepted[0]]
            pair = (
                str(verified_filter.get("resource") or ""),
                str(verified_filter.get("field") or ""),
            )
            if pair[1] in CONTROLLED_REFERENCE_FIELDS.get(pair[0], {}):
                _assign_filter_values(step.arguments, _filter_values(verified_filter))

    missing = sorted(set(expected) - referenced)
    if missing:
        raise ValueError("Query plan omitted verified filter id(s): " + ", ".join(missing))


def _governed_filter_value_matches(
    resource: str,
    field: str,
    established: Any,
    requested: Any,
) -> bool:
    if field in CONTROLLED_REFERENCE_FIELDS.get(resource, {}):
        return controlled_field_values_match(resource, field, established, requested)
    return str(established) == str(requested)


def _matching_grounded_canonical_values(
    resource: str,
    field: str,
    requested: Any,
    grounded_values: list[Any],
    *,
    require_candidate_id: bool,
    semantic_bindings: list[Any] | None = None,
) -> list[Any]:
    matches: list[Any] = []
    for item in grounded_values:
        controlled_owner_matches = {
            (str(binding.resource), str(binding.field))
            for binding in semantic_bindings or []
            if binding.field == field
            and item.canonical_value in binding.canonical_values
            and binding.resource is not None
            and field in CONTROLLED_REFERENCE_FIELDS.get(str(binding.resource), {})
        }
        if (
            item.field != field
            or (item.resource is not None and item.resource != resource)
            or item.canonical_value is None
            or (require_candidate_id and item.candidate_id is None)
            or (
                field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                and controlled_owner_matches != {(resource, field)}
            )
            or not _governed_filter_value_matches(
                resource,
                field,
                item.canonical_value,
                requested,
            )
        ):
            continue
        if not any(
            type(existing) is type(item.canonical_value) and existing == item.canonical_value
            for existing in matches
        ):
            matches.append(item.canonical_value)
    # A resource-qualified semantic binding is the authoritative carrier for an
    # ontology-defined value family. Grounding has already canonicalized every
    # member against the request vocabulary, so requiring a duplicate GroundedValue
    # for every member loses valid provenance without adding a safety boundary.
    if field not in CONTROLLED_REFERENCE_FIELDS.get(resource, {}):
        return matches
    for binding in semantic_bindings or []:
        if binding.resource != resource or binding.field != field:
            continue
        for canonical in binding.canonical_values:
            if not _governed_filter_value_matches(resource, field, canonical, requested):
                continue
            if not any(type(existing) is type(canonical) and existing == canonical for existing in matches):
                matches.append(canonical)
    return matches


def _ambiguous_filter_provenance_error(
    resource: str,
    field: str,
    requested: Any,
    candidates: list[Any],
) -> str:
    rendered = ", ".join(sorted(str(candidate) for candidate in candidates))
    return (
        f"{resource}.{field} model value {requested!r} ambiguously matches grounded "
        f"canonical candidates [{rendered}]"
    )


def _assign_filter_values(filter_spec: Any, values: list[Any]) -> None:
    if isinstance(filter_spec.get("values"), list):
        filter_spec["values"] = values
    elif values:
        filter_spec["value"] = values[0]


def _resolve_dataset(name: str, replacements: dict[str, str]) -> str:
    seen: set[str] = set()
    while name in replacements and name not in seen:
        seen.add(name)
        name = replacements[name]
    return name


def _rewrite_dataset_references(
    operations: list[PlannedOperation],
    result_names: list[str],
    replacements: dict[str, str],
) -> None:
    for step in operations:
        step.input = _resolve_dataset(step.input, replacements)
        if step.operation == "join" and isinstance(step.arguments.get("right"), str):
            step.arguments["right"] = _resolve_dataset(step.arguments["right"], replacements)
        if step.operation in {"set_union", "set_intersection"}:
            step.arguments["others"] = [
                _resolve_dataset(str(name), replacements) for name in step.arguments.get("others") or []
            ]
    for index, name in enumerate(result_names):
        result_names[index] = _resolve_dataset(name, replacements)


def _plan_field_origins(
    plan: QueryPlan,
) -> dict[str, dict[str, set[tuple[str, str]]]]:
    """Trace resource fields through the plan without applying business semantics."""

    datasets = {
        read.name: {
            field: {(read.resource, field)} for field in RESOURCE_FIELDS.get(read.resource, frozenset())
        }
        for read in plan.reads
    }
    for step in plan.operations:
        input_origins = datasets.get(step.input, {})
        if step.operation in {"filter", "sort", "limit", "rank"}:
            output = {field: set(origins) for field, origins in input_origins.items()}
        elif step.operation == "project":
            output = {
                str(field): set(input_origins.get(str(field), set()))
                for field in step.arguments.get("fields") or []
            }
        elif step.operation == "join":
            output = {field: set(origins) for field, origins in input_origins.items()}
            right = datasets.get(str(step.arguments.get("right") or ""), {})
            prefix = str(step.arguments.get("right_prefix") or "right_")
            for field, origins in right.items():
                output[f"{prefix}{field}"] = set(origins)
        elif step.operation in {"set_union", "set_intersection"}:
            output = {}
            for field in step.arguments.get("key_fields") or []:
                field_name = str(field)
                origins = set(input_origins.get(field_name, set()))
                for other in step.arguments.get("others") or []:
                    origins.update(datasets.get(str(other), {}).get(field_name, set()))
                output[field_name] = origins
        elif step.operation == "group":
            output = {
                str(field): set(input_origins.get(str(field), set()))
                for field in step.arguments.get("by") or []
            }
        else:
            output = {}
        datasets[step.output] = output
    return datasets


def _validate_visible_list_results(plan: QueryPlan, answer_shape: str) -> None:
    """Require list results to retain at least one reader-visible column."""

    if answer_shape != "list":
        return
    origins = _plan_field_origins(plan)
    empty_results: list[str] = []
    for result_name in plan.result_names:
        if plan.result_shapes.get(result_name) == "scalar":
            continue
        fields = origins.get(result_name, {})
        if not any(
            not field_origins
            or not any(origin in REGISTER_OPAQUE_IDENTIFIER_FIELDS for origin in field_origins)
            for field_origins in fields.values()
        ):
            empty_results.append(result_name)
    if empty_results:
        raise ValueError(
            "List result dataset(s) have no reader-visible columns after opaque Register "
            "identifiers are removed: "
            + ", ".join(empty_results)
            + ". Preserve a governed business identifier or display field in each final row."
        )


def _refresh_result_shapes(plan: QueryPlan) -> None:
    refreshed = QueryPlan.model_validate(plan.model_dump(mode="json"))
    plan.result_shapes = refreshed.result_shapes


def _normalize_qualitative_plan(
    plan: QueryPlan,
    qualitative_fields: set[tuple[str, str]],
) -> None:
    """Remove model-added text selection and terminal aggregation from a typed cohort."""

    if not qualitative_fields:
        return
    if any(
        field not in AUTHORIZED_QUALITATIVE_FIELDS.get(resource, frozenset())
        for resource, field in qualitative_fields
    ):
        return

    qualitative_resources = {resource for resource, _ in qualitative_fields}
    for read in plan.reads:
        if read.resource in qualitative_resources:
            read.q = None

    origins = _plan_field_origins(plan)
    replacements: dict[str, str] = {}
    retained: list[PlannedOperation] = []
    for step in plan.operations:
        step.input = _resolve_dataset(step.input, replacements)
        if step.operation == "join" and isinstance(step.arguments.get("right"), str):
            step.arguments["right"] = _resolve_dataset(step.arguments["right"], replacements)
        field = str(step.arguments.get("field") or "")
        field_origins = origins.get(step.input, {}).get(field, set())
        if step.operation == "filter" and field_origins & qualitative_fields:
            replacements[step.output] = step.input
            continue
        retained.append(step)
    plan.operations = retained
    _rewrite_dataset_references(plan.operations, plan.result_names, replacements)

    aggregate_operations = {
        "count",
        "sum",
        "distinct_count",
        "average",
        "min",
        "max",
        "missing_count",
        "group",
    }
    producers = {step.output: step for step in plan.operations}
    consumed = (
        {step.input for step in plan.operations}
        | {
            str(step.arguments.get("right"))
            for step in plan.operations
            if step.operation == "join" and step.arguments.get("right") is not None
        }
        | {
            str(name)
            for step in plan.operations
            if step.operation in {"set_union", "set_intersection"}
            for name in step.arguments.get("others") or []
        }
    )
    terminal_replacements = {
        name: producers[name].input
        for name in plan.result_names
        if name in producers and name not in consumed and producers[name].operation in aggregate_operations
    }
    if terminal_replacements:
        plan.operations = [step for step in plan.operations if step.output not in terminal_replacements]
        _rewrite_dataset_references(plan.operations, plan.result_names, terminal_replacements)
    _refresh_result_shapes(plan)


_READ_RESULT_REFERENCE = re.compile(r"^\$([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)$")


def _normalize_non_pushdown_read_filters(
    plan: QueryPlan,
    verified: VerifiedQuestion,
) -> None:
    """Lower only provenance-checked exact read filters into local operations."""

    dependent_reads = {
        match.group(1)
        for read in plan.reads
        for value in read.filters.values()
        if isinstance(value, str) and (match := _READ_RESULT_REFERENCE.fullmatch(value))
    }
    existing_names = {
        *(read.name for read in plan.reads),
        *(step.output for step in plan.operations),
    }
    inserted: list[PlannedOperation] = []
    replacements: dict[str, str] = {}
    for read in plan.reads:
        spec = RESOURCE_SPECS.get(read.resource)
        resource_fields = RESOURCE_FIELDS.get(read.resource, frozenset())
        if spec is None:
            continue
        unsupported = [field for field in read.filters if field not in spec.equality_filters]
        current_input = read.name
        for field in unsupported:
            if field not in resource_fields:
                continue
            value = read.filters[field]
            pair = (read.resource, field)
            controlled = field in CONTROLLED_REFERENCE_FIELDS.get(read.resource, {})
            composite = pair in GOVERNED_COMPOSITE_FIELDS
            if controlled or composite:
                canonical_values = _matching_grounded_canonical_values(
                    read.resource,
                    field,
                    value,
                    verified.grounded_values,
                    require_candidate_id=composite,
                    semantic_bindings=verified.semantic_bindings,
                )
                if not canonical_values:
                    raise ValueError(
                        f"Cannot relocate {read.resource}.{field}={value}; governed value lacks "
                        "host-validated executable provenance."
                    )
                if len(canonical_values) > 1:
                    raise ValueError(
                        "Cannot relocate ambiguous executable filter provenance: "
                        + _ambiguous_filter_provenance_error(
                            read.resource,
                            field,
                            value,
                            canonical_values,
                        )
                    )
                value = canonical_values[0]
                read.filters[field] = value
            matching_verified = [
                item
                for item in verified.filters
                if item.get("resource") == read.resource
                and item.get("field") == field
                and str(item.get("operator") or "equals") in {"eq", "equals", "="}
                and _filter_values(item) == [value]
            ]
            if field in AUTHORIZED_QUALITATIVE_FIELDS.get(read.resource, frozenset()):
                raise ValueError(
                    f"Cannot relocate qualitative predicate {read.resource}.{field}={value}; "
                    "qualitative source fields require an evidence obligation."
                )
            if not matching_verified:
                raise ValueError(
                    f"Cannot losslessly relocate {read.resource}.{field}={value}; no matching "
                    "verified exact-equality predicate exists."
                )
            if read.name in dependent_reads:
                raise ValueError(
                    f"Cannot relocate {read.resource}.{field}={value}; read '{read.name}' supplies "
                    "a dependent Register read before local operations execute."
                )
            del read.filters[field]
            base = f"host_{read.name}_{field}"
            output = base
            suffix = 2
            while output in existing_names:
                output = f"{base}_{suffix}"
                suffix += 1
            existing_names.add(output)
            inserted.append(
                FilterOperation(
                    name=output,
                    operation="filter",
                    input=current_input,
                    output=output,
                    arguments=FilterArguments(field=field, operator="eq", value=value),
                )
            )
            current_input = output
        if current_input != read.name:
            replacements[read.name] = current_input

    if not inserted:
        return
    _rewrite_dataset_references(plan.operations, plan.result_names, replacements)
    plan.operations = [*inserted, *plan.operations]
    _refresh_result_shapes(plan)


class ModelStages:
    def __init__(self, client: AsyncOpenAI, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def _json_call(
        self,
        *,
        stage: str,
        model: str,
        payload: BaseModel | dict[str, Any],
        output_type: type[OutputT],
        validator: Callable[[OutputT], None] | None = None,
        repair_context: dict[str, Any] | None = None,
    ) -> OutputT:
        serialized = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        request_body = {"input": serialized}
        response_format = _strict_response_format(stage, output_type)
        messages = cast(
            list[ChatCompletionMessageParam],
            [
                {
                    "role": "system",
                    "content": (
                        f"{PROMPTS[stage]} Return only the JSON object required by the supplied "
                        "strict response schema."
                    ),
                },
                {"role": "user", "content": json.dumps(request_body, ensure_ascii=False)},
            ],
        )
        log.info(
            "pipeline_model_request",
            extra={"stage": stage, "model": model,
                   **({"prompt": messages} if self.settings.log_pipeline else {})},
        )
        attempt = ModelCallUsage(repair=False)
        sink = _usage_sink.get()
        if sink is not None:
            sink.append(attempt)
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=cast(Any, response_format),
            max_completion_tokens=self.settings.llm_max_completion_tokens,
            extra_body={"reasoning": {"enabled": True}},
        )
        attempt.response_received = True
        attempt.provider = _response_provider(response)
        usage = getattr(response, "usage", None)
        if usage is not None:
            attempt.prompt_tokens = getattr(usage, "prompt_tokens", None)
            attempt.completion_tokens = getattr(usage, "completion_tokens", None)
            attempt.total_tokens = getattr(usage, "total_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            attempt.cached_prompt_tokens = getattr(details, "cached_tokens", None) if details else None
        raw_content = _response_content(response, stage=stage, repaired=False)
        content = _extract_structured_json(raw_content)
        if content != raw_content.strip().lstrip("\ufeff"):
            log.info(
                "pipeline_model_output_postprocessed",
                extra={"stage": stage, "model": model, "repair": False},
            )

        def validate(value: str) -> OutputT:
            parsed = output_type.model_validate_json(value)
            if validator is not None:
                validator(parsed)
            return parsed

        try:
            result = validate(content)
        except ValueError as exc:
            validation_errors = (
                json.loads(exc.json(include_url=False, include_input=False))
                if isinstance(exc, ValidationError)
                else [{"type": "value_error", "msg": str(exc)}]
            )
            attempt.validation_errors = validation_errors
            log.warning(
                "pipeline_model_output_invalid",
                extra={"stage": stage, "model": model, "errors": validation_errors},
            )
            repair_messages = cast(
                list[ChatCompletionMessageParam],
                [
                    {
                        "role": "system",
                        "content": (
                            "Repair the supplied JSON so it matches the supplied strict response schema. "
                            "Preserve its meaning and values. Change only what the validation errors "
                            "require. Use repair_context only to recover values already supplied in "
                            "the original stage input and to select canonical resource ids, owning "
                            "fields, stored candidate values, and ontology-supported relationships. "
                            "When repair_context supplies allowed_passage_ids, every passage_ids "
                            "entry must be copied exactly from that list; repair-context object "
                            "keys are not passage ids. "
                            "Do not introduce "
                            "or remove semantic ambiguity unless a validation error explicitly "
                            "requires it. Return only one JSON object."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "invalid_output": content,
                                "validation_errors": validation_errors,
                                **({"repair_context": repair_context} if repair_context is not None else {}),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            )
            repair_attempt = ModelCallUsage(repair=True)
            if sink is not None:
                sink.append(repair_attempt)
            repair_response = await self.client.chat.completions.create(
                model=model,
                messages=repair_messages,
                response_format=cast(Any, response_format),
                max_completion_tokens=self.settings.llm_max_completion_tokens,
                extra_body={"reasoning": {"enabled": True}},
            )
            repair_attempt.response_received = True
            repair_attempt.provider = _response_provider(repair_response)
            usage = getattr(repair_response, "usage", None)
            if usage is not None:
                repair_attempt.prompt_tokens = getattr(usage, "prompt_tokens", None)
                repair_attempt.completion_tokens = getattr(usage, "completion_tokens", None)
                repair_attempt.total_tokens = getattr(usage, "total_tokens", None)
                details = getattr(usage, "prompt_tokens_details", None)
                repair_attempt.cached_prompt_tokens = (
                    getattr(details, "cached_tokens", None) if details else None
                )
            raw_repaired_content = _response_content(repair_response, stage=stage, repaired=True)
            repaired_content = _extract_structured_json(raw_repaired_content)
            if repaired_content != raw_repaired_content.strip().lstrip("\ufeff"):
                log.info(
                    "pipeline_model_output_postprocessed",
                    extra={"stage": stage, "model": model, "repair": True},
                )
            try:
                result = validate(repaired_content)
            except ValueError as repair_exc:
                repair_attempt.validation_errors = (
                    json.loads(repair_exc.json(include_url=False, include_input=False))
                    if isinstance(repair_exc, ValidationError)
                    else [{"type": "value_error", "msg": str(repair_exc)}]
                )
                raise
        log.info(
            "pipeline_model_response",
            extra={"stage": stage, "model": model,
                   **({"response": result.model_dump(mode="json")} if self.settings.log_pipeline else {})},
        )
        return result

    async def resolve_conversation(self, value: ConversationInput) -> ConversationResolution:
        conversation_input = value.model_copy(
            update={"messages": _compact_conversation_messages(value.messages)}
        )

        def validate_resolution(result: ConversationResolutionDraft) -> None:
            if not result.intent_ledger:
                raise ValueError("Conversation Resolution requires a non-empty intent ledger.")
            if result.introduced_constraints:
                raise ValueError(
                    "Conversation Resolution introduced constraints: "
                    + ", ".join(result.introduced_constraints)
                )
            latest_user_index = max(
                (
                    index
                    for index, message in enumerate(conversation_input.messages)
                    if message.get("role") == "user"
                ),
                default=None,
            )
            ledger_indexes: set[int] = set()
            for intent in result.intent_ledger:
                if intent.source_message_index >= len(conversation_input.messages):
                    raise ValueError("Conversation intent source_message_index is outside the conversation.")
                source_message = conversation_input.messages[intent.source_message_index]
                if source_message.get("role") != "user":
                    raise ValueError("Conversation intent must cite a user message.")
                ledger_indexes.add(intent.source_message_index)
            if latest_user_index is not None and latest_user_index not in ledger_indexes:
                raise ValueError("Conversation intent ledger does not cover the current user turn.")

        draft = await self._json_call(
            stage="conversation_resolution",
            model=self.settings.conversation_model,
            payload=conversation_input,
            output_type=ConversationResolutionDraft,
            validator=validate_resolution,
            repair_context={"conversation_input": conversation_input.model_dump(mode="json")},
        )
        latest_user_index = max(
            (
                index
                for index, message in enumerate(conversation_input.messages)
                if message.get("role") == "user"
            ),
            default=None,
        )
        ledger = [
            PreservedIntent(
                **intent.model_dump(),
            )
            for intent in draft.intent_ledger
        ]
        return ConversationResolution(
            standalone_question=draft.standalone_question,
            intent_ledger=ledger,
            introduced_constraints=draft.introduced_constraints,
            used_prior_context=any(
                latest_user_index is not None and intent.source_message_index < latest_user_index
                for intent in ledger
            ),
        )

    async def interpret(
        self,
        resolution: ConversationResolution,
    ) -> QuestionInterpretation:
        return await self._json_call(
            stage="question_interpretation",
            model=self.settings.interpretation_model,
            payload={
                "instruction": (
                    "Preserve the user's wording. Later stages alone may use the ontology and "
                    "Register candidates to canonicalize it."
                ),
                "resolution": resolution.model_dump(),
            },
            output_type=QuestionInterpretation,
        )

    async def ground(
        self,
        interpretation: QuestionInterpretation,
        retrieval: SemanticRetrievalResult,
        candidates: dict[str, Any],
    ) -> ValueGroundingResult:
        passage_ids = {
            match["passage_id"]
            for match in _project_retrieval_matches(retrieval, audience=AUDIENCE_GROUNDING)
        }
        candidate_field_contracts = {
            "entity_id": ("entities", "id"),
            "company": ("entities", "legal_name"),
            "legal_name": ("entities", "legal_name"),
            "rm": ("people", "name"),
            "analyst": ("people", "name"),
            "counterparty_id": ("counterparties", "id"),
            "lender_name": ("counterparties", "name"),
        }
        candidate_records = {
            collection: {
                str(candidate["id"]): candidate
                for candidate in candidates.get(collection, [])
                if isinstance(candidate, dict) and candidate.get("id") is not None
            }
            for collection, _ in candidate_field_contracts.values()
        }
        composite_candidates = {
            str(candidate["id"]): candidate
            for candidate in candidates.get("governed_composite_values", [])
            if isinstance(candidate, dict)
            and candidate.get("id") is not None
            and candidate.get("resource") is not None
            and candidate.get("field") is not None
            and candidate.get("value") is not None
        }

        reference_candidates = candidates.get("reference_values", {})
        if not isinstance(reference_candidates, dict):
            reference_candidates = {}

        def term_tokens(value: Any) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z0-9]+", str(value).casefold())
                if token not in {"a", "an", "and", "for", "of", "the", "to"}
            }

        def candidate_matches_term(candidate: dict[str, Any], user_term: str) -> bool:
            def normalize(value: Any) -> str:
                return "".join(character for character in str(value).casefold() if character.isalnum())

            term = normalize(user_term)
            names = {
                normalize(candidate.get(field))
                for field in ("name", "full_name", "legal_name", "display_name", "code", "short_name")
                if candidate.get(field)
            }
            return bool(term) and any(term in name or name in term for name in names)

        def validate_grounding(result: ValueGroundingResult) -> None:
            meanings = [result.established_meaning]
            if result.issue is not None:
                meanings.extend(alternative.meaning for alternative in result.issue.alternatives)
            errors = _canonicalize_grounding_controlled_values(result, reference_candidates)
            issue = result.issue
            if issue is not None and issue.kind == "NOT_FOUND" and issue.missing_kind == "AUTHORITATIVE_DATA":
                normalized_issue = " ".join(issue.term.casefold().split())
                issue_targets_relationship = any(
                    " ".join(term.casefold().split()) in normalized_issue
                    for term in interpretation.relationship_terms
                    if term.strip()
                )
                issue_tokens = set(
                    re.findall(
                        r"[a-z0-9]+",
                        f"{issue.term} {issue.missing_definition or ''}".casefold(),
                    )
                )
                for grounded_value in result.established_meaning.grounded_values:
                    value_tokens = set(re.findall(r"[a-z0-9]+", grounded_value.user_term.casefold()))
                    owners = [
                        resource
                        for resource in result.established_meaning.resources
                        if grounded_value.field in RESOURCE_FIELDS.get(resource, frozenset())
                    ]
                    if (
                        not issue_targets_relationship
                        and grounded_value.canonical_value is not None
                        and value_tokens
                        and value_tokens <= issue_tokens
                        and owners
                    ):
                        errors.append(
                            "Grounding declared authoritative data missing for "
                            f"'{issue.term}', but already established canonical value "
                            f"'{grounded_value.canonical_value}' on governed field "
                            f"{owners[0]}.{grounded_value.field}. Grounding must establish the "
                            "predicate and defer matching-row existence to plan execution; a "
                            "pre-execution absence assumption is not an authoritative-data gap."
                        )
                reference_values = candidates.get("reference_values", {})
                if not issue_targets_relationship and isinstance(reference_values, dict):
                    for resource in result.established_meaning.resources:
                        for field, category in CONTROLLED_REFERENCE_FIELDS.get(resource, {}).items():
                            for candidate in reference_values.get(category, []):
                                value = candidate.get("value") if isinstance(candidate, dict) else candidate
                                value_tokens = set(re.findall(r"[a-z0-9]+", str(value).casefold()))
                                if value_tokens and value_tokens <= issue_tokens:
                                    errors.append(
                                        "Grounding declared authoritative data missing for "
                                        f"'{issue.term}', but caller-visible controlled value "
                                        f"'{value}' is filterable through governed field "
                                        f"{resource}.{field}. Grounding must establish that "
                                        "predicate from retrieved semantic material and defer "
                                        "matching-row existence to plan execution."
                                    )
            for meaning in meanings:
                for binding in meaning.semantic_bindings:
                    unknown = set(binding.passage_ids) - passage_ids
                    if unknown:
                        errors.append(
                            "Grounding cited ontology passages that were not retrieved: "
                            + ", ".join(sorted(unknown))
                        )
                for grounded_value in meaning.grounded_values:
                    composite_owners = [
                        (resource, grounded_value.field)
                        for resource in meaning.resources
                        if (resource, grounded_value.field) in GOVERNED_COMPOSITE_FIELDS
                    ]
                    if composite_owners:
                        if len(composite_owners) != 1:
                            errors.append(
                                f"Grounding field '{grounded_value.field}' has ambiguous governed "
                                "composite ownership."
                            )
                            continue
                        resource, field = composite_owners[0]
                        candidate = composite_candidates.get(str(grounded_value.candidate_id or ""))
                        if candidate is None:
                            errors.append(
                                f"Grounding governed composite {resource}.{field} requires a "
                                "caller-visible candidate_id."
                            )
                            continue
                        if candidate.get("resource") != resource or candidate.get("field") != field:
                            errors.append(
                                f"Grounding candidate_id for governed composite {resource}.{field} "
                                "belongs to a different resource or field."
                            )
                            continue
                        expected = str(candidate["value"])
                        if str(grounded_value.canonical_value) != expected:
                            errors.append(
                                f"Grounding governed composite {resource}.{field} must preserve "
                                f"caller-visible canonical value '{expected}'."
                            )
                        user_tokens = term_tokens(grounded_value.user_term)
                        candidate_tokens = term_tokens(expected)
                        if not user_tokens or not user_tokens <= candidate_tokens:
                            errors.append(
                                f"Grounding user_term '{grounded_value.user_term}' does not "
                                f"identify governed composite candidate '{expected}'."
                            )
                        continue

                    if any(
                        grounded_value.field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                        for resource in meaning.resources
                    ):
                        continue

                    contract = candidate_field_contracts.get(grounded_value.field)
                    if contract is None:
                        continue
                    collection, canonical_source = contract
                    if grounded_value.candidate_id is None:
                        errors.append(
                            f"Grounding field '{grounded_value.field}' requires a caller-visible "
                            "candidate_id."
                        )
                        continue
                    candidate = candidate_records[collection].get(grounded_value.candidate_id)
                    if candidate is None:
                        value_matches = [
                            candidate_id
                            for candidate_id, record in candidate_records[collection].items()
                            if record.get(canonical_source) is not None
                            and str(record[canonical_source]) == str(grounded_value.canonical_value)
                        ]
                        correction = (
                            f" Use candidate_id '{value_matches[0]}' for the unique "
                            f"caller-visible {collection}.{canonical_source} match."
                            if len(value_matches) == 1
                            else ""
                        )
                        errors.append(
                            f"Grounding candidate_id for '{grounded_value.field}' was not present "
                            f"in caller-visible {collection}.{correction}"
                        )
                        continue
                    expected = candidate.get(canonical_source)
                    if expected is None or str(grounded_value.canonical_value) != str(expected):
                        errors.append(
                            f"Grounding field '{grounded_value.field}' must use caller-visible "
                            f"{collection}.{canonical_source} as canonical_value; candidate_id is "
                            "identity provenance only unless the field itself stores an id."
                        )
                    if not candidate_matches_term(candidate, grounded_value.user_term):
                        errors.append(
                            f"Grounding user_term '{grounded_value.user_term}' does not name its "
                            f"selected caller-visible {collection} candidate."
                        )
                if "separate" in interpretation.standalone_question.casefold():
                    terms_by_candidate: dict[str, set[str]] = {}
                    for grounded_value in meaning.grounded_values:
                        if grounded_value.candidate_id is not None:
                            terms_by_candidate.setdefault(grounded_value.candidate_id, set()).add(
                                " ".join(grounded_value.user_term.casefold().split())
                            )
                    duplicated = [
                        candidate_id for candidate_id, terms in terms_by_candidate.items() if len(terms) > 1
                    ]
                    if duplicated:
                        duplicate_details = "; ".join(
                            f"candidate_id {candidate_id!r}: "
                            f"{', '.join(sorted(terms_by_candidate[candidate_id]))}"
                            for candidate_id in duplicated
                        )
                        errors.append(
                            "Grounding assigned one candidate identity to separately described "
                            "cohorts; keep an unidentified cohort ungrounded unless an established "
                            f"relationship proves identity. Conflicts: {duplicate_details}."
                        )
            if errors:
                raise ValueError("Grounding validation failed: " + " | ".join(dict.fromkeys(errors)))

        stable_candidates, question_specific_candidates = _split_grounding_candidates(candidates)
        result = await self._json_call(
            stage="value_grounding",
            model=self.settings.grounding_model,
            payload={
                "canonical_resource_fields": {
                    resource: sorted(fields) for resource, fields in RESOURCE_FIELDS.items()
                },
                "candidate_field_contracts": {
                    "entity_id": {
                        "candidate_collection": "question_specific_candidates.entities",
                        "canonical_value_source": "id",
                        "candidate_id_source": "id",
                    },
                    "company": {
                        "candidate_collection": "question_specific_candidates.entities",
                        "canonical_value_source": "legal_name",
                        "candidate_id_source": "id",
                    },
                    "rm": {
                        "candidate_collection": "caller_visible_candidates.people",
                        "canonical_value_source": "name",
                        "candidate_id_source": "id",
                        "display_value_source": "full_name",
                    },
                    "analyst": {
                        "candidate_collection": "caller_visible_candidates.people",
                        "canonical_value_source": "name",
                        "candidate_id_source": "id",
                        "display_value_source": "full_name",
                    },
                    "counterparty_id": {
                        "candidate_collection": "caller_visible_candidates.counterparties",
                        "canonical_value_source": "id",
                        "candidate_id_source": "id",
                    },
                    "lender_name": {
                        "candidate_collection": "caller_visible_candidates.counterparties",
                        "canonical_value_source": "name",
                        "candidate_id_source": "id",
                    },
                    "controlled_value": {
                        "candidate_collection": "caller_visible_candidates.reference_values",
                        "canonical_value_source": "value",
                    },
                    "governed_composite": {
                        "candidate_collection": "question_specific_candidates.governed_composite_values",
                        "canonical_value_source": "value",
                        "candidate_id_source": "id",
                        "resource_source": "resource",
                        "field_source": "field",
                    },
                },
                "caller_visible_candidates": stable_candidates,
                "interpretation": interpretation.model_dump(),
                "semantic_material": {
                    "query": retrieval.query,
                    "focus_queries": [item.model_dump() for item in retrieval.focus_queries],
                    "matches": _project_retrieval_matches(retrieval),
                },
                "question_specific_candidates": question_specific_candidates,
            },
            output_type=ValueGroundingResult,
            validator=validate_grounding,
            repair_context={
                "semantic_material": {
                    "query": retrieval.query,
                    "focus_queries": [item.model_dump() for item in retrieval.focus_queries],
                    "matches": _project_retrieval_matches(retrieval),
                },
                "allowed_passage_ids": [
                    item["passage_id"]
                    for item in _project_retrieval_matches(retrieval, audience=AUDIENCE_GROUNDING)
                ],
                "caller_visible_candidates": {
                    collection: candidates.get(collection, [])
                    for collection, _ in candidate_field_contracts.values()
                },
                "reference_values": _reachable_reference_values(candidates.get("reference_values", {})),
                "canonical_resource_fields": {
                    resource: sorted(fields) for resource, fields in RESOURCE_FIELDS.items()
                },
            },
        )
        return result

    async def answerability(
        self,
        interpretation: QuestionInterpretation,
        grounding: ValueGroundingResult,
        _retrieval: SemanticRetrievalResult,
    ) -> AnswerabilityResult:
        meaning = grounding.established_meaning

        def validate_answerability(result: AnswerabilityResultDraft) -> None:
            if result.outcome == "ANSWERABLE" and grounding.status != "RESOLVED":
                raise ValueError("Answerability marked unresolved Grounding as ANSWERABLE")
            issue = grounding.issue
            if issue is not None and issue.kind == "NOT_FOUND":
                required_outcome = (
                    "CLARIFICATION_REQUIRED" if issue.missing_kind == "IDENTITY" else "OUT_OF_SCOPE"
                )
                if result.outcome != required_outcome:
                    raise ValueError(
                        f"Answerability must return {required_outcome} for NOT_FOUND "
                        f"missing_kind={issue.missing_kind}."
                    )
            if (
                any(item.prevents_main_answer for item in meaning.unavailable_obligations)
                and result.outcome != "OUT_OF_SCOPE"
            ):
                raise ValueError(
                    "Answerability must return OUT_OF_SCOPE when an unavailable obligation "
                    "prevents the main answer."
                )
            if (
                meaning.unavailable_obligations
                and not any(item.prevents_main_answer for item in meaning.unavailable_obligations)
                and result.outcome != "ANSWERABLE"
            ):
                raise ValueError(
                    "Answerability must preserve supported main results when every unavailable "
                    "obligation is explicitly non-blocking."
                )
            verified = result.verified_question
            if verified is None:
                return
            expected_unavailable = {
                (item.description, item.reason, item.prevents_main_answer)
                for item in meaning.unavailable_obligations
            }
            actual_unavailable = {
                (
                    item.description,
                    item.unavailable_reason or "",
                    item.prevents_main_answer,
                )
                for item in verified.evidence_obligations
                if item.availability == "UNAVAILABLE"
            }
            if not expected_unavailable.issubset(actual_unavailable):
                raise ValueError(
                    "Answerability must preserve every unavailable auxiliary obligation from "
                    "Grounding in verified_question.evidence_obligations."
                )
            separation_filters = [
                item for item in verified.filters if item.get("operator") in PRESENTATION_OPERATORS
            ]
            if separation_filters and not any(
                obligation.kind == "display" for obligation in verified.evidence_obligations
            ):
                raise ValueError(
                    "Entity-separation presentation must be preserved by a display evidence "
                    "obligation, not encoded only as an executable filter."
                )
            grounded_resources = set(meaning.resources)
            unknown_resources = set(verified.resources) - grounded_resources
            if unknown_resources:
                raise ValueError(
                    "Answerability introduced resources not established by Grounding: "
                    + ", ".join(sorted(unknown_resources))
                )
            grounded_passages = {
                passage_id for binding in meaning.semantic_bindings for passage_id in binding.passage_ids
            }
            cited_passages = {
                passage_id for binding in verified.semantic_bindings for passage_id in binding.passage_ids
            } | {
                passage_id
                for obligation in verified.evidence_obligations
                for passage_id in obligation.passage_ids
            }
            unknown_passages = cited_passages - grounded_passages
            if unknown_passages:
                raise ValueError(
                    "Answerability cited ontology passages not established by Grounding: "
                    + ", ".join(sorted(unknown_passages))
                )
            verified.semantic_bindings = [item.model_copy(deep=True) for item in meaning.semantic_bindings]
            verified.grounded_values = [item.model_copy(deep=True) for item in meaning.grounded_values]

            qualitative_filter_errors: list[str] = []
            retained_filters: list[VerifiedFilter] = []
            for filter_spec in verified.filters:
                pair = (
                    str(filter_spec.get("resource") or ""),
                    str(filter_spec.get("field") or ""),
                )
                if pair[1] not in AUTHORIZED_QUALITATIVE_FIELDS.get(pair[0], frozenset()):
                    retained_filters.append(filter_spec)
                    continue
                binding = next(
                    (
                        item
                        for item in meaning.semantic_bindings
                        if item.resource == pair[0] and item.field == pair[1] and item.passage_ids
                    ),
                    None,
                )
                if binding is None:
                    rendered_values = ", ".join(str(value) for value in _filter_values(filter_spec))
                    qualitative_filter_errors.append(
                        f"{pair[0]}.{pair[1]}={rendered_values or '<predicate>'} has no grounded "
                        "qualitative semantic provenance"
                    )
                    continue
                if not any(
                    obligation.kind == "qualitative"
                    and obligation.resource == pair[0]
                    and obligation.field == pair[1]
                    for obligation in verified.evidence_obligations
                ):
                    criterion = ", ".join(str(value) for value in _filter_values(filter_spec))
                    verified.evidence_obligations.append(
                        EvidenceObligation(
                            kind="qualitative",
                            description=(
                                f"Analyze {pair[0]}.{pair[1]} across the exact structured cohort"
                                + (f" for the requested meaning '{criterion}'." if criterion else ".")
                            ),
                            resource=pair[0],
                            field=pair[1],
                            provenance="ontology",
                            passage_ids=list(binding.passage_ids),
                        )
                    )
            if qualitative_filter_errors:
                raise ValueError(
                    "Answerability executable filter provenance failed: "
                    + " | ".join(qualitative_filter_errors)
                )
            verified.filters = retained_filters

            provenance_errors: list[str] = []
            for filter_spec in verified.filters:
                resource = str(filter_spec.get("resource") or "")
                field = str(filter_spec.get("field") or "")
                pair = (resource, field)
                controlled = field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                composite = pair in GOVERNED_COMPOSITE_FIELDS
                if not controlled and not composite:
                    continue
                grounded = [
                    item
                    for item in meaning.grounded_values
                    if item.field == field
                    and resource in meaning.resources
                    and item.canonical_value is not None
                ]
                normalized_values: list[Any] = []
                filter_errors: list[str] = []
                for value in _filter_values(filter_spec):
                    canonical_values = _matching_grounded_canonical_values(
                        resource,
                        field,
                        value,
                        grounded,
                        require_candidate_id=composite,
                        semantic_bindings=meaning.semantic_bindings,
                    )
                    if not canonical_values:
                        filter_errors.append(
                            f"{resource}.{field}={value} lacks host-validated executable provenance"
                        )
                    elif len(canonical_values) > 1:
                        filter_errors.append(
                            _ambiguous_filter_provenance_error(
                                resource,
                                field,
                                value,
                                canonical_values,
                            )
                        )
                    else:
                        canonical = canonical_values[0]
                        if not any(
                            type(existing) is type(canonical) and existing == canonical
                            for existing in normalized_values
                        ):
                            normalized_values.append(canonical)
                if filter_errors:
                    provenance_errors.extend(filter_errors)
                else:
                    _assign_filter_values(filter_spec, normalized_values)
            if provenance_errors:
                raise ExecutableProvenanceError(
                    "Answerability executable filter provenance failed: " + " | ".join(provenance_errors)
                )

            qualitative_fields = {
                (obligation.resource, obligation.field)
                for obligation in verified.evidence_obligations
                if obligation.kind == "qualitative"
                and obligation.availability == "SUPPORTED"
                and obligation.resource is not None
                and obligation.field is not None
            }
            qualitative_errors: list[str] = []
            unauthorized_qualitative = sorted(
                f"{resource}.{field}"
                for resource, field in qualitative_fields
                if field not in AUTHORIZED_QUALITATIVE_FIELDS.get(resource, frozenset())
            )
            if unauthorized_qualitative:
                qualitative_errors.append(
                    "unauthorized qualitative source field(s): " + ", ".join(unauthorized_qualitative)
                )
            if qualitative_fields:
                verified.qualitative_analysis = True
            for resource, field in qualitative_fields:
                if any(
                    item.get("resource") == resource and item.get("field") == field
                    for item in verified.filters
                ):
                    qualitative_errors.append(
                        f"qualitative field {resource}.{field} cannot be an executable filter"
                    )
                if not any(
                    obligation.kind == "completeness"
                    and obligation.resource == resource
                    and obligation.field == field
                    for obligation in verified.evidence_obligations
                ):
                    source = next(
                        obligation
                        for obligation in verified.evidence_obligations
                        if obligation.kind == "qualitative" and obligation.availability == "SUPPORTED"
                        if obligation.resource == resource and obligation.field == field
                    )
                    verified.evidence_obligations.append(
                        source.model_copy(
                            update={
                                "kind": "completeness",
                                "description": (
                                    f"Report missing qualitative-source coverage for " f"{resource}.{field}."
                                ),
                            }
                        )
                    )
            if qualitative_errors:
                raise ValueError(
                    "Answerability qualitative-evidence validation failed: " + " | ".join(qualitative_errors)
                )
            expected_missing: set[tuple[str, str, Literal["predicate", "measure", "qualitative_source"]]] = {
                (
                    str(item.get("resource") or ""),
                    str(item.get("field") or ""),
                    "predicate",
                )
                for item in verified.filters
                if (
                    str(item.get("resource") or ""),
                    str(item.get("field") or ""),
                )
                in REPORT_UNASSESSABLE_FILTER_FIELDS
            }
            expected_missing.update(
                (
                    str(item.get("resource") or ""),
                    str(item.get("field") or ""),
                    "measure",
                )
                for item in verified.metrics
                if (
                    str(item.get("resource") or ""),
                    str(item.get("field") or ""),
                )
                in REPORT_UNASSESSABLE_FILTER_FIELDS
                and str(item.get("operation") or item.get("aggregation") or "")
                in {"sum", "average", "min", "max"}
            )
            expected_missing.update(
                (resource, field, "qualitative_source") for resource, field in qualitative_fields
            )
            verified.missing_data_requirements = [
                MissingDataRequirement(resource=resource, field=field, usage=usage)
                for resource, field, usage in sorted(expected_missing)
            ]

        try:
            result = await self._json_call(
                stage="answerability",
                model=self.settings.answerability_model,
                payload={
                    "interpretation": interpretation.model_dump(),
                    "grounding": grounding.model_dump(),
                },
                output_type=AnswerabilityResultDraft,
                validator=validate_answerability,
                repair_context={
                    "grounding": grounding.model_dump(mode="json"),
                    "canonical_resource_fields": {
                        resource: sorted(fields) for resource, fields in RESOURCE_FIELDS.items()
                    },
                },
            )
        except ExecutableProvenanceError:
            # Two model attempts tried to execute meaning that Grounding did not
            # establish. The only safe deterministic recovery is a non-executing,
            # business-facing unsupported outcome.
            return AnswerabilityResult(
                outcome="OUT_OF_SCOPE",
                reason=(
                    "The requested classification is not supported by the governed "
                    "caller-visible data, so I cannot apply it as a filter."
                ),
            )
        return AnswerabilityResult.model_validate(result.model_dump(mode="json"))

    async def plan(
        self,
        verified_question: VerifiedQuestion,
        retrieval: SemanticRetrievalResult,
    ) -> QueryPlan:
        planning_matches = _project_retrieval_matches(retrieval, audience=AUDIENCE_PLANNING)

        def validate_plan_coverage(draft: QueryPlanDraft) -> None:
            plan = QueryPlan.model_validate(draft.model_dump(mode="json"))
            qualitative_fields = {
                (requirement.resource, requirement.field)
                for requirement in verified_question.missing_data_requirements
                if requirement.usage == "qualitative_source"
            }
            if verified_question.qualitative_analysis:
                _normalize_qualitative_plan(plan, qualitative_fields)
            _normalize_non_pushdown_read_filters(plan, verified_question)
            _validate_filter_correspondence(plan, verified_question)
            if verified_question.qualitative_analysis:
                qualitative_resources = {resource for resource, _ in qualitative_fields}
                if any(read.resource in qualitative_resources and read.q is not None for read in plan.reads):
                    raise ValueError(
                        "Qualitative cohort reads cannot use q substring selection on the "
                        "qualitative resource."
                    )
                qualitative_names = {
                    name
                    for resource, field in qualitative_fields
                    for name in {
                        field,
                        *(
                            f"{step.arguments.get('right_prefix') or 'right_'}{field}"
                            for step in plan.operations
                            if step.operation == "join"
                            and step.arguments.get("right")
                            in {read.name for read in plan.reads if read.resource == resource}
                        ),
                    }
                }
                for step in plan.operations:
                    if step.operation == "filter" and step.arguments.get("field") in qualitative_names:
                        raise ValueError(
                            "Qualitative source fields cannot select or remove cohort rows; return "
                            "the exact structured cohort, including null source text, for "
                            "qualitative analysis."
                        )
                aggregate_outputs = {
                    step.output
                    for step in plan.operations
                    if step.operation
                    in {
                        "count",
                        "sum",
                        "distinct_count",
                        "average",
                        "min",
                        "max",
                        "missing_count",
                        "group",
                    }
                }
                if aggregate_outputs.intersection(plan.result_names):
                    raise ValueError(
                        "Qualitative result datasets must retain row grain and cannot be aggregate "
                        "outputs."
                    )
                reads_by_name = {read.name: read for read in plan.reads}
                producers = {step.output: step for step in plan.operations}

                def retains_field(dataset: str, resource: str, field: str) -> bool:
                    if dataset in reads_by_name:
                        return reads_by_name[dataset].resource == resource
                    step = producers.get(dataset)
                    if step is None:
                        return False
                    if step.operation in {"filter", "sort", "limit", "rank"}:
                        return retains_field(step.input, resource, field)
                    if step.operation == "project":
                        return field in (step.arguments.get("fields") or []) and retains_field(
                            step.input, resource, field
                        )
                    if step.operation == "join":
                        return retains_field(step.input, resource, field) or retains_field(
                            str(step.arguments.get("right") or ""), resource, field
                        )
                    if step.operation in {"set_union", "set_intersection"}:
                        return any(
                            retains_field(name, resource, field)
                            for name in [step.input, *(step.arguments.get("others") or [])]
                        )
                    return False

                missing_qualitative_fields = [
                    f"{resource}.{field}"
                    for resource, field in qualitative_fields
                    if not any(
                        retains_field(result_name, resource, field) for result_name in plan.result_names
                    )
                ]
                if missing_qualitative_fields:
                    raise ValueError(
                        "Qualitative result datasets do not retain authorized source fields: "
                        + ", ".join(sorted(missing_qualitative_fields))
                    )
            predicate_requirements = [
                item for item in verified_question.missing_data_requirements if item.usage == "predicate"
            ]
            measure_requirements = [
                item for item in verified_question.missing_data_requirements if item.usage == "measure"
            ]
            for requirement in [*predicate_requirements, *measure_requirements]:
                resource = requirement.resource
                field = requirement.field
                related_reads = [read for read in plan.reads if read.resource == resource]
                if any(field in read.filters for read in related_reads):
                    raise ValueError(
                        f"Missing-value plan must read {resource}.{field} unfiltered before "
                        "qualifying matches."
                    )

            def supplied_fields(resource: str, field: str) -> set[str]:
                supplied = {field}
                for join in plan.operations:
                    if join.operation != "join" or join.arguments.get("right") not in {
                        read.name for read in plan.reads if read.resource == resource
                    }:
                        continue
                    supplied.add(f"{join.arguments.get('right_prefix') or 'right_'}{field}")
                return supplied

            # Missing measure coverage belongs to the same cohort grain as its
            # aggregate. A model-added null-removal step changes that denominator
            # and makes grouped missing counts unknowable (or misleadingly zero).
            # Explicit user predicates remain valid because correspondence assigns
            # them a verified filter id before this check.
            for requirement in measure_requirements:
                measure_fields = supplied_fields(requirement.resource, requirement.field)
                discarded = [
                    step
                    for step in plan.operations
                    if step.operation == "filter"
                    and step.arguments.get("field") in measure_fields
                    and not (step.arguments.get("verified_filter_ids") or [])
                ]
                if discarded:
                    raise ValueError(
                        f"Missing-value measure {requirement.resource}.{requirement.field} must "
                        "retain null-bearing rows through aggregation so completeness is computed "
                        "at the aggregate or group grain; an unverified filter cannot discard them."
                    )

            filter_steps = [step for step in plan.operations if step.operation == "filter"]
            for step in filter_steps:
                step.arguments.pop("missing_policy", None)
            missing_predicate_plans: list[tuple[str, str]] = []
            for requirement in predicate_requirements:
                matching = [
                    step
                    for step in filter_steps
                    if step.arguments.get("field") in supplied_fields(requirement.resource, requirement.field)
                ]
                if not matching:
                    missing_predicate_plans.append((requirement.resource, requirement.field))
                    continue
                matching[0].arguments["missing_policy"] = "report_unassessable"
            if missing_predicate_plans:
                raise ValueError(
                    "Missing-data predicate has no corresponding post-read filter: "
                    + ", ".join(
                        f"{resource}.{field}.predicate" for resource, field in missing_predicate_plans
                    )
                )
            PlanExecutor(self.settings).validate_plan(plan)
            _validate_visible_list_results(plan, verified_question.answer_shape)
            draft._validated_plan = plan

        draft = await self._json_call(
            stage="query_planning",
            model=self.settings.planning_model,
            payload={
                "resource_contracts": {
                    resource: {
                        "path_kind": "fixed Register GET",
                        "equality_filters": sorted(RESOURCE_SPECS[resource].equality_filters),
                        "substring_search_fields": sorted(RESOURCE_SPECS[resource].search_fields),
                        "fields": sorted(fields),
                    }
                    for resource, fields in RESOURCE_FIELDS.items()
                },
                "instruction": (
                    "Name final datasets in result_names; the host derives their result shapes. "
                    "Use host operations for every count and "
                    "sum. Every operation input and join right dataset must be declared by a read "
                    "or produced by an earlier operation; include every relationship read required "
                    "by planning knowledge. Do not introduce a join, filter, value, or condition "
                    "that is not required by the verified question or planning knowledge. Value "
                    "Grounding is already complete: use canonical "
                    "values present in the "
                    "verified question directly and never re-read People to ground them again. "
                    "Use set operations only for separately produced cohorts at the same declared "
                    "canonical identity grain; join their identity-key output separately for display. "
                    "Use rank rather than sort plus limit when ties must be preserved, and plan a "
                    "separate missing_count when excluded null metrics are material. "
                    "For grouped numeric measures, aggregate the unfiltered cohort directly: group "
                    "aggregations compute assessed and missing counts per group, so do not remove "
                    "null measure rows before grouping or ranking. "
                    "For each predicate missing_data_requirement, plan one ordinary filter carrying "
                    "the requirement policy; the host counts and reports unassessable rows. "
                    "Never put that field in any read's filters; read it unfiltered before the "
                    "qualifying filter. "
                    "Only "
                    "equality_filters may appear in reads.filters, with scalar values. "
                    "A comma inside a scalar is literal text, never a list of alternatives; "
                    "the host evaluates that equality locally within bounded reads. "
                    "Use host in/not_in operations for multi-value predicates. A read "
                    "may use q only for the declared substring-search fields. A later read may use "
                    "a scalar from exactly one row of an earlier read as $read_name.field in an "
                    "equality filter. When a relationship is only enriching a primary cohort for "
                    "display, use a left join so missing optional links do not discard qualifying "
                    "primary records. A join defaults to left. Use inner only when the verified "
                    "question explicitly requires relationship existence or a predicate on the "
                    "related resource, and then set relationship_required=true. A resource noun "
                    "appearing only inside a canonical compound phrase is not such a requirement. "
                    "Use host filter operations for ranges, "
                    "thresholds, status "
                    "families, negation, dates, and null checks."
                    " For qualitative_analysis=true, construct the complete cohort only from "
                    "structured fields, return row-level records with each qualitative evidence "
                    "obligation's authorized field, and do not use q/contains/icontains to select "
                    "records by the requested qualitative meaning."
                ),
                "verified_question": _planning_verified_question_payload(verified_question),
                "planning_knowledge": planning_matches,
            },
            output_type=QueryPlanDraft,
            validator=validate_plan_coverage,
            repair_context={
                "verified_question": verified_question.model_dump(mode="json"),
                "canonical_resource_fields": {
                    resource: sorted(fields) for resource, fields in RESOURCE_FIELDS.items()
                },
            },
        )
        return draft.validated_plan

    async def qualitative(self, corpus: QualitativeCorpus) -> QualitativeAnalysisResult:
        if corpus.coverage.text_records == 0:
            return QualitativeAnalysisResult(findings=[], coverage=corpus.coverage)
        validated: QualitativeAnalysisResult | None = None

        def validate_draft(draft: QualitativeAnalysisDraft) -> None:
            nonlocal validated
            validated = validate_qualitative_findings(draft, corpus)

        authorized_records = [
            {
                "evidence_ref": record.evidence_ref,
                "resource": record.resource,
                "source_field": record.source_field,
                "source_text": record.source_text,
                "missing": record.missing,
                "truncated": record.truncated,
            }
            for record in corpus.records
        ]
        draft = await self._json_call(
            stage="qualitative_analysis",
            model=self.settings.qualitative_model,
            payload={
                "authorized_records": authorized_records,
                "coverage": corpus.coverage.model_dump(mode="json"),
                "validation_rules": [
                    "Within each finding, each evidence_ref/source_field pair may occur only once.",
                    "Every excerpt must be copied character-for-character from source_text.",
                    "Do not paraphrase, normalize punctuation, change case, or join non-contiguous text.",
                ],
            },
            output_type=QualitativeAnalysisDraft,
            validator=validate_draft,
            repair_context={
                "authorized_records": authorized_records,
                "coverage": corpus.coverage.model_dump(mode="json"),
                "validation_rules": [
                    "Within each finding, each evidence_ref/source_field pair may occur only once.",
                    "Every excerpt must be copied character-for-character from source_text.",
                    "Do not paraphrase, normalize punctuation, change case, or join non-contiguous text.",
                ],
            },
        )
        return validated or validate_qualitative_findings(draft, corpus)

    async def answer(self, verified_question: VerifiedQuestion, evidence: ResultEvidence) -> str:
        evidence_payload = {
            "facts": evidence.facts,
            "rows": {
                "count": len(evidence.rows),
                "columns": list(evidence.rows[0].keys()) if evidence.rows else [],
                "sample": evidence.rows[:5],
            },
            "contributing_record_count": len(evidence.contributing_records),
            "completeness": evidence.completeness,
            "retrieval_windows": [
                {
                    key: window.get(key)
                    for key in ("read_name", "resource", "completeness", "record_count")
                    if key in window
                }
                for window in evidence.retrieval_windows
            ],
            "metric_completeness": evidence.metric_completeness,
            "retrieved_at": evidence.retrieved_at.isoformat(),
            "caveats": evidence.caveats,
            "qualitative_findings": [item.model_dump(mode="json") for item in evidence.qualitative_findings],
            "qualitative_coverage": (
                evidence.qualitative_coverage.model_dump(mode="json")
                if evidence.qualitative_coverage is not None
                else None
            ),
            "unavailable_obligations": [
                item.model_dump(mode="json") for item in evidence.unavailable_obligations
            ],
        }
        if not evidence.rows:
            # Scalar results have no tabular rows; an empty row count is not a
            # missing-data caveat and must not compete with the computed facts.
            evidence_payload.pop("rows")
        result = await self._json_call(
            stage="answer_generation",
            model=self.settings.answer_model,
            payload={
                "verified_question": verified_question.model_dump(mode="json"),
                "result_evidence": evidence_payload,
                "instruction": (
                    "Use facts for totals. Answer in business-facing prose without referring to an "
                    "attached table. Do not transcribe sample rows or imply they are a complete list. "
                    "Explain material coverage limits in plain language."
                ),
            },
            output_type=AnswerGenerationResult,
        )
        return result.answer
