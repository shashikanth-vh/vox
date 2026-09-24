"""Stable input/output contracts for every Chitti reasoning stage."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    field_validator,
    model_serializer,
    model_validator,
)

from app.evidence import Completeness
from app.register_access import RESOURCE_FIELDS

ResourceName: TypeAlias = Annotated[  # type: ignore[valid-type]  # noqa: UP040
    Literal[*tuple(RESOURCE_FIELDS)],
    Field(description="Canonical Register resource id from the fixed resource catalogue."),
]
CanonicalField: TypeAlias = Annotated[  # type: ignore[valid-type]  # noqa: UP040
    Literal[*tuple(sorted({field for fields in RESOURCE_FIELDS.values() for field in fields}))],
    Field(description="Canonical Register field name; use only for a field owned by its resource."),
]
Scalar: TypeAlias = str | int | float | bool  # noqa: UP040
NonEmptyString: TypeAlias = Annotated[str, Field(min_length=1)]  # noqa: UP040


class ClosedModel(BaseModel):
    """A model-authored object whose vocabulary is part of the stage contract."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _require_owned_field(resource: str | None, field: str | None) -> None:
    if resource is not None and field is not None and field not in RESOURCE_FIELDS[resource]:
        owners = sorted(owner for owner, fields in RESOURCE_FIELDS.items() if field in fields)
        suffix = f"; actual field owners: {', '.join(owners)}" if owners else ""
        raise ValueError(f"unknown field '{field}' on resource '{resource}'{suffix}")


class MappingModel(ClosedModel):
    """Typed contract with the legacy mapping interface used by host execution code."""

    @staticmethod
    def _plain(value: Any) -> Any:
        if isinstance(value, MappingModel):
            return value._values()
        if isinstance(value, list):
            return [MappingModel._plain(item) for item in value]
        return value

    def _values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if value is None:
                continue
            if (
                not field.is_required()
                and name not in self.model_fields_set
                and value == field.get_default(call_default_factory=True)
            ):
                continue
            values[field.alias or name] = self._plain(value)
        return values

    @model_serializer
    def serialize_mapping(self) -> dict[str, Any]:
        return self._values()

    def __getitem__(self, key: str) -> Any:
        field_name = next(
            (name for name, field in type(self).model_fields.items() if field.alias == key),
            key,
        )
        if field_name not in type(self).model_fields:
            raise KeyError(key)
        value = getattr(self, field_name)
        if value is None:
            raise KeyError(key)
        return self._plain(value)

    def __setitem__(self, key: str, value: Any) -> None:
        field_name = next(
            (name for name, field in type(self).model_fields.items() if field.alias == key),
            key,
        )
        if field_name not in type(self).model_fields:
            raise KeyError(key)
        setattr(self, field_name, value)

    def __delitem__(self, key: str) -> None:
        field_name = next(
            (name for name, field in type(self).model_fields.items() if field.alias == key),
            key,
        )
        if field_name not in type(self).model_fields or getattr(self, field_name) is None:
            raise KeyError(key)
        setattr(self, field_name, None)

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def items(self):
        return self._values().items()

    def keys(self):
        return self._values().keys()

    def pop(self, key: str, default: Any = None) -> Any:
        try:
            value = self[key]
        except KeyError:
            return default
        del self[key]
        return value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return self._values() == other
        return super().__eq__(other)


class StageName(StrEnum):
    CONVERSATION = "conversation_resolution"
    INTERPRETATION = "question_interpretation"
    RETRIEVAL = "semantic_retrieval"
    GROUNDING = "value_grounding"
    ANSWERABILITY = "answerability"
    PLANNING = "query_planning"
    EXECUTION = "plan_execution"
    QUALITATIVE = "qualitative_analysis"
    EVIDENCE = "evidence_construction"
    ANSWER = "answer_generation"


class StageUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    providers: list[str] = Field(default_factory=list)
    attempted_calls: int = 0
    model_calls: int = 0
    repair_calls: int = 0
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    responses_with_usage: int = 0
    responses_missing_usage: int = 0
    measurement: Literal["measured", "partial", "unavailable"]


