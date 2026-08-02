"""Advaya handover — the durable, immutable handover PACKAGE with a real maker-checker.

Two phases, distinct authenticated identities, package integrity verified server-side:

* **Prepare** (`POST /v1/internal/handover-packages`, ``record_handover_package``). The MAKER
  drafts the package. The Register loads the Lending row server-side, confirms it is
  'Ready for Disbursement', and VERIFIES a COMPLETE package:
    - executed-document references are non-empty and reconciled against the on-file
      ``executed_agreement`` evidence (the executed agreement's digest must appear among them);
    - the CP/CS checklist version is reconciled against the APPROVED checklist that minted
      ``cp_cs_completion`` (mismatch refused; omitted → filled from the checklist);
    - delivery method + recipient are present.
  It then GENERATES the package manifest, computes its digest **server-side**, stores it, and
  writes a **Prepared** package — WITHOUT advancing the stage. The maker's identity comes from the
  authenticated context, never a submitted name.

* **Approve** (`POST /v1/internal/handover-packages/{lending_id}/approve`, ``approve_advaya_handover``).
  A DIFFERENT CHECKER (authenticated) approves — package 'Approved'. The Lending stage does
  NOT move: PRISM's workflow boundary is Advaya's ACCEPTANCE, not PRISM's own approval.

* **Submit** (`POST /v1/internal/handover-packages/{lending_id}/submit`). The approved package is
  marked 'Submitted' to Advaya. Advaya's answer arrives on ``/v1/internal/advaya-handoffs``
  (service lane): **Accepted** settles the package (freezing it, storing the acknowledgement as
  the one-time ``advaya_reference``); **Rejected** reopens the loop — correct, re-prepare,
  resubmit. 'Disbursed' is set ONLY by Advaya's disbursement tranches after acceptance.

    GET  /v1/lending/{id}/handover-package           read it (workspace / audit timeline)
    POST /v1/lending/{id}/handover-package/download   the generated package document + digest
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import date
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError, ValidationAppError
from app.core.evidence import load_evidence_kinds
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.advaya import AdvayaHandoverPackage
from app.models.cpcs import CpcsChecklist
from app.models.evidence import GovernanceEvidence, GovernanceEvidenceStatus
from app.models.trackers import LendingTracker

router = api_router()

_READY = "Ready for Disbursement"
_REQUIRED_EVIDENCE = {"cp_cs_completion", "executed_agreement"}


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class HandoverIn(BaseModel):
    """The MAKER's draft. Identities are NOT accepted here — the maker is the authenticated caller;
    the package reference/digest are GENERATED server-side."""

    model_config = ConfigDict(extra="forbid")
    lending_id: str = Field(min_length=1, max_length=64)
    executed_document_refs: list[DocumentRef] = Field(min_length=1)
    cpcs_checklist_version: int | None = Field(default=None, ge=1)
    delivery_method: str = Field(min_length=1, max_length=60)
    recipient: str = Field(min_length=1, max_length=200)
    note: str | None = None


def _ident(ctx: RequestContext) -> tuple[str, str]:
    """The authenticated (name, id) — id from the verified user, else the actor (service)."""
    name = ctx.actor
    uid = str(ctx.user.id) if ctx.user else ctx.actor
    return name, uid


def _serialize(row: AdvayaHandoverPackage) -> dict[str, Any]:
    def _num(v: Any) -> float | None:
        return float(v) if v is not None else None

    def _d(v: date | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "id": str(row.id), "handover_key": row.handover_key, "lending_id": row.lending_id,
        "deal_id": row.deal_id, "status": row.status,
        "facility_amount": _num(row.facility_amount),
        "proposed_disbursement_amount": _num(row.proposed_disbursement_amount),
        "proposed_disbursement_date": _d(row.proposed_disbursement_date),
        "cpcs_checklist_version": row.cpcs_checklist_version,
        "executed_document_refs": row.executed_document_refs or [],
        "package_reference": row.package_reference, "package_sha256": row.package_sha256,
        "initiated_by": row.initiated_by, "initiated_by_id": row.initiated_by_id,
        "approved_by": row.approved_by, "approved_by_id": row.approved_by_id,
        "delivery_method": row.delivery_method, "recipient": row.recipient,
        "advaya_reference": row.advaya_reference, "note": row.note,
        "snapshot": row.snapshot,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _package_for_lending(ctx: RequestContext, lending_id: str) -> AdvayaHandoverPackage | None:
    return (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lending_id,
        AdvayaHandoverPackage.deleted_at.is_(None)))).scalar_one_or_none()


async def _valid_evidence(ctx: RequestContext, subject_id: uuid.UUID, kind: str
                          ) -> list[GovernanceEvidence]:
    """Currently-valid GovernanceEvidence rows of ``kind`` for the subject (no terminal status)."""
    rows = list((await ctx.session.execute(select(GovernanceEvidence).where(
        GovernanceEvidence.subject_type == "Lending", GovernanceEvidence.subject_id == subject_id,
        GovernanceEvidence.evidence_kind == kind,
        GovernanceEvidence.deleted_at.is_(None)))).scalars().all())
    if not rows:
        return []
    invalid = set((await ctx.session.execute(select(GovernanceEvidenceStatus.evidence_id).where(
        GovernanceEvidenceStatus.evidence_id.in_([r.id for r in rows]),
        GovernanceEvidenceStatus.deleted_at.is_(None)))).scalars().all())
    return [r for r in rows if r.id not in invalid]


@router.post("/v1/internal/handover-packages", tags=["Internal"], status_code=201,
             summary="MAKER prepares the Advaya handover package (does NOT advance the stage)")
async def prepare_handover_package(payload: HandoverIn,
                                   ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "record_handover_package")

    try:
        lid = uuid.UUID(payload.lending_id)
    except (ValueError, AttributeError):
        raise ValidationAppError("lending_id must be a valid id.") from None

    existing = await _package_for_lending(ctx, payload.lending_id)
    if existing is not None and existing.status not in ("Returned", "Rejected"):
        # A handover is already in flight for this line — idempotent for the maker.
        return _serialize(existing)

    line = (await ctx.session.execute(select(LendingTracker).where(
        LendingTracker.tenant_id == ctx.tenant_id, LendingTracker.id == lid,
        LendingTracker.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if line is None:
        raise NotFoundError(f"No Lending line {payload.lending_id!r}.")
    if line.stage != _READY:
        raise ConflictError(
            f"Lending line is {line.stage!r}, not {_READY!r}; it cannot be handed over.")
    if line.proposed_disbursement_amount is None or line.proposed_disbursement_date is None:
        raise ValidationAppError(
            "proposed_disbursement_amount and proposed_disbursement_date must be set before "
            "handover.")

    # Evidence must be on file (currently valid).
    have = await load_evidence_kinds(ctx, "Lending", lid)
    missing = _REQUIRED_EVIDENCE - have
    if missing:
        raise ValidationAppError(
            f"Handover requires evidence {sorted(_REQUIRED_EVIDENCE)}; missing {sorted(missing)}.")

    # (1) Executed-document refs must reconcile with the on-file executed_agreement evidence — the
    # agreement's digest must be present among the submitted refs.
    ea = await _valid_evidence(ctx, lid, "executed_agreement")
    ea_digests = {e.sha256.lower() for e in ea if e.sha256}
    ref_digests = {d.sha256.lower() for d in payload.executed_document_refs}
    if ea_digests and not (ea_digests & ref_digests):
        raise ValidationAppError(
            "executed_document_refs do not include the on-file executed_agreement digest — the "
            "handover package must reference the executed agreement.")

    # (2) CP/CS checklist version must reconcile with the APPROVED checklist that minted
    # cp_cs_completion (via that evidence's decision_ref).
    cp = await _valid_evidence(ctx, lid, "cp_cs_completion")
    checklist_version: int | None = payload.cpcs_checklist_version
    decision_refs = [e.decision_ref for e in cp if e.decision_ref]
    if decision_refs:
        try:
            cid: uuid.UUID | None = uuid.UUID(decision_refs[0])
        except (ValueError, AttributeError):
            cid = None
        chk = (await ctx.session.execute(select(CpcsChecklist).where(
            CpcsChecklist.tenant_id == ctx.tenant_id, CpcsChecklist.id == cid))
            ).scalar_one_or_none() if cid else None
        if chk is not None:
            if checklist_version is None:
                checklist_version = chk.checklist_version
            elif checklist_version != chk.checklist_version:
                raise ValidationAppError(
                    f"cpcs_checklist_version {checklist_version} does not match the approved CP/CS "
                    f"checklist (v{chk.checklist_version}) that generated cp_cs_completion.")

    # (3) GENERATE the package manifest and compute its digest server-side, then store it.
    maker_name, maker_id = _ident(ctx)
    handover_key = f"advaya-handover:{payload.lending_id}"
    facility_amount = float(line.amount_cr) if line.amount_cr is not None else None
    prop_amt = float(line.proposed_disbursement_amount)
    prop_date = line.proposed_disbursement_date
    doc_refs = [d.model_dump() for d in payload.executed_document_refs]
    manifest = {
        "handover_key": handover_key, "lending_id": payload.lending_id,
        "deal_id": str(line.deal_id) if line.deal_id else None,
        "facility_amount": facility_amount, "proposed_disbursement_amount": prop_amt,
        "proposed_disbursement_date": prop_date.isoformat() if prop_date else None,
        "cpcs_checklist_version": checklist_version, "executed_document_refs": doc_refs,
        "delivery_method": payload.delivery_method, "recipient": payload.recipient,
        "prepared_by": maker_name, "note": payload.note,
        "evidence_on_file": sorted(have & _REQUIRED_EVIDENCE),
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    package_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    package_reference = f"handover/{ctx.tenant_code}/{payload.lending_id}.json"
    package_document = base64.b64encode(manifest_bytes).decode()

    snapshot = {**manifest, "package_reference": package_reference,
                "package_sha256": package_sha256, "stage_at_preparation": _READY,
                "request_id": request_id_ctx.get()}
    if existing is not None:
        # RE-PREPARE after a checker return: the same row is rebuilt (fresh manifest,
        # digest and maker identity) and goes back to 'Prepared' — the return reasons stay
        # in the audit trail; the package the checker sees is always the CURRENT one.
        row = existing
        row.facility_amount = facility_amount
        row.proposed_disbursement_amount = prop_amt
        row.proposed_disbursement_date = prop_date
        row.cpcs_checklist_version = checklist_version
        row.executed_document_refs = doc_refs
        row.package_reference = package_reference
        row.package_sha256 = package_sha256
        row.package_document = package_document
        row.initiated_by = maker_name
        row.initiated_by_id = maker_id
        row.delivery_method = payload.delivery_method
        row.recipient = payload.recipient
        row.status = "Prepared"
        row.note = payload.note
        row.snapshot = snapshot
        row.updated_by = ctx.actor
        action = "advaya.handover.reprepare"
    else:
        row = AdvayaHandoverPackage(
            tenant_id=ctx.tenant_id, handover_key=handover_key, lending_id=payload.lending_id,
            deal_id=str(line.deal_id) if line.deal_id else None,
            facility_amount=facility_amount,
            proposed_disbursement_amount=prop_amt, proposed_disbursement_date=prop_date,
            cpcs_checklist_version=checklist_version, executed_document_refs=doc_refs,
            package_reference=package_reference, package_sha256=package_sha256,
            package_document=package_document, initiated_by=maker_name,
            initiated_by_id=maker_id,
            delivery_method=payload.delivery_method, recipient=payload.recipient,
            status="Prepared", note=payload.note, snapshot=snapshot, created_by=ctx.actor)
        ctx.session.add(row)
        action = "advaya.handover.prepare"
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action=action,
        resource_type="advaya_handover_packages", resource_id=str(row.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": payload.lending_id, "handover_key": handover_key,
                 "package_sha256": package_sha256}))
    return _serialize(row)


@router.post("/v1/internal/handover-packages/{lending_id}/approve", tags=["Internal"],
             summary="CHECKER approves the handover (different person) — the stage does NOT move")
async def approve_handover_package(lending_id: str,
                                   ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """Internal approval only. PRISM's boundary is Advaya's ACCEPTANCE: approving the
    package makes it submittable; it does not assert that anything reached Advaya, and it
    never advances the Lending stage — 'Disbursed' is set only by Advaya's own
    disbursement callbacks once the handoff is Accepted."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_advaya_handover")
    pkg = (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lending_id,
        AdvayaHandoverPackage.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if pkg is None:
        raise NotFoundError(f"No prepared handover package for Lending line {lending_id!r}.")
    if pkg.status != "Prepared":
        # Already approved / further along — idempotent for the checker.
        return _serialize(pkg)

    checker_name, checker_id = _ident(ctx)
    # MAKER-CHECKER: the approver must be a DIFFERENT person than the preparer.
    if checker_id == pkg.initiated_by_id:
        raise ValidationAppError(
            "The handover must be approved by a DIFFERENT checker than the maker who prepared it.")

    pkg.approved_by = checker_name
    pkg.approved_by_id = checker_id
    pkg.status = "Approved"
    pkg.updated_by = ctx.actor
    if isinstance(pkg.snapshot, dict):
        pkg.snapshot = {**pkg.snapshot, "approved_by": checker_name}
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="advaya.handover.approve",
        resource_type="advaya_handover_packages", resource_id=str(pkg.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "from": "Prepared", "to": "Approved",
                 "maker": pkg.initiated_by, "checker": checker_name}))
    return _serialize(pkg)


