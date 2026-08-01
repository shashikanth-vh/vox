"""Governance-evidence endpoints — attach, list, and (append-only) revoke the IMMUTABLE artefacts
that evidence a real-world governance milestone (a Credit Committee approval, a sanction letter, an
executed facility agreement, a completed document set).

The shared policy engine's evidence gate refuses a sensitive lifecycle transition until the
CURRENTLY-VALID evidence that stage requires is on file here. So evidence must be TRUSTWORTHY, not
merely present:

* **Authorised by kind.** Each kind names an RBAC operation the caller must hold
  (``evam_backend_core.evidence``). A committee outcome / sanction letter is reserved to the credit
  authority and the designated workflow service; arbitrary kinds are rejected.
* **Provenance is VERIFIED, not asserted.** For a committee/sanction kind the caller supplies only a
  ``decision_ref``; the Register RESOLVES it against the durable, single-winner workflow-decision
  record, checks the outcome + tenant + subject + committee authority, and GENERATES the evidence's
  provenance (workflow/run/decider) from that record — invented provenance strings no longer work.
* **Bound to a real subject and scope.** The subject must exist and be of a type the kind allows,
  and every operation (attach / list / revoke / supersede) reloads the subject and enforces a SCOPED
  caller's row scope.
* **Immutable but revocable.** Rows are write-once, but a mistaken/fraudulent record is neutralised
  by APPENDING a terminal status ('Revoked'/'Invalidated'/'Superseded') under a row lock — the
  policy gate then stops accepting it, while history is preserved. Supersession requires the same
  subject AND kind.

    POST /v1/evidence               attach an authorised, provenance-verified evidence record
    GET  /v1/evidence               list evidence for a subject (subject-scope enforced)
    POST /v1/evidence/{id}/revoke   append a terminal status (subject-scope enforced, row-locked)
"""

from __future__ import annotations

import uuid
from typing import Any

from evam_backend_core.evidence import EVIDENCE_STATUSES, spec_for_kind
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.advaya import AdvayaHandoff
from app.models.cpcs import CpcsChecklist
from app.models.decisions import WorkflowDecision
from app.models.evidence import GovernanceEvidence, GovernanceEvidenceStatus
from app.repositories.subjects import SUBJECTS, load_subject

_SYNDICATION_AUTHORITY = {"Syn Head", "Management", "Admin"}
_AM_AUTHORITY = {"AM Head", "Management", "Admin"}

router = api_router(prefix="/v1/evidence", tags=["Evidence"])

_COMMITTEE_AUTHORITY = {"Credit Head", "Management", "Admin"}


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str = Field(min_length=1, max_length=40)
    subject_id: str = Field(min_length=1)
    evidence_kind: str = Field(min_length=1, max_length=60)
    reference: str = Field(min_length=1, max_length=500)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    note: str | None = Field(default=None, max_length=2000)
    # For a committee/sanction kind this is the ONLY provenance the caller supplies — the Register
    # resolves it against the durable decision and derives workflow_id/run_id/decider itself.
    decision_ref: str | None = Field(default=None, max_length=200)
    # For a non-committee governance kind (executed_agreement) the workflow run that produced it.
    workflow_id: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    # This row corrects/replaces an earlier one of the SAME kind, which is then marked Superseded.
    supersedes_id: str | None = Field(default=None)


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(default="Revoked", pattern="^(Revoked|Invalidated)$")
    reason: str = Field(min_length=1, max_length=2000)


def _require_identity(ctx: RequestContext) -> None:
    from app.authz.engine import service_ctx
    if ctx.user is None and service_ctx.get() is None:
        raise ForbiddenError("Governance evidence requires an identified principal.")


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ValidationAppError(f"{field} {value!r} is not a valid id.") from exc


