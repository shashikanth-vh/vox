from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from evam_backend_core.internal_token import verify_internal_context
from evam_register_client.models import Page
from pydantic import ValidationError

from app.config import Settings
from app.evidence import (
    CanonicalRecord,
    Completeness,
    RegisterEvidence,
    RegisterRead,
    RetrievalWindow,
)
from app.executor import PlanExecutor, _resolve_joined_field_names
from app.identity import CallerIdentity
from app.operations import OperationError
from app.register_access import (
    CONTROLLED_COMPATIBILITY_FIELDS,
    CONTROLLED_READ_STRATEGIES,
    CONTROLLED_REFERENCE_FIELDS,
    LEGACY_REFERENCE_ALIASES,
    LEGACY_STATE_NAMES,
    LOCAL_CONTROLLED_CATEGORIES,
    RESOURCE_SPECS,
    STATE_ABBREVIATIONS,
    RegisterAccess,
    RegisterPlanError,
    _canonical_reference_value,
    _controlled_values_match,
    canonical_controlled_value,
    canonicalize_controlled_filter_arguments,
    controlled_field_values_match,
)
from app.stage_models import QueryPlan

SECRET = "test-signing-secret-is-long-enough"


def _settings(**overrides) -> Settings:
    values = {
        "pipeline_enabled": True,
        "register_api_key": "svc-key",
        "internal_signing_secret": SECRET,
        "require_delegation": True,
        "llm_base_url": "https://llm.test/v1",
        "llm_api_key": "llm-key",
        "conversation_model": "conversation-model",
        "interpretation_model": "interpretation-model",
        "grounding_model": "grounding-model",
        "answerability_model": "answerability-model",
        "planning_model": "planning-model",
        "qualitative_model": "qualitative-model",
        "answer_model": "answer-model",
        "dense_model": "dense",
        "dense_model_revision": "dense-revision",
        "sparse_model": "sparse",
        "sparse_model_revision": "sparse-revision",
        "rerank_model": "rerank",
        "rerank_model_revision": "rerank-revision",
        "page_size": 2,
        "max_pages_per_resource": 3,
        "max_records_per_request": 10,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


IDENTITY = CallerIdentity(
    tenant="EVAM",
    email="admin@evamfinance.com",
    user_id="admin",
    roles=("Admin",),
    effective_views={"leads": "FULL"},
)


class FakeClient:
    configs = []
    calls = []

    def __init__(self, *, config):
        self.config = config
        self.configs.append(config)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def list(self, resource, *, limit, cursor, request_id, **filters):
        self.calls.append((resource, limit, cursor, request_id, filters))
        if cursor is None:
            return Page(
                items=[{"id": "1", "status": "Active"}, {"id": "2", "status": "Active"}],
                count=2,
                next_cursor="next",
            )
        return Page(items=[{"id": "3", "status": "Active"}], count=1)

    async def ref(self, *, request_id):
        return {"Sector": ["Solar - General", "Solar - EPC", "EV Mobility"]}


@pytest.fixture(autouse=True)
def _clear_fake():
    FakeClient.configs.clear()
    FakeClient.calls.clear()


async def test_bounded_pages_remint_exact_context_and_propagate_request_id(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="leads", filters={"status": "Active"}),
        identity=IDENTITY,
        request_id="request-123",
    )

    assert [record.record_id for record in evidence.records] == ["1", "2", "3"]
    assert evidence.window.completeness is Completeness.COMPLETE
    assert evidence.window.pages_retrieved == 2
    assert all(call[3] == "request-123" for call in FakeClient.calls)
    assert all(call[4].get("status") == "Active" for call in FakeClient.calls)
    assert len(FakeClient.configs) == 3  # reference vocabulary plus two paged reads
    for config in FakeClient.configs[1:]:
        token = config.extra_headers["X-Internal-Context"]
        verified = verify_internal_context(token, verify_key=SECRET)
        assert (verified.method, verified.path) == ("GET", "/v1/leads")


class ExactTemperatureClient(FakeClient):
    async def list(self, resource, *, limit, cursor, request_id, **filters):
        self.calls.append((resource, limit, cursor, request_id, filters))
        assert resource == "deals"
        assert filters == {"q": None, "temperature": "Hot"}
        return Page(items=[{"id": "deal-hot", "temperature": "Hot"}], count=1)


async def test_canonical_hot_filter_remains_selective_and_returns_the_hot_cohort(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", ExactTemperatureClient)

    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="deals", filters={"temperature": "Hot"}),
        identity=IDENTITY,
        request_id="canonical-hot",
        reference_values={"Temperature": ["Hot", "Warm", "Cold"]},
    )

    assert [record.record_id for record in evidence.records] == ["deal-hot"]
    assert evidence.window.completeness is Completeness.COMPLETE
    assert ExactTemperatureClient.calls[0][4] == {"q": None, "temperature": "Hot"}


def test_controlled_catalog_and_read_strategies_cover_only_approved_fields():
    assert CONTROLLED_REFERENCE_FIELDS["deals"]["temperature"] == "Temperature"
    assert CONTROLLED_READ_STRATEGIES[("deals", "temperature")] == "exact"
    assert CONTROLLED_READ_STRATEGIES[("entities", "lifecycle")] == "local"
    assert {
        ("entities", "sector"),
        ("entities", "state"),
        ("leads", "sector"),
        ("asset_monetisation", "state"),
        ("lending", "stage"),
        ("syndication_lenders", "status"),
    } == CONTROLLED_COMPATIBILITY_FIELDS
    assert "source" not in CONTROLLED_REFERENCE_FIELDS["leads"]
    assert "pending_with" not in CONTROLLED_REFERENCE_FIELDS["lending"]
    assert "pending_with" not in CONTROLLED_REFERENCE_FIELDS["syndication"]
    assert "counterparties" not in CONTROLLED_REFERENCE_FIELDS
    assert CONTROLLED_REFERENCE_FIELDS["syndication_lenders"]["status"] == "Lender Status"
    assert "nature" not in CONTROLLED_REFERENCE_FIELDS["asset_monetisation"]
    assert "sub_sector" not in CONTROLLED_REFERENCE_FIELDS["entities"]


