"""Durable workflows. The workflow code is deterministic and side-effect-free — all I/O
happens in activities. Temporal persists every step, so a crash resumes exactly where it
left off.

Three workflows:

* ``IngestInteractionWorkflow`` — the original minimal reference (kept for teaching).
* ``VoxTouchpointWorkflow``     — the genuine end-to-end VOX capture: resolve the company
  by canonical name → create the entity + lead when missing / update the active lead when
  present → log the full-fidelity interaction (transcript, audio ref, RMs, GPS,
  follow-up) → return everything it did. Workflow id = ``vox-{capture_id}``, so a retried
  upload replays the SAME workflow and the SAME Register writes — exactly-once end to end.
* ``LeadConversionWorkflow``    — human-in-the-loop: waits for an ``approve``/``reject``
  SIGNAL (with a timeout), exposes a ``status`` QUERY for dashboards, and on approval
  applies the conversion (deal + product lines + lead marked Converted).

The safety story everywhere: Temporal's automatic retries combined with stable,
workflow-derived idempotency keys give an **exactly-once effect** on the Register.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app import activities
    from app.types import (
        AdvayaHandoffInput,
        AdvayaHandoffResult,
        CallerContext,
        CpcsChecklistInput,
        CpcsChecklistResult,
        DealStructuringInput,
        DealStructuringResult,
        DocumentCollectionInput,
        DocumentCollectionResult,
        IngestResult,
        InteractionInput,
        LeadConversionInput,
        LeadConversionResult,
        LeadQualificationInput,
        LeadQualificationResult,
        VoxResult,
        VoxTouchpoint,
    )

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)
_IO: dict[str, Any] = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": _RETRY}

# The DECISION-CRITICAL activities (verifying a decision, applying the conversion, recording
# a rejection/timeout) use a LONG-LIVED, effectively-unbounded retry instead of the 5-attempt
# default: once the API has accepted a decision, a Register outage must NOT fail the run — it
# retries (capped backoff) for days until the Register recovers, so an accepted decision is
# never dropped after acknowledgement. schedule_to_close bounds the total reconciliation
# window; there is no maximum_attempts cap.
_DURABLE = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=0,   # unlimited — reconcile until the write lands
)
# No schedule_to_close cap: retries are GENUINELY unbounded (bounded only by the workflow's
# own execution timeout), so an accepted decision is never dropped because a Register outage
# outlasted an arbitrary window. Each attempt is still bounded (start_to_close).
_DURABLE_IO: dict[str, Any] = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": _DURABLE,
}


@workflow.defn
class IngestInteractionWorkflow:
    """The minimal reference — see VoxTouchpointWorkflow for the operational one."""

    @workflow.run
    async def run(self, inp: InteractionInput) -> IngestResult:
        # Stable key from the workflow id → activity retries (or a whole-workflow retry)
        # can never create a duplicate interaction in the Register.
        idem = f"wf:{workflow.info().workflow_id}"

        created = await workflow.execute_activity(
            activities.write_interaction, args=[inp, idem], **_IO)
        dossier = await workflow.execute_activity(
            activities.fetch_dossier, args=[created["entity_id"], inp.caller],
            start_to_close_timeout=timedelta(seconds=15), retry_policy=_RETRY)
        return IngestResult(
            interaction_id=created["id"],
            dossier_counts=dossier.get("counts", {}),
        )


@workflow.defn
class VoxTouchpointWorkflow:
    """Field capture → Register, handling both approved VOX scenarios:

    new company      → create entity → create lead → log interaction → follow-up
    existing company → resolve by canonical name → update/link active lead (create one
                       if none) → log interaction → follow-up
    """

    def __init__(self) -> None:
        self._stage = "starting"

    @workflow.query
    def status(self) -> str:
        """Live progress for dashboards/debugging: which step the run is on."""
        return self._stage

    @workflow.run
    async def run(self, tp: VoxTouchpoint) -> VoxResult:
        wf_id = workflow.info().workflow_id

        # -- 1. company: explicit id, canonical-name match, or create ---------
        entity_created = False
        entity_id = tp.entity_id
        if not entity_id:
            if not tp.company_name:
                raise ApplicationError(
                    "VoxTouchpoint needs company_name or entity_id", non_retryable=True)
            self._stage = "resolving company"
            entity = await workflow.execute_activity(
                activities.resolve_entity, args=[tp.company_name, tp.caller], **_IO)
            if entity is None:
                self._stage = "creating company"
                entity = await workflow.execute_activity(
                    activities.create_entity, args=[tp, f"wf:{wf_id}:entity"], **_IO)
                entity_created = True
            entity_id = entity["id"]
        assert entity_id is not None  # narrowed: explicit id, matched, or just created

        # -- 2. lead: link the active one, or open one ------------------------
        self._stage = "linking lead"
        lead_created = False
        lead: dict[str, Any] | None = await workflow.execute_activity(
            activities.find_active_lead, args=[entity_id, tp.caller], **_IO)
        if lead is None:
            lead = await workflow.execute_activity(
                activities.create_lead, args=[tp, entity_id, f"wf:{wf_id}:lead"], **_IO)
            lead_created = True
            # Assign the actual BDRM as the lead's owner (a real LineAssignment), so the
            # RM's scoped lists/reads/writes cover it — not just the rm name string.
            if tp.assigned_rm_id and lead is not None:
                await workflow.execute_activity(
                    activities.assign_lead_owner,
                    args=[lead["id"], tp.assigned_rm_id, tp.caller], **_IO)
        else:
            lead = await workflow.execute_activity(
                activities.update_lead_touch, args=[lead["id"], tp], **_IO)
        assert lead is not None  # narrowed: found-and-updated, or just created

        # -- 3. the interaction itself (full fidelity, exactly-once) ----------
        self._stage = "logging interaction"
        interaction = await workflow.execute_activity(
            activities.log_touchpoint,
            args=[tp, entity_id, lead["id"], f"wf:{wf_id}:interaction"], **_IO)

        self._stage = "done"
        follow_up = {}
        if tp.next_action or tp.next_meeting_date:
            follow_up = {"next_action": tp.next_action,
                         "next_action_date": tp.next_action_date,
                         "next_meeting_date": tp.next_meeting_date,
                         "calendar": "pending" if tp.next_meeting_date else None}
        return VoxResult(
            workflow_id=wf_id,
            entity_id=entity_id,
            entity_created=entity_created,
            lead_id=lead["id"],
            lead_created=lead_created,
            interaction_id=interaction["id"],
            follow_up={k: v for k, v in follow_up.items() if v is not None},
        )


@workflow.defn
class LeadConversionWorkflow:
    """Request → human decision → applied conversion. The human-in-the-loop pattern:

    * ``approve(by, note)`` / ``reject(by, note)`` — SIGNALS a Head sends (via the
      orchestrator API or the Temporal UI).
    * ``status()`` — a QUERY dashboards poll ("Pending" / "Approved" / ...).
    * No decision within ``approval_timeout_hours`` → TimedOut (recorded on the lead).
    """

    def __init__(self) -> None:
        self._decision: str | None = None      # None until a VERIFIED decision (or timeout)
        self._decided_by: str | None = None
        self._note: str | None = None
        # The VERIFIED approver context, captured once verify_decision confirms the signed
        # approval — recorded durably in history, so the conversion no longer depends on a
        # short-lived JWT surviving a worker outage.
        self._approver: CallerContext | None = None
        # A FIFO QUEUE of raw signals awaiting verification. Signals are UNTRUSTED input: they
        # are appended here, and the run loop validates each in arrival order BEFORE any can
        # become the final decision — so a direct Temporal signal can neither spoof a rejection
        # nor lock the run into a failing Approved state (both for approve AND reject).
        #
        # A QUEUE, not a single slot: two signals arriving close together (e.g. approve then
        # reject, or two approves) must not overwrite one another. Each is verified in order
        # and the FIRST that verifies wins — later ones are ignored once a decision is set. A
        # single mutable slot would silently drop the earlier signal (last-writer-wins), which
        # could discard a valid decision or let a later spoofed one clobber a real one.
        self._pending: list[tuple] = []
        self._stage = "Pending"

    @workflow.signal
    def approve(self, by: str, note: str | None = None,
                approval_token: str = "", decision_ref: str = "") -> None:
        if self._decision is None:
            self._pending.append(("Approved", by, note, approval_token, decision_ref))

    @workflow.signal
    def reject(self, by: str, note: str | None = None,
               approval_token: str = "", decision_ref: str = "") -> None:
        if self._decision is None:
            self._pending.append(("Rejected", by, note, approval_token, decision_ref))

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: LeadConversionInput) -> LeadConversionResult:
        wf_id = workflow.info().workflow_id
        lead = await workflow.execute_activity(
            activities.get_lead, args=[inp.lead_id, inp.caller], **_IO)
        entity_id = lead.get("entity_id")
        if not entity_id:
            raise ApplicationError(
                f"Lead {inp.lead_id} has no entity_id — link it to a company first.",
                non_retryable=True)

        # Wait durably for a human decision, but VERIFY each signal before it counts. A
        # spoofed/unverified signal is discarded and the run keeps waiting (so it can neither
        # finalise a fake rejection nor DoS a pending approval). The workflow can sleep here
        # for days and survive worker restarts.
        total = timedelta(hours=inp.approval_timeout_hours)
        start = workflow.now()
        while self._decision is None:
            remaining = total - (workflow.now() - start)
            if remaining <= timedelta(0):
                break
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending), timeout=remaining)
            except asyncio.TimeoutError:
                break
            # Drain the queue in ARRIVAL order; the first signal that verifies wins.
            while self._pending and self._decision is None:
                kind, by, note, token, decision_ref = self._pending.pop(0)
                self._stage = "Verifying decision"
                # Durable retry: the durable-path verification may need to READ the persisted
                # decision record, which must survive a Register outage.
                verified = await workflow.execute_activity(
                    activities.verify_decision,
                    args=[kind, by, token, wf_id, inp.lead_id, inp.caller.tenant,
                          decision_ref], **_DURABLE_IO)
                if not verified.get("valid"):
                    self._stage = "Pending"   # spoofed / unverified — ignore, keep waiting
                    continue
                # Outcome, approver AND note all come from the authoritative persisted record
                # (via verify_decision) — never the signal's latest-caller values.
                self._decision = kind
                self._note = verified.get("note")
                self._decided_by = verified.get("email") or by
                self._approver = CallerContext(
                    tenant=verified.get("tenant") or inp.caller.tenant,
                    email=verified.get("email", ""), user_id=verified.get("user_id", ""),
                    roles=list(verified.get("roles", [])),
                    effective_operations=verified.get("operations", {}),
                    effective_views=verified.get("views", {}), decision="FULL")

        if self._decision is None:
            self._stage = "TimedOut"
            # No verified decider on a timeout → record as the system (service) actor, not the
            # requester. Idempotent + durable so a retry never double-appends and an outage
            # never drops the outcome.
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id, f"Conversion request by {inp.requested_by} timed out "
                                   f"(workflow {wf_id}).", None], **_DURABLE_IO)
            return LeadConversionResult(workflow_id=wf_id, lead_id=inp.lead_id,
                                        status="TimedOut")

        if self._decision == "Rejected":
            self._stage = "Rejected"
            # Attributed to the VERIFIED rejecter (self._approver), never the original
            # requester. Idempotent + durable.
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id, f"Conversion rejected by {self._decided_by}: "
                                   f"{self._note or 'no note'} (workflow {wf_id}).",
                      self._approver], **_DURABLE_IO)
            return LeadConversionResult(workflow_id=wf_id, lead_id=inp.lead_id,
                                        status="Rejected", decided_by=self._decided_by,
                                        decision_note=self._note)

        # Approved → apply in ONE transactional Register call. All-or-nothing on the
        # server: no orphan deal/lines can survive a mid-apply failure, so the workflow
        # needs no compensation. Idempotent on the workflow id → a retry is safe.
        #
        # Authorized as the VERIFIED APPROVER (captured durably above when verify_decision
        # confirmed the signed approval) — never the requester, and not dependent on a
        # short-lived JWT at apply time.
        self._stage = "Applying"
        # Durable: the decision is accepted and recorded; a Register outage during apply must
        # reconcile (retry until it lands), never fail the run after acknowledgement.
        applied = await workflow.execute_activity(
            activities.convert_lead_txn,
            args=[inp, f"wf:{wf_id}:convert", self._approver, self._decided_by],
            **_DURABLE_IO)
        line_ids = {
            "lending": applied.get("lending_id"),
            "syndication": applied.get("syndication_id"),
            "asset-monetisation": applied.get("asset_mon_id"),
        }
        deal = {"id": applied["deal_id"]}

        self._stage = "Approved"
        return LeadConversionResult(
            workflow_id=wf_id, lead_id=inp.lead_id, status="Approved",
            decided_by=self._decided_by, decision_note=self._note,
            deal_id=deal["id"], lending_id=line_ids["lending"],
            syndication_id=line_ids["syndication"],
            asset_mon_id=line_ids["asset-monetisation"],
        )


# =============================================================================================== #
# Business-lifecycle workflows: Lead Qualification → Deal Structuring → Document Collection.
#
# The through-line: a governance-bearing transition never happens by typing a stage. The work is
# done here, the IMMUTABLE evidence is filed, and only then is the stage advanced through the
# Register's normal policy-enforcing API — which independently REFUSES the advance if the evidence
# is missing. The workflow is the audited path; the Register is the backstop.
# =============================================================================================== #
@workflow.defn
class LeadQualificationWorkflow:
    """Qualify a lead against the minimum bar to structure a deal. Records the qualification review
    as durable evidence on the lead; a pass hands off to Deal Structuring, a fail records why and
    stops. The first, cheapest gate in the pipeline — no deal work begins on an unqualified lead."""

    def __init__(self) -> None:
        self._stage = "Qualifying"

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: LeadQualificationInput) -> LeadQualificationResult:
        wf_id = workflow.info().workflow_id
        # The qualification review is itself durable evidence on the lead (so a later reader can see
        # WHY it qualified, immutably), whether it passed or failed.
        self._stage = "Recording qualification"
        kind = "lead_qualification" if inp.passed else "lead_qualification_failed"
        evidence = await workflow.execute_activity(
            activities.attach_evidence,
            args=["Lead", inp.lead_id, kind,
                  inp.qualification_reference or f"qualification/{wf_id}",
                  inp.qualification_sha256,
                  inp.reason or ("qualified" if inp.passed else "not qualified"),
                  inp.caller],
            **_DURABLE_IO)

        if not inp.passed:
            self._stage = "NotQualified"
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id,
                      f"Lead not qualified by {inp.qualified_by}: "
                      f"{inp.reason or 'no reason given'} (workflow {wf_id}).", inp.caller],
                **_DURABLE_IO)
            return LeadQualificationResult(
                workflow_id=wf_id, lead_id=inp.lead_id, status="NotQualified",
                evidence_id=evidence.get("id"), note=inp.reason)

        self._stage = "Qualified"
        await workflow.execute_activity(
            activities.mark_lead_note,
            args=[inp.lead_id,
                  f"Lead qualified by {inp.qualified_by} — ready for structuring "
                  f"(workflow {wf_id}).", inp.caller],
            **_DURABLE_IO)
        return LeadQualificationResult(
            workflow_id=wf_id, lead_id=inp.lead_id, status="Qualified",
            evidence_id=evidence.get("id"), note=inp.reason)


@workflow.defn
class DealStructuringWorkflow:
    """Structure a deal to the sanction milestone. Walk the ordered pipeline (→ Diligence → Note
    Circulated), circulate the credit note, then wait for the Credit Committee's decision (a signal).
    On approval, FILE the committee-approval + sanction-letter evidence and advance the deal to
    'Sanctioned' — a transition the Register's evidence gate accepts ONLY because that evidence is
    now on file. A hand-rolled PATCH to 'Sanctioned' is refused all the same, so the workflow is
    the ONLY way the milestone is reached."""

    def __init__(self) -> None:
        self._notified = False
        self._stage = "Structuring"

    @workflow.signal
    def committee_decision(self, decision_ref: str = "") -> None:
        # A WAKE-UP ONLY. The run re-reads the AUTHORITATIVE committee decision the orchestrator
        # persisted (fresh-authorized, single-winner) and derives the outcome from THAT record — so
        # a direct Temporal signal carries no trusted outcome/approver/note/references. The payload
        # is ignored on purpose.
        self._notified = True

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: DealStructuringInput) -> DealStructuringResult:
        wf_id = workflow.info().workflow_id
        caller = inp.caller

        # -- 1. Walk the ordered pipeline to the committee stage (Note Circulated). Each hop goes
        #       through the Register's policy-enforcing API, so sequencing is enforced. Idempotent.
        deal = await workflow.execute_activity(
            activities.get_resource, args=["deals", inp.deal_id, caller], **_IO)
        for stage in ("Diligence", "Note Circulated"):
            if _stage_before(deal.get("stage"), stage):
                self._stage = f"Advancing to {stage}"
                deal = await workflow.execute_activity(
                    activities.advance_stage,
                    args=["deals", inp.deal_id, "stage", stage, None, caller], **_IO)

        # -- 2. Circulate the structured credit note as evidence (the structuring artefact).
        evidence_ids: list = []
        if inp.credit_note_reference:
            self._stage = "Circulating credit note"
            note_ev = await workflow.execute_activity(
                activities.attach_evidence,
                args=["Deal", inp.deal_id, "credit_note", inp.credit_note_reference,
                      None, "Structured credit note circulated to committee", caller],
                **_DURABLE_IO)
            evidence_ids.append(note_ev.get("id"))

        # -- 3. Wait durably for the Credit Committee decision, VERIFYING each wake-up against the
        #       AUTHORITATIVE record the orchestrator persisted (fresh-authorized, single-winner).
        #       A spoofed/direct signal (no record, or a record for another subject) is ignored and
        #       the run keeps waiting — the outcome is NEVER taken from the signal.
        self._stage = "Awaiting committee decision"
        total = timedelta(hours=inp.decision_timeout_hours)
        start = workflow.now()
        verified: dict[str, Any] | None = None
        while verified is None:
            remaining = total - (workflow.now() - start)
            if remaining <= timedelta(0):
                break
            try:
                await workflow.wait_condition(lambda: self._notified, timeout=remaining)
            except asyncio.TimeoutError:
                break
            self._notified = False
            self._stage = "Verifying committee decision"
            v = await workflow.execute_activity(
                activities.verify_committee_decision, args=[inp.deal_id, caller], **_DURABLE_IO)
            if v.get("valid"):
                verified = v
            else:
                self._stage = "Awaiting committee decision"   # spoofed / premature → keep waiting

        if verified is None:
            self._stage = "TimedOut"
            return DealStructuringResult(
                workflow_id=wf_id, deal_id=inp.deal_id, status="TimedOut",
                stage=deal.get("stage"), evidence_ids=evidence_ids,
                note="No committee decision within the window.")

        # Everything below is derived from the VERIFIED record — not the signal.
        outcome = verified["outcome"]
        decided_by = verified.get("decided_by")
        note = verified.get("note")
        committee_ref = verified.get("committee_reference") or f"committee/{wf_id}"
        sanction_ref = verified.get("sanction_letter_reference") or f"sanction/{wf_id}"
        decision_ref = wf_id   # the evidence cites the decision keyed on this workflow id

        if outcome == "Rejected":
            self._stage = "Rejected"
            await workflow.execute_activity(
                activities.attach_evidence,
                args=["Deal", inp.deal_id, "credit_committee_rejection", committee_ref,
                      None, note or "Committee rejected", caller, decision_ref],
                **_DURABLE_IO)
            await workflow.execute_activity(
                activities.advance_stage,
                args=["deals", inp.deal_id, "stage", "Rejected", None, caller], **_DURABLE_IO)
            return DealStructuringResult(
                workflow_id=wf_id, deal_id=inp.deal_id, status="Rejected",
                decided_by=decided_by, stage="Rejected", evidence_ids=evidence_ids, note=note)

        # -- 4. Approved → FILE the sanction evidence (committee approval + sanction letter), each
        #       VERIFIED by the Register against the same authoritative decision, BEFORE advancing.
        self._stage = "Filing sanction evidence"
        for kind, ref in (("credit_committee_approval", committee_ref),
                          ("sanction_letter", sanction_ref)):
            ev = await workflow.execute_activity(
                activities.attach_evidence,
                args=["Deal", inp.deal_id, kind, ref, None, note, caller, decision_ref],
                **_DURABLE_IO)
            evidence_ids.append(ev.get("id"))

        # -- 5. Advance to Sanctioned WITH the mandatory fields (durable: reconcile through outages).
        self._stage = "Sanctioning"
        extra = {k: v for k, v in
                 {"product_type": inp.product_type, "rm": inp.rm}.items() if v is not None}
        sanctioned = await workflow.execute_activity(
            activities.advance_stage,
            args=["deals", inp.deal_id, "stage", "Sanctioned", extra, caller], **_DURABLE_IO)

        # -- 6. The same committee decision sanctions the deal's LENDING FACILITY. The Register
        #       gates Lending's 'Sanctioned' on evidence filed against the LENDING subject, and a
        #       decision is bound to its subject — so each line cites the per-line decision the
        #       orchestrator recorded under "{wf_id}:lending:{line_id}" with the committee's own
        #       authority. Without this the lending line could never leave 'Note Circulated', and
        #       CP/CS + the Advaya handover would be unreachable.
        self._stage = "Sanctioning lending facility"
        for line in await workflow.execute_activity(
                activities.find_lines_for_deal, args=["lending", inp.deal_id, caller], **_IO):
            line_id = str(line.get("id"))
            for st in ("Diligence", "Note Circulated"):
                if _stage_before(line.get("stage"), st):
                    line = await workflow.execute_activity(
                        activities.advance_stage,
                        args=["lending", line_id, "stage", st, None, caller], **_IO)
            line_ref = f"{wf_id}:lending:{line_id}"
            for kind, ref in (("credit_committee_approval", committee_ref),
                              ("sanction_letter", sanction_ref)):
                ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["Lending", line_id, kind, ref, None, note, caller, line_ref],
                    **_DURABLE_IO)
                evidence_ids.append(ev.get("id"))
            # The deal's `extra` carries product_type, which is a DEAL field — LendingUpdate is
            # extra="forbid", so reusing it here is a 422. Send only fields the lending line
            # actually has.
            line_extra = {k: v for k, v in {"rm": inp.rm}.items() if v is not None}
            await workflow.execute_activity(
                activities.advance_stage,
                args=["lending", line_id, "stage", "Sanctioned", line_extra or None, caller],
                **_DURABLE_IO)

        self._stage = "Sanctioned"
        return DealStructuringResult(
            workflow_id=wf_id, deal_id=inp.deal_id, status="Sanctioned",
            decided_by=decided_by, stage=sanctioned.get("stage"),
            evidence_ids=evidence_ids, note=note)


@workflow.defn
class DocumentCollectionWorkflow:
    """Collect the executed documentation for a line and, once the mandatory set is complete, file
    the executed-agreement evidence. Documents arrive as signals (a portal upload, an ops action);
    the workflow tracks the checklist and completes when every required item is received. This is
    the evidence-producing half of the 'document completeness' gate the reviewer named."""

    def __init__(self) -> None:
        self._received: dict = {}      # name -> {"reference","sha256"}
        self._stage = "Collecting"

    @workflow.signal
    def document_received(self, name: str, reference: str = "",
                          sha256: str = "") -> None:
        if name:
            self._received[name] = {"reference": reference, "sha256": sha256 or None}

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.query
    def outstanding(self) -> list:
        return list(self._outstanding)

    @workflow.run
    async def run(self, inp: DocumentCollectionInput) -> DocumentCollectionResult:
        wf_id = workflow.info().workflow_id
        required = list(inp.required_documents or [])
        self._outstanding = list(required)

        def _all_in() -> bool:
            return all(name in self._received for name in required)

        # Wait durably until every mandatory document has arrived (or the window closes).
        timed_out = False
        if required:
            try:
                await workflow.wait_condition(
                    _all_in, timeout=timedelta(hours=inp.collection_timeout_hours))
            except asyncio.TimeoutError:
                timed_out = True

        self._outstanding = [n for n in required if n not in self._received]
        evidence_ids: list = []
        # File each received document as immutable evidence.
        for name in required:
            if name in self._received:
                got = self._received[name]
                self._stage = f"Filing {name}"
                ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=[inp.subject_type, inp.subject_id, f"document:{name}",
                          got.get("reference") or f"doc/{name}/{wf_id}",
                          got.get("sha256"), f"Document '{name}' received", inp.caller],
                    **_DURABLE_IO)
                evidence_ids.append(ev.get("id"))

        if timed_out or self._outstanding:
            self._stage = "TimedOut"
            return DocumentCollectionResult(
                workflow_id=wf_id, subject_type=inp.subject_type, subject_id=inp.subject_id,
                status="TimedOut", received=[n for n in required if n in self._received],
                outstanding=self._outstanding, evidence_ids=evidence_ids)

        # Complete → the executed documentation set is on file; file the summary executed-agreement
        # evidence that the next milestone (e.g. CP/CS Completed) can rely on.
        self._stage = "Complete"
        summary = await workflow.execute_activity(
            activities.attach_evidence,
            args=[inp.subject_type, inp.subject_id, "executed_agreement",
                  f"docset/{wf_id}", None,
                  f"Executed document set complete ({len(required)} items)", inp.caller],
            **_DURABLE_IO)
        evidence_ids.append(summary.get("id"))
        return DocumentCollectionResult(
            workflow_id=wf_id, subject_type=inp.subject_type, subject_id=inp.subject_id,
            status="Complete", received=list(required), outstanding=[],
            evidence_ids=evidence_ids)


@workflow.defn
class AdvayaHandoffWorkflow:
    """PREPARE the Advaya handover — phase one of a two-person maker-checker.

    PRISM does NOT disburse the loan itself: there is no Advaya integration, so the terminal is
    'Handed Over to Advaya'. This workflow (the MAKER's action) creates the durable handover PACKAGE
    in a **Prepared** state via ``POST /v1/internal/handover-packages``: the Register loads the
    Lending row server-side, confirms it is 'Ready for Disbursement', re-verifies the CP/CS +
    executed-document evidence, reconciles the executed-document refs + CP/CS checklist version,
    snapshots the AUTHORITATIVE amounts (never trusting workflow inputs), GENERATES the package
    manifest with a server-side digest, and stores it — WITHOUT advancing the stage. A DIFFERENT
    checker then approves it (``POST /v1/workflows/advaya-handover/{lending_id}/approve`` →
    ``/v1/internal/handover-packages/{id}/approve``), which advances the stage. No Advaya call, no
    fabricated acknowledgement is ever made."""

    def __init__(self) -> None:
        self._stage = "Starting"

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: AdvayaHandoffInput) -> AdvayaHandoffResult:
        wf_id = workflow.info().workflow_id
        caller = inp.caller

        self._stage = "Preparing handover package"
        package = {
            "executed_document_refs": inp.executed_document_refs,
            "cpcs_checklist_version": inp.cpcs_checklist_version,
            "delivery_method": inp.delivery_method,
            "recipient": inp.recipient,
            "note": inp.note,
        }
        pkg = await workflow.execute_activity(
            activities.create_handover_package,
            args=[inp.lending_id, {k: v for k, v in package.items() if v is not None}, caller],
            **_DURABLE_IO)

        self._stage = "Prepared — awaiting checker approval"
        return AdvayaHandoffResult(
            workflow_id=wf_id, lending_id=inp.lending_id, status=pkg.get("status") or "Prepared",
            handover_package_id=pkg.get("id"), handover_key=pkg.get("handover_key"),
            note="Handover package prepared; a different checker must approve it to hand the "
                 "facility over to Advaya.")


@workflow.defn
class CpcsChecklistWorkflow:
    """PREPARE the authoritative CP/CS checklist — the maker's phase of the CP/CS maker-checker.

    The maker submits the CP/CS conditions (each typed CP or CS, with waiver / CS-deferment
    governance); this workflow records the checklist via ``POST /v1/internal/cpcs-checklists`` (the
    Register validates structure, waiver authority, reasons and expiries). A DIFFERENT checker then
    approves it (``POST /v1/workflows/cpcs-checklists/{id}/approve``), after which
    ``cp_cs_completion`` may be minted from it and the line advanced to 'CP/CS Completed'."""

    def __init__(self) -> None:
        self._stage = "Starting"

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: CpcsChecklistInput) -> CpcsChecklistResult:
        wf_id = workflow.info().workflow_id
        self._stage = "Preparing CP/CS checklist"
        payload = {"items": inp.items, "deal_id": inp.deal_id,
                   "checklist_version": inp.checklist_version, "status": "Completed",
                   "note": inp.note}
        chk = await workflow.execute_activity(
            activities.prepare_cpcs_checklist,
            args=[inp.lending_id, {k: v for k, v in payload.items() if v is not None}, inp.caller],
            **_DURABLE_IO)
        self._stage = "Prepared — awaiting checker approval"
        return CpcsChecklistResult(
            workflow_id=wf_id, lending_id=inp.lending_id, checklist_id=chk.get("id"),
            status=chk.get("status") or "Completed",
            note="CP/CS checklist prepared; a different checker must approve it before "
                 "cp_cs_completion can be filed.")


# Ordered position of a Deal/Lending pipeline stage, for "is X before Y" checks in structuring.
_DEAL_ORDER = ["Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
               "CP/CS Completed", "Ready for Disbursement", "Handed Over to Advaya",
               "Disbursement Pending"]


def _stage_before(current: str | None, target: str) -> bool:
    """True when ``current`` is earlier than ``target`` in the deal pipeline (so a forward hop is
    needed). Unknown/None current → treat as earliest (advance). Off-track stages (On Hold /
    Rejected) are not ordered — never auto-advanced from here."""
    if current == target:
        return False
    if current is None:
        return True
    if current not in _DEAL_ORDER or target not in _DEAL_ORDER:
        return False
    return _DEAL_ORDER.index(current) < _DEAL_ORDER.index(target)