async def _subject_or_404(ctx: RequestContext, subject_type: str, subject_id: uuid.UUID):  # noqa: ANN202
    if subject_type not in SUBJECTS:
        raise ValidationAppError(f"Unknown subject_type {subject_type!r}.")
    subject = await load_subject(ctx.session, ctx.tenant_id, subject_type, subject_id)
    if subject is None:
        raise NotFoundError(f"{subject_type} '{subject_id}' does not exist.")
    return subject


async def _enforce_subject_scope(ctx: RequestContext, subject_type: str, subject,  # noqa: ANN001
                                 granted=None) -> None:  # noqa: ANN001
    """A SCOPED human authority may only operate on evidence for a subject in their scope. A caller
    whose grant for THIS operation is FULL (broad authority, e.g. the Credit committee for deals),
    Admin / Management, and named services all pass. Applied UNIFORMLY to attach, list, revoke and
    supersede so none of them is a scope hole. ``granted`` is the operation's access level when the
    caller path has one (attach/revoke); list has none and relies on the role exemption + row scope."""
    from app.authz import scope as scope_mod
    from app.authz.matrix import Access

    if ctx.user is None:
        return  # a named service is authenticated at the key layer
    if granted is Access.FULL:
        return  # a FULL grant for this operation is not row-scoped
    roles = set(ctx.user.roles or [])
    if roles & {"Admin", "Management"}:
        return
    if subject_type == "Entity":
        return
    user_scope = await scope_mod.build_scope(ctx, ctx.user)
    if not await scope_mod.row_in_scope(ctx, user_scope, subject_type, subject):
        raise ForbiddenError(
            f"This {subject_type} is not in your scope; you may not operate on its evidence.")


async def _verify_committee_decision(ctx: RequestContext, spec, payload: EvidenceIn,  # noqa: ANN001, ANN202
                                     authority: set[str] | None = None,
                                     authority_label: str = "committee"):
    """Resolve ``decision_ref`` against the durable single-winner workflow-decision record and PROVE
    it authorises this governance evidence: it exists, its outcome matches the kind's required
    outcome, it is bound to THIS tenant + subject, and it was recorded by committee authority.
    Returns the decision row, whose (workflow_id, run_id, decided_by) become the evidence's
    authoritative provenance."""
    if not payload.decision_ref:
        raise ValidationAppError(
            f"{payload.evidence_kind!r} must cite the authoritative committee decision_ref.")
    decision = (await ctx.session.execute(select(WorkflowDecision).where(
        WorkflowDecision.tenant_id == ctx.tenant_id,
        WorkflowDecision.workflow_id == payload.decision_ref))).scalar_one_or_none()
    if decision is None:
        raise ValidationAppError(
            f"decision_ref {payload.decision_ref!r} does not resolve to a recorded committee "
            "decision for this tenant.")
    if decision.decision != spec.decision_outcome:
        raise ValidationAppError(
            f"The cited decision is {decision.decision!r}, not {spec.decision_outcome!r}; "
            f"{payload.evidence_kind!r} cannot be filed against it.")
    if (decision.subject_type != payload.subject_type
            or str(decision.subject_id) != str(payload.subject_id)):
        raise ValidationAppError(
            "The cited decision is for a different subject than this evidence.")
    if not (set(decision.roles or []) & (authority or _COMMITTEE_AUTHORITY)):
        raise ValidationAppError(
            f"The cited decision was not recorded by {authority_label} authority.")
    return decision