def test_every_register_backed_catalog_category_exists_in_reference_fixture():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "reference-values.json").read_text()
    )
    required = {
        category
        for fields in CONTROLLED_REFERENCE_FIELDS.values()
        for category in fields.values()
        if category not in LOCAL_CONTROLLED_CATEGORIES
    }

    assert required <= fixture.keys()
    assert all(fixture[category] for category in required)
    assert "State" not in fixture


def test_mechanical_controlled_canonicalization_is_unique_and_resource_scoped():
    references = {
        "Temperature": ["Hot", "Warm", "Cold"],
        "Lending Stage": ["Ready for Disbursement"],
    }
    assert canonical_controlled_value("deals", "temperature", " HOT ", references) == "Hot"
    assert (
        canonical_controlled_value(
            "lending", "stage", "ready   for disbursement", references
        )
        == "Ready for Disbursement"
    )
    assert (
        canonical_controlled_value(
            "lending", "stage", "Disbursement Pending", references
        )
        == "Ready for Disbursement"
    )
    assert canonicalize_controlled_filter_arguments(
        "lending",
        "stage",
        {"operator": "eq", "value": "Disbursement Pending"},
        references,
    ) == {
        "operator": "in",
        "values": ["Ready for Disbursement", "Disbursement Pending"],
    }
    assert canonical_controlled_value("deals", "temperature", "Scorching", references) is None
    assert (
        canonical_controlled_value(
            "deals", "temperature", "hot", {"Temperature": ["Hot", " HOT "]}
        )
        is None
    )


async def test_record_limit_marks_partial(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    evidence = await RegisterAccess(_settings(max_records_per_request=2)).read(
        RegisterRead(resource="leads"), identity=IDENTITY, request_id="bounded"
    )
    assert len(evidence.records) == 2
    assert evidence.window.completeness is Completeness.PARTIAL_LIMIT
    assert evidence.window.next_cursor_present is True


class LegacyControlledValueClient(FakeClient):
    async def ref(self, *, request_id):
        self.calls.append(("ref", None, None, request_id, {}))
        return {
            "Sector": [
                {"value": "Solar - General", "label": "Solar - General"},
                {"value": "Solar - EPC", "label": "Solar - EPC"},
                {"value": "EV Mobility", "label": "EV Mobility"},
            ]
        }

    async def list(self, resource, *, limit, cursor, request_id, **filters):
        assert resource == "entities"
        assert "sector" not in filters
        self.calls.append((resource, limit, cursor, request_id, filters))
        return Page(
            items=[
                {"id": "legacy-ev", "legal_name": "Legacy Mobility", "sector": "EV"},
                {"id": "current-ev", "legal_name": "Current Mobility", "sector": "EV Mobility"},
                {"id": "solar", "legal_name": "Ambiguous Solar", "sector": "Solar"},
                {
                    "id": "renewables-solar",
                    "legal_name": "Legacy Renewable",
                    "sector": "Renewables - Solar",
                },
            ],
            count=3,
        )


async def test_controlled_filter_resolves_unique_legacy_prefix_at_read_boundary(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", LegacyControlledValueClient)

    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="entities", filters={"sector": "EV Mobility"}),
        identity=IDENTITY,
        request_id="legacy-controlled-value",
    )

    assert [record.record_id for record in evidence.records] == ["legacy-ev", "current-ev"]
    assert {record.fields["sector"] for record in evidence.records} == {"EV Mobility"}
    assert evidence.window.completeness is Completeness.COMPLETE


async def test_legacy_sector_family_matches_each_current_family_filter(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", LegacyControlledValueClient)

    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="entities", filters={"sector": "Solar - General"}),
        identity=IDENTITY,
        request_id="ambiguous-controlled-value",
    )

    assert [record.record_id for record in evidence.records] == [
        "solar",
        "renewables-solar",
    ]
    assert {record.fields["sector"] for record in evidence.records} == {"Solar", "Renewables - Solar"}


async def test_unfiltered_read_preserves_ambiguous_legacy_values(
    monkeypatch,
):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", LegacyControlledValueClient)

    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="entities"),
        identity=IDENTITY,
        request_id="unfiltered-legacy-controlled-value",
    )

    by_id = {record.record_id: record.fields["sector"] for record in evidence.records}
    assert by_id == {
        "legacy-ev": "EV Mobility",
        "current-ev": "EV Mobility",
        "solar": "Solar",
        "renewables-solar": "Renewables - Solar",
    }
    assert any(call[0] == "ref" for call in LegacyControlledValueClient.calls)


class LegacyStateClient(FakeClient):
    async def ref(self, *, request_id):
        return {"Sector": [], "State": [{"value": "MH"}, {"value": "GJ"}]}

    async def list(self, resource, *, limit, cursor, request_id, **filters):
        assert resource == "entities"
        assert "state" not in filters
        return Page(
            items=[
                {"id": "mh-code", "legal_name": "Maharashtra Code", "state": "MH"},
                {"id": "mh-name", "legal_name": "Maharashtra Name", "state": "Maharashtra"},
                {"id": "gj-code", "legal_name": "Gujarat Code", "state": "GJ"},
                {"id": "missing", "legal_name": "Missing State", "state": None},
            ],
            count=4,
        )


