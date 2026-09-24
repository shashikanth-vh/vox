"""Explicit Chitti stage orchestration with no business-rule fallbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from evam_backend_core.logging import get_logger
from evam_register_client.errors import AuthError, ForbiddenError, RegisterError
from pydantic import BaseModel

from app.config import Settings
from app.contracts import AUDIENCE_GROUNDING
from app.evidence import Completeness, RegisterRead
from app.executor import ExecutionResult, PlanExecutor
from app.identity import CallerIdentity
from app.model_stages import ModelCallUsage, ModelResponseTerminationError, ModelStages, _usage_sink
from app.qualitative import AUTHORIZED_QUALITATIVE_FIELDS, build_qualitative_corpus
from app.register_access import (
    CONTROLLED_REFERENCE_FIELDS,
    GOVERNED_COMPOSITE_FIELDS,
    REGISTER_OPAQUE_IDENTIFIER_FIELDS,
    RegisterAccess,
    _normalize_register_evidence,
    controlled_reference_values,
)
from app.semantic import SemanticRetriever
from app.stage_models import (
    AnswerabilityResult,
    ConversationInput,
    ConversationResolution,
    QualitativeStageOutput,
    QueryPlan,
    QuestionInterpretation,
    ResultEvidence,
    RetrievalFocus,
    RetrievalNeed,
    RowPresentation,
    SemanticFacet,
    SemanticRetrievalResult,
    StageName,
    StageRecord,
    StageUsage,
    ValueGroundingResult,
)

log = get_logger("chitti.pipeline")
RESULT_TABLE_DISPLAY_LIMIT = 200
_reference_values: ContextVar[dict[str, list[Any]] | None] = ContextVar(
    "chitti_reference_values", default=None
)
_stage_progress: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "chitti_stage_progress", default=None
)
ResultT = TypeVar("ResultT")

_STAGE_ACTIVITY = {
    StageName.CONVERSATION: "Understanding your request and relevant conversation context…",
    StageName.INTERPRETATION: "Identifying the requested result, business terms, and constraints…",
    StageName.RETRIEVAL: "Finding the business definitions needed to interpret your request…",
    StageName.GROUNDING: "Matching your wording to Ledger concepts and visible business values…",
    StageName.ANSWERABILITY: "Checking whether the request can be answered safely and completely…",
    StageName.PLANNING: "Preparing a read-only plan to answer the request…",
    StageName.EXECUTION: "Reading the relevant data and performing the requested calculations…",
    StageName.QUALITATIVE: "Analyzing the available qualitative evidence…",
    StageName.EVIDENCE: "Checking completeness and assembling the answer evidence…",
    StageName.ANSWER: "Preparing the final answer from the verified evidence…",
}

_STAGE_FAILURE = {
    StageName.CONVERSATION: "I couldn't understand the request well enough to continue.",
    StageName.INTERPRETATION: "I couldn't identify the requested result and constraints reliably.",
    StageName.RETRIEVAL: "I couldn't retrieve the business definitions needed for this request.",
    StageName.GROUNDING: "I couldn't match the request to supported Ledger concepts reliably.",
    StageName.ANSWERABILITY: "I couldn't determine whether the request can be answered safely.",
    StageName.PLANNING: "I couldn't prepare a valid read-only data plan.",
    StageName.EXECUTION: "I couldn't finish reading or calculating the requested result.",
    StageName.QUALITATIVE: "I couldn't complete the qualitative evidence analysis.",
    StageName.EVIDENCE: "I couldn't assemble sufficiently reliable answer evidence.",
    StageName.ANSWER: "I couldn't prepare the final answer from the verified evidence.",
}


def _public_progress_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _stage_progress_description(
    name: StageName,
    status: Literal["started", "completed", "failed"],
    result: Any = None,
) -> str:
    if status == "started":
        return _STAGE_ACTIVITY[name]
    if status == "failed":
        return _STAGE_FAILURE[name]

    if name == StageName.CONVERSATION and isinstance(result, ConversationResolution):
        context = " using the earlier conversation" if result.used_prior_context else ""
        question = _public_progress_text(result.standalone_question, limit=180)
        return f"Understood your request{context} as: {question}"
    if name == StageName.INTERPRETATION and isinstance(result, QuestionInterpretation):
        unresolved = len(result.unresolved_terms)
        suffix = (
            f"; {unresolved} phrase{'s' if unresolved != 1 else ''} still need resolution"
            if unresolved
            else ""
        )
        return (
            f"Interpreted this as a {result.requested_shape} result with "
            f"{len(result.terms)} relevant business term{'s' if len(result.terms) != 1 else ''}{suffix}."
        )
    if name == StageName.RETRIEVAL and isinstance(result, SemanticRetrievalResult):
        passage_ids = {match.passage_id for match in [*result.matches, *result.planning_matches]}
        return (
            f"Found {len(passage_ids)} relevant business definition"
            f"{'s' if len(passage_ids) != 1 else ''} to interpret and plan the request."
        )
    if name == StageName.GROUNDING and isinstance(result, ValueGroundingResult):
        if result.status == "NEEDS_CLARIFICATION" and result.issue is not None:
            reason = _public_progress_text(result.issue.reason)
            return f"A business term needs clarification: {result.issue.term}. {reason}"
        meaning = result.established_meaning
        visible_matches = [
            f"{item.user_term} → {item.candidate_label}"
            for item in meaning.grounded_values
            if item.candidate_label
        ][:3]
        suffix = f" Matched {', '.join(visible_matches)}." if visible_matches else ""
        return (
            f"Resolved {len(meaning.semantic_bindings)} business meaning"
            f"{'s' if len(meaning.semantic_bindings) != 1 else ''}.{suffix}"
        )
    if name == StageName.ANSWERABILITY and isinstance(result, AnswerabilityResult):
        if result.outcome == "ANSWERABLE" and result.verified_question is not None:
            return f"Confirmed I can answer this as a {result.verified_question.answer_shape} result."
        if result.outcome == "CLARIFICATION_REQUIRED":
            return f"I need one clarification: {_public_progress_text(result.clarification_question)}"
        return f"This request is outside the available scope: {_public_progress_text(result.reason)}"
    if name == StageName.PLANNING and isinstance(result, QueryPlan):
        return (
            f"Prepared a read-only plan with {len(result.reads)} data read"
            f"{'s' if len(result.reads) != 1 else ''} and {len(result.operations)} calculation step"
            f"{'s' if len(result.operations) != 1 else ''}."
        )
    if name == StageName.EXECUTION and isinstance(result, ExecutionResult):
        result_rows = sum(len(result.datasets.get(name, [])) for name in result.result_names)
        completeness = "complete" if result.completeness == Completeness.COMPLETE else "partial"
        return (
            f"Finished the data work with {result_rows} result row"
            f"{'s' if result_rows != 1 else ''}; the retrieved evidence is {completeness}."
        )
    if name == StageName.QUALITATIVE and isinstance(result, QualitativeStageOutput):
        coverage = result.coverage
        return (
            f"Identified {len(result.analysis.findings)} supported theme"
            f"{'s' if len(result.analysis.findings) != 1 else ''} across "
            f"{coverage.assessed_records} assessed record"
            f"{'s' if coverage.assessed_records != 1 else ''}; coverage is "
            f"{coverage.completeness.lower()}."
        )
    if name == StageName.EVIDENCE and isinstance(result, ResultEvidence):
        completeness = "complete" if result.completeness == Completeness.COMPLETE else "partial"
        return (
            f"Assembled {len(result.facts)} verified fact"
            f"{'s' if len(result.facts) != 1 else ''} and {len(result.rows)} result row"
            f"{'s' if len(result.rows) != 1 else ''}; evidence is {completeness}."
        )
    if name == StageName.ANSWER:
        return "Finished preparing the answer from the verified evidence."
    return "Finished this part of the request."


def _focused_retrieval_queries(
    interpretation: QuestionInterpretation,
) -> tuple[list[RetrievalFocus], int]:
    """Build two bounded focus groups while preserving each surface retrieval need."""
    subjects = [term.text for term in interpretation.terms if term.role == "subject"]
    terms_by_role = {
        role: [term.source_text for term in interpretation.terms if term.role == role]
        for role in ("relationship", "dimension", "book_scope", "lifecycle")
    }
    lifecycle_terms = list(terms_by_role["lifecycle"])
    lifecycle_terms.extend(interpretation.time_expressions)
    metric_terms = list(interpretation.ranking_terms)

    def query(parts: list[str], *, include_subject: bool = True) -> str:
        unique: list[str] = []
        seen: set[str] = set()
        for part in [*parts, *(subjects[:1] if include_subject else [])]:
            normalized = " ".join(part.casefold().split())
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique.append(part)
        return " ".join(unique)[:300].strip()

    def need(facet: SemanticFacet, parts: list[str]) -> RetrievalNeed | None:
        need_query = query(parts)
        if not parts or not need_query:
            return None
        return RetrievalNeed(facet=facet, query=need_query)

    structural_needs = [
        item
        for item in (
            need("relationship", terms_by_role["relationship"]),
            need("dimension", terms_by_role["dimension"]),
        )
        if item is not None
    ]
    scoping_needs = [
        item
        for item in (
            need("book_scope", terms_by_role["book_scope"]),
            need("lifecycle", lifecycle_terms),
        )
        if item is not None
    ]
    metric_need = need("metric", metric_terms)
    if metric_need is not None:
        target = scoping_needs if len(scoping_needs) < 2 else structural_needs
        if len(target) < 2:
            target.append(metric_need)

    queries: list[RetrievalFocus] = []
    if structural_needs:
        queries.append(
            RetrievalFocus(
                responsibility="relationship_dimension",
                needs=structural_needs,
                query=query([item.query for item in structural_needs], include_subject=False),
            )
        )
    if scoping_needs:
        has_lifecycle_or_metric = any(item.facet in {"lifecycle", "metric"} for item in scoping_needs)
        queries.append(
            RetrievalFocus(
                responsibility=("lifecycle_metric" if has_lifecycle_or_metric else "book_scope"),
                needs=scoping_needs,
                query=query([item.query for item in scoping_needs], include_subject=False),
            )
        )
    if not queries and subjects:
        subject_query = query(subjects, include_subject=False)
        queries.append(
            RetrievalFocus(
                responsibility="book_scope",
                needs=[
                    RetrievalNeed(facet="book_scope", query=subject_query),
                    RetrievalNeed(facet="resource", query=subject_query),
                ],
                query=subject_query,
            )
        )
    queries = queries[:2]
    return queries, len(queries)


class PipelineStageError(RuntimeError):
    def __init__(
        self,
        stage: StageName,
        detail: str,
        *,
        failure_name: str,
        last_completed_stage: StageName | None,
        stages: list[dict[str, Any]],
        retrieval_trace: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.failure_name = failure_name
        self.last_completed_stage = last_completed_stage
        self.stages = stages
        self.retrieval_trace = retrieval_trace
        super().__init__(detail)


@dataclass(slots=True)
class PipelineResponse:
    content: str
    metadata: dict[str, Any]
    public_content: str | None = None
    public_tables: list[dict[str, Any]] | None = None


class ChittiPipeline:
    def __init__(
        self,
        settings: Settings,
        stages: ModelStages,
        retriever: SemanticRetriever,
        *,
        access: RegisterAccess | None = None,
        executor: PlanExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.stages = stages
        self.retriever = retriever
        self.access = access or RegisterAccess(settings)
        self.executor = executor or PlanExecutor(settings, self.access)

    async def _stage(
        self,
        records: list[StageRecord],
        name: StageName,
        value: Any,
        call: Callable[[], Awaitable[ResultT]],
        *,
        model: str | None = None,
    ) -> ResultT:
        started = datetime.now(UTC)
        started_clock = time.monotonic()
        stage_input = _json_value(value)
        usage_sink: list[ModelCallUsage] = []
        usage_token: Token[list[ModelCallUsage] | None] = _usage_sink.set(usage_sink)
        try:
            if callback := _stage_progress.get():
                callback(
                    {
                        "stage": name,
                        "status": "started",
                        "description": _stage_progress_description(name, "started"),
                        "elapsed_ms": 0.0,
                        "model": model,
                    }
                )
            log.info("pipeline_stage_started", extra={"stage": name, "model": model,
                        **({"input": stage_input} if self.settings.log_pipeline else {})})
            try:
                result = await call()
            except asyncio.CancelledError:
                elapsed = (time.monotonic() - started_clock) * 1000
                usage = _aggregate_usage(usage_sink)
                records.append(
                    StageRecord(
                        stage=name,
                        status="failed",
                        started_at=started,
                        completed_at=datetime.now(UTC),
                        elapsed_ms=elapsed,
                        model=model,
                        input=stage_input,
                        error="cancelled",
                        usage=usage,
                    )
                )
                if callback := _stage_progress.get():
                    callback(
                        {
                            "stage": name,
                            "status": "failed",
                            "description": _stage_progress_description(name, "failed"),
                            "elapsed_ms": elapsed,
                            "model": model,
                        }
                    )
                log.warning(
                    "pipeline_stage_failed",
                    extra={
                        "stage": name,
                        "elapsed_ms": elapsed,
                        "usage": usage.model_dump(mode="json") if usage else None,
                    },
                )
                raise
            except Exception as exc:
                elapsed = (time.monotonic() - started_clock) * 1000
                usage = _aggregate_usage(usage_sink)
                records.append(
                    StageRecord(
                        stage=name,
                        status="failed",
                        started_at=started,
                        completed_at=datetime.now(UTC),
                        elapsed_ms=elapsed,
                        model=model,
                        input=stage_input,
                        error=str(exc),
                        usage=usage,
                    )
                )
                if callback := _stage_progress.get():
                    callback(
                        {
                            "stage": name,
                            "status": "failed",
                            "description": _stage_progress_description(name, "failed"),
                            "elapsed_ms": elapsed,
                            "model": model,
                        }
                    )
                log.exception(
                    "pipeline_stage_failed",
                    extra={
                        "stage": name,
                        "elapsed_ms": elapsed,
                        "usage": usage.model_dump(mode="json") if usage else None,
                    },
                )
                raise PipelineStageError(
                    name,
                    str(exc),
                    failure_name=_failure_name(name, exc),
                    last_completed_stage=next(
                        (record.stage for record in reversed(records[:-1]) if record.status == "completed"),
                        None,
                    ),
                    stages=_failure_stages(records),
                    retrieval_trace=(
                        _retrieval_trace(records) if self.settings.capture_retrieval_trace else None
                    ),
                ) from exc
            elapsed = (time.monotonic() - started_clock) * 1000
            output = _json_value(result)
            usage = _aggregate_usage(usage_sink)
            records.append(
                StageRecord(
                    stage=name,
                    status="completed",
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    elapsed_ms=elapsed,
                    model=model,
                    input=stage_input,
                    output=output,
                    usage=usage,
                )
            )
            log.info(
                "pipeline_stage_completed",
                extra={
                    "stage": name,
                    "elapsed_ms": elapsed,
                    **({"output": _log_value(output)} if self.settings.log_pipeline else {}),
                    "usage": usage.model_dump(mode="json") if usage else None,
                },
            )
            if callback := _stage_progress.get():
                callback(
                    {
                        "stage": name,
                        "status": "completed",
                        "description": _stage_progress_description(name, "completed", result),
                        "elapsed_ms": elapsed,
                        "model": model,
                    }
                )
            return result
        finally:
            _usage_sink.reset(usage_token)

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        identity: CallerIdentity,
        request_id: str,
        on_stage: Callable[[dict[str, Any]], None] | None = None,
    ) -> PipelineResponse:
        progress_token = _stage_progress.set(on_stage)
        try:
            return await self._run(messages, identity=identity, request_id=request_id)
        finally:
            _stage_progress.reset(progress_token)

    async def _run(
        self,
        messages: list[dict[str, Any]],
        *,
        identity: CallerIdentity,
        request_id: str,
    ) -> PipelineResponse:
        records: list[StageRecord] = []
        resolution = await self._stage(
            records,
            StageName.CONVERSATION,
            {"messages": messages},
            lambda: self.stages.resolve_conversation(ConversationInput(messages=messages)),
            model=self.settings.conversation_model,
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        interpretation = await self._stage(
            records,
            StageName.INTERPRETATION,
            resolution,
            lambda: self.stages.interpret(resolution),
            model=self.settings.interpretation_model,
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        retrieval_query = " ".join(
            [
                interpretation.standalone_question,
                *(term.text for term in interpretation.terms),
                *(literal.text for literal in interpretation.literals),
                *interpretation.grouping_terms,
                *interpretation.ranking_terms,
                *interpretation.relationship_terms,
                *interpretation.unresolved_terms,
            ]
        )
        focus_queries, focus_slots = _focused_retrieval_queries(interpretation)
        retrieval = await self._stage(
            records,
            StageName.RETRIEVAL,
            {
                "query": retrieval_query,
                "focus_queries": focus_queries,
                "focus_slots": focus_slots,
            },
            lambda: self.retriever.search(
                retrieval_query,
                focus_queries=focus_queries,
                focus_slots=focus_slots,
                audience=AUDIENCE_GROUNDING,
            ),
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        grounding = await self._stage(
            records,
            StageName.GROUNDING,
            {"interpretation": interpretation, "candidate_source": "Register people"},
            lambda: self._ground(
                interpretation,
                retrieval,
                resolution=resolution,
                identity=identity,
                request_id=request_id,
            ),
            model=self.settings.grounding_model,
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        answerability = await self._stage(
            records,
            StageName.ANSWERABILITY,
            {"interpretation": interpretation, "grounding": grounding},
            lambda: self.stages.answerability(interpretation, grounding, retrieval),
            model=self.settings.answerability_model,
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped
        if answerability.outcome != "ANSWERABLE":
            return self._non_answer(answerability, records, identity, request_id)

        verified_question = answerability.verified_question
        if verified_question is None:  # guarded by the AnswerabilityResult contract
            raise RuntimeError("ANSWERABLE outcome did not contain a Verified Question.")
        plan = await self._stage(
            records,
            StageName.PLANNING,
            {"verified_question": verified_question, "retrieval": retrieval},
            lambda: self._plan(verified_question, retrieval),
            model=self.settings.planning_model,
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        execution = await self._stage(
            records,
            StageName.EXECUTION,
            plan,
            lambda: self.executor.execute(
                plan,
                identity=identity,
                request_id=request_id,
                reference_values=_reference_values.get(),
            ),
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        retrieved_at = datetime.now(UTC)
        qualitative = None
        corpus = None
        if verified_question.qualitative_analysis or verified_question.answer_shape == "qualitative":

            async def run_qualitative() -> QualitativeStageOutput:
                local_corpus = build_qualitative_corpus(execution, self.settings)
                local_result = await self.stages.qualitative(local_corpus)
                return QualitativeStageOutput.from_run(local_corpus, local_result)

            qualitative_stage = await self._stage(
                records,
                StageName.QUALITATIVE,
                {"result_names": execution.result_names},
                run_qualitative,
                model=self.settings.qualitative_model,
            )
            corpus = qualitative_stage.corpus
            qualitative = qualitative_stage.analysis
            stopped = self._diagnostic_stop(records, identity, request_id)
            if stopped:
                return stopped

        selected = {
            name: _answer_safe_rows(
                execution.datasets[name],
                (execution.result_field_sources or {}).get(name, {}),
            )
            for name in execution.result_names
        }
        facts: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        for name, values in selected.items():
            result_shape = execution.result_shapes[name]
            if result_shape == "scalar":
                if len(values) != 1:
                    raise ValueError(
                        f"Scalar result '{name}' produced {len(values)} rows; expected exactly one."
                    )
                facts[name] = values[0]
            else:
                facts[f"{name}_row_count"] = len(values)
                rows.extend({"result": name, **row} for row in values)
        caveats = []
        for result_name, issues in (execution.controlled_value_issues or {}).items():
            for field, count in issues.items():
                if count:
                    caveats.append(
                        f"Final result '{result_name}' contains "
                        f"{count} rows whose `{field}` value does not resolve to a single "
                        "current reference label."
                    )
        if execution.completeness != Completeness.COMPLETE:
            caveats.append("One or more Register reads stopped at a configured traversal limit.")
        if any(read.resource == "lending" for read in plan.reads):
            caveats.append("Operational Register reads exclude records marked as requiring reconciliation.")
            if any(
                "stage_updated_at" in read.filters for read in plan.reads if read.resource == "lending"
            ) or any(
                origin.get("resource") == "lending" and origin.get("field") == "stage_updated_at"
                for fields in (execution.result_field_sources or {}).values()
                for origins in fields.values()
                for origin in origins
            ):
                caveats.append(
                    "Stage-date comparisons use the recorded stage_updated_at date. Register does not "
                    "expose whether it was backfilled from a modification or creation date, so elapsed "
                    "time is not a verified duration in stage; missing dates remain unageable."
                )
        metric_completeness = execution.metric_completeness or {}
        for result_name, fields in metric_completeness.items():
            for field, counts in fields.items():
                if counts["missing_count"]:
                    caveats.append(
                        f"Result '{result_name}' assessed {counts['assessed_count']} rows for "
                        f"'{field}'; {counts['missing_count']} rows had no value."
                    )
        if qualitative is not None:
            coverage = qualitative.coverage
            if coverage.missing_records:
                caveats.append(
                    f"Qualitative analysis assessed {coverage.assessed_records} records; "
                    f"{coverage.missing_records} had no authorized source text."
                )
            if coverage.completeness == "PARTIAL":
                caveats.append(
                    "Qualitative analysis is partial: " + (coverage.limitation or "bounded corpus")
                )
        unavailable_obligations = [
            obligation
            for obligation in verified_question.evidence_obligations
            if obligation.availability == "UNAVAILABLE"
        ]
        for obligation in unavailable_obligations:
            caveats.append(
                f"Unavailable check: {obligation.description} — " f"{obligation.unavailable_reason}."
            )
        evidence = await self._stage(
            records,
            StageName.EVIDENCE,
            {"execution": execution, "identity_scope": identity.display_scope},
            lambda: _immediate(
                ResultEvidence(
                    verified_question=verified_question,
                    facts=facts,
                    rows=rows,
                    row_presentation=RowPresentation(
                        total_count=len(rows),
                        displayed_count=min(len(rows), RESULT_TABLE_DISPLAY_LIMIT),
                        truncated=len(rows) > RESULT_TABLE_DISPLAY_LIMIT,
                    ),
                    contributing_ids=execution.contributing_ids,
                    contributing_records=execution.contributing_records or [],
                    completeness=execution.completeness,
                    retrieval_windows=execution.windows,
                    metric_completeness=metric_completeness,
                    scope="Caller-visible Register data under the current access scope",
                    retrieved_at=retrieved_at,
                    caveats=caveats,
                    qualitative_findings=(qualitative.findings if qualitative is not None else []),
                    qualitative_coverage=(qualitative.coverage if qualitative is not None else None),
                    unavailable_obligations=unavailable_obligations,
                )
            ),
        )
        stopped = self._diagnostic_stop(records, identity, request_id)
        if stopped:
            return stopped

        fallback = False
        try:
            answer = await self._stage(
                records,
                StageName.ANSWER,
                evidence,
                lambda: self.stages.answer(verified_question, evidence),
                model=self.settings.answer_model,
            )
        except PipelineStageError:
            if not _can_render_validated_fallback(execution.completeness, corpus, qualitative):
                raise
            answer = _render_validated_evidence(evidence)
            fallback = True
        # Keep the model's business prose separate from the internal inspection table.
        public_content = answer if not fallback else None
        if rows and not verified_question.qualitative_analysis:
            answer = (
                f"{answer.rstrip()}\n\n" f"{_render_result_table(rows, execution.result_field_sources or {})}"
            )
        outcome = (
            "PARTIAL_RESULT"
            if execution.completeness != Completeness.COMPLETE
            or (qualitative is not None and qualitative.coverage.completeness == "PARTIAL")
            else "ANSWERED"
        )
        metadata = self._metadata(records, identity, request_id, outcome, execution.completeness)
        if fallback:
            metadata["answer_generation_fallback"] = True
            metadata["failed_stage"] = StageName.ANSWER
            metadata["last_completed_stage"] = next(
                record.stage for record in reversed(records) if record.status == "completed"
            )
        metadata["public_evidence"] = [{
            "reference": "E1",
            "label": f"Based on {len(evidence.contributing_records)} accessible records",
            "retrieved_at": evidence.retrieved_at.isoformat(),
        }] if evidence.contributing_records else []
        if rows:
            metadata["result_table"] = rows
        from app.result_tables import result_tables

        return PipelineResponse(
            content=answer,
            metadata=metadata,
            public_content=public_content,
            public_tables=result_tables(execution, plan),
        )

    async def _plan(self, verified_question: Any, retrieval: Any) -> Any:
        plan = await self.stages.plan(verified_question, retrieval)
        self.executor.validate_plan(plan)
        return plan

    async def _ground(
        self,
        interpretation: Any,
        retrieval: Any,
        *,
        resolution: ConversationResolution,
        identity: CallerIdentity,
        request_id: str,
    ) -> Any:
        identity_reads = [
            RegisterRead(resource="people"),
            RegisterRead(resource="counterparties"),
        ]
        entity_reads = [
            RegisterRead(resource="entities", q=query)
            for query in _entity_candidate_queries(interpretation, resolution)
        ]
        retrieved_composite_pairs = {
            (str(match.metadata.get("resource") or ""), str(match.metadata.get("field") or ""))
            for match in [*retrieval.matches, *retrieval.planning_matches]
            if (
                str(match.metadata.get("resource") or ""),
                str(match.metadata.get("field") or ""),
            )
            in GOVERNED_COMPOSITE_FIELDS
        }
        composite_resources = sorted({resource for resource, _ in retrieved_composite_pairs})
        composite_reads = [RegisterRead(resource=resource) for resource in composite_resources]
        reads = [*identity_reads, *entity_reads, *composite_reads]
        results, reference_values = await asyncio.gather(
            asyncio.gather(
                *(
                    self.access.read(
                        read,
                        identity=identity,
                        request_id=request_id,
                        defer_controlled_normalization=read.resource == "entities",
                    )
                    for read in reads
                )
            ),
            self.access.reference_values(identity=identity, request_id=request_id),
        )
        people_result, counterparties_result = results[:2]
        entity_results = results[2 : 2 + len(entity_reads)]
        composite_results = results[2 + len(entity_reads) :]
        reference_values = controlled_reference_values(reference_values)
        _reference_values.set(reference_values)
        entity_results = [_normalize_register_evidence(result, reference_values) for result in entity_results]
        entity_records = {record.record_id: record for result in entity_results for record in result.records}
        composite_values: list[dict[str, str]] = []
        seen_composite_values: set[tuple[str, str, str]] = set()
        for resource, result in zip(composite_resources, composite_results, strict=True):
            fields = sorted(field for owner, field in retrieved_composite_pairs if owner == resource)
            for record in result.records:
                for field in fields:
                    value = record.fields.get(field)
                    if value is None or not str(value).strip():
                        continue
                    key = (resource, field, str(value))
                    if key in seen_composite_values:
                        continue
                    seen_composite_values.add(key)
                    digest = hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
                    composite_values.append(
                        {
                            "id": f"composite-{digest}",
                            "resource": resource,
                            "field": field,
                            "value": str(value),
                        }
                    )
        candidates = {
            "people": [
                _select(record.fields, "id", "name", "full_name", "email", "role", "inactive")
                for record in people_result.records
            ],
            "counterparties": [
                _select(record.fields, "id", "name", "short_name", "counterparty_type", "is_active")
                for record in counterparties_result.records
            ],
            "entities": [
                _select(record.fields, "id", "code", "legal_name", "display_name", "entity_type")
                for record in entity_records.values()
            ],
            "reference_values": {
                category: reference_values[category]
                for mapping in CONTROLLED_REFERENCE_FIELDS.values()
                for category in set(mapping.values())
                if category in reference_values and reference_values[category]
            },
            "governed_composite_values": composite_values,
            "completeness": {
                "people": people_result.window.completeness,
                "counterparties": counterparties_result.window.completeness,
                "entities": _combined_completeness(entity_results),
                **{
                    resource: result.window.completeness
                    for resource, result in zip(composite_resources, composite_results, strict=True)
                },
            },
        }
        return await self.stages.ground(interpretation, retrieval, candidates)

    def _diagnostic_stop(
        self,
        records: list[StageRecord],
        identity: CallerIdentity,
        request_id: str,
    ) -> PipelineResponse | None:
        last = records[-1]
        if self.settings.pipeline_stop_after != last.stage:
            return None
        return PipelineResponse(
            content=(
                f"Local Chitti diagnostic stopped after {last.stage}.\n"
                f"{json.dumps(last.output, indent=2, ensure_ascii=False)}"
            ),
            metadata=self._metadata(records, identity, request_id, "DIAGNOSTIC_STOP", Completeness.COMPLETE),
        )

    def _non_answer(
        self,
        answerability: AnswerabilityResult,
        records: list[StageRecord],
        identity: CallerIdentity,
        request_id: str,
    ) -> PipelineResponse:
        content = answerability.clarification_question or answerability.reason
        return PipelineResponse(
            content=content,
            metadata=self._metadata(
                records, identity, request_id, answerability.outcome, Completeness.COMPLETE
            ),
        )

    def _metadata(
        self,
        records: list[StageRecord],
        identity: CallerIdentity,
        request_id: str,
        outcome: str,
        completeness: Completeness,
    ) -> dict[str, Any]:
        usages = [record.usage for record in records if record.usage is not None]
        attempted = sum(item.attempted_calls for item in usages)
        source = (
            "measured"
            if attempted and all(item.measurement == "measured" for item in usages)
            else (
                "partial"
                if attempted and any(item.measurement == "partial" for item in usages)
                else "unavailable"
                if attempted
                else "estimate"
            )
        )
        metadata: dict[str, Any] = {
            "request_id": request_id,
            "outcome": outcome,
            "last_completed_stage": records[-1].stage,
            "failed_stage": None,
            "completeness": completeness,
            "scope": identity.display_scope,
            "pipeline_status": "CONNECTED_TO_REGISTER",
            "usage_source": source,
            "stages": [
                {
                    "stage": record.stage,
                    "status": record.status,
                    "elapsed_ms": record.elapsed_ms,
                    "model": record.model,
                    "usage": record.usage.model_dump(mode="json") if record.usage else None,
                }
                for record in records
            ],
        }
        if self.settings.capture_retrieval_trace:
            metadata["retrieval_trace"] = _retrieval_trace(records)
        return metadata


async def _immediate(value: ResultT) -> ResultT:
    return value


def _aggregate_usage(calls: list[ModelCallUsage]) -> StageUsage | None:
    if not calls:
        return None
    received = [call for call in calls if call.response_received]

    def complete(field: str) -> int | None:
        values = [getattr(call, field) for call in received]
        return sum(values) if received and all(value is not None for value in values) else None

    prompt = complete("prompt_tokens")
    completion = complete("completion_tokens")
    total = complete("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    cached = complete("cached_prompt_tokens")
    with_usage = sum(
        call.prompt_tokens is not None and call.completion_tokens is not None for call in received
    )
    measurement: Literal["measured", "partial", "unavailable"] = (
        "measured"
        if len(received) == len(calls) and with_usage == len(received)
        else "partial"
        if received
        else "unavailable"
    )
    return StageUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_prompt_tokens=cached,
        providers=list(dict.fromkeys(call.provider for call in received if call.provider)),
        attempted_calls=len(calls),
        model_calls=len(received),
        repair_calls=sum(call.repair for call in calls),
        validation_errors=[
            {
                "attempt": index,
                "repair": call.repair,
                "errors": call.validation_errors,
            }
            for index, call in enumerate(calls, start=1)
            if call.validation_errors
        ],
        responses_with_usage=with_usage,
        responses_missing_usage=len(received) - with_usage,
        measurement=measurement,
    )


def _select(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def _answer_safe_rows(
    rows: list[dict[str, Any]],
    field_sources: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    raw_fields = {
        field
        for field, origins in field_sources.items()
        if any(
            origin.get("field") in AUTHORIZED_QUALITATIVE_FIELDS.get(origin.get("resource", ""), frozenset())
            for origin in origins
        )
    }
    return [{field: value for field, value in row.items() if field not in raw_fields} for row in rows]


def _can_render_validated_fallback(
    execution_completeness: Completeness,
    corpus: Any,
    qualitative: Any,
) -> bool:
    return bool(
        execution_completeness == Completeness.COMPLETE
        and corpus is not None
        and qualitative is not None
        and corpus.coverage.completeness == "COMPLETE"
        and corpus.coverage.text_records > 0
        and qualitative.coverage.completeness == "COMPLETE"
    )


def _render_validated_evidence(evidence: ResultEvidence) -> str:
    lines = ["I could not generate the usual prose, so here is the validated evidence:"]
    for name, value in evidence.facts.items():
        lines.append(f"- {name}: {json.dumps(value, ensure_ascii=False, default=str)}")
    for finding in evidence.qualitative_findings:
        lines.append(
            f"- Recorded theme — {finding.label} ({finding.support_count} supporting records): "
            f"{finding.summary}"
        )
    if evidence.qualitative_coverage is not None:
        coverage = evidence.qualitative_coverage
        lines.append(
            f"- Coverage: assessed {coverage.assessed_records} of {coverage.total_records} records; "
            f"{coverage.missing_records} lacked authorized text."
        )
    for obligation in evidence.unavailable_obligations:
        lines.append(f"- Unavailable: {obligation.description} — {obligation.unavailable_reason}.")
    lines.extend(f"- Caveat: {caveat}" for caveat in evidence.caveats)
    return "\n".join(lines)


def _render_result_table(
    rows: list[dict[str, Any]],
    field_sources: dict[str, dict[str, list[dict[str, str]]]],
    *,
    display_limit: int = RESULT_TABLE_DISPLAY_LIMIT,
) -> str:
    def markdown_cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")

    if not rows:
        return ""
    visible_rows = rows[:display_limit]
    columns: list[str] = []
    column_origins: dict[str, list[dict[str, str]]] = {}
    for row in visible_rows:
        result_name = str(row.get("result") or "")
        for key in row:
            if key == "result":
                continue
            if key not in columns:
                columns.append(key)
            column_origins.setdefault(key, []).extend(field_sources.get(result_name, {}).get(key, []))
    columns = [
        key
        for key in columns
        if not any(
            (origin.get("resource"), origin.get("field")) in REGISTER_OPAQUE_IDENTIFIER_FIELDS
            for origin in column_origins.get(key, [])
        )
    ]
    lines = [
        "| " + " | ".join(markdown_cell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in visible_rows:
        lines.append("| " + " | ".join(markdown_cell(row.get(column, "")) for column in columns) + " |")
    if len(rows) > display_limit:
        lines.append(f"\nShowing {display_limit} of {len(rows)} rows.")
    return "\n".join(lines)


def _entity_candidate_queries(
    interpretation: Any,
    resolution: ConversationResolution,
) -> list[str]:
    """Return at most four model-identified identity-bearing surface queries."""
    queries = [intent.resolved_text.strip() for intent in resolution.intent_ledger if intent.kind == "entity"]
    queries.extend(
        term.text.strip()
        for term in interpretation.terms
        if term.role in {"actor", "relationship", "subject"}
    )
    unique: dict[str, str] = {}
    for query in queries:
        if query:
            unique.setdefault(query.casefold(), query)
    return list(unique.values())[:4]


def _combined_completeness(results: list[Any]) -> Completeness:
    if not results:
        return Completeness.COMPLETE
    for completeness in (
        Completeness.FAILED,
        Completeness.PARTIAL_TIMEOUT,
        Completeness.PARTIAL_LIMIT,
    ):
        if any(result.window.completeness == completeness for result in results):
            return completeness
    return Completeness.COMPLETE


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        result = value.model_dump(mode="json")
    elif hasattr(value, "__dataclass_fields__"):
        result = {key: _json_compatible(getattr(value, key)) for key in value.__dataclass_fields__}
    elif isinstance(value, dict):
        result = {str(key): _json_compatible(item) for key, item in value.items()}
    else:
        result = {"value": _json_compatible(value)}
    return result


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    return value


def _log_value(value: Any) -> Any:
    """Keep stage provenance while bounding bulk caller-visible row logging."""

    if isinstance(value, dict):
        return {str(key): _log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > 20 and all(isinstance(item, dict) for item in value):
            return {
                "row_count": len(value),
                "sample_ids": [str(item.get("id") or item.get("record_id") or "") for item in value[:5]],
                "truncated": True,
            }
        return [_log_value(item) for item in value]
    return value


_STAGE_FAILURES: dict[StageName, str] = {
    StageName.CONVERSATION: "CONVERSATION_RESOLUTION_FAILED",
    StageName.INTERPRETATION: "QUESTION_INTERPRETATION_FAILED",
    StageName.RETRIEVAL: "SEMANTIC_RETRIEVAL_FAILED",
    StageName.GROUNDING: "VALUE_GROUNDING_FAILED",
    StageName.ANSWERABILITY: "ANSWERABILITY_FAILED",
    StageName.PLANNING: "QUERY_PLANNING_FAILED",
    StageName.EXECUTION: "PLAN_EXECUTION_FAILED",
    StageName.EVIDENCE: "EVIDENCE_CONSTRUCTION_FAILED",
    StageName.QUALITATIVE: "QUALITATIVE_ANALYSIS_FAILED",
    StageName.ANSWER: "ANSWER_GENERATION_FAILED",
}


def _failure_name(stage: StageName, exc: Exception) -> str:
    if isinstance(exc, ModelResponseTerminationError):
        return {
            "refusal": "MODEL_REFUSED",
            "length": "MODEL_RESPONSE_TRUNCATED",
            "content_filter": "MODEL_RESPONSE_FILTERED",
        }[exc.kind]
    if isinstance(exc, AuthError | ForbiddenError):
        return "REGISTER_ACCESS_DENIED"
    if isinstance(exc, RegisterError):
        return "REGISTER_RESOURCE_FAILED"
    return _STAGE_FAILURES[stage]


def _failure_stages(records: list[StageRecord]) -> list[dict[str, Any]]:
    by_name = {record.stage: record for record in records}
    result = []
    for stage in StageName:
        record = by_name.get(stage)
        result.append(
            {
                "stage": stage,
                "status": record.status if record else "not_run",
                "elapsed_ms": record.elapsed_ms if record else None,
                "model": record.model if record else None,
                "usage": record.usage.model_dump(mode="json") if record and record.usage else None,
            }
        )
    return result


def _retrieval_trace(records: list[StageRecord]) -> dict[str, Any]:
    return {
        record.stage: {
            "input": record.input,
            "output": record.output,
        }
        for record in records
        if record.stage in {StageName.INTERPRETATION, StageName.RETRIEVAL}
    }