@router.post("/v1/internal/handover-packages/{lending_id}/submit", tags=["Internal"],
             summary="SUBMIT the approved package to Advaya (recorded intent, not acceptance)")
async def submit_handover_package(lending_id: str,
                                  ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """Marks the approved package as sent to Advaya. Submission is PRISM's claim; the
    boundary state is Advaya's answer — ``/v1/internal/advaya-handoffs`` records the
    Accepted/Rejected outcome and only THAT settles the package."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_advaya_handover")
    pkg = (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lending_id,
        AdvayaHandoverPackage.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if pkg is None:
        raise NotFoundError(f"No handover package for Lending line {lending_id!r}.")
    if pkg.status == "Submitted":
        return _serialize(pkg)                      # idempotent resend
    if pkg.status != "Approved":
        raise ConflictError(
            f"Package is {pkg.status!r}; only an Approved package can be submitted "
            "(prepare → approve → submit → Advaya accepts/rejects).")
    submitter, _submitter_id = _ident(ctx)
    pkg.status = "Submitted"
    pkg.updated_by = ctx.actor
    if isinstance(pkg.snapshot, dict):
        pkg.snapshot = {**pkg.snapshot, "submitted_by": submitter,
                        "submitted_request_id": request_id_ctx.get()}
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="advaya.handover.submit",
        resource_type="advaya_handover_packages", resource_id=str(pkg.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "from": "Approved", "to": "Submitted",
                 "submitted_by": submitter}))
    return _serialize(pkg)


class HandoverReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A return without a reason is useless to the maker — the note is mandatory.
    note: str = Field(min_length=1, max_length=2000)


@router.post("/v1/internal/handover-packages/{lending_id}/return", tags=["Internal"],
             summary="CHECKER returns the handover package to its maker (with reasons)")
async def return_handover_package(lending_id: str, payload: HandoverReturnIn,
                                  ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """RETURN-TO-MAKER on the handover: the checker sends a 'Prepared' package back for
    amendment instead of approving it. The line does NOT move (it stays at 'Ready for
    Disbursement'); the maker re-prepares — same row, fresh manifest + digest — and a
    DIFFERENT checker approves the rebuilt package. Reasons are mandatory and audited."""
    from app.authz.engine import enforce_operation

    enforce_operation(ctx.user, "approve_advaya_handover")   # checker authority, like approve
    pkg = (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lending_id,
        AdvayaHandoverPackage.deleted_at.is_(None)).with_for_update())).scalar_one_or_none()
    if pkg is None:
        raise NotFoundError(f"No prepared handover package for Lending line {lending_id!r}.")
    if pkg.status != "Prepared":
        raise ConflictError(
            f"The handover package is {pkg.status!r}; only a 'Prepared' package can be "
            "returned to its maker.")
    checker_name, checker_id = _ident(ctx)
    if checker_id == pkg.initiated_by_id:
        raise ValidationAppError(
            "The handover must be returned by a DIFFERENT checker than the maker who "
            "prepared it.")
    pkg.status = "Returned"
    pkg.note = payload.note
    pkg.updated_by = ctx.actor
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=ctx.actor, action="advaya.handover.return",
        resource_type="advaya_handover_packages", resource_id=str(pkg.id),
        request_id=request_id_ctx.get(),
        changes={"lending_id": lending_id, "status": "Returned", "checker": checker_name,
                 "note": payload.note}))
    return _serialize(pkg)


@router.get("/v1/lending/{lending_id}/handover-package", tags=["Lending"],
            summary="Read a Lending line's Advaya handover package")
async def get_handover_package(lending_id: str,
                               ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_service_read

    enforce_service_read("/v1/lending", ctx.user)
    row = await _package_for_lending(ctx, lending_id)
    if row is None:
        raise NotFoundError(f"No handover package for Lending line {lending_id!r}.")
    return _serialize(row)


@router.post("/v1/lending/{lending_id}/handover-package/download", tags=["Lending"],
             summary="Download the generated handover package document + integrity digest")
async def download_handover_package(lending_id: str,
                                    ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    from app.authz.engine import enforce_service_read

    enforce_service_read("/v1/lending", ctx.user)
    row = await _package_for_lending(ctx, lending_id)
    if row is None:
        raise NotFoundError(f"No handover package for Lending line {lending_id!r}.")
    if not row.package_document:
        raise ValidationAppError("This handover package has no generated document on file.")
    # Integrity self-check: the stored digest must match the stored document.
    digest = hashlib.sha256(base64.b64decode(row.package_document)).hexdigest()
    if digest != row.package_sha256:
        raise ConflictError("Handover package digest mismatch — the document may be corrupt.")
    return {
        "handover_package_id": str(row.id), "lending_id": row.lending_id, "status": row.status,
        "package_reference": row.package_reference, "package_sha256": row.package_sha256,
        "content_type": "application/json", "encoding": "base64",
        "document_base64": row.package_document,
    }
