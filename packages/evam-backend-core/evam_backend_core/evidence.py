"""The authoritative governance-evidence registry — the single place that says, for every kind of
evidence the platform recognises, WHAT it evidences, WHICH subjects it may attach to, WHO is
authorised to attach it, and how STRICTLY it must be proven.

This closes the "any identified principal can manufacture sanction evidence" hole: evidence is no
longer a free-text string anyone may write. Each kind names the RBAC OPERATION a caller must hold
to attach it (so committee/sanction evidence is reserved to the designated committee / sanction
authorities and their workflow service — an RM, an Analyst or an unrelated service is refused), and
GOVERNANCE kinds additionally require an integrity digest and a binding to the authoritative
workflow/decision that produced them. Arbitrary kinds are rejected.

Kept in evam-backend-core so the Register (enforcement) and the gateway/orchestrator (front-door
mapping) share ONE definition and cannot drift."""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "EvidenceKindSpec", "EVIDENCE_KINDS", "DOCUMENT_KIND_PREFIX", "EVIDENCE_STATUSES",
    "spec_for_kind", "is_known_kind",
]

# Terminal statuses an evidence row can be given via the append-only status model. A row in any of
# these is no longer "currently valid" and the policy loader must ignore it.
EVIDENCE_STATUSES = ("Revoked", "Invalidated", "Superseded")

# Document evidence kinds are dynamic (one per required document name), so they share a controlled
# PREFIX rather than an enumerated entry: e.g. "document:facility_agreement".
DOCUMENT_KIND_PREFIX = "document:"


class EvidenceKindSpec(NamedTuple):
    """What it takes to attach one kind of evidence.

    * ``operation``   — the RBAC operation a caller MUST hold (matrix op / service grant). This is
      the authorisation gate: only roles/services granted this op may attach the kind.
    * ``subject_types`` — the subject types this kind may be attached to (a committee approval
      belongs on a Deal/Lending, never a Counterparty).
    * ``governance``  — a governance milestone: requires an integrity digest (sha256) AND a binding
      to the authoritative workflow + decision that produced it (workflow_id, run_id, decision_ref).
    * ``decision_outcome`` — when set (with ``verify_source='committee'``), the kind must be VERIFIED
      against a durable Credit Committee workflow-decision record whose outcome equals this value
      ('Approved' / 'Rejected'), bound to the same tenant + subject, recorded by committee authority.
    * ``verify_source`` — which authoritative record the Register resolves the kind against and
      GENERATES its provenance from (rather than trusting caller-supplied strings): ``'committee'``
      (a workflow-decision record), ``'cpcs'`` (an ``Approved`` CP/CS checklist for the same subject,
      completed by a maker and approved by a DIFFERENT checker), ``'advaya'`` (an ``Accepted``
      Advaya-handoff record with a matching payload digest), or ``None`` (a governance kind still
      needs a digest + run provenance, but there is no external authoritative record to resolve —
      e.g. the executed-agreement document milestone).
    """

    operation: str
    subject_types: frozenset[str]
    governance: bool
    decision_outcome: str | None = None
    verify_source: str | None = None


_DEAL_LINE = frozenset({"Deal", "Lending"})
_SYN = frozenset({"Syndication"})
_AM = frozenset({"AssetMonetisation"})

