from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from app.config import Settings
from app.evidence import CanonicalRecord, Completeness, RegisterEvidence, RetrievalWindow
from app.executor import PlanExecutor
from app.identity import CallerIdentity
from app.model_stages import ModelStages
from app.pipeline import ChittiPipeline, _entity_candidate_queries
from app.register_access import RegisterAccess
from app.stage_models import (
    ConversationResolution,
    QueryPlan,
    QuestionInterpretation,
    SemanticRetrievalResult,
    SurfaceTerm,
    ValueGroundingResult,
)

IDENTITY = CallerIdentity(
    tenant="EVAM",
    email="reader@example.com",
    user_id="reader",
    roles=("Viewer",),
    posture="delegated",
)


def _entity(record_id: str, legal_name: str, *, code: str | None = None) -> CanonicalRecord:
    return CanonicalRecord(
        resource="entities",
        record_id=record_id,
        fields={
            "id": record_id,
            "code": code,
            "legal_name": legal_name,
            "display_name": legal_name,
            "entity_type": "Company",
        },
    )


class CandidateAccess:
    def __init__(self, entities: list[CanonicalRecord]) -> None:
        self.entities = entities
        self.read_resources: list[str] = []

    async def read(self, read, **_kwargs):
        self.read_resources.append(read.resource)
        records = []
        if read.resource == "entities":
            query = read.q.casefold()
            records = [
                record
                for record in self.entities
                if any(
                    query in str(record.fields.get(field) or "").casefold()
                    for field in ("legal_name", "display_name", "code")
                )
            ]
        now = datetime.now(UTC)
        return RegisterEvidence(
            read=read,
            records=records,
            window=RetrievalWindow(
                resource=read.resource,
                started_at=now,
                completed_at=now,
                pages_retrieved=1,
                records_retrieved=len(records),
                completeness=Completeness.COMPLETE,
            ),
        )

    async def reference_values(self, **_kwargs):
        return {}


class CapturingStages:
    def __init__(self) -> None:
        self.candidates = None

    async def ground(self, _interpretation, _retrieval, candidates):
        self.candidates = candidates
        return ValueGroundingResult(status="RESOLVED")


async def _candidate_entities(
    question: str, term: str, visible_entities, *, unresolved: list[str] | None = None
):
    access = CandidateAccess(visible_entities)
    stages = CapturingStages()
    pipeline = ChittiPipeline(
        Settings(_env_file=None),  # type: ignore[call-arg]
        cast(ModelStages, stages),
        retriever=None,  # type: ignore[arg-type]
        access=cast(RegisterAccess, access),
    )
    interpretation = QuestionInterpretation(
        standalone_question=question,
        terms=[SurfaceTerm(text=term, role="subject", source_text=term)],
        requested_shape="scalar",
        unresolved_terms=unresolved or [],
    )
    resolution = ConversationResolution(
        standalone_question=question,
        intent_ledger=[
            {
                "kind": "entity",
                "source_message_index": 0,
                "source_text": term,
                "resolved_text": term,
            }
        ],
    )
    await pipeline._ground(  # noqa: SLF001 - verifies the candidate-stage boundary
        interpretation,
        SemanticRetrievalResult(query=question, matches=[]),
        resolution=resolution,
        identity=IDENTITY,
        request_id="candidate-test",
    )
    assert stages.candidates is not None
    return access, stages.candidates["entities"]


async def test_entity_candidates_support_exact_and_normalized_identity_names():
    entities = [
        _entity("entity-1", "Northwind Renewables Private Limited", code="NW-42"),
        _entity("entity-2", "Unrelated Mobility Limited", code="UM-7"),
    ]

    access, normalized = await _candidate_entities(
        "What exposure does northwind renewables have?",
        "northwind renewables",
        entities,
        unresolved=["Northwind Renewables"],
    )
    _, exact_code = await _candidate_entities("Show exposure for NW-42", "NW-42", entities)

    assert access.read_resources == ["people", "counterparties", "entities"]
    assert [row["id"] for row in normalized] == ["entity-1"]
    assert [row["id"] for row in exact_code] == ["entity-1"]


