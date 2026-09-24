"""Governed corpus construction and host validation for qualitative evidence."""

from __future__ import annotations

from collections.abc import Iterable

from app.config import Settings
from app.executor import ExecutionResult
from app.stage_models import (
    AuthorizedQualitativeRecord,
    QualitativeAnalysisDraft,
    QualitativeAnalysisResult,
    QualitativeCorpus,
    QualitativeCoverage,
    QualitativeSupport,
    ValidatedQualitativeFinding,
)

# This is an evidence allowlist, not a general list of text returned by Register.
# Each entry must be supported by the governed ontology.
AUTHORIZED_QUALITATIVE_FIELDS: dict[str, frozenset[str]] = {
    "lending": frozenset({"remarks"}),
    "syndication_lenders": frozenset({"last_chase_note", "last_reply_note"}),
}


class QualitativeValidationError(ValueError):
    """The proposed findings are not supported by the authorized corpus."""


def build_qualitative_corpus(
    execution: ExecutionResult,
    settings: Settings,
) -> QualitativeCorpus:
    """Extract only allowlisted text from the exact final execution datasets."""

    candidates: dict[tuple[str, str, str], AuthorizedQualitativeRecord] = {}
    authorized_cohort_row_count = 0
    for result_name in execution.result_names:
        field_origins = (execution.result_field_sources or {}).get(result_name, {})
        has_authorized_origin = any(
            str(origin.get("field") or "")
            in AUTHORIZED_QUALITATIVE_FIELDS.get(str(origin.get("resource") or ""), frozenset())
            for origins in field_origins.values()
            for origin in origins
        )
        for cohort_row in (execution.cohort_rows or {}).get(result_name, []):
            if has_authorized_origin:
                authorized_cohort_row_count += 1
            fields = cohort_row.get("fields") or {}
            lineage = {str(token) for token in cohort_row.get("lineage") or []}
            for result_field, origins in field_origins.items():
                for origin in origins:
                    resource = str(origin.get("resource") or "")
                    source_field = str(origin.get("field") or "")
                    if source_field not in AUTHORIZED_QUALITATIVE_FIELDS.get(resource, frozenset()):
                        continue
                    for record_id in _resource_ids(lineage, resource):
                        raw = fields.get(result_field)
                        text = str(raw).strip() if raw is not None and str(raw).strip() else None
                        key = (resource, record_id, source_field)
                        existing = candidates.get(key)
                        if existing is None or (existing.source_text is None and text is not None):
                            candidates[key] = AuthorizedQualitativeRecord(
                                evidence_ref=_evidence_ref(resource, record_id, source_field),
                                resource=resource,
                                record_id=record_id,
                                source_field=source_field,
                                source_text=text,
                                missing=text is None,
                            )

    ordered = [
        candidates[key].model_copy(update={"evidence_ref": f"qref_{index:04d}"})
        for index, key in enumerate(sorted(candidates), start=1)
    ]
    bounded: list[AuthorizedQualitativeRecord] = []
    remaining_chars = settings.qualitative_max_total_chars
    limitation_reasons: list[str] = []
    for record in ordered:
        if len(bounded) >= settings.qualitative_max_records:
            limitation_reasons.append("record limit reached")
            break
        if record.source_text is None:
            bounded.append(record)
            continue
        if remaining_chars <= 0:
            limitation_reasons.append("total character limit reached")
            break
        allowed = min(settings.qualitative_max_chars_per_field, remaining_chars)
        text = record.source_text[:allowed]
        truncated = len(text) < len(record.source_text)
        if truncated:
            limitation_reasons.append("one or more source fields were truncated")
        bounded.append(record.model_copy(update={"source_text": text, "truncated": truncated}))
        remaining_chars -= len(text)

    if len(bounded) < len(ordered) and not limitation_reasons:
        limitation_reasons.append("corpus limit reached")
    text_records = sum(record.source_text is not None for record in bounded)
    missing_records = len(bounded) - text_records
    if authorized_cohort_row_count and not ordered:
        raise QualitativeValidationError("Qualitative cohort rows had no authorized source records.")
    if text_records == 0 and ordered:
        limitation_reasons.append("no authorized source text was recovered from the cohort")
    limitation = "; ".join(dict.fromkeys(limitation_reasons)) or None
    return QualitativeCorpus(
        records=bounded,
        coverage=QualitativeCoverage(
            total_records=len(ordered),
            assessed_records=len(bounded),
            text_records=text_records,
            missing_records=missing_records,
            completeness="PARTIAL" if limitation else "COMPLETE",
            limitation=limitation,
        ),
        authorized_cohort_row_count=authorized_cohort_row_count,
    )


def validate_qualitative_findings(
    draft: QualitativeAnalysisDraft,
    corpus: QualitativeCorpus,
) -> QualitativeAnalysisResult:
    """Reject invented provenance and compute authoritative support counts in host code."""

    authorized = {
        (record.evidence_ref, record.source_field): record
        for record in corpus.records
        if record.source_text is not None
    }
    findings: list[ValidatedQualitativeFinding] = []
    for finding in draft.findings:
        _validate_supports(finding.supports, authorized, kind="support")
        _validate_supports(finding.conflicts, authorized, kind="conflict")
        support_ids = sorted(
            {
                authorized[(support.evidence_ref, support.source_field)].record_id
                for support in finding.supports
            }
        )
        findings.append(
            ValidatedQualitativeFinding(
                **finding.model_dump(),
                supporting_record_ids=support_ids,
                support_count=len(support_ids),
            )
        )
    return QualitativeAnalysisResult(findings=findings, coverage=corpus.coverage)


def _validate_supports(
    supports: Iterable[QualitativeSupport],
    authorized: dict[tuple[str, str], AuthorizedQualitativeRecord],
    *,
    kind: str,
) -> None:
    seen: set[tuple[str, str]] = set()
    for support in supports:
        key = (support.evidence_ref, support.source_field)
        if key in seen:
            raise QualitativeValidationError(
                f"Duplicate qualitative {kind} for evidence reference " f"'{support.evidence_ref}'."
            )
        seen.add(key)
        record = authorized.get(key)
        if record is None:
            raise QualitativeValidationError(
                f"Qualitative {kind} cites an unauthorized record or field: "
                f"{support.evidence_ref}.{support.source_field}."
            )
        if support.excerpt not in (record.source_text or ""):
            raise QualitativeValidationError(
                f"Qualitative {kind} excerpt is not an exact source substring for "
                f"'{support.evidence_ref}'."
            )


def _resource_ids(lineage: set[str], resource: str) -> list[str]:
    prefix = f"{resource}:"
    return sorted(token[len(prefix) :] for token in lineage if token.startswith(prefix))


def _evidence_ref(resource: str, record_id: str, source_field: str) -> str:
    """Return an internal placeholder replaced by a bounded-corpus ordinal."""

    del resource, record_id, source_field
    return "qref_pending"
