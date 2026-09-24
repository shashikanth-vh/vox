"""Validated execution of model-planned names against fixed read/operation maps."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.contracts import PRESENTATION_OPERATORS
from app.evidence import Completeness, RegisterRead
from app.identity import CallerIdentity
from app.operations import LINEAGE_FIELD, OPERATIONS, OperationError, Rows, execute_operation
from app.register_access import (
    CONTROLLED_REFERENCE_FIELDS,
    GOVERNED_COMPOSITE_FIELDS,
    REGISTER_JOIN_RELATIONSHIPS,
    RESOURCE_FIELDS,
    RESOURCE_SPECS,
    RegisterAccess,
    RegisterPlanError,
    canonicalize_controlled_filter_arguments,
    controlled_value_issue_count,
)
from app.stage_models import QueryPlan


@dataclass(slots=True)
class ExecutionResult:
    datasets: dict[str, Rows]
    result_names: list[str]
    result_shapes: dict[str, str]
    contributing_ids: list[str]
    completeness: Completeness
    windows: list[dict[str, Any]]
    contributing_records: list[dict[str, str]] | None = None
    metric_completeness: dict[str, dict[str, dict[str, int]]] | None = None
    cohort_rows: dict[str, list[dict[str, Any]]] | None = None
    result_field_sources: dict[str, dict[str, list[dict[str, str]]]] | None = None
    controlled_value_issues: dict[str, dict[str, int]] | None = None


class PlanExecutor:
    def __init__(self, settings: Settings, access: RegisterAccess | None = None) -> None:
        self.settings = settings
        self.access = access or RegisterAccess(settings)

    async def execute(
        self,
        plan: QueryPlan,
        *,
        identity: CallerIdentity,
        request_id: str,
        reference_values: dict[str, list[Any]] | None = None,
    ) -> ExecutionResult:
        self.validate_plan(plan)
        requests = [
            RegisterRead(resource=read.resource, q=read.q, filters=dict(read.filters.items()))
            for read in plan.reads
        ]
        # Validate the whole read set before scheduling any concurrent request. A single
        # invented resource/filter therefore yields zero Register calls.
        for request in requests:
            self.access.validate_read(request)
        datasets: dict[str, Rows] = {}
        field_sources: dict[str, dict[str, set[tuple[str, str]]]] = {}
        quality_by_name: dict[str, dict[str, dict[str, int]]] = {}
        evidence_by_name: dict[str, Any] = {}
        pending = list(plan.reads)
        while pending:
            ready = [
                read for read in pending if _read_dependencies(dict(read.filters.items())) <= datasets.keys()
            ]
            if not ready:
                names = ", ".join(read.name for read in pending)
                raise RegisterPlanError(f"Unresolvable dependent Register reads: {names}.")
            resolved = [
                RegisterRead(
                    resource=read.resource,
                    q=read.q,
                    filters={
                        field: _resolve_reference(value, datasets) for field, value in read.filters.items()
                    },
                )
                for read in ready
            ]
            batch = await asyncio.gather(
                *(
                    self.access.read(
                        request,
                        identity=identity,
                        request_id=request_id,
                        reference_values=reference_values,
                    )
                    for request in resolved
                )
            )
            for read, result in zip(ready, batch, strict=True):
                datasets[read.name] = [
                    {
                        **record.fields,
                        LINEAGE_FIELD: [f"{read.resource}:{record.record_id}"],
                    }
                    for record in result.records
                ]
                field_sources[read.name] = {
                    field: {(read.resource, field)} for field in RESOURCE_FIELDS[read.resource]
                }
                quality_by_name[read.name] = {}
                evidence_by_name[read.name] = result
                pending.remove(read)
        evidence = [evidence_by_name[read.name] for read in plan.reads]
        materialization_limit = (
            self.settings.max_records_per_request * self.settings.max_resources_per_request
        )
        for step in plan.operations:
            input_rows = datasets[step.input]
            arguments = _resolve_joined_field_names(
                step.operation,
                dict(step.arguments),
                set(field_sources[step.input]),
                {name: set(sources) for name, sources in field_sources.items()},
            )
            if step.operation == "filter":
                field = str(arguments.get("field") or "")
                controlled_sources = {
                    (resource, source_field)
                    for resource, source_field in field_sources[step.input].get(field, set())
                    if source_field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                }
                if len(controlled_sources) == 1:
                    resource, source_field = next(iter(controlled_sources))
                    arguments = canonicalize_controlled_filter_arguments(
                        resource,
                        source_field,
                        arguments,
                        reference_values,
                    )
            datasets[step.output] = execute_operation(step.operation, input_rows, arguments, datasets)
            if len(datasets[step.output]) > materialization_limit:
                raise OperationError(
                    f"Operation '{step.name}' exceeded the bounded materialization limit "
                    f"of {materialization_limit} rows."
                )
            quality_by_name[step.output] = {
                **quality_by_name[step.input],
                **_operation_completeness(step.operation, input_rows, arguments, datasets),
            }
            field_sets = {name: set(sources) for name, sources in field_sources.items()}
            output_fields = self._operation_fields(
                step.operation, field_sets[step.input], arguments, field_sets
            )
            field_sources[step.output] = self._operation_field_sources(
                step.operation,
                field_sources[step.input],
                arguments,
                field_sources,
                output_fields,
            )
        result_names = plan.result_names or (
            [plan.operations[-1].output] if plan.operations else [plan.reads[-1].name]
        )
        completeness = (
            Completeness.COMPLETE
            if all(item.window.completeness == Completeness.COMPLETE for item in evidence)
            else Completeness.PARTIAL_LIMIT
        )
        contributing_tokens = sorted(
            {
                str(record_token)
                for name in result_names
                for row in datasets[name]
                for record_token in (row.get(LINEAGE_FIELD) or [])
            }
        )
        contributing_records = [
            {"resource": token.split(":", 1)[0], "id": token.split(":", 1)[1]}
            for token in contributing_tokens
            if ":" in token
        ]
        contributing_ids = sorted(
            {token.split(":", 1)[1] if ":" in token else token for token in contributing_tokens}
        )
        metric_completeness = {name: quality_by_name[name] for name in result_names if quality_by_name[name]}
        cohort_rows = {
            name: [
                {
                    "fields": {key: value for key, value in row.items() if key != LINEAGE_FIELD},
                    "lineage": list(row.get(LINEAGE_FIELD) or []),
                }
                for row in datasets[name]
            ]
            for name in result_names
        }
        result_field_sources = {
            name: {
                field: [
                    {"resource": resource, "field": source_field}
                    for resource, source_field in sorted(origins)
                ]
                for field, origins in field_sources[name].items()
            }
            for name in result_names
        }
        final_controlled_issues: dict[str, dict[str, int]] = {}
        for name in result_names:
            issues: dict[str, int] = {}
            for result_field, origins in field_sources[name].items():
                for resource, source_field in origins:
                    count = controlled_value_issue_count(
                        resource,
                        source_field,
                        [row.get(result_field) for row in datasets[name]],
                        reference_values or {},
                    )
                    if count:
                        issues[result_field] = max(issues.get(result_field, 0), count)
            if issues:
                final_controlled_issues[name] = issues
        for rows in datasets.values():
            for row in rows:
                row.pop(LINEAGE_FIELD, None)
        return ExecutionResult(
            datasets=datasets,
            result_names=result_names,
            result_shapes={name: plan.result_shapes[name] for name in result_names},
            contributing_ids=contributing_ids,
            completeness=completeness,
            windows=[
                {
                    **item.window.model_dump(mode="json"),
                    "read_name": read.name,
                }
                for read, item in zip(plan.reads, evidence, strict=True)
            ],
            contributing_records=contributing_records,
            metric_completeness=metric_completeness,
            cohort_rows=cohort_rows,
            result_field_sources=result_field_sources,
            controlled_value_issues=final_controlled_issues,
        )

    def validate_plan(self, plan: QueryPlan) -> None:
        if len(plan.reads) > self.settings.max_resources_per_request:
            raise RegisterPlanError("Plan exceeds the configured Register resource limit.")
        if not plan.reads:
            raise RegisterPlanError("Plan must declare at least one Register read.")
        if len({read.name for read in plan.reads}) != len(plan.reads):
            raise RegisterPlanError("Register read names must be unique.")
        available = {read.name for read in plan.reads}
        fields: dict[str, set[str]] = {}
        field_sources: dict[str, dict[str, set[tuple[str, str]]]] = {}
        non_left_join_origins: dict[str, set[tuple[str, str]]] = {}
        report_missing_resources: set[str] = set()
        read_names = {read.name for read in plan.reads}
        read_positions = {read.name: index for index, read in enumerate(plan.reads)}
        for index, read in enumerate(plan.reads):
            resource_fields = RESOURCE_FIELDS.get(read.resource)
            if resource_fields is None:
                raise RegisterPlanError(f"Unknown Register resource '{read.resource}'.")
            supported_filters = RESOURCE_SPECS[read.resource].equality_filters
            unsupported_filters = sorted(set(read.filters) - supported_filters)
            if unsupported_filters:
                non_pushdown = sorted(set(unsupported_filters) & resource_fields)
                invented = sorted(set(unsupported_filters) - resource_fields)
                details = []
                if non_pushdown:
                    details.append(
                        "valid field(s) must use a post-read filter operation: " + ", ".join(non_pushdown)
                    )
                if invented:
                    details.append("unknown or semantic pseudo-field(s): " + ", ".join(invented))
                raise RegisterPlanError(
                    f"Unsupported filter placement for '{read.resource}': " + "; ".join(details)
                )
            if read.q and "$" in read.q:
                raise RegisterPlanError("Register q searches cannot contain result references.")
            for value in read.filters.values():
                reference = _reference(value)
                if reference is None:
                    continue
                source, field = reference
                if source not in read_names:
                    raise RegisterPlanError(f"Unknown read dependency '{source}'.")
                if read_positions[source] >= index:
                    raise RegisterPlanError(f"Read '{read.name}' may reference only an earlier named read.")
                source_read = next(item for item in plan.reads if item.name == source)
                source_fields = RESOURCE_FIELDS.get(source_read.resource, frozenset())
                if field not in source_fields:
                    raise RegisterPlanError(f"Unknown referenced field '{field}' on read '{source}'.")
            fields[read.name] = set(resource_fields)
            field_sources[read.name] = {field: {(read.resource, field)} for field in resource_fields}
            non_left_join_origins[read.name] = set()
        for step in plan.operations:
            if step.operation not in OPERATIONS:
                raise RegisterPlanError(f"Unknown operation '{step.operation}'.")
            if step.output in available:
                raise RegisterPlanError(f"Duplicate dataset name '{step.output}'.")
            if step.input not in available:
                raise RegisterPlanError(f"Unknown operation input '{step.input}'.")
            if step.operation == "filter" and step.arguments.get("operator") in PRESENTATION_OPERATORS:
                raise RegisterPlanError(
                    "Unsupported presentation operator "
                    f"'{step.arguments.get('operator')}' reached the executor; "
                    "preserve separation as display evidence instead of an executable filter."
                )
            if step.operation == "join":
                right = str(step.arguments.get("right") or "")
                if right not in available:
                    raise RegisterPlanError(f"Unknown join dataset '{right}'.")
                if step.arguments.get("how", "left") == "inner":
                    right_resources = {
                        resource for origins in field_sources[right].values() for resource, _ in origins
                    }
                    if report_missing_resources & right_resources:
                        raise RegisterPlanError(
                            "Missing-value fields must use a left join when the join supplies "
                            "their nullable resource."
                        )
            resolved_arguments = _resolve_joined_field_names(
                step.operation, dict(step.arguments), fields[step.input], fields
            )
            if step.operation == "filter" and resolved_arguments.get("operator") in {"contains", "icontains"}:
                field = str(resolved_arguments.get("field") or "")
                governed_origins = sorted(
                    (resource, source_field)
                    for resource, source_field in field_sources[step.input].get(field, set())
                    if source_field in CONTROLLED_REFERENCE_FIELDS.get(resource, {})
                    or (resource, source_field) in GOVERNED_COMPOSITE_FIELDS
                )
                if governed_origins:
                    rendered = ", ".join(
                        f"{resource}.{source_field}" for resource, source_field in governed_origins
                    )
                    raise RegisterPlanError(
                        "Substring predicates are unsafe for governed or composite field(s): "
                        f"{rendered}. Use exact authoritative values or report the requested "
                        "meaning as unavailable."
                    )
            try:
                output_fields = self._operation_fields(
                    step.operation, fields[step.input], resolved_arguments, fields
                )
            except RegisterPlanError as exc:
                available_fields = ", ".join(sorted(fields[step.input]))
                unknown_match = re.search(r"Unknown field '([^']+)'", str(exc))
                suggestion = ""
                if unknown_match is not None:
                    unknown = unknown_match.group(1)
                    suffix_matches = sorted(
                        field for field in fields[step.input] if field.endswith(f"_{unknown}")
                    )
                    if len(suffix_matches) == 1:
                        suggestion = f" Use the unique joined field '{suffix_matches[0]}' instead."
                raise RegisterPlanError(
                    f"Operation '{step.name}' on input dataset '{step.input}' is invalid: "
                    f"{exc}{suggestion} Available input fields: {available_fields}."
                ) from exc
            if step.operation == "join":
                self._validate_join_relationship(
                    step.input,
                    str(step.arguments.get("right") or ""),
                    str(resolved_arguments.get("left_key") or ""),
                    str(resolved_arguments.get("right_key") or ""),
                    field_sources,
                )
            if step.operation == "filter" and step.arguments.get("missing_policy") == "report_unassessable":
                field = str(resolved_arguments.get("field") or "")
                report_missing_resources.update(
                    resource for resource, _ in field_sources[step.input].get(field, set())
                )
                if non_left_join_origins.get(step.input):
                    raise RegisterPlanError(
                        f"Missing-value field '{field}' is filtered after an inner join; "
                        "all joins on its input lineage must be left."
                    )
            fields[step.output] = output_fields
            field_sources[step.output] = self._operation_field_sources(
                step.operation,
                field_sources[step.input],
                resolved_arguments,
                field_sources,
                output_fields,
            )
            inherited_inner = set(non_left_join_origins.get(step.input, set()))
            if step.operation in {"set_union", "set_intersection"}:
                inherited_inner.update(
                    origin
                    for name in step.arguments.get("others") or []
                    for origin in non_left_join_origins.get(str(name), set())
                )
            if step.operation == "join" and str(step.arguments.get("how") or "left") == "inner":
                right = str(step.arguments.get("right") or "")
                inherited_inner.update(
                    origin for origins in field_sources[right].values() for origin in origins
                )
            non_left_join_origins[step.output] = inherited_inner
            available.add(step.output)
        result_names = plan.result_names or (
            [plan.operations[-1].output] if plan.operations else [plan.reads[-1].name]
        )
        unknown = [name for name in result_names if name not in available]
        if unknown:
            raise RegisterPlanError(f"Unknown result dataset(s): {', '.join(unknown)}.")

    @staticmethod
    def _validate_join_relationship(
        left_dataset: str,
        right_dataset: str,
        left_key: str,
        right_key: str,
        field_sources: dict[str, dict[str, set[tuple[str, str]]]],
    ) -> None:
        left_origins = field_sources[left_dataset].get(left_key, set())
        right_origins = field_sources[right_dataset].get(right_key, set())
        if len(left_origins) == 1 and left_origins == right_origins:
            return
        if any(
            frozenset({left, right}) in REGISTER_JOIN_RELATIONSHIPS
            for left in left_origins
            for right in right_origins
        ):
            return
        left = ", ".join(f"{resource}.{field}" for resource, field in sorted(left_origins)) or "derived"
        right = ", ".join(f"{resource}.{field}" for resource, field in sorted(right_origins)) or "derived"
        raise RegisterPlanError(
            "Join path is not a governed Register relationship: "
            f"{left_dataset}.{left_key} ({left}) -> "
            f"{right_dataset}.{right_key} ({right})."
        )

    @staticmethod
    def _operation_field_sources(
        operation: str,
        input_sources: dict[str, set[tuple[str, str]]],
        arguments: dict[str, Any],
        datasets: dict[str, dict[str, set[tuple[str, str]]]],
        output_fields: set[str],
    ) -> dict[str, set[tuple[str, str]]]:
        def copied(fields_to_copy: set[str]) -> dict[str, set[tuple[str, str]]]:
            return {field: set(input_sources.get(field, set())) for field in fields_to_copy}

        if operation in {"filter", "sort", "limit", "rank"}:
            return copied(output_fields)
        if operation == "project":
            return copied(output_fields)
        if operation == "join":
            output = copied(set(input_sources))
            right = str(arguments.get("right") or "")
            prefix = str(arguments.get("right_prefix") or "right_")
            for field, origins in datasets[right].items():
                output.setdefault(f"{prefix}{field}", set()).update(origins)
            return output
        if operation == "group":
            by = {str(field) for field in arguments.get("by") or []}
            return {
                field: set(input_sources.get(field, set())) if field in by else set()
                for field in output_fields
            }
        if operation in {"set_union", "set_intersection"}:
            names = [str(name) for name in arguments.get("others") or []]
            return {
                field: set(input_sources.get(field, set())).union(
                    *(datasets[name].get(field, set()) for name in names)
                )
                for field in output_fields
            }
        return {field: set() for field in output_fields}

    @staticmethod
    def _operation_fields(
        operation: str,
        input_fields: set[str],
        arguments: dict[str, Any],
        datasets: dict[str, set[str]],
    ) -> set[str]:
        def require(field: str, *, dataset_fields: set[str] = input_fields) -> None:
            if not field or field not in dataset_fields:
                raise RegisterPlanError(f"Unknown field '{field}'.")

        if operation == "filter":
            require(str(arguments.get("field") or ""))
            operator = str(arguments.get("operator") or "eq")
            allowed = {
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
            }
            if operator not in allowed:
                raise RegisterPlanError(f"Unsupported filter operator '{operator}'.")
            if operator in {"in", "not_in"} and not isinstance(arguments.get("values"), list):
                raise RegisterPlanError(f"Filter operator '{operator}' requires values list.")
            if operator in {"older_than_days", "newer_than_days"}:
                days = arguments.get("days")
                if not isinstance(days, int) or days < 0:
                    raise RegisterPlanError(
                        f"Filter operator '{operator}' requires non-negative integer days."
                    )
            return set(input_fields)
        if operation == "sort":
            require(str(arguments.get("field") or ""))
            if "descending" in arguments and not isinstance(arguments["descending"], bool):
                raise RegisterPlanError("sort descending must be boolean.")
            return set(input_fields)
        if operation == "join":
            right = str(arguments.get("right") or "")
            right_fields = datasets[right]
            how = str(arguments.get("how") or "left")
            if how not in {"inner", "left"}:
                raise RegisterPlanError(f"Unsupported join type '{how}'.")
            if how == "inner" and arguments.get("relationship_required") is not True:
                raise RegisterPlanError(
                    "inner join requires relationship_required=true in the verified plan."
                )
            require(str(arguments.get("left_key") or ""))
            require(str(arguments.get("right_key") or ""), dataset_fields=right_fields)
            prefix = str(arguments.get("right_prefix") or "right_")
            return set(input_fields) | {f"{prefix}{field}" for field in right_fields}
        if operation == "count":
            return {str(arguments.get("as") or "count")}
        if operation in {"sum", "average", "min", "max"}:
            field = str(arguments.get("field") or "")
            require(field)
            name = str(arguments.get("as") or f"{operation}_{field}")
            return {name, "assessed_count", "missing_count"}
        if operation == "missing_count":
            field = str(arguments.get("field") or "")
            require(field)
            return {str(arguments.get("as") or f"missing_{field}"), "assessed_count"}
        if operation == "distinct_count":
            distinct_fields = arguments.get("fields")
            if not isinstance(distinct_fields, list) or not distinct_fields:
                raise RegisterPlanError("distinct_count requires a non-empty fields list.")
            for field in distinct_fields:
                require(str(field))
            _validate_null_keys(arguments, "distinct_count")
            return {str(arguments.get("as") or "distinct_count")}
        if operation == "group":
            by = arguments.get("by")
            aggregations = arguments.get("aggregations")
            if not isinstance(by, list) or not isinstance(aggregations, list):
                raise RegisterPlanError("group requires by and aggregations lists.")
            output = {str(field) for field in by}
            for field in output:
                require(field)
            for aggregation in aggregations:
                if not isinstance(aggregation, dict):
                    raise RegisterPlanError("Each aggregation must be an object.")
                aggregation_name = str(aggregation.get("operation") or "")
                if aggregation_name not in {
                    "count",
                    "sum",
                    "distinct_count",
                    "average",
                    "min",
                    "max",
                    "missing_count",
                }:
                    raise RegisterPlanError(f"Unsupported aggregation '{aggregation_name}'.")
                if aggregation_name != "count":
                    require(str(aggregation.get("field") or ""))
                if aggregation_name == "distinct_count":
                    _validate_null_keys(aggregation, "group distinct_count")
                name = str(aggregation.get("as") or aggregation_name)
                output.add(name)
                if aggregation_name in {"average", "min", "max"}:
                    output.update({f"{name}_assessed_count", f"{name}_missing_count"})
            return output
        if operation == "project":
            projected = arguments.get("fields")
            if not isinstance(projected, list):
                raise RegisterPlanError("project requires a fields list.")
            output = {str(field) for field in projected}
            for field in output:
                require(field)
            return output
        if operation in {"set_union", "set_intersection"}:
            others = arguments.get("others")
            key_fields = arguments.get("key_fields")
            if not isinstance(others, list) or not others:
                raise RegisterPlanError(f"{operation} requires a non-empty others list.")
            if not isinstance(key_fields, list) or not key_fields:
                raise RegisterPlanError(f"{operation} requires a non-empty key_fields list.")
            for field in key_fields:
                require(str(field))
            for other in others:
                name = str(other)
                if name not in datasets:
                    raise RegisterPlanError(f"Unknown set dataset '{name}'.")
                for field in key_fields:
                    require(str(field), dataset_fields=datasets[name])
            _validate_null_keys(arguments, operation)
            return {str(field) for field in key_fields}
        if operation == "rank":
            require(str(arguments.get("field") or ""))
            count = arguments.get("count", 1)
            if not isinstance(count, int) or count < 1:
                raise RegisterPlanError("rank count must be a positive integer.")
            direction = str(arguments.get("direction") or "top")
            if direction not in {"top", "bottom"}:
                raise RegisterPlanError("rank direction must be 'top' or 'bottom'.")
            return set(input_fields)
        if operation == "limit":
            count = arguments.get("count", 10)
            if not isinstance(count, int) or count < 0:
                raise RegisterPlanError("limit count must be a non-negative integer.")
            return set(input_fields)
        raise RegisterPlanError(f"Unknown operation '{operation}'.")


def _validate_null_keys(arguments: dict[str, Any], operation: str) -> None:
    null_keys = str(arguments.get("null_keys") or "exclude")
    if null_keys not in {"exclude", "include"}:
        raise RegisterPlanError(f"{operation} null_keys must be 'exclude' or 'include'.")


def _operation_completeness(
    operation: str,
    rows: Rows,
    arguments: dict[str, Any],
    datasets: dict[str, Rows],
) -> dict[str, dict[str, int]]:
    fields: list[str] = []
    assessed_rows = rows
    if operation in {"sum", "average", "min", "max", "missing_count", "rank"} or (
        operation == "filter" and arguments.get("missing_policy") == "report_unassessable"
    ):
        fields = [str(arguments.get("field") or "")]
    elif operation == "distinct_count":
        fields = [str(field) for field in arguments.get("fields") or []]
    elif operation in {"set_union", "set_intersection"}:
        fields = [str(field) for field in arguments.get("key_fields") or []]
        assessed_rows = [
            *rows,
            *(row for name in arguments.get("others") or [] for row in datasets[str(name)]),
        ]
    elif operation == "group":
        fields = [
            str(aggregation.get("field") or "")
            for aggregation in arguments.get("aggregations") or []
            if isinstance(aggregation, dict) and aggregation.get("operation") != "count"
        ]
    return {
        field: {
            "assessed_count": sum(row.get(field) is not None for row in assessed_rows),
            "missing_count": sum(row.get(field) is None for row in assessed_rows),
        }
        for field in dict.fromkeys(fields)
        if field
    }


_REFERENCE = re.compile(r"^\$([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)$")


def _reference(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _REFERENCE.fullmatch(value)
    return (match.group(1), match.group(2)) if match else None


def _read_dependencies(filters: dict[str, Any]) -> set[str]:
    return {reference[0] for value in filters.values() if (reference := _reference(value)) is not None}


def _resolve_reference(value: Any, datasets: dict[str, Rows]) -> Any:
    reference = _reference(value)
    if reference is None:
        return value
    dataset_name, field = reference
    rows = datasets[dataset_name]
    if len(rows) != 1:
        raise RegisterPlanError(
            f"Read dependency '{dataset_name}' must resolve to exactly one row; got {len(rows)}."
        )
    if field not in rows[0]:
        raise RegisterPlanError(f"Read dependency '{dataset_name}' has no field '{field}'.")
    resolved = rows[0][field]
    if not isinstance(resolved, str | int | float | bool):
        raise RegisterPlanError(f"Read dependency '{dataset_name}.{field}' is not a scalar filter value.")
    return resolved


def _resolve_joined_field_names(
    operation: str,
    arguments: dict[str, Any],
    input_fields: set[str],
    datasets: dict[str, set[str]],
) -> dict[str, Any]:
    """Resolve deterministic suffix names produced by prefixed joins."""
    # Rewrite a deep copy so suffix resolution never mutates the validated plan.
    result = deepcopy(dict(arguments))

    def resolve(value: Any, available: set[str]) -> Any:
        if not isinstance(value, str) or value in available:
            return value
        matches = [field for field in available if field.endswith(f"_{value}")]
        return matches[0] if len(matches) == 1 else value

    def fields_in(items: list[Any], available: set[str]) -> None:
        for index, value in enumerate(items):
            items[index] = resolve(value, available)

    if operation in {"filter", "sort", "sum", "average", "min", "max", "missing_count", "rank"}:
        if "field" in result:
            result["field"] = resolve(result["field"], input_fields)
    elif operation == "join":
        result["left_key"] = resolve(result.get("left_key"), input_fields)
        result["right_key"] = resolve(
            result.get("right_key"),
            datasets.get(str(result.get("right") or ""), set()),
        )
    elif operation == "distinct_count":
        fields_in(result.get("fields") or [], input_fields)
    elif operation == "group":
        fields_in(result.get("by") or [], input_fields)
        for aggregation in result.get("aggregations") or []:
            if isinstance(aggregation, dict) and "field" in aggregation:
                aggregation["field"] = resolve(aggregation["field"], input_fields)
    elif operation == "project":
        fields_in(result.get("fields") or [], input_fields)
    elif operation in {"set_union", "set_intersection"}:
        for index, value in enumerate(result.get("key_fields") or []):
            candidates = {resolve(value, input_fields)}
            for name in result.get("others") or []:
                candidates.add(resolve(value, datasets.get(str(name), set())))
            if len(candidates) == 1:
                result["key_fields"][index] = candidates.pop()
    return result