async def test_every_state_name_maps_to_its_primary_abbreviation():
    assert len(STATE_ABBREVIATIONS) == 36
    for state, abbreviations in STATE_ABBREVIATIONS.items():
        assert _canonical_reference_value(state, [{"value": abbreviations[0]}], "State") == abbreviations[0]
        for abbreviation in abbreviations:
            expected = abbreviations[0] if abbreviation == abbreviations[0] else abbreviation
            assert (
                _canonical_reference_value(abbreviation, [{"value": abbreviations[0]}], "State") == expected
            )


def test_every_state_alias_has_symmetric_controlled_field_provenance() -> None:
    for state, abbreviations in STATE_ABBREVIATIONS.items():
        for abbreviation in abbreviations:
            assert controlled_field_values_match("entities", "state", state, abbreviation)
            assert controlled_field_values_match("entities", "state", abbreviation, state)
            assert controlled_field_values_match("asset_monetisation", "state", state, abbreviation)
        for established in abbreviations:
            for requested in abbreviations:
                assert controlled_field_values_match("entities", "state", established, requested)


def test_legacy_state_names_share_their_declared_code_family() -> None:
    for legacy, abbreviations in LEGACY_STATE_NAMES.items():
        for abbreviation in abbreviations:
            assert controlled_field_values_match("entities", "state", legacy, abbreviation)
            assert controlled_field_values_match("entities", "state", abbreviation, legacy)


def test_controlled_field_provenance_rejects_unrelated_state() -> None:
    assert not controlled_field_values_match("entities", "state", "MH", "Gujarat")


def test_controlled_field_provenance_rejects_unknown_field_as_programmer_error() -> None:
    with pytest.raises(ValueError, match="entities.unknown is not a controlled reference field"):
        controlled_field_values_match("entities", "unknown", "MH", "Maharashtra")


def test_all_declared_legacy_sector_values_match_their_current_values():
    aliases = LEGACY_REFERENCE_ALIASES["Sector"]
    for legacy, current_values in aliases.items():
        for current in current_values:
            assert _controlled_values_match(
                legacy,
                current,
                category="Sector",
                reference_values=[],
            )


def test_exact_caller_visible_label_wins_over_alias_key():
    assert (
        _canonical_reference_value("Solar", [{"value": "Solar"}, {"value": "Solar - General"}], "Sector")
        == "Solar"
    )


@pytest.mark.asyncio
async def test_deferred_normalization_rejects_controlled_filters():
    with pytest.raises(RegisterPlanError, match="Deferred controlled normalization"):
        await RegisterAccess(_settings()).read(
            RegisterRead(resource="entities", filters={"sector": "EV Mobility"}),
            identity=IDENTITY,
            request_id="deferred-controlled-filter",
            defer_controlled_normalization=True,
        )


async def test_state_filter_matches_full_name_and_legacy_abbreviation(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", LegacyStateClient)

    evidence = await RegisterAccess(_settings()).read(
        RegisterRead(resource="entities", filters={"state": "Maharashtra"}),
        identity=IDENTITY,
        request_id="legacy-state-value",
    )

    assert [record.record_id for record in evidence.records] == ["mh-code", "mh-name"]
    assert {record.fields["state"] for record in evidence.records} == {"Maharashtra"}


async def test_unknown_resource_cannot_become_a_url(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    with pytest.raises(RegisterPlanError, match="Unknown Register resource"):
        await RegisterAccess(_settings()).read(
            RegisterRead(resource="https://attacker.invalid"),
            identity=IDENTITY,
            request_id="unknown",
        )
    assert FakeClient.configs == []


async def test_unknown_filter_is_rejected_before_a_register_client_exists(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    with pytest.raises(RegisterPlanError, match="Unsupported filter"):
        await RegisterAccess(_settings()).read(
            RegisterRead(resource="leads", filters={"attacker_url": "https://invalid"}),
            identity=IDENTITY,
            request_id="unknown-filter",
        )
    assert FakeClient.configs == []


async def test_entire_concurrent_plan_is_validated_before_any_register_call(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(
            {
                "reads": [
                    {"name": "valid", "resource": "leads", "filters": {"status": "Active"}},
                    {"name": "invented", "resource": "people", "filters": {"initials": "CM"}},
                ],
                "operations": [],
                "result_names": ["valid"],
            }
        )
    assert FakeClient.calls == []


async def test_join_dataset_must_be_declared_before_any_register_call(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "facilities", "resource": "lending"}],
            "operations": [
                {
                    "name": "attach_entities",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                    },
                }
            ],
            "result_names": ["joined"],
        }
    )
    with pytest.raises(RegisterPlanError, match="Unknown join dataset 'entities'"):
        await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="prevalidate-join")
    assert FakeClient.calls == []


async def test_inner_join_requires_explicit_relationship_semantics_before_any_read(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "deals", "resource": "deals"},
            ],
            "operations": [
                {
                    "name": "attach_deals",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "deals",
                        "left_key": "deal_id",
                        "right_key": "id",
                        "how": "inner",
                    },
                }
            ],
            "result_names": ["joined"],
        }
    )

    with pytest.raises(RegisterPlanError, match="relationship_required=true"):
        await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="prevalidate-inner")
    assert FakeClient.calls == []


async def test_plausible_but_ungoverned_join_path_is_rejected_before_reads(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "staff", "resource": "people"},
            ],
            "operations": [
                {
                    "name": "wrong_staff_identity",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "staff",
                        "left_key": "rm",
                        "right_key": "id",
                        "how": "left",
                    },
                }
            ],
        }
    )

    with pytest.raises(RegisterPlanError, match="not a governed Register relationship"):
        await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="ungoverned-join")
    assert FakeClient.calls == []


def test_governed_staff_handle_join_uses_people_name_not_person_uuid():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "staff", "resource": "people"},
            ],
            "operations": [
                {
                    "name": "staff_display",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "staff",
                        "left_key": "analyst",
                        "right_key": "name",
                        "how": "left",
                    },
                }
            ],
        }
    )

    PlanExecutor(_settings()).validate_plan(plan)