async def test_entity_candidates_preserve_ambiguous_near_duplicates_without_merging():
    entities = [
        _entity("entity-1", "Zephyr Charging Private Limited"),
        _entity("entity-2", "Zephyr Electric Private Limited"),
        _entity("entity-3", "Northwind Logistics Limited"),
    ]

    _, candidates = await _candidate_entities("Show Zephyr exposure", "Zephyr", entities)

    assert [(row["id"], row["legal_name"]) for row in candidates] == [
        ("entity-1", "Zephyr Charging Private Limited"),
        ("entity-2", "Zephyr Electric Private Limited"),
    ]


async def test_grounding_reference_lookup_runs_concurrently_with_entity_reads():
    class BarrierAccess:
        def __init__(self):
            self.entity_reads_started = asyncio.Event()
            self.reference_calls = 0
            self.read_reference_values = []

        def validate_read(self, _request):
            return None

        async def read(self, read, **kwargs):
            self.read_reference_values.append(kwargs.get("reference_values"))
            if read.resource == "entities":
                self.entity_reads_started.set()
            return RegisterEvidence(
                read=read,
                records=[],
                window=RetrievalWindow(
                    resource=read.resource,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                    pages_retrieved=1,
                    records_retrieved=0,
                    completeness=Completeness.COMPLETE,
                ),
            )

        async def reference_values(self, **_kwargs):
            self.reference_calls += 1
            await asyncio.wait_for(self.entity_reads_started.wait(), timeout=0.2)
            return {"Sector": ["Solar - General"]}

    class GroundStages:
        async def ground(self, _interpretation, _retrieval, candidates):
            return ValueGroundingResult(status="RESOLVED")

    access = BarrierAccess()
    pipeline = ChittiPipeline(
        Settings(_env_file=None),
        cast(ModelStages, GroundStages()),
        retriever=None,  # type: ignore[arg-type]
        access=cast(RegisterAccess, access),
    )
    interpretation = QuestionInterpretation(
        standalone_question="Find four entities",
        requested_shape="scalar",
        terms=[SurfaceTerm(text=f"entity-{i}", role="subject", source_text=f"entity-{i}") for i in range(4)],
    )

    await asyncio.wait_for(
        pipeline._ground(  # noqa: SLF001 - pins the shared reference-read barrier
            interpretation,
            SemanticRetrievalResult(query="entities", matches=[]),
            resolution=ConversationResolution(standalone_question="Find four entities"),
            identity=IDENTITY,
            request_id="reference-barrier",
        ),
        timeout=0.5,
    )
    assert access.reference_calls == 1

    plan = QueryPlan.model_validate(
        {
            "reads": [
                {"name": "entity_a", "resource": "entities", "filters": {"sector": "Solar"}},
                {"name": "entity_b", "resource": "entities", "filters": {"sector": "Solar"}},
            ],
            "operations": [],
            "result_names": ["entity_a", "entity_b"],
        }
    )
    await PlanExecutor(Settings(_env_file=None), access=cast(RegisterAccess, access)).execute(
        plan,
        identity=IDENTITY,
        request_id="reference-barrier-plan-reads",
        reference_values={"Sector": ["Solar - General"]},
    )
    assert access.reference_calls == 1
    assert access.read_reference_values[-2:] == [
        {"Sector": ["Solar - General"]},
        {"Sector": ["Solar - General"]},
    ]


async def test_entity_candidates_respect_visible_records_and_absent_names():
    visible = [_entity("entity-visible", "Northwind Renewables Private Limited")]

    _, restricted = await _candidate_entities("Show Zephyr exposure", "Zephyr", visible)
    _, absent = await _candidate_entities("Show active lending records", "lending", visible)

    assert restricted == []
    assert absent == []


def test_entity_candidate_queries_use_intent_slots_and_are_capped():
    interpretation = QuestionInterpretation(
        standalone_question="Show exposure for an entity",
        terms=[
            SurfaceTerm(text=f"subject-{index}", role="subject", source_text="subject") for index in range(6)
        ],
        requested_shape="scalar",
        unresolved_terms=["unresolved"],
    )
    resolution = ConversationResolution(
        standalone_question=interpretation.standalone_question,
        intent_ledger=[
            {
                "kind": "entity",
                "source_message_index": 0,
                "source_text": "named entity",
                "resolved_text": "named entity",
            }
        ],
    )

    assert _entity_candidate_queries(interpretation, resolution) == [
        "named entity",
        "subject-0",
        "subject-1",
        "subject-2",
    ]