async def _verify_advaya_handoff(ctx: RequestContext, payload: EvidenceIn):  # noqa: ANN202
    """Resolve ``decision_ref`` (the handoff key) against an ACCEPTED Advaya-handoff record and PROVE
    it authorises this acknowledgement: it exists for this tenant, was Accepted, is bound to THIS
    Lending subject, and its payload digest matches the ``sha256`` supplied. Returns the handoff row,
    whose (workflow_id, run_id) become the evidence's authoritative provenance."""
    if not payload.decision_ref:
        raise ValidationAppError(
            "advaya_acknowledgement must cite the Advaya handoff key (decision_ref).")
    if not payload.sha256:
        raise ValidationAppError(
            "advaya_acknowledgement requires the handoff payload sha256 digest.")
    if payload.subject_type != "Lending":
        raise ValidationAppError("advaya_acknowledgement applies to a Lending line.")
    handoff = (await ctx.session.execute(select(AdvayaHandoff).where(
        AdvayaHandoff.tenant_id == ctx.tenant_id,
        AdvayaHandoff.handoff_key == payload.decision_ref,
        AdvayaHandoff.deleted_at.is_(None)))).scalar_one_or_none()
    if handoff is None:
        raise ValidationAppError(
            f"decision_ref {payload.decision_ref!r} does not resolve to an Advaya handoff.")
    if handoff.status != "Accepted":
        raise ValidationAppError(
            f"The Advaya handoff is {handoff.status!r}, not 'Accepted'; the disbursement "
            "acknowledgement cannot be filed against it.")
    if str(handoff.lending_id) != str(payload.subject_id):
        raise ValidationAppError("The Advaya handoff is for a different Lending line.")
    if handoff.payload_sha256 != payload.sha256:
        raise ValidationAppError(
            "The supplied digest does not match the accepted Advaya handoff's payload hash.")
    return handoff


async def _verify_cpcs_checklist(ctx: RequestContext, payload: EvidenceIn):  # noqa: ANN202
    """Resolve ``decision_ref`` (the CP/CS checklist id) against an APPROVED checklist and PROVE it
    authorises this cp_cs_completion: it exists for this tenant, is 'Approved', is bound to THIS
    Lending line, and was approved by a DIFFERENT checker than its preparer (maker-checker). The
    checklist (id + version) becomes the evidence's authoritative provenance — so cp_cs_completion
    can no longer be caller-attached without the underlying authoritative checklist."""
    if payload.subject_type != "Lending":
        raise ValidationAppError("cp_cs_completion applies to a Lending line.")
    if not payload.decision_ref:
        raise ValidationAppError(
            "cp_cs_completion must cite the approved CP/CS checklist id (decision_ref).")
    try:
        cid = uuid.UUID(payload.decision_ref)
    except (ValueError, AttributeError):
        raise ValidationAppError(
            "cp_cs_completion decision_ref must be the CP/CS checklist id.") from None
    chk = (await ctx.session.execute(select(CpcsChecklist).where(
        CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.id == cid,
        CpcsChecklist.deleted_at.is_(None)))).scalar_one_or_none()
    if chk is None:
        raise ValidationAppError(
            f"decision_ref {payload.decision_ref!r} does not resolve to a CP/CS checklist.")
    if chk.status != "Approved":
        raise ValidationAppError(
            f"The CP/CS checklist is {chk.status!r}, not 'Approved'; cp_cs_completion cannot be "
            "filed against it.")
    if str(chk.lending_id) != str(payload.subject_id):
        raise ValidationAppError("The CP/CS checklist is for a different Lending line.")
    if not chk.approved_by_id or chk.approved_by_id == chk.prepared_by_id:
        raise ValidationAppError(
            "The CP/CS checklist must be approved by a DIFFERENT checker than its preparer.")
    return chk


