from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.operations import LINEAGE_FIELD, OperationError, execute_operation


def test_filter_count_and_decimal_sum_are_question_agnostic():
    rows = [
        {"id": "1", "stage": "Sanctioned", "amount_cr": 0.1, LINEAGE_FIELD: ["1"]},
        {"id": "2", "stage": "Disbursed", "amount_cr": 0.2, LINEAGE_FIELD: ["2"]},
        {"id": "3", "stage": "Rejected", "amount_cr": 99, LINEAGE_FIELD: ["3"]},
    ]
    selected = execute_operation(
        "filter",
        rows,
        {"field": "stage", "operator": "in", "values": ["Sanctioned", "Disbursed"]},
        {"lending": rows},
    )
    count = execute_operation("count", selected, {"as": "facilities"}, {})
    total = execute_operation("sum", selected, {"field": "amount_cr", "as": "amount_cr"}, {})

    assert count == [{"facilities": 2, LINEAGE_FIELD: ["1", "2"]}]
    assert total == [
        {
            "amount_cr": "0.3",
            "assessed_count": 2,
            "missing_count": 0,
            LINEAGE_FIELD: ["1", "2"],
        }
    ]


def test_group_and_join_preserve_contributing_record_lineage():
    leads = [
        {"rm": "CM", LINEAGE_FIELD: ["lead-1"]},
        {"rm": "CM", LINEAGE_FIELD: ["lead-2"]},
        {"rm": "SD", LINEAGE_FIELD: ["lead-3"]},
    ]
    grouped = execute_operation(
        "group",
        leads,
        {"by": ["rm"], "aggregations": [{"operation": "count", "as": "count"}]},
        {},
    )
    assert grouped[0] == {"rm": "CM", "count": 2, LINEAGE_FIELD: ["lead-1", "lead-2"]}

    lending = [{"entity_id": "e1", LINEAGE_FIELD: ["facility-1"]}]
    entities = [{"id": "e1", "name": "Acme", LINEAGE_FIELD: ["entity-1"]}]
    joined = execute_operation(
        "join",
        lending,
        {
            "right": "entities",
            "left_key": "entity_id",
            "right_key": "id",
            "right_prefix": "entity_",
            "how": "inner",
            "relationship_required": True,
        },
        {"entities": entities},
    )
    assert joined == [
        {
            "entity_id": "e1",
            "entity_name": "Acme",
            LINEAGE_FIELD: ["entity-1", "facility-1"],
        }
    ]


def test_group_keeps_solar_separate_from_solar_general():
    rows = [
        {"sector": "Solar", LINEAGE_FIELD: ["solar"]},
        {"sector": "Solar - General", LINEAGE_FIELD: ["general"]},
    ]

    grouped = execute_operation(
        "group",
        rows,
        {"by": ["sector"], "aggregations": [{"operation": "count", "as": "count"}]},
        {},
    )

    assert {(row["sector"], row["count"]) for row in grouped} == {("Solar", 1), ("Solar - General", 1)}


def test_left_join_preserves_rows_without_optional_relationships():
    facilities = [
        {"id": "f1", "deal_id": "d1", LINEAGE_FIELD: ["f1"]},
        {"id": "f2", "deal_id": None, LINEAGE_FIELD: ["f2"]},
    ]
    deals = [{"id": "d1", "code": "ACME", LINEAGE_FIELD: ["d1"]}]

    joined = execute_operation(
        "join",
        facilities,
        {
            "right": "deals",
            "left_key": "deal_id",
            "right_key": "id",
            "right_prefix": "related_",
        },
        {"deals": deals},
    )

    assert joined == [
        {
            "id": "f1",
            "deal_id": "d1",
            "related_code": "ACME",
            "related_id": "d1",
            LINEAGE_FIELD: ["d1", "f1"],
        },
        {
            "id": "f2",
            "deal_id": None,
            "related_code": None,
            "related_id": None,
            LINEAGE_FIELD: ["f2"],
        },
    ]


