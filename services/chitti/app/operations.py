"""Question-agnostic, Decimal-safe dispatch for executed Register datasets."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


class OperationError(ValueError):
    pass


Rows = list[dict[str, Any]]
Operation = Callable[[Rows, dict[str, Any], dict[str, Rows]], Rows]
LINEAGE_FIELD = "__chitti_ids"


def _field(row: dict[str, Any], name: str) -> Any:
    if name not in row:
        raise OperationError(f"Unknown field '{name}'.")
    return row[name]


def _filter(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    field = str(args.get("field") or "")
    operator = str(args.get("operator") or "eq")
    if operator not in {
        "eq", "ne", "in", "not_in", "is_null", "not_null", "gt", "gte", "lt", "lte",
        "contains", "icontains", "older_than_days", "newer_than_days",
    }:
        raise OperationError(f"Unsupported filter operator '{operator}'.")
    values = args.get("values")
    expected = args.get("value")

    def keep(row: dict[str, Any]) -> bool:
        actual = _field(row, field)
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "in":
            return actual in (values if isinstance(values, list) else [])
        if operator == "not_in":
            return actual not in (values if isinstance(values, list) else [])
        if operator == "is_null":
            return actual is None
        if operator == "not_null":
            return actual is not None
        if operator in {"contains", "icontains"}:
            if actual is None or expected is None:
                return False
            left, right = str(actual), str(expected)
            if operator == "icontains":
                left, right = left.casefold(), right.casefold()
            return right in left
        if operator in {"older_than_days", "newer_than_days"}:
            if actual is None:
                return False
            days = args.get("days")
            if not isinstance(days, int) or days < 0:
                raise OperationError(f"{operator} requires a non-negative integer days value.")
            as_of_raw = args.get("as_of")
            try:
                as_of = (
                    date.fromisoformat(str(as_of_raw))
                    if as_of_raw
                    else datetime.now(UTC).date()
                )
            except ValueError as exc:
                raise OperationError("Ageing filter as_of must be an ISO date.") from exc
            try:
                actual_date = date.fromisoformat(str(actual)[:10])
            except ValueError as exc:
                raise OperationError(f"Field '{field}' contains a non-date value.") from exc
            cutoff = as_of - timedelta(days=days)
            return actual_date < cutoff if operator == "older_than_days" else actual_date > cutoff
        if actual is None or expected is None:
            return False
        comparison = _ordered_values(actual, expected, field)
        if operator == "gt":
            return comparison[0] > comparison[1]
        if operator == "gte":
            return comparison[0] >= comparison[1]
        if operator == "lt":
            return comparison[0] < comparison[1]
        if operator == "lte":
            return comparison[0] <= comparison[1]
        raise OperationError(f"Unsupported filter operator '{operator}'.")

    return [row for row in rows if keep(row)]


def _ordered_values(actual: Any, expected: Any, field: str) -> tuple[Any, Any]:
    try:
        return Decimal(str(actual)), Decimal(str(expected))
    except InvalidOperation:
        pass
    if isinstance(actual, str) and isinstance(expected, str):
        return actual, expected
    raise OperationError(f"Field '{field}' cannot be compared to the supplied value.")


def _join(rows: Rows, args: dict[str, Any], datasets: dict[str, Rows]) -> Rows:
    right_name = str(args.get("right") or "")
    if right_name not in datasets:
        raise OperationError(f"Unknown join dataset '{right_name}'.")
    left_key = str(args.get("left_key") or "")
    right_key = str(args.get("right_key") or "")
    prefix = str(args.get("right_prefix") or "right_")
    how = str(args.get("how") or "left")
    if how not in {"inner", "left"}:
        raise OperationError(f"Unsupported join type '{how}'.")
    index: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    right_fields: set[str] = set()
    for right in datasets[right_name]:
        index[_field(right, right_key)].append(right)
        right_fields.update(key for key in right if key != LINEAGE_FIELD)
    joined: Rows = []
    for left in rows:
        matches = index.get(_field(left, left_key), [])
        if how == "left" and not matches:
            combined = dict(left)
            for key in right_fields:
                combined[f"{prefix}{key}"] = None
            joined.append(combined)
            continue
        for right in matches:
            combined = dict(left)
            for key, value in right.items():
                if key == LINEAGE_FIELD:
                    continue
                combined[f"{prefix}{key}"] = value
            combined[LINEAGE_FIELD] = sorted(set(
                list(left.get(LINEAGE_FIELD) or []) + list(right.get(LINEAGE_FIELD) or [])
            ))
            joined.append(combined)
    return joined


def _count(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    return [{
        str(args.get("as") or "count"): len(rows),
        LINEAGE_FIELD: _lineage(rows),
    }]


def _decimal(value: Any, field: str) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OperationError(f"Field '{field}' contains a non-numeric value.") from exc


def _numeric_values(rows: Rows, field: str) -> tuple[list[Decimal], int]:
    values: list[Decimal] = []
    missing = 0
    for row in rows:
        value = _field(row, field)
        if value is None:
            missing += 1
        else:
            values.append(_decimal(value, field))
    return values, missing


def _sum(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    field = str(args.get("field") or "")
    values, missing = _numeric_values(rows, field)
    total = sum(values, Decimal(0))
    return [{
        str(args.get("as") or f"sum_{field}"): str(total),
        "assessed_count": len(values),
        "missing_count": missing,
        LINEAGE_FIELD: _lineage(rows),
    }]


def _distinct_count(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    fields = args.get("fields")
    if not isinstance(fields, list) or not fields:
        raise OperationError("distinct_count requires a non-empty fields list.")
    null_keys = str(args.get("null_keys") or "exclude")
    if null_keys not in {"exclude", "include"}:
        raise OperationError("distinct_count null_keys must be 'exclude' or 'include'.")
    contributing = [
        row for row in rows
        if null_keys == "include"
        or all(_field(row, str(field)) is not None for field in fields)
    ]
    keys = {
        tuple(_field(row, str(field)) for field in fields)
        for row in contributing
    }
    return [{
        str(args.get("as") or "distinct_count"): len(keys),
        LINEAGE_FIELD: _lineage(contributing),
    }]


def _numeric_aggregate(
    rows: Rows, args: dict[str, Any], operation: str
) -> Rows:
    field = str(args.get("field") or "")
    values, missing = _numeric_values(rows, field)
    result: Decimal | None
    if not values:
        result = None
    elif operation == "average":
        result = sum(values, Decimal(0)) / Decimal(len(values))
    elif operation == "min":
        result = min(values)
    elif operation == "max":
        result = max(values)
    else:  # pragma: no cover - fixed dispatch only
        raise OperationError(f"Unsupported numeric aggregate '{operation}'.")
    return [{
        str(args.get("as") or f"{operation}_{field}"): None if result is None else str(result),
        "assessed_count": len(values),
        "missing_count": missing,
        LINEAGE_FIELD: _lineage(rows),
    }]


def _average(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    return _numeric_aggregate(rows, args, "average")


def _minimum(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    return _numeric_aggregate(rows, args, "min")


def _maximum(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    return _numeric_aggregate(rows, args, "max")


def _missing_count(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    field = str(args.get("field") or "")
    missing = sum(_field(row, field) is None for row in rows)
    return [{
        str(args.get("as") or f"missing_{field}"): missing,
        "assessed_count": len(rows) - missing,
        LINEAGE_FIELD: _lineage(rows),
    }]


def _group(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    by = args.get("by")
    aggregations = args.get("aggregations")
    if not isinstance(by, list) or not isinstance(aggregations, list):
        raise OperationError("group requires by and aggregations lists.")
    groups: dict[tuple[Any, ...], Rows] = defaultdict(list)
    for row in rows:
        groups[tuple(_field(row, str(field)) for field in by)].append(row)
    output: Rows = []
    for key, members in groups.items():
        item = {str(field): value for field, value in zip(by, key, strict=True)}
        item[LINEAGE_FIELD] = _lineage(members)
        for aggregation in aggregations:
            if not isinstance(aggregation, dict):
                raise OperationError("Each aggregation must be an object.")
            operation = str(aggregation.get("operation") or "")
            name = str(aggregation.get("as") or operation)
            if operation == "count":
                item[name] = len(members)
            elif operation == "distinct_count":
                field = str(aggregation.get("field") or "")
                null_keys = str(aggregation.get("null_keys") or "exclude")
                if null_keys not in {"exclude", "include"}:
                    raise OperationError(
                        "group distinct_count null_keys must be 'exclude' or 'include'."
                    )
                values = [_field(member, field) for member in members]
                item[name] = len({
                    value for value in values
                    if null_keys == "include" or value is not None
                })
            elif operation == "sum":
                field = str(aggregation.get("field") or "")
                values, missing = _numeric_values(members, field)
                item[name] = str(sum(values, Decimal(0)))
                item[f"{name}_assessed_count"] = len(values)
                item[f"{name}_missing_count"] = missing
            elif operation in {"average", "min", "max"}:
                field = str(aggregation.get("field") or "")
                values, missing = _numeric_values(members, field)
                if not values:
                    result = None
                elif operation == "average":
                    result = sum(values, Decimal(0)) / Decimal(len(values))
                elif operation == "min":
                    result = min(values)
                else:
                    result = max(values)
                item[name] = None if result is None else str(result)
                item[f"{name}_assessed_count"] = len(values)
                item[f"{name}_missing_count"] = missing
            elif operation == "missing_count":
                field = str(aggregation.get("field") or "")
                item[name] = sum(_field(member, field) is None for member in members)
            else:
                raise OperationError(f"Unsupported aggregation '{operation}'.")
        output.append(item)
    return output


def _project(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    fields = args.get("fields")
    if not isinstance(fields, list):
        raise OperationError("project requires a fields list.")
    return [{
        **{str(field): _field(row, str(field)) for field in fields},
        LINEAGE_FIELD: list(row.get(LINEAGE_FIELD) or []),
    } for row in rows]


def _sort(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    field = str(args.get("field") or "")
    populated = [row for row in rows if _field(row, field) is not None]
    missing = [row for row in rows if _field(row, field) is None]
    numeric = True
    for row in populated:
        try:
            Decimal(str(_field(row, field)))
        except (InvalidOperation, ValueError):
            numeric = False
            break
    key = (
        (lambda row: Decimal(str(_field(row, field))))
        if numeric
        else (lambda row: _field(row, field))
    )
    return [
        *sorted(populated, key=key, reverse=bool(args.get("descending", False))),
        *missing,
    ]


def _set_operation(
    rows: Rows, args: dict[str, Any], datasets: dict[str, Rows], *, intersection: bool
) -> Rows:
    others = args.get("others")
    key_fields = args.get("key_fields")
    if not isinstance(others, list) or not others:
        raise OperationError("set operations require a non-empty others list.")
    if not isinstance(key_fields, list) or not key_fields:
        raise OperationError("set operations require a non-empty key_fields list.")
    null_keys = str(args.get("null_keys") or "exclude")
    if null_keys not in {"exclude", "include"}:
        raise OperationError("set operation null_keys must be 'exclude' or 'include'.")
    collections = [rows]
    for name in others:
        if str(name) not in datasets:
            raise OperationError(f"Unknown set dataset '{name}'.")
        collections.append(datasets[str(name)])

    def key(row: dict[str, Any]) -> tuple[Any, ...] | None:
        value = tuple(_field(row, str(field)) for field in key_fields)
        return value if null_keys == "include" or all(item is not None for item in value) else None

    indexes: list[dict[tuple[Any, ...], Rows]] = []
    for collection in collections:
        index: dict[tuple[Any, ...], Rows] = defaultdict(list)
        for row in collection:
            row_key = key(row)
            if row_key is not None:
                index[row_key].append(row)
        indexes.append(index)
    selected_keys = set(indexes[0])
    for index in indexes[1:]:
        selected_keys = (
            selected_keys & set(index) if intersection else selected_keys | set(index)
        )
    output: Rows = []
    seen: set[tuple[Any, ...]] = set()
    for collection in collections:
        for row in collection:
            row_key = key(row)
            if row_key is None or row_key in seen or row_key not in selected_keys:
                continue
            contributors = [
                member
                for index in indexes
                for member in index.get(row_key, [])
            ]
            output.append({
                **{str(field): _field(row, str(field)) for field in key_fields},
                LINEAGE_FIELD: _lineage(contributors),
            })
            seen.add(row_key)
    return output


def _set_union(rows: Rows, args: dict[str, Any], datasets: dict[str, Rows]) -> Rows:
    return _set_operation(rows, args, datasets, intersection=False)


def _set_intersection(rows: Rows, args: dict[str, Any], datasets: dict[str, Rows]) -> Rows:
    return _set_operation(rows, args, datasets, intersection=True)


def _rank(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    field = str(args.get("field") or "")
    count = args.get("count", 1)
    direction = str(args.get("direction") or "top")
    if not isinstance(count, int) or count < 1:
        raise OperationError("rank count must be a positive integer.")
    if direction not in {"top", "bottom"}:
        raise OperationError("rank direction must be 'top' or 'bottom'.")
    populated = [row for row in rows if _field(row, field) is not None]
    if not populated:
        return []
    numeric = True
    for row in populated:
        try:
            Decimal(str(_field(row, field)))
        except (InvalidOperation, ValueError):
            numeric = False
            break
    value = (
        (lambda row: Decimal(str(_field(row, field))))
        if numeric
        else (lambda row: _field(row, field))
    )
    ordered = sorted(populated, key=value, reverse=direction == "top")
    boundary = value(ordered[min(count, len(ordered)) - 1])
    if direction == "top":
        return [row for row in ordered if value(row) >= boundary]
    return [row for row in ordered if value(row) <= boundary]


def _limit(rows: Rows, args: dict[str, Any], _: dict[str, Rows]) -> Rows:
    count = args.get("count")
    if not isinstance(count, int) or count < 0:
        raise OperationError("limit count must be a non-negative integer.")
    return rows[:count]


OPERATIONS: dict[str, Operation] = {
    "filter": _filter,
    "join": _join,
    "count": _count,
    "sum": _sum,
    "distinct_count": _distinct_count,
    "average": _average,
    "min": _minimum,
    "max": _maximum,
    "missing_count": _missing_count,
    "group": _group,
    "project": _project,
    "sort": _sort,
    "limit": _limit,
    "set_union": _set_union,
    "set_intersection": _set_intersection,
    "rank": _rank,
}


def _lineage(rows: Rows) -> list[str]:
    return sorted({
        str(record_id)
        for row in rows
        for record_id in (row.get(LINEAGE_FIELD) or [])
    })


def execute_operation(
    operation: str, rows: Rows, arguments: dict[str, Any], datasets: dict[str, Rows]
) -> Rows:
    implementation = OPERATIONS.get(operation)
    if implementation is None:
        raise OperationError(f"Unknown operation '{operation}'.")
    return implementation(rows, arguments, datasets)