def _serialize(row: GovernanceEvidence, invalid_ids: set) -> dict[str, Any]:
    return {
        "id": str(row.id), "subject_type": row.subject_type,
        "subject_id": str(row.subject_id), "evidence_kind": row.evidence_kind,
        "reference": row.reference, "sha256": row.sha256, "note": row.note,
        "recorded_by": row.recorded_by, "workflow_id": row.workflow_id, "run_id": row.run_id,
        "decision_ref": row.decision_ref,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "valid": row.id not in invalid_ids,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("", status_code=201,
             summary="Attach an authorised, provenance-verified governance-evidence record")
async def attach_evidence(payload: EvidenceIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    _require_identity(ctx)
    spec = spec_for_kind(payload.evidence_kind)
    if spec is None:
        raise ValidationAppError(
            f"Unknown evidence_kind {payload.evidence_kind!r}; kinds are a controlled vocabulary.")
    if payload.subject_type not in spec.subject_types:
        raise ValidationAppError(
            f"Evidence kind {payload.evidence_kind!r} may not be attached to a "
            f"{payload.subject_type} (allowed: {sorted(spec.subject_types)}).")
    # AUTHORISATION BY KIND — fail-closed for a user/role or named service without the grant.
    granted = enforce_operation(ctx.user, spec.operation)
    subject_uuid = _parse_uuid(payload.subject_id, "subject_id")
    subject = await _subject_or_404(ctx, payload.subject_type, subject_uuid)
    await _enforce_subject_scope(ctx, payload.subject_type, subject, granted)

    # PROVENANCE — dispatch on the kind's authoritative verification source.
    prov_workflow, prov_run, decision_ref = payload.workflow_id, payload.run_id, payload.decision_ref
    if spec.verify_source == "committee":
        # Committee/sanction: VERIFY against the durable decision and generate provenance from it.
        if not payload.sha256:
            raise ValidationAppError(
                f"{payload.evidence_kind!r} is governance evidence and requires a sha256 digest.")
        decision = await _verify_committee_decision(ctx, spec, payload)
        prov_workflow, prov_run = decision.workflow_id, decision.run_id
        decision_ref = decision.workflow_id
    elif spec.verify_source == "syndication":
        # Syndication sanction: VERIFY against the durable syndication decision (Syn Head
        # authority, subject-bound) and generate provenance from it.
        if not payload.sha256:
            raise ValidationAppError(
                f"{payload.evidence_kind!r} is governance evidence and requires a sha256 digest.")
        decision = await _verify_committee_decision(
            ctx, spec, payload, authority=_SYNDICATION_AUTHORITY,
            authority_label="syndication")
        prov_workflow, prov_run = decision.workflow_id, decision.run_id
        decision_ref = decision.workflow_id
    elif spec.verify_source == "asset_mon":
        # AM closure approval: VERIFY against the durable asset-monetisation decision
        # (AM Head authority, subject-bound) and generate provenance from it.
        if not payload.sha256:
            raise ValidationAppError(
                f"{payload.evidence_kind!r} is governance evidence and requires a sha256 digest.")
        decision = await _verify_committee_decision(
            ctx, spec, payload, authority=_AM_AUTHORITY,
            authority_label="asset-monetisation")
        prov_workflow, prov_run = decision.workflow_id, decision.run_id
        decision_ref = decision.workflow_id
    elif spec.verify_source == "cpcs":
        # CP/CS completion: VERIFY against an Approved maker-checker checklist and generate
        # provenance from it — so it is no longer caller-attached.
        checklist = await _verify_cpcs_checklist(ctx, payload)
        prov_workflow, prov_run = f"cpcs:{checklist.id}", str(checklist.checklist_version)
        decision_ref = str(checklist.id)
    elif spec.verify_source == "advaya":
        # Advaya ack: DORMANT unless a real Advaya integration is enabled — the acknowledgement path
        # is not executable by default (no synthetic disbursement).
        from app.core.config import get_settings
        if not get_settings().advaya_integration_enabled:
            raise ValidationAppError(
                "advaya_acknowledgement is disabled: there is no Advaya integration "
                "(REGISTER_ADVAYA_INTEGRATION_ENABLED is off). The current terminal is "
                "'Disbursed'.")
        # VERIFY against an Accepted Advaya-handoff record (matching payload digest), and generate
        # provenance from it — so it can't be manufactured with invented values.
        handoff = await _verify_advaya_handoff(ctx, payload)
        prov_workflow, prov_run = handoff.workflow_id, handoff.run_id
        decision_ref = handoff.handoff_key
    elif spec.governance:
        # Governance but with no external authoritative record (executed_agreement / cp_cs): still
        # require a digest AND the workflow run that produced it.
        if not payload.sha256:
            raise ValidationAppError(
                f"{payload.evidence_kind!r} is governance evidence and requires a sha256 digest.")
        if not (payload.workflow_id and payload.run_id):
            raise ValidationAppError(
                f"{payload.evidence_kind!r} must cite its workflow_id and run_id.")
        # PRESENCE was not enough: a caller could cite an invented run and the provenance would be
        # recorded as fact. The cited workflow must RESOLVE to a decision recorded for THIS tenant
        # and THIS subject, so the citation is verifiable after the fact.
        cited = (await ctx.session.execute(select(WorkflowDecision).where(
            WorkflowDecision.tenant_id == ctx.tenant_id,
            WorkflowDecision.workflow_id == payload.workflow_id))).scalar_one_or_none()
        if cited is None:
            raise ValidationAppError(
                f"workflow_id {payload.workflow_id!r} does not resolve to a recorded workflow "
                f"decision for this tenant; {payload.evidence_kind!r} cannot cite it.")
        if (cited.subject_type != payload.subject_type
                or str(cited.subject_id) != str(payload.subject_id)):
            raise ValidationAppError(
                f"The cited workflow {payload.workflow_id!r} belongs to a different subject "
                f"({cited.subject_type} {cited.subject_id}) than this evidence.")

    # SUPERSESSION integrity — the prior row must be the SAME subject AND kind, this tenant, and
    # currently valid, so a scoped document authority cannot supersede committee evidence.
    supersedes_uuid: uuid.UUID | None = None
    if payload.supersedes_id:
        supersedes_uuid = _parse_uuid(payload.supersedes_id, "supersedes_id")
        prior = (await ctx.session.execute(select(GovernanceEvidence).where(
            GovernanceEvidence.id == supersedes_uuid,
            GovernanceEvidence.tenant_id == ctx.tenant_id,
            GovernanceEvidence.deleted_at.is_(None)))).scalar_one_or_none()
        if prior is None:
            raise ValidationAppError("supersedes_id does not reference an evidence row.")
        if (prior.subject_type != payload.subject_type
                or str(prior.subject_id) != str(payload.subject_id)
                or prior.evidence_kind != payload.evidence_kind):
            raise ValidationAppError(
                "A superseding row must have the SAME subject and evidence_kind as the row it "
                "replaces.")
        if await _has_terminal_status(ctx, prior.id):
            raise ValidationAppError("The row being superseded is no longer valid.")

    actor = ctx.user.email if ctx.user is not None else ctx.actor
    row = GovernanceEvidence(
        tenant_id=ctx.tenant_id, subject_type=payload.subject_type,
        subject_id=subject_uuid, evidence_kind=payload.evidence_kind,
        reference=payload.reference.strip(), sha256=(payload.sha256 or None),
        note=payload.note, recorded_by=actor, workflow_id=prov_workflow,
        run_id=prov_run, decision_ref=decision_ref,
        supersedes_id=supersedes_uuid, created_by=ctx.actor)
    ctx.session.add(row)
    try:
        await ctx.session.flush()
    except Exception as exc:  # noqa: BLE001 - translate the unique-decision violation
        from sqlalchemy.exc import IntegrityError
        if isinstance(exc, IntegrityError):
            raise ConflictError(
                "This decision already backs an evidence record of this kind.") from exc
        raise
    if supersedes_uuid is not None:
        ctx.session.add(GovernanceEvidenceStatus(
            tenant_id=ctx.tenant_id, evidence_id=supersedes_uuid, status="Superseded",
            reason=f"Superseded by {row.id}.", actor=actor, created_by=ctx.actor))
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="evidence.attach",
        resource_type="governance_evidence", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"subject_type": payload.subject_type, "subject_id": payload.subject_id,
                 "evidence_kind": payload.evidence_kind, "reference": payload.reference.strip(),
                 "sha256": payload.sha256, "workflow_id": prov_workflow, "run_id": prov_run,
                 "decision_ref": decision_ref, "supersedes_id": payload.supersedes_id,
                 "by": actor}))
    return _serialize(row, set())