def test_threshold_null_and_ageing_filters_are_deterministic():
    rows = [
        {"id": "1", "value_cr": 250, "touched": "2026-07-01", LINEAGE_FIELD: ["1"]},
        {"id": "2", "value_cr": 251, "touched": "2026-08-10", LINEAGE_FIELD: ["2"]},
        {"id": "3", "value_cr": None, "touched": None, LINEAGE_FIELD: ["3"]},
    ]
    above = execute_operation("filter", rows, {"field": "value_cr", "operator": "gt", "value": 250}, {})
    assert [row["id"] for row in above] == ["2"]
    missing = execute_operation("filter", rows, {"field": "value_cr", "operator": "is_null"}, {})
    assert [row["id"] for row in missing] == ["3"]
    stale = execute_operation(
        "filter",
        rows,
        {
            "field": "touched",
            "operator": "older_than_days",
            "days": 21,
            "as_of": "2026-08-15",
        },
        {},
    )
    assert [row["id"] for row in stale] == ["1"]


def test_ageing_filter_rejects_non_iso_as_of_with_operation_error():
    with pytest.raises(OperationError, match="as_of must be an ISO date"):
        execute_operation(
            "filter",
            [{"touched": "2026-07-01"}],
            {
                "field": "touched",
                "operator": "older_than_days",
                "days": 21,
                "as_of": "14 August",
            },
            {},
        )


def test_ageing_filter_uses_current_date_when_as_of_is_omitted():
    old_date = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()

    result = execute_operation(
        "filter",
        [{"id": "old", "touched": old_date}],
        {"field": "touched", "operator": "older_than_days", "days": 21},
        {},
    )

    assert result == [{"id": "old", "touched": old_date}]


def test_numeric_sort_orders_decimal_strings_and_keeps_nulls_last():
    rows = [
        {"id": "ten", "amount": "10", LINEAGE_FIELD: ["ten"]},
        {"id": "negative", "amount": "-2.5", LINEAGE_FIELD: ["negative"]},
        {"id": "two", "amount": "2.05", LINEAGE_FIELD: ["two"]},
        {"id": "missing", "amount": None, LINEAGE_FIELD: ["missing"]},
    ]

    ascending = execute_operation("sort", rows, {"field": "amount"}, {})
    descending = execute_operation("sort", rows, {"field": "amount", "descending": True}, {})

    assert [row["id"] for row in ascending] == ["negative", "two", "ten", "missing"]
    assert [row["id"] for row in descending] == ["ten", "two", "negative", "missing"]


def test_string_sort_remains_lexical():
    rows = [{"value": "b"}, {"value": "10a"}, {"value": "2a"}]

    result = execute_operation("sort", rows, {"field": "value"}, {})

    assert [row["value"] for row in result] == ["10a", "2a", "b"]


def test_distinct_count_has_explicit_null_policy_and_combined_lineage():
    rows = [
        {"entity_id": "e1", "book": "lending", LINEAGE_FIELD: ["l1"]},
        {"entity_id": "e1", "book": "syndication", LINEAGE_FIELD: ["s1"]},
        {"entity_id": None, "book": "lending", LINEAGE_FIELD: ["l2"]},
    ]

    excluded = execute_operation(
        "distinct_count", rows, {"fields": ["entity_id"], "null_keys": "exclude"}, {}
    )
    included = execute_operation(
        "distinct_count", rows, {"fields": ["entity_id"], "null_keys": "include"}, {}
    )

    assert excluded == [{"distinct_count": 1, LINEAGE_FIELD: ["l1", "s1"]}]
    assert included == [{"distinct_count": 2, LINEAGE_FIELD: ["l1", "l2", "s1"]}]


def test_numeric_aggregates_report_assessed_and_missing_without_fabricating_zero():
    rows = [
        {"amount": "1.25", LINEAGE_FIELD: ["1"]},
        {"amount": None, LINEAGE_FIELD: ["2"]},
        {"amount": "2.75", LINEAGE_FIELD: ["3"]},
    ]

    average = execute_operation("average", rows, {"field": "amount"}, {})
    minimum = execute_operation("min", rows, {"field": "amount"}, {})
    maximum = execute_operation("max", rows, {"field": "amount"}, {})
    all_null = execute_operation("average", [{"amount": None, LINEAGE_FIELD: ["4"]}], {"field": "amount"}, {})

    assert average == [
        {
            "average_amount": "2.00",
            "assessed_count": 2,
            "missing_count": 1,
            LINEAGE_FIELD: ["1", "2", "3"],
        }
    ]
    assert minimum[0]["min_amount"] == "1.25"
    assert maximum[0]["max_amount"] == "2.75"
    assert all_null == [
        {
            "average_amount": None,
            "assessed_count": 0,
            "missing_count": 1,
            LINEAGE_FIELD: ["4"],
        }
    ]