def test_identity_self_join_is_governed_only_for_one_identical_origin():
    field_sources = {
        "left": {"id": {("deals", "id")}},
        "right": {"id": {("deals", "id")}},
    }
    PlanExecutor._validate_join_relationship("left", "right", "id", "id", field_sources)


@pytest.mark.parametrize(
    "field_sources",
    [
        {"left": {"id": set()}, "right": {"id": set()}},
        {
            "left": {"id": {("deals", "id"), ("lending", "id")}},
            "right": {"id": {("deals", "id"), ("people", "id")}},
        },
    ],
)
def test_identity_join_does_not_bypass_empty_or_multi_origin_provenance(field_sources):
    with pytest.raises(RegisterPlanError, match="not a governed Register relationship"):
        PlanExecutor._validate_join_relationship("left", "right", "id", "id", field_sources)


def test_joined_field_resolution_is_structural_and_does_not_rewrite_filter_values():
    arguments = {"field": "legal_name", "operator": "eq", "value": "legal_name"}
    resolved = _resolve_joined_field_names(
        "filter", arguments, {"entity_legal_name"}, {"input": {"entity_legal_name"}}
    )
    assert resolved == {"field": "entity_legal_name", "operator": "eq", "value": "legal_name"}
    assert arguments == {"field": "legal_name", "operator": "eq", "value": "legal_name"}


def test_joined_field_resolution_handles_right_prefix_and_rejects_ambiguous_or_unknown():
    assert (
        _resolve_joined_field_names("filter", {"field": "legal_name"}, {"right_legal_name"}, {})["field"]
        == "right_legal_name"
    )
    assert (
        _resolve_joined_field_names(
            "filter",
            {"field": "legal_name"},
            {"right_legal_name", "left_legal_name"},
            {},
        )["field"]
        == "legal_name"
    )
    assert (
        _resolve_joined_field_names("filter", {"field": "missing"}, {"right_legal_name"}, {})["field"]
        == "missing"
    )


def test_joined_keys_group_fields_and_set_keys_resolve_across_datasets():
    assert _resolve_joined_field_names(
        "join",
        {"left_key": "entity_id", "right": "entities", "right_key": "id"},
        {"entity_entity_id"},
        {"entities": {"entity_id", "id"}},
    ) == {"left_key": "entity_entity_id", "right": "entities", "right_key": "id"}
    arguments = {
        "by": ["sector"],
        "aggregations": [{"operation": "sum", "field": "amount_cr", "as": "total"}],
    }
    original = copy.deepcopy(arguments)
    assert _resolve_joined_field_names("group", arguments, {"right_sector", "right_amount_cr"}, {}) == {
        "by": ["right_sector"],
        "aggregations": [{"operation": "sum", "field": "right_amount_cr", "as": "total"}],
    }
    assert arguments == original
    assert _resolve_joined_field_names(
        "set_union",
        {"key_fields": ["entity_id"], "others": ["other"]},
        {"right_entity_id"},
        {"other": {"right_entity_id"}},
    )["key_fields"] == ["right_entity_id"]
    assert _resolve_joined_field_names(
        "set_union",
        {"key_fields": ["entity_id"], "others": ["other"]},
        {"right_entity_id"},
        {"other": {"left_entity_id"}},
    )["key_fields"] == ["entity_id"]


class StaticPlanAccess:
    def __init__(self, rows_by_resource):
        self.rows_by_resource = rows_by_resource
        self.calls = []

    def validate_read(self, request):
        return RESOURCE_SPECS[request.resource]

    async def read(self, request, **_kwargs):
        self.calls.append(request)
        now = datetime.now(UTC)
        records = [
            RegisterEvidence(
                read=request,
                records=[
                    CanonicalRecord(resource=request.resource, record_id=str(row["id"]), fields=row)
                    for row in self.rows_by_resource.get(request.resource, [])
                ],
                window=RetrievalWindow(
                    resource=request.resource,
                    started_at=now,
                    completed_at=now,
                    pages_retrieved=1,
                    records_retrieved=len(self.rows_by_resource.get(request.resource, [])),
                    completeness=Completeness.COMPLETE,
                ),
            )
        ]
        return records[0]


@pytest.mark.asyncio
async def test_joined_suffix_filter_executes_and_preserves_plan_and_provenance():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "attach_entities",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "right_prefix": "entity_",
                    },
                },
                {
                    "name": "select_name",
                    "operation": "filter",
                    "input": "joined",
                    "output": "selected",
                    "arguments": {
                        "field": "legal_name",
                        "operator": "eq",
                        "value": "Acme",
                    },
                },
            ],
            "result_names": ["selected"],
        }
    )
    before = plan.model_dump(mode="json")
    result = await PlanExecutor(
        _settings(),
        access=StaticPlanAccess(
            {
                "lending": [{"id": "l1", "entity_id": "e1"}, {"id": "l2", "entity_id": "e2"}],
                "entities": [{"id": "e1", "legal_name": "Acme"}, {"id": "e2", "legal_name": "Other"}],
            }
        ),
    ).execute(plan, identity=IDENTITY, request_id="joined-suffix-execute")

    assert [row["entity_legal_name"] for row in result.datasets["selected"]] == ["Acme"]
    assert result.contributing_records == [
        {"resource": "entities", "id": "e1"},
        {"resource": "lending", "id": "l1"},
    ]
    assert result.metric_completeness == {}
    assert plan.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_plan_windows_keep_two_reads_separately_identified():
    access = StaticPlanAccess({"lending": [{"id": "l1"}], "syndication": [{"id": "s1"}]})
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "own_book", "resource": "lending"},
                {"name": "off_book", "resource": "syndication"},
            ],
            "operations": [],
            "result_names": ["own_book", "off_book"],
        }
    )

    result = await PlanExecutor(_settings(), access=access).execute(
        plan, identity=IDENTITY, request_id="scoped-read-names"
    )

    assert [window["read_name"] for window in result.windows] == ["own_book", "off_book"]