class StageRecord(BaseModel):
    stage: StageName
    status: Literal["started", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None = None
    elapsed_ms: float | None = None
    model: str | None = None
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    usage: StageUsage | None = None


class ConversationInput(BaseModel):
    messages: list[dict[str, Any]]
    confirmed_context: dict[str, Any] = Field(default_factory=dict)


class PreservedIntent(BaseModel):
    kind: Literal[
        "entity",
        "scope",
        "temporal",
        "status",
        "polarity",
        "inclusion",
        "exclusion",
        "separate_cohort",
        "metric",
        "relationship",
        "other",
    ]
    source_message_index: int = Field(ge=0)
    resolved_text: str = Field(min_length=1)


class ConversationIntentDraft(BaseModel):
    kind: Literal[
        "entity",
        "scope",
        "temporal",
        "status",
        "polarity",
        "inclusion",
        "exclusion",
        "separate_cohort",
        "metric",
        "relationship",
        "other",
    ]
    source_message_index: int = Field(ge=0)
    resolved_text: str = Field(min_length=1)


class ConversationResolutionDraft(BaseModel):
    standalone_question: str = Field(min_length=1)
    intent_ledger: list[ConversationIntentDraft] = Field(default_factory=list)
    introduced_constraints: list[str] = Field(default_factory=list, max_length=0)


class ConversationResolution(BaseModel):
    standalone_question: str = Field(min_length=1)
    intent_ledger: list[PreservedIntent] = Field(default_factory=list)
    introduced_constraints: list[str] = Field(default_factory=list, max_length=0)
    used_prior_context: bool = False


class SurfaceTerm(BaseModel):
    text: str = Field(min_length=1)
    role: Literal[
        "subject",
        "measure",
        "book_scope",
        "dimension",
        "lifecycle",
        "filter",
        "relationship",
        "actor",
        "presentation",
        "unknown",
    ] = "unknown"
    source_text: str = Field(min_length=1)


class SurfaceLiteral(BaseModel):
    text: str = Field(min_length=1)
    kind: Literal["number", "currency", "percentage", "date", "duration", "operator"]
    value: str | int | float | None = None
    unit: str | None = None
    operator: str | None = None
    source_text: str = Field(min_length=1)


class QuestionInterpretation(BaseModel):
    standalone_question: str = Field(min_length=1)
    terms: list[SurfaceTerm] = Field(default_factory=list)
    literals: list[SurfaceLiteral] = Field(default_factory=list)
    requested_shape: Literal["scalar", "list", "grouped", "ranked", "rate", "qualitative"]
    grouping_terms: list[str] = Field(default_factory=list)
    ranking_terms: list[str] = Field(default_factory=list)
    time_expressions: list[str] = Field(default_factory=list)
    relationship_terms: list[str] = Field(default_factory=list)
    qualitative_analysis: bool = False
    unresolved_terms: list[str] = Field(default_factory=list)


class RetrievalMatch(BaseModel):
    passage_id: str
    source: str
    version: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    dense_score: float | None = None
    sparse_score: float | None = None
    fusion_score: float
    rerank_score: float | None = None
    exact_match: bool = False
    normalized_match: bool = False
    origins: list[str] = Field(default_factory=list)


SemanticFacet = Literal[
    "relationship",
    "dimension",
    "lifecycle",
    "metric",
    "book_scope",
    "resource",
]


class RetrievalNeed(BaseModel):
    facet: SemanticFacet
    query: str = Field(min_length=1, max_length=300)


class RetrievalFocus(BaseModel):
    responsibility: Literal["relationship_dimension", "lifecycle_metric", "book_scope"]
    needs: list[RetrievalNeed] = Field(min_length=1, max_length=2)
    query: str = Field(min_length=1, max_length=300)


class SemanticRetrievalResult(BaseModel):
    query: str
    matches: list[RetrievalMatch]
    planning_matches: list[RetrievalMatch] = Field(default_factory=list)
    focus_queries: list[RetrievalFocus] = Field(default_factory=list)
    semantic_query_count: int = Field(default=1, ge=1, le=3)
    passage_limit: int = Field(default=6, ge=1, le=50)
    estimated_context_tokens: int = Field(default=0, ge=0)


class GroundedValue(BaseModel):
    resource: ResourceName | None = None
    field: CanonicalField
    user_term: str
    canonical_value: str | None = None
    candidate_id: str | None = None
    candidate_label: str | None = None
    exact_match: bool = False
    normalized_match: bool = False

    @model_validator(mode="after")
    def require_owned_field(self) -> GroundedValue:
        _require_owned_field(self.resource, self.field)
        return self


class SemanticBindingDetails(ClosedModel):
    missing_policy: Literal["report_unassessable"] | None = None
    related_resource: ResourceName | None = None
    related_field: NonEmptyString | None = None


class SemanticBinding(BaseModel):
    user_term: str = Field(min_length=1)
    binding_kind: Literal[
        "resource",
        "field",
        "lifecycle",
        "metric",
        "relationship",
        "time",
        "null_policy",
    ]
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    canonical_values: list[str | int | float | bool] = Field(default_factory=list)
    definition: str | None = None
    details: SemanticBindingDetails = Field(default_factory=SemanticBindingDetails)
    passage_ids: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def canonicalize_surface_binding_kind(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("binding_kind") == "book_scope":
            value = {**value, "binding_kind": "resource"}
        return value

    @model_validator(mode="after")
    def require_owned_field(self) -> SemanticBinding:
        _require_owned_field(self.resource, self.field)
        return self


class UnavailableObligation(BaseModel):
    description: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    prevents_main_answer: bool = False


class GroundedMeaning(BaseModel):
    resources: list[ResourceName] = Field(default_factory=list)
    semantic_bindings: list[SemanticBinding] = Field(default_factory=list)
    grounded_values: list[GroundedValue] = Field(default_factory=list)
    unavailable_obligations: list[UnavailableObligation] = Field(default_factory=list)


class GroundingAlternative(BaseModel):
    label: str = Field(min_length=1)
    meaning: GroundedMeaning


class GroundingIssue(BaseModel):
    term: str = Field(min_length=1)
    kind: Literal["AMBIGUOUS", "NOT_FOUND"]
    reason: str = Field(min_length=1)
    missing_definition: str | None = None
    missing_kind: Literal["IDENTITY", "AUTHORITATIVE_DATA"] | None = None
    alternatives: list[GroundingAlternative] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_complete_ambiguity_alternatives(self) -> GroundingIssue:
        if self.kind == "AMBIGUOUS" and len(self.alternatives) < 2:
            raise ValueError("AMBIGUOUS grounding issues require at least two alternatives")
        if self.kind == "NOT_FOUND" and not self.missing_definition:
            raise ValueError("NOT_FOUND grounding issues require missing_definition")
        if self.kind == "NOT_FOUND" and self.missing_kind is None:
            raise ValueError("NOT_FOUND grounding issues require missing_kind")
        if self.kind == "AMBIGUOUS" and self.missing_kind is not None:
            raise ValueError("AMBIGUOUS grounding issues cannot contain missing_kind")
        return self


class ValueGroundingResult(BaseModel):
    status: Literal["RESOLVED", "NEEDS_CLARIFICATION"]
    established_meaning: GroundedMeaning = Field(default_factory=GroundedMeaning)
    issue: GroundingIssue | None = None

    @model_validator(mode="after")
    def require_status_consistent_issue(self) -> ValueGroundingResult:
        if self.status == "RESOLVED" and self.issue is not None:
            raise ValueError("RESOLVED grounding cannot contain an issue")
        if self.status == "NEEDS_CLARIFICATION" and self.issue is None:
            raise ValueError("NEEDS_CLARIFICATION grounding requires an issue")
        return self


class EvidenceObligation(BaseModel):
    kind: Literal[
        "metric",
        "row_field",
        "display",
        "completeness",
        "definition",
        "answer_shape",
        "qualitative",
        "unavailable",
    ]
    description: str = Field(min_length=1)
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    provenance: Literal["user", "ontology", "register"]
    passage_ids: list[str] = Field(default_factory=list)
    source_text: str | None = None
    availability: Literal["SUPPORTED", "UNAVAILABLE"] = "SUPPORTED"
    unavailable_reason: str | None = None
    prevents_main_answer: bool = False

    @model_validator(mode="after")
    def require_matching_provenance(self) -> EvidenceObligation:
        _require_owned_field(self.resource, self.field)
        if self.provenance == "ontology" and not self.passage_ids:
            raise ValueError("Ontology evidence obligations require passage_ids")
        if self.provenance == "user" and not self.source_text:
            raise ValueError("User evidence obligations require source_text")
        if self.availability == "UNAVAILABLE" and not self.unavailable_reason:
            raise ValueError("Unavailable evidence obligations require unavailable_reason")
        if self.availability == "SUPPORTED" and self.unavailable_reason is not None:
            raise ValueError("Supported evidence obligations cannot have unavailable_reason")
        return self


class MissingDataRequirement(BaseModel):
    resource: ResourceName
    field: CanonicalField
    policy: Literal["report_unassessable"] = "report_unassessable"
    usage: Literal["predicate", "measure", "qualitative_source"]

    @model_validator(mode="after")
    def require_owned_field(self) -> MissingDataRequirement:
        _require_owned_field(self.resource, self.field)
        return self


class VerifiedMetric(MappingModel):
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    operation: Literal["count", "sum", "distinct_count", "average", "min", "max"] | None = None
    aggregation: Literal["count", "sum", "distinct_count", "average", "min", "max"] | None = None
    name: NonEmptyString | None = None
    unit: NonEmptyString | None = None

    @model_validator(mode="after")
    def require_owned_field(self) -> VerifiedMetric:
        _require_owned_field(self.resource, self.field)
        return self


class VerifiedFilter(MappingModel):
    resource: ResourceName | None = None
    field: CanonicalField
    operator: Literal[
        "eq",
        "equals",
        "=",
        "ne",
        "in",
        "not_in",
        "is_null",
        "not_null",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "icontains",
        "older_than_days",
        "newer_than_days",
        "exclude_from_combination",
        "separate",
        "display_separately",
    ] = Field(default="eq", description="Predicate operator; set operators use values, not value.")
    value: Scalar | None = Field(
        default=None, description="Single predicate value when operator is not in/not_in."
    )
    values: list[Scalar] | None = Field(
        default=None, description="Predicate values used only by in and not_in."
    )
    days: int | None = Field(default=None, ge=0)
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_text: str | None = None
    missing_policy: Literal["report_unassessable"] | None = None
    _filter_id: str | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def normalize_singleton_scalar_values(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        operator = value.get("operator", "eq")
        scalar = value.get("value")
        values = value.get("values")
        if (
            operator not in {"in", "not_in", "is_null", "not_null"}
            and isinstance(values, list)
            and len(values) == 1
            and (scalar is None or values == [scalar])
        ):
            canonical = dict(value)
            canonical["value"] = scalar if scalar is not None else values[0]
            canonical["values"] = None
            return canonical
        return value

    @model_validator(mode="after")
    def require_operator_value_shape(self) -> VerifiedFilter:
        if self.operator in {"in", "not_in"}:
            if self.values is None or self.value is not None:
                raise ValueError(f"Filter operator '{self.operator}' requires values only")
        elif self.operator in {"is_null", "not_null"}:
            if self.value is not None or self.values is not None:
                raise ValueError(f"Filter operator '{self.operator}' accepts no value")
        elif self.values is not None:
            raise ValueError(f"Filter operator '{self.operator}' accepts one value, not values")
        return self

    @property
    def filter_id(self) -> str:
        if self._filter_id is None:
            raise ValueError("Verified filter id has not been assigned by the host")
        return self._filter_id


class VerifiedTimeSemantics(MappingModel):
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    kind: NonEmptyString | None = None
    value: str | int | float | None = None
    start: date | None = None
    end: date | None = None
    as_of: date | None = None


class VerifiedRanking(MappingModel):
    field: NonEmptyString | None = None
    direction: Literal["top", "bottom"] | None = None
    count: int | None = Field(default=None, ge=1)
    ties: Literal["preserve", "break"] | None = None


class RelationshipRequirement(MappingModel):
    resource: ResourceName | None = None
    field: CanonicalField | None = None
    related_resource: ResourceName | None = None
    related_field: CanonicalField | None = None
    kind: NonEmptyString | None = None
    required: bool | None = None

    @model_validator(mode="after")
    def require_owned_fields(self) -> RelationshipRequirement:
        _require_owned_field(self.resource, self.field)
        _require_owned_field(self.related_resource, self.related_field)
        return self


class VerifiedQuestion(BaseModel):
    standalone_question: str = Field(min_length=1)
    resources: list[ResourceName] = Field(min_length=1)
    answer_shape: Literal["scalar", "list", "grouped", "ranked", "rate", "qualitative"]
    metrics: list[VerifiedMetric] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[VerifiedFilter] = Field(default_factory=list)
    time_semantics: VerifiedTimeSemantics = Field(default_factory=VerifiedTimeSemantics)
    grouping: list[str] = Field(default_factory=list)
    ranking: VerifiedRanking = Field(default_factory=VerifiedRanking)
    relationship_requirements: list[RelationshipRequirement] = Field(default_factory=list)
    semantic_bindings: list[SemanticBinding] = Field(default_factory=list)
    grounded_values: list[GroundedValue] = Field(default_factory=list)
    evidence_obligations: list[EvidenceObligation] = Field(min_length=1)
    missing_data_requirements: list[MissingDataRequirement] = Field(default_factory=list)
    qualitative_analysis: bool = False

    @model_validator(mode="after")
    def assign_and_validate_filter_ids(self) -> VerifiedQuestion:
        for index, filter_spec in enumerate(self.filters, start=1):
            if filter_spec.resource is None:
                owners = [
                    resource for resource in self.resources if filter_spec.field in RESOURCE_FIELDS[resource]
                ]
                if len(owners) == 1:
                    filter_spec.resource = owners[0]
            if (
                filter_spec.resource is not None
                and filter_spec.field not in RESOURCE_FIELDS[filter_spec.resource]
            ):
                raise ValueError(
                    f"Field '{filter_spec.field}' does not belong to resource " f"'{filter_spec.resource}'"
                )
            filter_spec._filter_id = f"f{index}"
        return self

    @model_validator(mode="before")
    @classmethod
    def canonicalize_set_filters(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("filters"), list):
            return value
        changed = False
        filters: list[Any] = []
        for filter_spec in value["filters"]:
            if (
                isinstance(filter_spec, dict)
                and "values" not in filter_spec
                and isinstance(filter_spec.get("value"), list)
            ):
                canonical_filter = dict(filter_spec)
                canonical_filter["values"] = canonical_filter.pop("value")
                filters.append(canonical_filter)
                changed = True
            else:
                filters.append(filter_spec)
        if not changed:
            return value
        canonical = dict(value)
        canonical["filters"] = filters
        return canonical

    @model_validator(mode="before")
    @classmethod
    def normalize_filter_missing_policies(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("filters"), list):
            return value
        canonical = dict(value)
        requirements = list(canonical.get("missing_data_requirements") or [])
        requirement_keys = {
            (item.get("resource"), item.get("field"), item.get("usage"))
            for item in requirements
            if isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in ("resource", "field", "usage"))
        }
        filters: list[Any] = []
        for filter_spec in canonical["filters"]:
            if not isinstance(filter_spec, dict) or filter_spec.get("missing_policy") is None:
                filters.append(filter_spec)
                continue
            normalized_filter = dict(filter_spec)
            policy = normalized_filter.pop("missing_policy")
            filters.append(normalized_filter)
            key = (
                normalized_filter.get("resource"),
                normalized_filter.get("field"),
                "predicate",
            )
            if key not in requirement_keys:
                requirements.append(
                    {
                        "resource": key[0],
                        "field": key[1],
                        "policy": policy,
                        "usage": key[2],
                    }
                )
                requirement_keys.add(key)
        canonical["filters"] = filters
        canonical["missing_data_requirements"] = requirements
        return canonical


class VerifiedQuestionDraft(VerifiedQuestion):
    """Lenient model output; missing-data plumbing is replaced by the host."""

    missing_data_requirements: list[Any] = Field(default_factory=list)


class AnswerabilityResult(BaseModel):
    outcome: Literal["ANSWERABLE", "CLARIFICATION_REQUIRED", "OUT_OF_SCOPE"]
    reason: str
    verified_question: VerifiedQuestion | None = None
    clarification_question: str | None = None

    @model_validator(mode="after")
    def verified_question_only_when_answerable(self) -> AnswerabilityResult:
        if self.outcome == "ANSWERABLE" and not self.verified_question:
            raise ValueError("ANSWERABLE requires verified_question")
        if self.outcome != "ANSWERABLE" and self.verified_question is not None:
            raise ValueError("Only ANSWERABLE may contain verified_question")
        if self.outcome == "CLARIFICATION_REQUIRED" and not self.clarification_question:
            raise ValueError("CLARIFICATION_REQUIRED requires clarification_question")
        if self.outcome != "CLARIFICATION_REQUIRED" and self.clarification_question is not None:
            raise ValueError("Only CLARIFICATION_REQUIRED may contain clarification_question")
        return self


class AnswerabilityResultDraft(AnswerabilityResult):
    verified_question: VerifiedQuestionDraft | None = None


class PlannedReadFilter(ClosedModel):
    resource: ResourceName
    field: CanonicalField
    value: Scalar
    verified_filter_ids: list[NonEmptyString] = Field(
        default_factory=list,
        description="Host-assigned verified-filter ids this Register pushdown implements.",
    )

    @model_validator(mode="after")
    def require_owned_field(self) -> PlannedReadFilter:
        _require_owned_field(self.resource, self.field)
        return self


class PlannedReadFilters(RootModel[list[PlannedReadFilter]]):
    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(item.field for item in self.root)

    def __len__(self) -> int:
        return len(self.root)

    def __getitem__(self, field: str) -> Scalar:
        try:
            return next(item.value for item in self.root if item.field == field)
        except StopIteration as exc:
            raise KeyError(field) from exc

    def __setitem__(self, field: str, value: Scalar) -> None:
        for item in self.root:
            if item.field == field:
                item.value = value
                return
        raise KeyError(field)

    def __delitem__(self, field: str) -> None:
        for index, item in enumerate(self.root):
            if item.field == field:
                del self.root[index]
                return
        raise KeyError(field)

    def get(self, field: str, default: Any = None) -> Any:
        try:
            return self[field]
        except (KeyError, StopIteration):
            return default

    def keys(self):
        return {item.field for item in self.root}

    def values(self):
        return [item.value for item in self.root]

    def items(self):
        return [(item.field, item.value) for item in self.root]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return dict(self.items()) == other
        return super().__eq__(other)


class PlannedRead(BaseModel):
    name: NonEmptyString
    resource: ResourceName
    q: str | None = Field(default=None, min_length=1, max_length=300)
    filters: PlannedReadFilters = Field(default_factory=lambda: PlannedReadFilters(root=[]))

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_filter_map(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("filters"), dict):
            return value
        resource = value.get("resource")
        return {
            **value,
            "filters": [
                {"resource": resource, "field": field, "value": item}
                for field, item in value["filters"].items()
            ],
        }

    @model_validator(mode="after")
    def require_filter_resource(self) -> PlannedRead:
        mismatched = [item.field for item in self.filters.root if item.resource != self.resource]
        if mismatched:
            raise ValueError(
                f"Read '{self.name}' filters must use resource '{self.resource}': " + ", ".join(mismatched)
            )
        return self


class EmptyArguments(MappingModel):
    pass


class FilterArguments(MappingModel):
    field: NonEmptyString = Field(
        description="Field on the input dataset; derived and joined field names are allowed."
    )
    operator: Literal[
        "eq",
        "ne",
        "in",
        "not_in",
        "is_null",
        "not_null",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "icontains",
        "older_than_days",
        "newer_than_days",
    ] = Field(default="eq", description="Executable predicate operator; in/not_in use values, not value.")
    value: Scalar | None = Field(default=None, description="Single predicate value for scalar operators.")
    values: list[Scalar] | None = Field(
        default=None, description="Predicate values used only by in and not_in."
    )
    days: int | None = Field(default=None, ge=0)
    as_of: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="ISO calendar date used as the deterministic ageing reference date.",
    )
    missing_policy: Literal["report_unassessable"] | None = Field(
        default=None,
        description="Preserve and count null input values before applying this predicate.",
    )
    verified_filter_ids: list[NonEmptyString] = Field(
        default_factory=list,
        description="Host-assigned verified-filter ids this operation structurally implements.",
    )

    @field_validator("as_of", mode="before")
    @classmethod
    def require_iso_as_of(cls, value: Any) -> Any:
        if value is None:
            return value
        try:
            parsed = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != value:
            raise ValueError("ageing filter as_of must be an ISO date in YYYY-MM-DD form")
        return value

    @model_validator(mode="after")
    def require_filter_shape(self) -> FilterArguments:
        if self.operator in {"in", "not_in"}:
            if self.values is None or self.value is not None:
                raise ValueError(f"Filter operator '{self.operator}' requires values only")
        elif self.operator in {"is_null", "not_null"}:
            if self.value is not None or self.values is not None:
                raise ValueError(f"Filter operator '{self.operator}' accepts no value")
        elif self.operator in {"older_than_days", "newer_than_days"}:
            if self.days is None:
                raise ValueError(f"Filter operator '{self.operator}' requires days")
        elif self.values is not None:
            raise ValueError(f"Filter operator '{self.operator}' accepts one value, not values")
        return self


class JoinArguments(MappingModel):
    right: NonEmptyString = Field(description="Previously produced dataset joined to the input dataset.")
    left_key: NonEmptyString = Field(description="Existing field on the input dataset.")
    right_key: NonEmptyString = Field(description="Existing field on the right dataset.")
    how: Literal["left", "inner"] = Field(
        default="left",
        description=(
            "Use left for optional display enrichment; inner only for required relationship " "existence."
        ),
    )
    relationship_required: bool = Field(
        default=False,
        description="Must be true when how is inner and the verified question requires the relationship.",
    )
    right_prefix: NonEmptyString = Field(
        default="right_", description="Flat prefix added to every field from the right dataset."
    )


class NamedOutputArguments(MappingModel):
    as_: NonEmptyString | None = Field(default=None, alias="as")


class FieldAggregateArguments(NamedOutputArguments):
    field: NonEmptyString


class DistinctCountArguments(NamedOutputArguments):
    fields: list[NonEmptyString] = Field(min_length=1)
    null_keys: Literal["exclude", "include"] = "exclude"


class MissingCountArguments(NamedOutputArguments):
    field: NonEmptyString


class CountAggregation(MappingModel):
    operation: Literal["count"]
    as_: NonEmptyString | None = Field(default=None, alias="as")


class FieldAggregation(MappingModel):
    operation: Literal["sum", "average", "min", "max", "missing_count"]
    field: NonEmptyString
    as_: NonEmptyString | None = Field(default=None, alias="as")


class DistinctAggregation(MappingModel):
    operation: Literal["distinct_count"]
    field: NonEmptyString
    null_keys: Literal["exclude", "include"] = "exclude"
    as_: NonEmptyString | None = Field(default=None, alias="as")


GroupAggregation: TypeAlias = (  # noqa: UP040
    CountAggregation | FieldAggregation | DistinctAggregation
)


class GroupArguments(MappingModel):
    by: list[NonEmptyString]
    aggregations: list[GroupAggregation]


class ProjectArguments(MappingModel):
    fields: list[NonEmptyString]


class SortArguments(MappingModel):
    field: NonEmptyString
    descending: bool = False


class LimitArguments(MappingModel):
    count: int = Field(ge=0)


class SetArguments(MappingModel):
    others: list[NonEmptyString] = Field(min_length=1)
    key_fields: list[NonEmptyString] = Field(min_length=1)
    null_keys: Literal["exclude", "include"] = "exclude"


class RankArguments(MappingModel):
    field: NonEmptyString
    direction: Literal["top", "bottom"] = "top"
    count: int = Field(default=1, ge=1)


class OperationBase(ClosedModel):
    name: NonEmptyString = Field(description="Unique human-readable step name.")
    input: NonEmptyString = Field(description="Name of a read or earlier operation dataset.")
    output: NonEmptyString = Field(description="Unique dataset name produced by this operation.")


class FilterOperation(OperationBase):
    operation: Literal["filter"]
    arguments: FilterArguments


class JoinOperation(OperationBase):
    operation: Literal["join"]
    arguments: JoinArguments


class CountOperation(OperationBase):
    operation: Literal["count"]
    arguments: NamedOutputArguments = Field(default_factory=NamedOutputArguments)


class SumOperation(OperationBase):
    operation: Literal["sum"]
    arguments: FieldAggregateArguments


class AverageOperation(OperationBase):
    operation: Literal["average"]
    arguments: FieldAggregateArguments


class MinOperation(OperationBase):
    operation: Literal["min"]
    arguments: FieldAggregateArguments


class MaxOperation(OperationBase):
    operation: Literal["max"]
    arguments: FieldAggregateArguments


class DistinctCountOperation(OperationBase):
    operation: Literal["distinct_count"]
    arguments: DistinctCountArguments


class MissingCountOperation(OperationBase):
    operation: Literal["missing_count"]
    arguments: MissingCountArguments


class GroupOperation(OperationBase):
    operation: Literal["group"]
    arguments: GroupArguments


class ProjectOperation(OperationBase):
    operation: Literal["project"]
    arguments: ProjectArguments


class SortOperation(OperationBase):
    operation: Literal["sort"]
    arguments: SortArguments


class LimitOperation(OperationBase):
    operation: Literal["limit"]
    arguments: LimitArguments


class SetUnionOperation(OperationBase):
    operation: Literal["set_union"]
    arguments: SetArguments


class SetIntersectionOperation(OperationBase):
    operation: Literal["set_intersection"]
    arguments: SetArguments


class RankOperation(OperationBase):
    operation: Literal["rank"]
    arguments: RankArguments


PlannedOperation: TypeAlias = (  # noqa: UP040
    FilterOperation
    | JoinOperation
    | CountOperation
    | SumOperation
    | DistinctCountOperation
    | AverageOperation
    | MinOperation
    | MaxOperation
    | MissingCountOperation
    | GroupOperation
    | ProjectOperation
    | SortOperation
    | LimitOperation
    | SetUnionOperation
    | SetIntersectionOperation
    | RankOperation
)


class QueryPlan(BaseModel):
    reads: list[PlannedRead]
    operations: list[PlannedOperation]
    result_names: list[str] = Field(default_factory=list)
    result_shapes: dict[str, Literal["scalar", "rows", "grouped_rows"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_result_shapes(self) -> QueryPlan:
        if not self.result_names:
            if self.operations:
                self.result_names = [self.operations[-1].output]
            elif self.reads:
                self.result_names = [self.reads[-1].name]

        producers = {step.output: step for step in self.operations}
        read_names = {read.name for read in self.reads}

        def structural_shape(
            dataset: str,
            seen: frozenset[str] = frozenset(),
        ) -> Literal["scalar", "rows", "grouped_rows"]:
            if dataset in seen:
                raise ValueError(f"Cyclic result-shape lineage for '{dataset}'")
            if dataset in read_names:
                return "rows"
            step = producers.get(dataset)
            if step is None:
                return "rows"
            if step.operation in {
                "count",
                "distinct_count",
                "sum",
                "average",
                "min",
                "max",
                "missing_count",
            }:
                return "scalar"
            if step.operation == "group":
                return "grouped_rows"
            if step.operation in {"filter", "project", "sort", "limit", "rank"}:
                return structural_shape(step.input, seen | {dataset})
            return "rows"

        expected = {name: structural_shape(name) for name in self.result_names}
        self.result_shapes = expected
        return self


class QueryPlanDraft(ClosedModel):
    """Model-authored plan; result shapes are derived by the host after validation."""

    # Lenient provider responses may include a result_shapes map. It is excluded from
    # the draft schema and ignored because the host derives it after validation.
    model_config = ConfigDict(extra="ignore")

    reads: list[PlannedRead]
    operations: list[PlannedOperation]
    result_names: list[NonEmptyString] = Field(default_factory=list)
    _validated_plan: QueryPlan | None = PrivateAttr(default=None)

    @property
    def validated_plan(self) -> QueryPlan:
        if self._validated_plan is None:
            raise ValueError("Query plan draft has not been host-validated")
        return self._validated_plan


class AuthorizedQualitativeRecord(BaseModel):
    evidence_ref: str
    resource: str
    record_id: str
    source_field: str
    source_text: str | None = None
    missing: bool = False
    truncated: bool = False

    @model_validator(mode="after")
    def require_text_or_missing(self) -> AuthorizedQualitativeRecord:
        if self.missing == (self.source_text is not None):
            raise ValueError("Authorized qualitative records require text or missing=true")
        return self


class QualitativeCoverage(BaseModel):
    total_records: int = Field(ge=0)
    assessed_records: int = Field(ge=0)
    text_records: int = Field(ge=0)
    missing_records: int = Field(ge=0)
    completeness: Literal["COMPLETE", "PARTIAL"]
    limitation: str | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> QualitativeCoverage:
        if self.assessed_records > self.total_records:
            raise ValueError("Assessed qualitative records cannot exceed total records")
        if self.text_records + self.missing_records != self.assessed_records:
            raise ValueError("Qualitative text and missing counts must equal assessed records")
        if self.completeness == "PARTIAL" and not self.limitation:
            raise ValueError("Partial qualitative coverage requires a limitation")
        return self


class QualitativeCorpus(BaseModel):
    records: list[AuthorizedQualitativeRecord]
    coverage: QualitativeCoverage
    authorized_cohort_row_count: int = Field(ge=0)


class QualitativeSupport(BaseModel):
    evidence_ref: str
    source_field: str
    excerpt: str = Field(min_length=1)


class QualitativeFindingDraft(BaseModel):
    label: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    supports: list[QualitativeSupport] = Field(min_length=1)
    conflicts: list[QualitativeSupport] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class QualitativeAnalysisDraft(BaseModel):
    findings: list[QualitativeFindingDraft] = Field(default_factory=list)


class ValidatedQualitativeFinding(QualitativeFindingDraft):
    supporting_record_ids: list[str]
    support_count: int = Field(ge=1)


class QualitativeAnalysisResult(BaseModel):
    findings: list[ValidatedQualitativeFinding] = Field(default_factory=list)
    coverage: QualitativeCoverage


class QualitativeStageOutput(BaseModel):
    """Safe orchestration result; the authorized corpus stays private to the pipeline."""

    analysis: QualitativeAnalysisResult
    coverage: QualitativeCoverage
    _corpus: QualitativeCorpus = PrivateAttr()

    @classmethod
    def from_run(
        cls, corpus: QualitativeCorpus, analysis: QualitativeAnalysisResult
    ) -> QualitativeStageOutput:
        output = cls(analysis=analysis, coverage=corpus.coverage)
        output._corpus = corpus
        return output

    @property
    def corpus(self) -> QualitativeCorpus:
        return self._corpus


class RowPresentation(BaseModel):
    total_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def counts_are_consistent(self) -> RowPresentation:
        if self.displayed_count > self.total_count:
            raise ValueError("Displayed row count cannot exceed total row count")
        if self.truncated != (self.displayed_count < self.total_count):
            raise ValueError("Row truncation must match displayed and total counts")
        return self


class ResultEvidence(BaseModel):
    verified_question: VerifiedQuestion
    facts: dict[str, Any]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_presentation: RowPresentation
    contributing_ids: list[str] = Field(default_factory=list)
    contributing_records: list[dict[str, str]] = Field(default_factory=list)
    completeness: Completeness
    retrieval_windows: list[dict[str, Any]] = Field(default_factory=list)
    metric_completeness: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    scope: str
    retrieved_at: datetime
    caveats: list[str] = Field(default_factory=list)
    qualitative_findings: list[ValidatedQualitativeFinding] = Field(default_factory=list)
    qualitative_coverage: QualitativeCoverage | None = None
    unavailable_obligations: list[EvidenceObligation] = Field(default_factory=list)

    @model_validator(mode="after")
    def row_presentation_matches_evidence(self) -> ResultEvidence:
        if self.row_presentation.total_count != len(self.rows):
            raise ValueError("Row presentation total must equal the evidence row count")
        return self


class AnswerGenerationResult(BaseModel):
    answer: str = Field(min_length=1)