def test_missing_count_does_not_treat_empty_string_as_null():
    rows = [
        {"amount": None, LINEAGE_FIELD: ["1"]},
        {"amount": "", LINEAGE_FIELD: ["2"]},
        {"amount": "0", LINEAGE_FIELD: ["3"]},
    ]

    result = execute_operation("missing_count", rows, {"field": "amount"}, {})

    assert result == [{"missing_amount": 1, "assessed_count": 2, LINEAGE_FIELD: ["1", "2", "3"]}]


def test_set_operations_use_declared_identity_order_and_combined_lineage():
    lending = [
        {"entity_id": "e2", "name": "Two", LINEAGE_FIELD: ["l2"]},
        {"entity_id": "e1", "name": "One", LINEAGE_FIELD: ["l1"]},
        {"entity_id": None, "name": "Unknown", LINEAGE_FIELD: ["lx"]},
    ]
    syndication = [
        {"entity_id": "e1", "name": "One duplicate", LINEAGE_FIELD: ["s1"]},
        {"entity_id": "e3", "name": "Three", LINEAGE_FIELD: ["s3"]},
    ]
    datasets = {"syndication": syndication}

    union = execute_operation(
        "set_union",
        lending,
        {"others": ["syndication"], "key_fields": ["entity_id"]},
        datasets,
    )
    intersection = execute_operation(
        "set_intersection",
        lending,
        {"others": ["syndication"], "key_fields": ["entity_id"]},
        datasets,
    )

    assert [row["entity_id"] for row in union] == ["e2", "e1", "e3"]
    assert union[1][LINEAGE_FIELD] == ["l1", "s1"]
    assert intersection == [{"entity_id": "e1", LINEAGE_FIELD: ["l1", "s1"]}]


def test_rank_preserves_boundary_ties_and_excludes_null_metrics():
    rows = [
        {"lender": "A", "approvals": "5", LINEAGE_FIELD: ["a"]},
        {"lender": "B", "approvals": "3", LINEAGE_FIELD: ["b"]},
        {"lender": "C", "approvals": "3", LINEAGE_FIELD: ["c"]},
        {"lender": "D", "approvals": None, LINEAGE_FIELD: ["d"]},
        {"lender": "E", "approvals": "1", LINEAGE_FIELD: ["e"]},
    ]

    top = execute_operation("rank", rows, {"field": "approvals", "count": 2, "direction": "top"}, {})
    bottom = execute_operation("rank", rows, {"field": "approvals", "count": 2, "direction": "bottom"}, {})

    assert [row["lender"] for row in top] == ["A", "B", "C"]
    assert [row["lender"] for row in bottom] == ["E", "B", "C"]


def test_group_supports_new_aggregates_and_retains_group_lineage():
    rows = [
        {"line": "A", "entity_id": "e1", "amount": "1", LINEAGE_FIELD: ["1"]},
        {"line": "A", "entity_id": "e1", "amount": None, LINEAGE_FIELD: ["2"]},
        {"line": "A", "entity_id": "e2", "amount": "3", LINEAGE_FIELD: ["3"]},
    ]

    result = execute_operation(
        "group",
        rows,
        {
            "by": ["line"],
            "aggregations": [
                {"operation": "distinct_count", "field": "entity_id", "as": "companies"},
                {"operation": "sum", "field": "amount", "as": "total_amount"},
                {"operation": "average", "field": "amount", "as": "average_amount"},
                {"operation": "missing_count", "field": "amount", "as": "missing_amounts"},
            ],
        },
        {},
    )

    assert result == [
        {
            "line": "A",
            "companies": 2,
            "total_amount": "4",
            "total_amount_assessed_count": 2,
            "total_amount_missing_count": 1,
            "average_amount": "2",
            "average_amount_assessed_count": 2,
            "average_amount_missing_count": 1,
            "missing_amounts": 1,
            LINEAGE_FIELD: ["1", "2", "3"],
        }
    ]


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("distinct_count", {"fields": ["entity_id"], "null_keys": "guess"}, "null_keys"),
        ("set_union", {"others": [], "key_fields": ["entity_id"]}, "others"),
        ("rank", {"field": "score", "count": 0}, "positive integer"),
    ],
)
def test_new_operations_reject_invalid_arguments(operation, arguments, message):
    with pytest.raises(OperationError, match=message):
        execute_operation(operation, [{"entity_id": "e1", "score": 1}], arguments, {})
