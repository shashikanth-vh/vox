"""The MANUAL Advaya attestation lane — production reality before the API integration.

Advaya confirms offline (an acceptance letter, a UTR, an email); an AUTHORISED EVAM
user finishes the flow in PRISM on Advaya's behalf, on their OWN verified identity,
citing the offline artefact. Production-grade means:

* **human lane** — gateway-routed, OIDC-verified caller; a service key is refused
  here (machines use ``/v1/internal/…``). The write is attributed to the person.
* **authority-gated** — the same senior-credit authority that approves the handover
  (``approve_advaya_handover``: Credit Head / Management / Admin), company-scoped.
* **artefact-cited** — ``reference`` (Advaya's letter no. / UTR / ack id) is
  MANDATORY; it becomes the handoff acknowledgement / tranche reference, so every
  attestation points at the offline document it relays. It also keys idempotency:
  re-sending the same reference replays, it never duplicates.
* **same machinery** — the attestation drives EXACTLY the code the machine lane
  drives (``apply_handoff`` / ``apply_tranche``): same package settlement, digest
  from PRISM's OWN submitted package (a human never types a hash), same ceilings,
  actuals and stage move. Downstream logic cannot tell the lanes apart — only the
  provenance can: rows and audit entries carry ``source=manual-attestation``.

    POST /v1/lending/{lending_id}/advaya-events
        {"event": "accepted"|"rejected"|"disbursed", "reference": "...",
         "note"?, "amount_cr"? (disbursed), "disbursed_on"? (disbursed)}

When the real Advaya integration goes live, disable this lane (it is one route) and
nothing else changes — both lanes fed the same state machine all along.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.advaya import HandoffIn, apply_handoff
from app.api.custom import _ensure_subject_scope
from app.api.tranches import TrancheIn, apply_tranche
from app.core.errors import ConflictError, ForbiddenError, ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.models.advaya import AdvayaHandoverPackage

router = api_router()

_SOURCE = "manual-attestation"


class AdvayaEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(pattern="^(accepted|rejected|disbursed)$")
    # Advaya's offline artefact — letter number / UTR / acknowledgement id. Mandatory:
    # an attestation without a citable document is just an assertion.
    reference: str = Field(min_length=3, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    # 'disbursed' only: the tranche amount Advaya confirmed (₹ Cr) and its value date.
    amount_cr: float | None = Field(default=None, gt=0)
    disbursed_on: date | None = None


@router.post("/v1/lending/{lending_id}/advaya-events", tags=["Advaya"], status_code=201,
             summary="Record Advaya's OFFLINE confirmation (manual attestation, human lane)")
async def record_manual_advaya_event(
    lending_id: uuid.UUID,
    payload: AdvayaEventIn,
    ctx: RequestContext = Depends(get_context),
) -> dict[str, Any]:
    if ctx.user is None:
        raise ForbiddenError(
            "Manual Advaya events need a verified HUMAN identity — the attestation is "
            "attributed to the person recording it. A machine integration uses the "
            "service lane (/v1/internal/…).")
    # The same authority that approves the handover may attest its outcome; a SCOPED
    # grant must also cover this line's company.
    await _ensure_subject_scope(ctx, "approve_advaya_handover", "Lending", lending_id)
    lid = str(lending_id)

    if payload.event == "disbursed":
        if payload.amount_cr is None:
            raise ValidationAppError(
                "A 'disbursed' event needs amount_cr — the tranche amount Advaya "
                "confirmed.")
        tranche = await apply_tranche(ctx, lid, TrancheIn(
            tranche_ref=payload.reference, amount=payload.amount_cr,
            disbursed_on=payload.disbursed_on, advaya_reference=payload.reference,
            note=payload.note), source=_SOURCE)
        return {"event": "disbursed", "source": _SOURCE,
                "recorded_by": ctx.user.email, "tranche": tranche}

    # accepted / rejected settle the SUBMITTED package. The digest the outcome binds
    # to is PRISM's own submitted package digest — the attester cites the artefact,
    # never a hash.
    pkg = (await ctx.session.execute(select(AdvayaHandoverPackage).where(
        AdvayaHandoverPackage.tenant_id == ctx.tenant_id,
        AdvayaHandoverPackage.lending_id == lid,
        AdvayaHandoverPackage.deleted_at.is_(None)))).scalar_one_or_none()
    if pkg is None or not pkg.package_sha256:
        raise ConflictError(
            f"No handover package exists for Lending line {lid!r}; an Advaya outcome "
            "cannot be attested for a package PRISM never made.")
    status = "Accepted" if payload.event == "accepted" else "Rejected"
    handoff = await apply_handoff(ctx, HandoffIn(
        handoff_key=f"advaya-handoff:{lid}:manual:{payload.reference}",
        lending_id=lid, payload_sha256=pkg.package_sha256, status=status,
        acknowledgement_id=payload.reference, note=payload.note), source=_SOURCE)
    return {"event": payload.event, "source": _SOURCE,
            "recorded_by": ctx.user.email, "handoff": handoff}