async def _has_terminal_status(ctx: RequestContext, evidence_id: uuid.UUID) -> bool:
    found = (await ctx.session.execute(select(GovernanceEvidenceStatus.id).where(
        GovernanceEvidenceStatus.evidence_id == evidence_id,
        GovernanceEvidenceStatus.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
    return found is not None


@router.post("/{evidence_id}/revoke",
             summary="Append a terminal status so the gate no longer accepts this evidence")
async def revoke_evidence(evidence_id: str, payload: RevokeIn,
                          ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    _require_identity(ctx)
    eid = _parse_uuid(evidence_id, "evidence_id")
    # Lock the evidence row so two concurrent revocations serialise (no repeated/contradictory
    # terminal statuses race in).
    row = (await ctx.session.execute(select(GovernanceEvidence).where(
        GovernanceEvidence.id == eid,
        GovernanceEvidence.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Evidence '{evidence_id}' not found.")
    spec = spec_for_kind(row.evidence_kind)
    granted = None
    if spec is not None:
        granted = enforce_operation(ctx.user, spec.operation)  # same authority that could attach
    # Subject scope is enforced for revocation exactly as for attachment.
    subject = await _subject_or_404(ctx, row.subject_type, row.subject_id)
    await _enforce_subject_scope(ctx, row.subject_type, subject, granted)
    if payload.status not in EVIDENCE_STATUSES:
        raise ValidationAppError(f"status must be one of {EVIDENCE_STATUSES}.")
    if await _has_terminal_status(ctx, row.id):
        raise ConflictError("This evidence already carries a terminal status.")
    actor = ctx.user.email if ctx.user is not None else ctx.actor
    ctx.session.add(GovernanceEvidenceStatus(
        tenant_id=ctx.tenant_id, evidence_id=row.id, status=payload.status,
        reason=payload.reason.strip(), actor=actor, created_by=ctx.actor))
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="evidence.revoke",
        resource_type="governance_evidence", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"status": payload.status, "reason": payload.reason.strip(),
                 "evidence_kind": row.evidence_kind, "subject_type": row.subject_type,
                 "subject_id": str(row.subject_id), "by": actor}))
    return {"id": str(row.id), "status": payload.status}


@router.get("", summary="List governance evidence on file for a subject (subject-scope enforced)")
async def list_evidence(ctx: RequestContext = Depends(get_context),
                        subject_type: str = Query(min_length=1, max_length=40),
                        subject_id: str = Query(min_length=1)) -> dict[str, Any]:
    _require_identity(ctx)
    sid = _parse_uuid(subject_id, "subject_id")
    subject = await _subject_or_404(ctx, subject_type, sid)
    await _enforce_subject_scope(ctx, subject_type, subject)   # subject-level read authorisation
    rows = (await ctx.session.execute(
        select(GovernanceEvidence).where(
            GovernanceEvidence.subject_type == subject_type,
            GovernanceEvidence.subject_id == sid,
            GovernanceEvidence.deleted_at.is_(None))
        .order_by(GovernanceEvidence.created_at.asc()))).scalars().all()
    invalid: set = set()
    if rows:
        invalid = set((await ctx.session.execute(
            select(GovernanceEvidenceStatus.evidence_id).where(
                GovernanceEvidenceStatus.evidence_id.in_([r.id for r in rows]),
                GovernanceEvidenceStatus.deleted_at.is_(None)))).scalars().all())
    return {"items": [_serialize(r, invalid) for r in rows]}