def test_plan_validation_rejects_a_genuinely_unknown_field():
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "entities", "resource": "entities"}],
            "operations": [
                {
                    "name": "bad",
                    "operation": "filter",
                    "input": "entities",
                    "output": "bad",
                    "arguments": {"field": "not_a_register_field", "operator": "eq", "value": "x"},
                }
            ],
        }
    )

    with pytest.raises(RegisterPlanError, match="Unknown field 'not_a_register_field'"):
        PlanExecutor(_settings()).validate_plan(plan)


@pytest.mark.asyncio
async def test_nested_group_uses_resolved_joined_field_and_keeps_serialized_plan():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "attach",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "right_prefix": "entity_",
                    },
                },
                {
                    "name": "by_name",
                    "operation": "group",
                    "input": "joined",
                    "output": "grouped",
                    "arguments": {
                        "by": ["legal_name"],
                        "aggregations": [{"operation": "sum", "field": "amount_cr", "as": "total"}],
                    },
                },
            ],
            "result_names": ["grouped"],
        }
    )
    before = plan.model_dump(mode="json")
    result = await PlanExecutor(
        _settings(),
        access=StaticPlanAccess(
            {
                "lending": [{"id": "l1", "entity_id": "e1", "amount_cr": 10}],
                "entities": [{"id": "e1", "legal_name": "Acme"}],
            }
        ),
    ).execute(plan, identity=IDENTITY, request_id="nested-group-resolution")

    assert result.datasets["grouped"][0]["entity_legal_name"] == "Acme"
    assert result.datasets["grouped"][0]["total"] == "10"
    assert plan.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_set_union_executes_one_resolved_key_across_all_others():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "own_book", "resource": "lending"},
                {"name": "off_book", "resource": "syndication"},
            ],
            "operations": [
                {
                    "name": "all_entities",
                    "operation": "set_union",
                    "input": "own_book",
                    "output": "all_entities",
                    "arguments": {"others": ["off_book"], "key_fields": ["entity_id"]},
                }
            ],
            "result_names": ["all_entities"],
        }
    )
    result = await PlanExecutor(
        _settings(),
        access=StaticPlanAccess(
            {
                "lending": [{"id": "l1", "entity_id": "e1"}],
                "syndication": [{"id": "s1", "entity_id": "e2"}],
            }
        ),
    ).execute(plan, identity=IDENTITY, request_id="set-key-resolution")

    assert {row["entity_id"] for row in result.datasets["all_entities"]} == {"e1", "e2"}


@pytest.mark.asyncio
async def test_dependency_failure_does_not_narrow_through_an_alias_target():
    access = StaticPlanAccess({"entities": []})
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "entity", "resource": "entities", "q": "Acme"},
                {"name": "lending", "resource": "lending", "filters": {"entity_id": "$entity.id"}},
            ],
            "operations": [],
        }
    )

    with pytest.raises(RegisterPlanError, match="exactly one row"):
        await PlanExecutor(_settings(), access=access).execute(
            plan, identity=IDENTITY, request_id="dependency-empty"
        )
    assert [request.resource for request in access.calls] == ["entities"]


def test_set_filter_list_under_value_is_rejected_by_the_typed_contract():
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(
            {
                "reads": [{"name": "asks", "resource": "syndication"}],
                "operations": [
                    {
                        "name": "live_asks",
                        "operation": "filter",
                        "input": "asks",
                        "output": "live_asks",
                        "arguments": {
                            "field": "status",
                            "operator": "not_in",
                            "value": ["Dropped", "Withdrawn", "Rejected", "Sanctioned", "Disbursed"],
                        },
                    }
                ],
            }
        )


def test_governed_lender_name_comparison_supports_imported_rows_without_ids():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "directory", "resource": "counterparties"},
                {"name": "submissions", "resource": "syndication_lenders"},
            ],
            "operations": [
                {
                    "name": "approach_match",
                    "operation": "join",
                    "input": "directory",
                    "output": "matched",
                    "arguments": {"right": "submissions", "left_key": "name", "right_key": "lender_name"},
                }
            ],
        }
    )

    PlanExecutor(_settings()).validate_plan(plan)


def test_governed_join_provenance_survives_prefixed_chained_join():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "lenders", "resource": "syndication_lenders"},
                {"name": "asks", "resource": "syndication"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "attach_ask",
                    "operation": "join",
                    "input": "lenders",
                    "output": "with_ask",
                    "arguments": {
                        "right": "asks",
                        "left_key": "syndication_id",
                        "right_key": "id",
                        "right_prefix": "ask_",
                    },
                },
                {
                    "name": "attach_entity",
                    "operation": "join",
                    "input": "with_ask",
                    "output": "with_entity",
                    "arguments": {
                        "right": "entities",
                        "left_key": "ask_entity_id",
                        "right_key": "id",
                        "right_prefix": "entity_",
                    },
                },
            ],
        }
    )

    PlanExecutor(_settings()).validate_plan(plan)


def test_derived_alias_cannot_be_used_to_forge_a_governed_join_key():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "forge_identity",
                    "operation": "count",
                    "input": "facilities",
                    "output": "forged",
                    "arguments": {"as": "entity_id"},
                },
                {
                    "name": "invalid_join",
                    "operation": "join",
                    "input": "forged",
                    "output": "joined",
                    "arguments": {"right": "entities", "left_key": "entity_id", "right_key": "id"},
                },
            ],
        }
    )

    with pytest.raises(RegisterPlanError, match=r"entity_id \(derived\)"):
        PlanExecutor(_settings()).validate_plan(plan)