# The authoritative kind registry. Operations are defined in ``rbac.OPERATIONS`` and granted to the
# committee / sanction / document / qualification authorities (and the workflow service) there.
EVIDENCE_KINDS: dict[str, EvidenceKindSpec] = {
    # Credit Committee outcome — the sanction gate's core evidence. Verified against the durable
    # committee decision (must be Approved / Rejected respectively).
    "credit_committee_approval": EvidenceKindSpec("attach_committee_evidence", _DEAL_LINE, True,
                                                  decision_outcome="Approved",
                                                  verify_source="committee"),
    "credit_committee_rejection": EvidenceKindSpec("attach_committee_evidence", _DEAL_LINE, True,
                                                   decision_outcome="Rejected",
                                                   verify_source="committee"),
    # The issued sanction letter — verified against the committee APPROVAL decision.
    "sanction_letter": EvidenceKindSpec("attach_sanction_evidence", _DEAL_LINE, True,
                                        decision_outcome="Approved", verify_source="committee"),
    # The executed facility agreement / document set — governance-grade (digest + run provenance)
    # but not an external authoritative record.
    "executed_agreement": EvidenceKindSpec("attach_document_evidence", _DEAL_LINE, True),
    # CP/CS (conditions precedent / subsequent) completion — VERIFIED against an APPROVED CP/CS
    # checklist for the same subject (a maker completed it, a DIFFERENT checker approved it), so it
    # can no longer be caller-attached. Reserved to the checklist-approving authority + workflow svc.
    "cp_cs_completion": EvidenceKindSpec("approve_cpcs_checklist", _DEAL_LINE, True,
                                         verify_source="cpcs"),
    # The Advaya (downstream loan-management) acknowledgement — VERIFIED against an Accepted
    # Advaya-handoff record (matching payload digest). DORMANT: it gates the future 'Disbursement
    # Pending' state, which exists only under an enabled Advaya integration; attach_advaya_evidence
    # is not in the baseline workflow grant, so this path is not executable by default.
    "advaya_acknowledgement": EvidenceKindSpec("attach_advaya_evidence", _DEAL_LINE, True,
                                               verify_source="advaya"),
    # The structured credit note circulated to committee — an artefact, not a governance gate.
    # REVISIONS (committee rework) file the same kind again with a new reference/version note;
    # the full circulation history stays on the line, immutably.
    "credit_note": EvidenceKindSpec("attach_committee_evidence", _DEAL_LINE, False),
    # CONDITIONS attached to a conditional committee approval — governance-grade: verified
    # against the same per-line APPROVED committee decision the sanction evidence cites.
    "sanction_conditions": EvidenceKindSpec("attach_committee_evidence", _DEAL_LINE, True,
                                            decision_outcome="Approved",
                                            verify_source="committee"),
    # A sanction validity window lapsed before the facility progressed — the expiry record
    # the monitor files (an artefact for audit + reporting, not a lifecycle gate).
    "sanction_expired": EvidenceKindSpec("attach_committee_evidence", _DEAL_LINE, False),
    # Lead qualification review — attached by the qualification authority; not governance-grade.
    # Syndication mandate lifecycle: the IM circulated to lenders (versioned artefact —
    # every circulation stays on the record), the mandate's SANCTION (governance: verified
    # against the recorded syndication decision), and the lender allocation summary.
    "im_document": EvidenceKindSpec("attach_syndication_evidence", _SYN, False),
    "syndication_sanction": EvidenceKindSpec("attach_syndication_evidence", _SYN, True,
                                             decision_outcome="Approved",
                                             verify_source="syndication"),
    "syndication_allocation": EvidenceKindSpec("attach_syndication_evidence", _SYN, False),
    # Asset-monetisation mandate lifecycle: the teaser circulated to buyers (versioned
    # artefact), per-buyer NDA / data-room access records, NBO / binding offers (the offer
    # comparison's immutable inputs), and the CLOSURE approval (governance: verified
    # against the recorded asset-monetisation decision).
    "teaser_document": EvidenceKindSpec("attach_am_evidence", _AM, False),
    "am_nda": EvidenceKindSpec("attach_am_evidence", _AM, False),
    "am_offer": EvidenceKindSpec("attach_am_evidence", _AM, False),
    "am_closure_approval": EvidenceKindSpec("attach_am_evidence", _AM, True,
                                            decision_outcome="Approved",
                                            verify_source="asset_mon"),
    "lead_qualification": EvidenceKindSpec("attach_qualification_evidence",
                                           frozenset({"Lead"}), False),
    "lead_qualification_failed": EvidenceKindSpec("attach_qualification_evidence",
                                                  frozenset({"Lead"}), False),
}


def spec_for_kind(kind: str) -> EvidenceKindSpec | None:
    """Resolve a kind to its spec, honouring the controlled ``document:<name>`` prefix. Returns
    None for an unrecognised kind (which the caller must reject — no arbitrary strings)."""
    if kind in EVIDENCE_KINDS:
        return EVIDENCE_KINDS[kind]
    if kind.startswith(DOCUMENT_KIND_PREFIX) and len(kind) > len(DOCUMENT_KIND_PREFIX):
        # A specific received document — attached by the document authority, not governance-grade.
        return EvidenceKindSpec("attach_document_evidence", _DEAL_LINE, False)
    return None


def is_known_kind(kind: str) -> bool:
    return spec_for_kind(kind) is not None
