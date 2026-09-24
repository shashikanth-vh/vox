from types import SimpleNamespace

from app.evidence import Completeness
from app.pipeline import PipelineResponse
from app.presentation import public_result
from app.result_tables import result_tables


def execution(count=75):
    rows = [{"id": f"internal-{i}", "deal_no": f"DEAL-{i}", "temperature": "Hot",
             "rm": "Asha", "remarks": "Discuss funding", "entity_id": "private-entity"}
            for i in range(count)]
    sources = {key: [{"resource": "deals", "field": key}] for key in rows[0]}
    return SimpleNamespace(
        datasets={"book": rows, "hot": rows}, result_names=["hot"],
        result_shapes={"hot": "rows"}, result_field_sources={"hot": sources},
        cohort_rows={}, completeness=Completeness.COMPLETE,
    ), SimpleNamespace(reads=[SimpleNamespace(name="book", resource="deals")])


def test_all_75_records_have_business_columns_and_deal_targets():
    data, plan = execution()
    table, = result_tables(data, plan)
    assert table["total"] == len(table["rows"]) == 75
    assert table["columns"] == ["Group code", "Temperature", "RM", "Remarks"]
    assert table["rows"][-1]["deal"] == "DEAL-74"
    assert "private-entity" not in str(table)
    assert "internal-" not in str(table)


def test_projected_record_link_uses_single_validated_lineage():
    data, plan = execution(2)
    data.datasets["hot"] = [{"owner": "Asha"}]
    data.result_field_sources["hot"] = {"owner": [{"resource": "deals", "field": "rm"}]}
    data.cohort_rows = {"hot": [{"lineage": ["deals:internal-1"]}]}
    assert result_tables(data, plan)[0]["rows"][0]["deal"] == "DEAL-1"
    data.cohort_rows["hot"][0]["lineage"].append("deals:internal-0")
    assert "deal" not in result_tables(data, plan)[0]["rows"][0]


def test_aliases_cannot_expose_ids_and_links_cannot_be_urls():
    data, plan = execution(1)
    data.datasets["hot"][0].update({"deal_no": "javascript:alert(1)", "friendly": "private-id"})
    data.result_field_sources["hot"]["friendly"] = [{"resource": "deals", "field": "entity_id"}]
    table, = result_tables(data, plan)
    assert "private-id" not in str(table)
    assert "deal" not in table["rows"][0]


def test_scalar_results_do_not_expand_contributing_records_into_a_table():
    data, plan = execution()
    data.result_shapes["hot"] = "scalar"
    assert result_tables(data, plan) == []


def test_duplicate_group_code_columns_are_merged():
    data, plan = execution(2)
    for row in data.datasets["hot"]:
        row["code"] = row["deal_no"]
    data.result_field_sources["hot"]["code"] = [{"resource": "deals", "field": "code"}]
    data.result_field_sources["hot"] = dict(reversed(list(data.result_field_sources["hot"].items())))
    assert result_tables(data, plan)[0]["columns"].count("Group code") == 1
    assert result_tables(data, plan)[0]["columns"][0] == "Group code"


def test_response_budget_counts_escaped_unicode_and_reports_omitted_records():
    data, plan = execution(2000)
    for row in data.datasets["hot"]:
        row["remarks"] = "अ" * 300
    table, = result_tables(data, plan)
    assert table["total"] == 2000
    assert 75 < len(table["rows"]) < 2000
    import json
    assert len(json.dumps(table)) < 650_000


def test_only_successful_public_response_includes_record_tables():
    data, plan = execution()
    result = PipelineResponse(content="Internal", public_content="75 hot deals.",
        metadata={"outcome": "ANSWERED", "completeness": "COMPLETE"},
        public_tables=result_tables(data, plan))
    assert len(public_result(result).metadata["tables"][0]["rows"]) == 75
    result.metadata["outcome"] = "FAILED"
    assert "tables" not in public_result(result).metadata