async def test_operation_fields_are_validated_before_any_register_call(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "leads", "resource": "leads"}],
            "operations": [
                {
                    "name": "invented_filter",
                    "operation": "filter",
                    "input": "leads",
                    "output": "filtered",
                    "arguments": {"field": "entity.display_name", "operator": "eq", "value": "Ledger"},
                }
            ],
            "result_names": ["filtered"],
        }
    )
    with pytest.raises(RegisterPlanError, match="Unknown field 'entity.display_name'"):
        await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="prevalidate-fields")
    assert FakeClient.calls == []


def test_read_map_matches_primary_register_get_resources():
    assert set(RESOURCE_SPECS) == {
        "entities",
        "people",
        "counterparties",
        "leads",
        "deals",
        "lending",
        "syndication",
        "syndication_lenders",
        "asset_monetisation",
    }
    assert RESOURCE_SPECS["entities"].equality_filters == {
        "sector",
        "lens",
        "register_status",
        "entity_type",
        "state",
        "promoter_group_code",
        "code",
    }
    assert RESOURCE_SPECS["people"].equality_filters == {"role", "inactive"}
    assert RESOURCE_SPECS["syndication_lenders"].path == "/v1/syndication-lenders"
    assert RESOURCE_SPECS["asset_monetisation"].api_name == "asset-monetisation"


async def test_unknown_operation_fails_before_any_register_call(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(
            {
                "reads": [{"name": "assets", "resource": "asset_monetisation"}],
                "operations": [
                    {
                        "name": "unsafe",
                        "operation": "execute_python",
                        "input": "assets",
                        "output": "result",
                        "arguments": {},
                    }
                ],
            }
        )
    assert FakeClient.calls == []


def test_operations_have_validated_field_contracts():
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "lending", "resource": "lending"},
                {"name": "syndication", "resource": "syndication"},
            ],
            "operations": [
                {
                    "name": "combined",
                    "operation": "set_union",
                    "input": "lending",
                    "output": "combined",
                    "arguments": {"others": ["syndication"], "key_fields": ["entity_id"]},
                },
                {
                    "name": "distinct",
                    "operation": "distinct_count",
                    "input": "combined",
                    "output": "distinct",
                    "arguments": {"fields": ["entity_id"], "null_keys": "exclude", "as": "companies"},
                },
                {
                    "name": "average",
                    "operation": "average",
                    "input": "lending",
                    "output": "average",
                    "arguments": {"field": "amount_cr"},
                },
                {
                    "name": "rank",
                    "operation": "rank",
                    "input": "lending",
                    "output": "ranked",
                    "arguments": {"field": "amount_cr", "count": 1, "direction": "top"},
                },
            ],
            "result_names": ["distinct", "average", "ranked"],
        }
    )

    PlanExecutor(_settings()).validate_plan(plan)


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        (
            "set_intersection",
            {"others": ["syndication"], "key_fields": ["lender_name"]},
            "Unknown field 'lender_name'",
        ),
        (
            "distinct_count",
            {"fields": ["entity_id"], "null_keys": "infer"},
            "null_keys",
        ),
        (
            "rank",
            {"field": "amount_cr", "count": 0},
            "greater than or equal to 1",
        ),
    ],
)
def test_operation_arguments_fail_plan_validation(operation, arguments, message):
    payload = {
        "reads": [
            {"name": "lending", "resource": "lending"},
            {"name": "syndication", "resource": "syndication"},
        ],
        "operations": [
            {
                "name": "invalid",
                "operation": operation,
                "input": "lending",
                "output": "invalid",
                "arguments": arguments,
            }
        ],
    }

    if operation == "set_intersection":
        plan = QueryPlan.model_validate(payload)
        with pytest.raises(RegisterPlanError, match=message):
            PlanExecutor(_settings()).validate_plan(plan)
    else:
        with pytest.raises(ValidationError, match=message):
            QueryPlan.model_validate(payload)


class DependencyClient(FakeClient):
    async def list(self, resource, *, limit, cursor, request_id, q=None, **filters):
        self.calls.append((resource, limit, cursor, request_id, {"q": q, **filters}))
        if resource == "entities":
            return Page(
                items=[{"id": "entity-1", "legal_name": "Example Energy"}],
                count=1,
            )
        assert resource == "lending"
        assert filters == {"entity_id": "entity-1"}
        return Page(
            items=[{"id": "facility-1", "entity_id": "entity-1", "stage": "Sanctioned"}],
            count=1,
        )


async def test_named_read_result_can_feed_a_later_register_read(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", DependencyClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "company", "resource": "entities", "q": "Example Energy"},
                {
                    "name": "facilities",
                    "resource": "lending",
                    "filters": {"entity_id": "$company.id"},
                },
            ],
            "operations": [],
            "result_names": ["facilities"],
        }
    )
    result = await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="dependent-read")
    assert result.datasets["facilities"][0]["id"] == "facility-1"
    assert [call[0] for call in DependencyClient.calls] == ["entities", "lending"]
    assert DependencyClient.calls[0][4]["q"] == "Example Energy"


class CompletenessClient(FakeClient):
    async def list(self, resource, *, limit, cursor, request_id, **filters):
        assert resource == "lending"
        assert cursor is None
        return Page(
            items=[
                {"id": "facility-1", "amount_cr": "5.25"},
                {"id": "facility-2", "amount_cr": None},
            ],
            count=2,
        )


