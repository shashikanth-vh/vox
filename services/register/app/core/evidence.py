"""Loading + break-glass helpers for the evidence-based lifecycle gates.

The shared policy engine (:data:`evam_backend_core.policy.EVIDENCE_FOR_STAGE`) decides WHICH
evidence kinds a sensitive stage requires; this module supplies the register-side plumbing that
tells it which kinds are actually ON FILE for a subject, and governs the audited break-glass that
is the only way past a missing-evidence gate."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.security import RequestContext
from app.models.evidence import GovernanceEvidence, GovernanceEvidenceStatus


async def load_evidence_kinds(ctx: RequestContext, subject_type: str,
                              subject_id: uuid.UUID) -> set[str]:
    """The set of CURRENTLY-VALID evidence KINDS recorded for one business record, which the policy
    engine's evidence gate checks against the kinds the target stage requires.

    Only evidence that has NO terminal status event ('Revoked' / 'Invalidated' / 'Superseded') in
    the append-only status ledger counts — so a mistaken or fraudulent attachment that has since
    been revoked no longer satisfies the gate, even though its (immutable) row and history remain."""
    rows = (await ctx.session.execute(
        select(GovernanceEvidence.id, GovernanceEvidence.evidence_kind).where(
            GovernanceEvidence.subject_type == subject_type,
            GovernanceEvidence.subject_id == subject_id,
            GovernanceEvidence.deleted_at.is_(None)))).all()
    if not rows:
        return set()
    invalid = set((await ctx.session.execute(
        select(GovernanceEvidenceStatus.evidence_id).where(
            GovernanceEvidenceStatus.evidence_id.in_([r[0] for r in rows]),
            GovernanceEvidenceStatus.deleted_at.is_(None)))).scalars().all())
    return {kind for eid, kind in rows if eid not in invalid}


def break_glass_allowed(ctx: RequestContext) -> bool:
    """WHO may bypass a missing-evidence gate: only a designated senior authority — Admin or
    Management — and never a service (a machine caller has no user). The bypass is always audited
    by the caller that grants it."""
    return ctx.user is not None and (ctx.user.is_admin or "Management" in ctx.user.roles)