async def test_execution_carries_resource_lineage_and_metric_completeness(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", CompletenessClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "facilities", "resource": "lending"}],
            "operations": [
                {
                    "name": "known_value",
                    "operation": "sum",
                    "input": "facilities",
                    "output": "known_value",
                    "arguments": {"field": "amount_cr"},
                }
            ],
            "result_names": ["known_value"],
        }
    )

    result = await PlanExecutor(_settings()).execute(
        plan, identity=IDENTITY, request_id="metric-completeness"
    )

    assert result.datasets["known_value"] == [
        {
            "sum_amount_cr": "5.25",
            "assessed_count": 1,
            "missing_count": 1,
        }
    ]
    assert result.contributing_ids == ["facility-1", "facility-2"]
    assert result.contributing_records == [
        {"resource": "lending", "id": "facility-1"},
        {"resource": "lending", "id": "facility-2"},
    ]
    assert result.metric_completeness == {
        "known_value": {"amount_cr": {"assessed_count": 1, "missing_count": 1}}
    }
    assert result.cohort_rows == {
        "known_value": [
            {
                "fields": {"sum_amount_cr": "5.25", "assessed_count": 1, "missing_count": 1},
                "lineage": ["lending:facility-1", "lending:facility-2"],
            }
        ]
    }
    assert result.result_field_sources == {
        "known_value": {
            "sum_amount_cr": [],
            "assessed_count": [],
            "missing_count": [],
        }
    }


async def test_report_blanks_filter_propagates_into_downstream_count(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", CompletenessClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [{"name": "facilities", "resource": "lending"}],
            "operations": [
                {
                    "name": "matching",
                    "operation": "filter",
                    "input": "facilities",
                    "output": "matching",
                    "arguments": {
                        "field": "amount_cr",
                        "operator": "eq",
                        "value": "5.25",
                        "missing_policy": "report_unassessable",
                    },
                },
                {
                    "name": "facility_count",
                    "operation": "count",
                    "input": "matching",
                    "output": "facility_count",
                    "arguments": {},
                },
            ],
            "result_names": ["facility_count"],
        }
    )
    result = await PlanExecutor(_settings()).execute(
        plan, identity=IDENTITY, request_id="filter-count-completeness"
    )

    assert result.datasets["facility_count"] == [{"count": 1}]
    assert result.metric_completeness == {
        "facility_count": {"amount_cr": {"assessed_count": 1, "missing_count": 1}}
    }


class RelatedStateCompletenessClient(FakeClient):
    async def list(self, resource, *, limit, cursor, request_id, **filters):
        assert cursor is None
        assert filters == {"q": None}
        if resource == "deals":
            return Page(
                items=[
                    {"id": "deal-1", "entity_id": "entity-1"},
                    {"id": "deal-2", "entity_id": "entity-2"},
                    {"id": "deal-3", "entity_id": "entity-missing"},
                ],
                count=3,
            )
        assert resource == "entities"
        return Page(
            items=[
                {"id": "entity-1", "state": "Maharashtra"},
                {"id": "entity-2", "state": None},
            ],
            count=2,
        )


async def test_related_controlled_filter_preserves_and_counts_unassessable_rows(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", RelatedStateCompletenessClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "deals", "resource": "deals"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "with_entity",
                    "operation": "join",
                    "input": "deals",
                    "output": "with_entity",
                    "arguments": {
                        "right": "entities",
                        "left_key": "entity_id",
                        "right_key": "id",
                        "how": "left",
                        "right_prefix": "entity_",
                    },
                },
                {
                    "name": "missing_state",
                    "operation": "missing_count",
                    "input": "with_entity",
                    "output": "missing_state",
                    "arguments": {"field": "entity_state", "as": "missing_state_count"},
                },
                {
                    "name": "maharashtra",
                    "operation": "filter",
                    "input": "with_entity",
                    "output": "maharashtra",
                    "arguments": {
                        "field": "entity_state",
                        "operator": "eq",
                        "value": "Maharashtra",
                    },
                },
                {
                    "name": "deal_count",
                    "operation": "count",
                    "input": "maharashtra",
                    "output": "deal_count",
                    "arguments": {"as": "deal_count"},
                },
            ],
            "result_names": ["deal_count", "missing_state"],
        }
    )

    result = await PlanExecutor(_settings()).execute(
        plan, identity=IDENTITY, request_id="related-state-completeness"
    )

    assert result.datasets["deal_count"] == [{"deal_count": 1}]
    assert result.datasets["missing_state"] == [
        {
            "missing_state_count": 2,
            "assessed_count": 1,
        }
    ]


async def test_controlled_value_issues_exclude_rows_removed_by_boundary_filter(monkeypatch):
    class FilteredClient:
        def __init__(self, *, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def list(self, resource, *, limit, cursor, request_id, **filters):
            return Page(
                items=[
                    {"id": "kept", "sector": "Solar"},
                    {"id": "discarded", "sector": "Unknown"},
                ],
                count=2,
            )

    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FilteredClient)
    access = RegisterAccess(_settings())

    evidence = await access.read(
        RegisterRead(resource="entities", filters={"sector": "Solar"}),
        identity=IDENTITY,
        request_id="controlled-value-filter-count",
        reference_values={"Sector": ["Solar - General"]},
    )

    assert [record.record_id for record in evidence.records] == ["kept"]
    assert evidence.window.controlled_value_issues == {}


def test_controlled_host_filter_expands_multi_value_legacy_family():
    assert canonicalize_controlled_filter_arguments(
        "entities",
        "sector",
        {"field": "sector", "operator": "eq", "value": "Solar"},
    ) == {
        "field": "sector",
        "operator": "in",
        "values": [
            "Solar",
            "Solar - General",
            "Solar - EPC",
            "Solar - Developer",
            "Solar - Rooftop",
            "Solar - OEM",
        ],
    }


def test_specific_solar_filter_keeps_only_compatible_legacy_spellings():
    assert canonicalize_controlled_filter_arguments(
        "entities",
        "sector",
        {"field": "sector", "operator": "eq", "value": "Solar - EPC"},
    ) == {
        "field": "sector",
        "operator": "in",
        "values": ["Solar - EPC", "Renewables - Solar", "Solar"],
    }


class MultiplyingJoinClient(FakeClient):
    async def list(self, resource, *, limit, cursor, request_id, **filters):
        assert cursor is None
        if resource == "lending":
            return Page(
                items=[{"id": f"facility-{index}", "entity_id": "entity-1"} for index in range(3)], count=3
            )
        assert resource == "entities"
        return Page(
            items=[{"id": "entity-1", "legal_name": f"Duplicate {index}"} for index in range(3)], count=3
        )


async def test_hash_join_fails_closed_at_bounded_materialization_limit(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", MultiplyingJoinClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "facilities", "resource": "lending"},
                {"name": "entities", "resource": "entities"},
            ],
            "operations": [
                {
                    "name": "multiply",
                    "operation": "join",
                    "input": "facilities",
                    "output": "joined",
                    "arguments": {"right": "entities", "left_key": "entity_id", "right_key": "id"},
                }
            ],
        }
    )

    with pytest.raises(OperationError, match="bounded materialization limit of 6 rows"):
        await PlanExecutor(_settings(max_records_per_request=3, max_resources_per_request=2)).execute(
            plan, identity=IDENTITY, request_id="bounded-join"
        )


async def test_read_cannot_reference_a_later_result(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    plan = QueryPlan.model_validate(
        {
            "reads": [
                {
                    "name": "facilities",
                    "resource": "lending",
                    "filters": {"entity_id": "$company.id"},
                },
                {"name": "company", "resource": "entities", "q": "Example Energy"},
            ],
            "operations": [],
        }
    )
    with pytest.raises(RegisterPlanError, match="only an earlier named read"):
        await PlanExecutor(_settings()).execute(plan, identity=IDENTITY, request_id="forward-reference")
    assert FakeClient.calls == []


def test_reverse_alias_closure_uses_normalized_tokens():
    canonical = canonicalize_controlled_filter_arguments(
        "entities",
        "sector",
        {"operator": "eq", "value": "solar - general"},
    )
    assert canonical["operator"] == "in"
    assert "Solar" in canonical["values"]


def test_read_contract_tracks_register_filter_and_search_allowlists():
    source = Path(__file__).resolve().parents[2] / "register/app/api/resources.py"
    specs = {}
    for node in ast.walk(ast.parse(source.read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "ResourceSpec":
            continue
        args = {kw.arg: kw.value for kw in node.keywords}
        path = ast.literal_eval(args["prefix"])
        repo = args["repo"]
        repo_args = {kw.arg: kw.value for kw in repo.keywords}
        specs[path] = (
            set(ast.literal_eval(args.get("filterable", ast.List(elts=[])))),
            set(ast.literal_eval(repo_args.get("searchable", ast.List(elts=[])))),
        )
    for spec in RESOURCE_SPECS.values():
        assert (spec.equality_filters, spec.search_fields) == specs[spec.path]


@pytest.mark.parametrize("page_cap", [1, 3])
async def test_literal_comma_filter_preserves_equality_and_pagination_limits(monkeypatch, page_cap):
    class CommaClient(FakeClient):
        async def list(self, resource, *, limit, cursor, request_id, **filters):
            self.calls.append(filters)
            assert filters == {"q": None, "nature": "Seller"}
            if cursor is None:
                return Page(items=[{"id": "single", "investor": "North"}], count=1, next_cursor="next")
            return Page(items=[{"id": "literal", "investor": "North, South"}], count=1)

    monkeypatch.setattr("app.register_access.AsyncRegisterClient", CommaClient)
    evidence = await RegisterAccess(_settings(max_pages_per_resource=page_cap)).read(
        RegisterRead(resource="asset_monetisation", filters={"investor": "North, South", "nature": "Seller"}),
        identity=IDENTITY,
        request_id="literal-comma",
    )
    assert [r.record_id for r in evidence.records] == ([] if page_cap == 1 else ["literal"])
    assert evidence.window.completeness == (
        Completeness.PARTIAL_LIMIT if page_cap == 1 else Completeness.COMPLETE
    )


@pytest.mark.parametrize(("raw", "canonical"), [
    ("IM in Prep", "IM Under Preparation"), ("onhold", "On Hold"),
    ("rejected", "Declined"), ("disbursed", "Disbursed"), ("drop", "Dropped"),
])
def test_lender_status_aliases_follow_current_register(raw, canonical):
    assert canonical_controlled_value("syndication_lenders", "status", raw, {}) == canonical


def test_lender_vocabulary_matches_register_transition_api():
    from app.register_access import LENDER_STATUSES

    source = Path(__file__).resolve().parents[2] / "register/app/api/custom.py"
    tree = ast.parse(source.read_text())
    states = next(
        node.value for node in tree.body
        if isinstance(node, ast.AnnAssign) and node.target.id == "_LENDER_TRANSITIONS"
    )
    assert set(LENDER_STATUSES) == {ast.literal_eval(key) for key in states.keys} - {""}


async def test_private_ca_and_machine_prefix_reach_all_register_clients(monkeypatch):
    monkeypatch.setattr("app.register_access.AsyncRegisterClient", FakeClient)
    settings = _settings()
    settings.register_base_url = "https://services.example.test:8443/machine"
    settings.register_ca_file = "/etc/chitti/tls.crt"
    access = RegisterAccess(settings)
    await access.read(RegisterRead(resource="leads"), identity=IDENTITY, request_id="tls-read")
    await access.reference_values(identity=IDENTITY, request_id="tls-ref")
    assert len(FakeClient.configs) >= 3
    for config in FakeClient.configs:
        assert config.base_url == settings.register_base_url
        assert config.ca_file == settings.register_ca_file
        assert config.extra_headers["X-Internal-Context"]
