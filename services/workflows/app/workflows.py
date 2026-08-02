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
from temporalio.common import RetryPolicy, SearchAttributeKey
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
        CovenantMonitorInput,
        CovenantMonitorResult,
        DocumentCollectionInput,
        DocumentCollectionResult,
        DocumentExpiryInput,
        DocumentExpiryResult,
        EwsCaseInput,
        EwsCaseResult,
        IngestResult,
        InteractionInput,
        LeadConversionInput,
        LeadConversionResult,
        LeadQualificationInput,
        LeadQualificationResult,
        SanctionExpiryInput,
        SanctionExpiryResult,
        AssetMonetisationInput,
        AssetMonetisationResult,
        SyndicationMandateInput,
        SyndicationMandateResult,
        VoxResult,
        VoxTouchpoint,
    )

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    # 8 attempts ≈ a 90-second window (1+2+4+8+16+30+30), enough to ride out a DB
    # restart or connection-pool flush; 5 gave up after ~15s, which turned every brief
    # Register blip into a dead run. All _IO activities are idempotency-keyed, so the
    # extra attempts are replay-safe.
    maximum_attempts=8,
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
# ------------------------------------------------------------------------------------------- #
# Release-1 foundation: run-control, SLA tracking and search attributes — shared by every
# human-in-the-loop workflow. Everything here is DETERMINISTIC (pure state + workflow.now());
# all IO goes through activities.
# ------------------------------------------------------------------------------------------- #

# Per-run search attributes for the Temporal UI/CLI. OPT-IN (input.emit_search_attributes):
# they must be registered on the server first — see services/workflows/README.md.
_SA_BUSINESS_STATUS = SearchAttributeKey.for_keyword("PrismBusinessStatus")
_SA_SUBJECT = SearchAttributeKey.for_keyword("PrismSubject")


class _Foundation:
    """The shared run-control + SLA state machine for a decision-waiting workflow.

    * ``controls`` — the UNTRUSTED queue of (action, control_ref) signals; each is verified
      against the durable control record before it has any effect (same spoof-proof pattern
      as decisions).
    * ``business_status`` — what the RUN means to the business (AwaitingDecision /
      ReturnedForInformation / Cancelled / …); the workflow's technical stage stays separate.
    * SLA bookkeeping — reminder count + escalation flag; the clock RESETS on resubmit.
    """

    def __init__(self) -> None:
        self.controls: list[tuple[str, str]] = []
        self.business_status = "AwaitingDecision"
        self.cancelled_by: str | None = None
        self.cancel_note: str | None = None
        self.reminders_sent = 0
        self.escalated = False

    def state(self, technical_stage: str) -> dict:
        return {"business_status": self.business_status, "technical_stage": technical_stage,
                "reminders_sent": self.reminders_sent, "escalated": self.escalated}

    def next_wakeup(self, waited: timedelta, remaining: timedelta,
                    reminder_hours: float, escalation_hours: float) -> timedelta:
        """The next moment anything is due: the decision deadline, the next SLA reminder, or
        the escalation point — whichever comes first (never <= 0)."""
        wake = remaining
        if reminder_hours > 0:
            due = timedelta(hours=reminder_hours * (self.reminders_sent + 1)) - waited
            if due > timedelta(0):
                wake = min(wake, due)
        if escalation_hours > 0 and not self.escalated:
            due = timedelta(hours=escalation_hours) - waited
            if due > timedelta(0):
                wake = min(wake, due)
        return max(wake, timedelta(seconds=1))

    def due_sla_event(self, waited: timedelta, reminder_hours: float,
                      escalation_hours: float) -> str | None:
        """Which SLA event is due NOW (escalation outranks a reminder), updating the
        bookkeeping. None = nothing due."""
        if (escalation_hours > 0 and not self.escalated
                and waited >= timedelta(hours=escalation_hours)):
            self.escalated = True
            return "sla_escalation"
        if (reminder_hours > 0
                and waited >= timedelta(hours=reminder_hours * (self.reminders_sent + 1))):
            self.reminders_sent += 1
            return "sla_reminder"
        return None


async def _emit_ops(event: str, detail: dict, notify_to: list | None = None,
                    severity: str = "info") -> None:
    """Raise an operational event (log + optional webhook + durable notifications).
    BEST-EFFORT by design: ops visibility must never take a business workflow down, so
    failures are swallowed after the activity's own bounded retry.

    ``notify_to`` names the humans who should ALSO receive this as a durable notification
    (in-app inbox + configured external channels) when the deployment enables
    notifications — the increment-7 upgrade of this seam. The event's ``subject``
    ("Lead:{id}" style) becomes the notification's subject binding."""
    recipients = sorted({r for r in (notify_to or []) if r})
    if recipients:
        subject = str(detail.get("subject") or "")
        stype, _, sid = subject.partition(":")
        title = event.replace("_", " ").capitalize() + (f" — {subject}" if subject else "")
        # The dedupe discriminator makes each DISTINCT occurrence its own notification
        # (different document, different reminder round) while an activity retry of the
        # SAME occurrence stays a no-op at the Register.
        discriminator = "|".join(
            str(detail[k]) for k in ("subject", "document_id", "expires_on",
                                     "waiting_hours") if detail.get(k) is not None)
        detail = {**detail, "notify": {
            "recipients": recipients, "severity": severity, "title": title,
            "discriminator": discriminator,
            "subject_type": stype or None, "subject_id": sid or None}}
    try:
        await workflow.execute_activity(
            activities.emit_operational_event, args=[event, detail],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2))
    except Exception:  # noqa: BLE001 — ops emission is never load-bearing
        workflow.logger.warning("ops_event_failed", extra={"event": event})


def _upsert_search(enabled: bool, business_status: str, subject: str) -> None:
    """Reflect the run's business status into Temporal search attributes (when the
    deployment registered them) so ops can filter runs in the UI/CLI."""
    if enabled:
        workflow.upsert_search_attributes([
            _SA_BUSINESS_STATUS.value_set(business_status),
            _SA_SUBJECT.value_set(subject)])


def _lead_rank(lead: dict, tp: Any) -> tuple:
    """Deterministic ranking key for choosing among a company's ACTIVE leads (higher wins):
    the lead owned by the capturing/owning RM outranks everything, then lens match, then
    sector match, then recency; the id is the final tiebreak so the order is total and
    replay-stable. Two leads are a genuine TIE (worth asking a human) when their score
    triples are equal."""
    rms = {x for x in (tp.assigned_rm, tp.performed_by) if x}
    score = 0
    if lead.get("rm") and lead.get("rm") in rms:
        score += 4
    if tp.lens and lead.get("lens") == tp.lens:
        score += 2
    if tp.sector and lead.get("sector") == tp.sector:
        score += 1
    return (score, str(lead.get("last_interaction_date") or ""), str(lead.get("id")))


def evaluate_checklist(items: list) -> dict:
    """The configurable qualification checklist, evaluated: every REQUIRED item must pass.
    Pure and shape-tolerant (items are plain dicts) — the same summary is returned to the
    caller and filed in the qualification evidence."""
    failed = [str(i.get("key") or i.get("label") or "?") for i in items
              if i.get("required", True) and not i.get("passed", False)]
    return {"passed": not failed, "failed_required": failed,
            "items_total": len(items),
            "items_passed": sum(1 for i in items if i.get("passed", False))}


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
        # Confirmation gates (increment 2). The candidate lists are what the RUN itself
        # proposed — a confirmation signal may only pick from them (or "create new"), so a
        # forged signal can at worst choose a legitimate candidate, never inject an id.
        self._company_candidates: list[dict] = []
        self._company_choice: str | None = None      # entity id, or "" = create new
        self._lead_candidates: list[dict] = []
        self._lead_choice: str | None = None
        self._confirmed_by: str | None = None

    @workflow.query
    def status(self) -> str:
        """Live progress for dashboards/debugging: which step the run is on."""
        return self._stage

    @workflow.query
    def pending_confirmation(self) -> dict:
        """What (if anything) this run is waiting on a human for, with the candidates —
        everything a UI needs to render the confirmation prompt."""
        if self._stage == "awaiting company confirmation":
            return {"kind": "company", "candidates": self._company_candidates}
        if self._stage == "awaiting lead selection":
            return {"kind": "lead", "candidates": self._lead_candidates}
        return {}

    @workflow.signal
    def confirm_company(self, entity_id: str, by: str = "") -> None:
        """Resolve the ambiguous-company gate: one of the proposed candidate ids, or ""
        to create a new company. Anything else is ignored (whitelist, not trust)."""
        if self._company_choice is None and (
                entity_id == "" or any(str(c.get("id")) == entity_id
                                       for c in self._company_candidates)):
            self._company_choice = entity_id
            self._confirmed_by = by or self._confirmed_by

    @workflow.signal
    def select_lead(self, lead_id: str, by: str = "") -> None:
        """Resolve the multi-lead gate: must be one of the proposed candidate ids."""
        if self._lead_choice is None and any(str(c.get("id")) == lead_id
                                             for c in self._lead_candidates):
            self._lead_choice = lead_id
            self._confirmed_by = by or self._confirmed_by

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
            resolution = await workflow.execute_activity(
                activities.resolve_entity_candidates,
                args=[tp.company_name, tp.caller], **_IO)
            entity = resolution.get("exact")
            candidates = resolution.get("candidates") or []
            if entity is None and candidates and tp.require_company_confirmation:
                # AMBIGUOUS: close candidates but no exact match. Ask the capturing RM
                # instead of silently creating a near-duplicate company. The run parks
                # durably; the choice is whitelisted to the candidates the run proposed.
                self._company_candidates = [
                    {"id": str(c.get("id")), "legal_name": c.get("legal_name"),
                     "display_name": c.get("display_name"), "sector": c.get("sector")}
                    for c in candidates]
                self._stage = "awaiting company confirmation"
                try:
                    await workflow.wait_condition(
                        lambda: self._company_choice is not None,
                        timeout=timedelta(hours=tp.confirmation_timeout_hours))
                except asyncio.TimeoutError:
                    raise ApplicationError(
                        f"Company confirmation for '{tp.company_name}' was not answered "
                        f"within {tp.confirmation_timeout_hours}h — nothing was written; "
                        "confirm via /confirm-company on a fresh capture.",
                        non_retryable=True) from None
                if self._company_choice:
                    entity = next(c for c in candidates
                                  if str(c.get("id")) == self._company_choice)
                # "" → the RM says it really is a NEW company → create below.
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
        active = await workflow.execute_activity(
            activities.find_active_leads, args=[entity_id, tp.caller], **_IO)
        lead: dict[str, Any] | None = None
        if len(active) == 1:
            lead = active[0]
        elif len(active) > 1:
            # Several active leads: rank by owning RM > lens > sector > recency. Only a
            # GENUINE tie at the top (equal scores) is worth a human's time — and only
            # when the deployment asked for it.
            ranked = sorted(active, key=lambda ld: _lead_rank(ld, tp), reverse=True)
            top_score = _lead_rank(ranked[0], tp)[0]
            tied = [ld for ld in ranked if _lead_rank(ld, tp)[0] == top_score]
            if len(tied) > 1 and tp.require_lead_confirmation:
                self._lead_candidates = [
                    {"id": str(ld.get("id")), "rm": ld.get("rm"), "lens": ld.get("lens"),
                     "sector": ld.get("sector"),
                     "last_interaction_date": ld.get("last_interaction_date")}
                    for ld in tied]
                self._stage = "awaiting lead selection"
                try:
                    await workflow.wait_condition(
                        lambda: self._lead_choice is not None,
                        timeout=timedelta(hours=tp.confirmation_timeout_hours))
                except asyncio.TimeoutError:
                    raise ApplicationError(
                        "Lead selection was not answered in time — nothing was written; "
                        "re-capture and pick via /select-lead.", non_retryable=True) from None
                lead = next(ld for ld in tied if str(ld.get("id")) == self._lead_choice)
            else:
                lead = ranked[0]
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
        # Increment 7 (flag): the follow-up becomes a FIRST-CLASS calendar event, not just
        # a meta.calendar note. Idempotent per run (the activity matches by workflow_id),
        # organised by the owning RM. Best-effort: a calendar hiccup never voids a capture
        # that is already fully logged.
        if tp.create_calendar_event and tp.next_meeting_date:
            organizer = tp.assigned_rm or tp.performed_by
            if organizer:
                try:
                    event = await workflow.execute_activity(
                        activities.create_calendar_event,
                        args=["Lead", lead["id"],
                              tp.next_action or "Follow-up meeting",
                              tp.next_meeting_date, organizer,
                              [a for a in (tp.performed_by,) if a and a != organizer],
                              "VOX", tp.caller],
                        **_DURABLE_IO)
                    follow_up["calendar_event_id"] = event.get("id")
                    follow_up["calendar"] = "scheduled"
                except Exception:  # noqa: BLE001 — the capture itself already succeeded
                    workflow.logger.warning("calendar_event_failed",
                                            extra={"lead_id": lead["id"]})
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
        self._fnd = _Foundation()

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

    @workflow.signal
    def control(self, action: str, control_ref: str = "") -> None:
        """Run-control (cancel / return / resubmit). UNTRUSTED like every signal: queued
        here, verified against the durable control record before it does anything."""
        if self._decision is None:
            self._fnd.controls.append((action, control_ref))

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.query
    def state(self) -> dict:
        """BUSINESS status and TECHNICAL stage, separately — dashboards should never have
        to infer one from the other."""
        return self._fnd.state(self._stage)

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
        start = workflow.now() - timedelta(hours=inp.resumed_elapsed_hours)
        _upsert_search(inp.emit_search_attributes, self._fnd.business_status,
                       f"Lead:{inp.lead_id}")
        # The "committee-review task": tell the deployment's approvers a decision is now
        # awaited (falling back to the requester so the park is never silent). The parked
        # run itself is the queryable work item; SLA reminders below keep it alive.
        await _emit_ops("awaiting_conversion_decision", {
            "subject": f"Lead:{inp.lead_id}", "requested_by": inp.requested_by,
            "business_status": self._fnd.business_status},
            notify_to=(inp.approver_notify or [inp.requested_by]))
        while self._decision is None:
            waited = workflow.now() - start
            remaining = total - waited
            if remaining <= timedelta(0):
                break
            # SLA: fire whatever is due before sleeping again (escalation outranks reminder).
            if (due := self._fnd.due_sla_event(
                    waited, inp.sla_reminder_hours, inp.sla_escalation_hours)) is not None:
                await _emit_ops(due, {
                    "subject": f"Lead:{inp.lead_id}", "requested_by": inp.requested_by,
                    "business_status": self._fnd.business_status,
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=[inp.requested_by],
                    severity="warning" if due == "sla_escalation" else "info")
                continue
            # History pressure (very long waits): carry the elapsed window across the reset.
            if workflow.info().is_continue_as_new_suggested():
                import dataclasses
                workflow.continue_as_new(dataclasses.replace(
                    inp, resumed_elapsed_hours=waited.total_seconds() / 3600))
            try:
                await workflow.wait_condition(
                    lambda: bool(self._pending) or bool(self._fnd.controls),
                    timeout=self._fnd.next_wakeup(waited, remaining,
                                                  inp.sla_reminder_hours,
                                                  inp.sla_escalation_hours))
            except asyncio.TimeoutError:
                continue
            # Run-control first: a verified cancel ends the run; return/resubmit flip the
            # business status (and resubmit restarts the SLA clock).
            while self._fnd.controls and self._decision is None:
                action, ref = self._fnd.controls.pop(0)
                self._stage = "Verifying control"
                v = await workflow.execute_activity(
                    activities.verify_control, args=[ref, inp.caller], **_DURABLE_IO)
                self._stage = "Pending"
                if not v.get("valid"):
                    continue                      # spoofed / unrecorded — ignore
                verified_action = v["action"]
                if verified_action == "Cancelled":
                    self._fnd.business_status = "Cancelled"
                    self._fnd.cancelled_by = v.get("by")
                    self._fnd.cancel_note = v.get("note")
                elif verified_action == "ReturnedForInformation":
                    self._fnd.business_status = "ReturnedForInformation"
                elif verified_action == "Resubmitted":
                    self._fnd.business_status = "AwaitingDecision"
                    start = workflow.now()        # the decision window restarts, fully
                    self._fnd.reminders_sent = 0
                    self._fnd.escalated = False
                _upsert_search(inp.emit_search_attributes, self._fnd.business_status,
                               f"Lead:{inp.lead_id}")
                # A RETURN is the maker's to-do (amend and resubmit); a RESUBMIT puts
                # the run back in the approvers' queue; a CANCEL confirms to the maker.
                await _emit_ops("run_control", {
                    "subject": f"Lead:{inp.lead_id}", "action": verified_action,
                    "by": v.get("by"), "note": v.get("note")},
                    notify_to=(inp.approver_notify or [inp.requested_by])
                    if verified_action == "Resubmitted" else [inp.requested_by])
            if self._fnd.business_status == "Cancelled":
                self._stage = "Cancelled"
                await workflow.execute_activity(
                    activities.mark_lead_note,
                    args=[inp.lead_id,
                          f"Conversion request cancelled by {self._fnd.cancelled_by}: "
                          f"{self._fnd.cancel_note or 'no note'} (workflow {wf_id}).",
                          None], **_DURABLE_IO)
                return LeadConversionResult(workflow_id=wf_id, lead_id=inp.lead_id,
                                            status="Cancelled",
                                            decided_by=self._fnd.cancelled_by,
                                            decision_note=self._fnd.cancel_note)
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
            self._fnd.business_status = "TimedOut"
            _upsert_search(inp.emit_search_attributes, "TimedOut", f"Lead:{inp.lead_id}")
            await _emit_ops("decision_timeout", {
                "subject": f"Lead:{inp.lead_id}", "requested_by": inp.requested_by,
                "window_hours": inp.approval_timeout_hours},
                notify_to=[inp.requested_by], severity="warning")
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
            self._fnd.business_status = "Rejected"
            _upsert_search(inp.emit_search_attributes, "Rejected", f"Lead:{inp.lead_id}")
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
        self._fnd.business_status = "Approved"
        _upsert_search(inp.emit_search_attributes, "Approved", f"Lead:{inp.lead_id}")
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
        # A CHECKLIST, when supplied, is authoritative: the outcome is COMPUTED (every
        # required item must pass), never asserted — and the evaluation itself becomes part
        # of the immutable qualification evidence. Without one, the legacy passed flag
        # stands (the deployment hasn't configured a checklist).
        summary: dict = {}
        passed = inp.passed
        reason = inp.reason
        if inp.checklist:
            summary = evaluate_checklist(inp.checklist)
            passed = summary["passed"]
            if not passed:
                reason = (f"required checklist items failed: "
                          f"{', '.join(summary['failed_required'])}"
                          + (f" — {inp.reason}" if inp.reason else ""))
        # The qualification review is itself durable evidence on the lead (so a later reader can see
        # WHY it qualified, immutably), whether it passed or failed.
        self._stage = "Recording qualification"
        kind = "lead_qualification" if passed else "lead_qualification_failed"
        note = reason or ("qualified" if passed else "not qualified")
        if summary:
            note = (f"{note} [checklist {summary['items_passed']}/{summary['items_total']}"
                    f" passed]")
        evidence = await workflow.execute_activity(
            activities.attach_evidence,
            args=["Lead", inp.lead_id, kind,
                  inp.qualification_reference or f"qualification/{wf_id}",
                  inp.qualification_sha256, note, inp.caller],
            **_DURABLE_IO)

        if not passed:
            self._stage = "NotQualified"
            await workflow.execute_activity(
                activities.mark_lead_note,
                args=[inp.lead_id,
                      f"Lead not qualified by {inp.qualified_by}: "
                      f"{reason or 'no reason given'} (workflow {wf_id}).", inp.caller],
                **_DURABLE_IO)
            return LeadQualificationResult(
                workflow_id=wf_id, lead_id=inp.lead_id, status="NotQualified",
                evidence_id=evidence.get("id"), note=reason, checklist_summary=summary)

        self._stage = "Qualified"
        await workflow.execute_activity(
            activities.mark_lead_note,
            args=[inp.lead_id,
                  f"Lead qualified by {inp.qualified_by} — ready for structuring "
                  f"(workflow {wf_id}).", inp.caller],
            **_DURABLE_IO)
        return LeadQualificationResult(
            workflow_id=wf_id, lead_id=inp.lead_id, status="Qualified",
            evidence_id=evidence.get("id"), note=reason, checklist_summary=summary)


@workflow.defn
class DealStructuringWorkflow:
    """Structure a deal's LENDING FACILITY to the sanction milestone. The deal itself carries only
    the COMMERCIAL funnel (its `stage` is never touched here); every credit transition runs on the
    deal's lending tracker line(s): walk each line up the ordered pipeline (→ Diligence → Note
    Circulated), circulate the credit note as Lending evidence, then wait for the Credit
    Committee's decision (a signal). On approval, FILE the committee-approval + sanction-letter
    evidence per line (citing the per-line subject-bound decision the orchestrator recorded) and
    advance the line to 'Sanctioned' — a transition the Register's evidence gate accepts ONLY
    because that evidence is now on file. A hand-rolled PATCH to 'Sanctioned' is refused all the
    same, so the workflow is the ONLY way the milestone is reached."""

    def __init__(self) -> None:
        self._notified = False
        self._stage = "Structuring"
        self._fnd = _Foundation()
        # Credit-note versioning: v1 is the note circulated at start; each committee-rework
        # revision (revise_credit_note) bumps it.
        self._note_version = 0
        self._note_revisions: list[tuple] = []

    @workflow.signal
    def committee_decision(self, decision_ref: str = "") -> None:
        # A WAKE-UP ONLY. The run re-reads the AUTHORITATIVE committee decision the orchestrator
        # persisted (fresh-authorized, single-winner) and derives the outcome from THAT record — so
        # a direct Temporal signal carries no trusted outcome/approver/note/references. The payload
        # is ignored on purpose.
        self._notified = True

    @workflow.signal
    def control(self, action: str, control_ref: str = "") -> None:
        """Run-control (cancel / return / resubmit). UNTRUSTED like every signal: queued
        here, verified against the durable control record before it does anything."""
        self._fnd.controls.append((action, control_ref))

    @workflow.signal
    def revise_credit_note(self, reference: str, sha256: str = "", by: str = "") -> None:
        """Committee REWORK: circulate a REVISED credit note while the run awaits (or was
        returned for) a decision. Each revision is filed as a NEW credit_note evidence
        version on every line — the full circulation history stays immutable; the version
        counter tells the committee (and the audit) exactly what was decided on."""
        if reference.strip():
            self._note_revisions.append((reference.strip(), sha256 or None, by))

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.query
    def state(self) -> dict:
        """BUSINESS status and TECHNICAL stage, separately — dashboards should never have
        to infer one from the other."""
        return {**self._fnd.state(self._stage),
                "credit_note_version": self._note_version}

    @workflow.run
    async def run(self, inp: DealStructuringInput) -> DealStructuringResult:
        wf_id = workflow.info().workflow_id
        caller = inp.caller

        # -- 0. Credit execution runs on the deal's LENDING line(s) — a deal with none has nothing
        #       to structure. Fail clearly up front rather than wait for a committee decision that
        #       could sanction nothing.
        lines = await workflow.execute_activity(
            activities.find_lines_for_deal, args=["lending", inp.deal_id, caller], **_IO)
        if not lines:
            self._stage = "NoLendingLine"
            return DealStructuringResult(
                workflow_id=wf_id, deal_id=inp.deal_id, status="NoLendingLine",
                note="This deal has no lending tracker line. Create the facility line first — "
                     "credit structuring and sanction run on the lending record, not the deal.")

        # -- 1. Walk each line up the ordered pipeline to the committee stage (Note Circulated).
        #       Each hop goes through the Register's policy-enforcing API, so sequencing is
        #       enforced. Idempotent.
        for i, line in enumerate(lines):
            line_id = str(line.get("id"))
            for stage in ("Diligence", "Note Circulated"):
                if _stage_before(line.get("stage"), stage):
                    self._stage = f"Advancing to {stage}"
                    line = await workflow.execute_activity(
                        activities.advance_stage,
                        args=["lending", line_id, "stage", stage, None, caller], **_IO)
            lines[i] = line

        # -- 2. Circulate the structured credit note as evidence (the structuring artefact),
        #       filed against each lending line — the subject the committee will decide on.
        evidence_ids: list = []
        if inp.credit_note_reference:
            self._stage = "Circulating credit note"
            self._note_version = 1
            for line in lines:
                note_ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["Lending", str(line.get("id")), "credit_note",
                          inp.credit_note_reference, None,
                          "Structured credit note circulated to committee (v1)", caller],
                    **_DURABLE_IO)
                evidence_ids.append(note_ev.get("id"))

        # -- 3. Wait durably for the Credit Committee decision, VERIFYING each wake-up against the
        #       AUTHORITATIVE record the orchestrator persisted (fresh-authorized, single-winner).
        #       A spoofed/direct signal (no record, or a record for another subject) is ignored and
        #       the run keeps waiting — the outcome is NEVER taken from the signal.
        self._stage = "Awaiting committee decision"
        subject = f"Deal:{inp.deal_id}"
        total = timedelta(hours=inp.decision_timeout_hours)
        start = workflow.now() - timedelta(hours=inp.resumed_elapsed_hours)
        _upsert_search(inp.emit_search_attributes, self._fnd.business_status, subject)
        # The "committee-review task": notify the deployment's approvers that the note is
        # circulated and a committee decision is now awaited (fallback: the requester).
        await _emit_ops("awaiting_committee_decision", {
            "subject": subject, "requested_by": inp.requested_by,
            "note_reference": inp.credit_note_reference,
            "business_status": self._fnd.business_status},
            notify_to=(inp.approver_notify or [inp.requested_by]))
        verified: dict[str, Any] | None = None
        facility_outcomes: dict[str, dict[str, Any]] = {}
        while verified is None:
            waited = workflow.now() - start
            remaining = total - waited
            if remaining <= timedelta(0):
                break
            if (due := self._fnd.due_sla_event(
                    waited, inp.sla_reminder_hours, inp.sla_escalation_hours)) is not None:
                await _emit_ops(due, {
                    "subject": subject, "requested_by": inp.requested_by,
                    "business_status": self._fnd.business_status,
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=[inp.requested_by],
                    severity="warning" if due == "sla_escalation" else "info")
                continue
            if workflow.info().is_continue_as_new_suggested():
                import dataclasses
                workflow.continue_as_new(dataclasses.replace(
                    inp, resumed_elapsed_hours=waited.total_seconds() / 3600))
            try:
                await workflow.wait_condition(
                    lambda: (self._notified or bool(self._fnd.controls)
                             or bool(self._note_revisions)),
                    timeout=self._fnd.next_wakeup(waited, remaining,
                                                  inp.sla_reminder_hours,
                                                  inp.sla_escalation_hours))
            except asyncio.TimeoutError:
                continue
            # Committee REWORK: file each revised credit note as the next VERSION on every
            # line (immutable circulation history), before any decision is verified — the
            # committee always decides on the version the state query reports.
            while self._note_revisions:
                ref, sha, revised_by = self._note_revisions.pop(0)
                self._note_version += 1
                self._stage = f"Circulating credit note v{self._note_version}"
                for line in lines:
                    ev = await workflow.execute_activity(
                        activities.attach_evidence,
                        args=["Lending", str(line.get("id")), "credit_note", ref, sha,
                              f"Revised credit note circulated to committee "
                              f"(v{self._note_version}"
                              + (f", by {revised_by}" if revised_by else "") + ")",
                              caller],
                        **_DURABLE_IO)
                    evidence_ids.append(ev.get("id"))
                self._stage = "Awaiting committee decision"
                await _emit_ops("credit_note_revised", {
                    "subject": subject, "version": self._note_version,
                    "reference": ref, "by": revised_by})
            # Run-control first (verified against the durable control record, fail-closed).
            while self._fnd.controls:
                action, ref = self._fnd.controls.pop(0)
                self._stage = "Verifying control"
                v = await workflow.execute_activity(
                    activities.verify_control, args=[ref, caller], **_DURABLE_IO)
                self._stage = "Awaiting committee decision"
                if not v.get("valid"):
                    continue
                verified_action = v["action"]
                if verified_action == "Cancelled":
                    self._fnd.business_status = "Cancelled"
                    self._fnd.cancelled_by = v.get("by")
                    self._fnd.cancel_note = v.get("note")
                elif verified_action == "ReturnedForInformation":
                    self._fnd.business_status = "ReturnedForInformation"
                elif verified_action == "Resubmitted":
                    self._fnd.business_status = "AwaitingDecision"
                    start = workflow.now()
                    self._fnd.reminders_sent = 0
                    self._fnd.escalated = False
                _upsert_search(inp.emit_search_attributes, self._fnd.business_status, subject)
                # A RETURN is the maker's to-do (amend and resubmit); a RESUBMIT puts
                # the run back in the approvers' queue; a CANCEL confirms to the maker.
                await _emit_ops("run_control", {
                    "subject": subject, "action": verified_action,
                    "by": v.get("by"), "note": v.get("note")},
                    notify_to=(inp.approver_notify or [inp.requested_by])
                    if verified_action == "Resubmitted" else [inp.requested_by])
            if self._fnd.business_status == "Cancelled":
                self._stage = "Cancelled"
                return DealStructuringResult(
                    workflow_id=wf_id, deal_id=inp.deal_id, status="Cancelled",
                    decided_by=self._fnd.cancelled_by,
                    stage=lines[0].get("stage"), evidence_ids=evidence_ids,
                    note=self._fnd.cancel_note)
            if not self._notified:
                continue
            self._notified = False
            self._stage = "Verifying committee decision"
            v = await workflow.execute_activity(
                activities.verify_committee_decision, args=[inp.deal_id, caller], **_DURABLE_IO)
            if v.get("valid"):
                # Committee approval is FACILITY-SPECIFIC: the orchestrator records one
                # outcome per lending line before signalling. Read them all; a gap means
                # this wake-up did not come from the orchestrator → keep waiting.
                fv = await workflow.execute_activity(
                    activities.verify_facility_decisions,
                    args=[[str(line.get("id")) for line in lines], caller], **_DURABLE_IO)
                if fv.get("valid"):
                    verified = v
                    facility_outcomes = fv["facilities"]
                else:
                    self._stage = "Awaiting committee decision"
            else:
                self._stage = "Awaiting committee decision"   # spoofed / premature → keep waiting

        if verified is None:
            self._stage = "TimedOut"
            self._fnd.business_status = "TimedOut"
            _upsert_search(inp.emit_search_attributes, "TimedOut", subject)
            await _emit_ops("decision_timeout", {
                "subject": subject, "requested_by": inp.requested_by,
                "window_hours": inp.decision_timeout_hours},
                notify_to=[inp.requested_by], severity="warning")
            return DealStructuringResult(
                workflow_id=wf_id, deal_id=inp.deal_id, status="TimedOut",
                stage=lines[0].get("stage"), evidence_ids=evidence_ids,
                note="No committee decision within the window.")

        # Everything below is derived from the VERIFIED records — not the signal.
        decided_by = verified.get("decided_by")
        note = verified.get("note")
        committee_ref = verified.get("committee_reference") or f"committee/{wf_id}"
        sanction_ref = verified.get("sanction_letter_reference") or f"sanction/{wf_id}"

        # -- 4. Act on each facility's OWN recorded outcome. Committee approval is
        #       facility-specific: an approved line gets the sanction evidence (committee
        #       approval + sanction letter, each VERIFIED by the Register against the per-line
        #       subject-bound decision under "{wf_id}:lending:{line_id}") and advances to
        #       'Sanctioned'; a rejected line gets the rejection evidence and moves to
        #       'Rejected'. A single deal-wide result never implicitly sanctions lines — even a
        #       grouped submission was recorded per facility, and that record is what rules here.
        #       The deal's commercial funnel is the RM's call — the workflow never touches it.
        line_outcomes: dict[str, str] = {}
        for line in lines:
            line_id = str(line.get("id"))
            line_ref = f"{wf_id}:lending:{line_id}"
            fac = facility_outcomes.get(line_id, {})
            line_note = fac.get("note") or note
            if fac.get("outcome") == "Approved":
                self._stage = "Filing sanction evidence"
                for kind, ref in (("credit_committee_approval", committee_ref),
                                  ("sanction_letter", sanction_ref)):
                    ev = await workflow.execute_activity(
                        activities.attach_evidence,
                        args=["Lending", line_id, kind, ref, None, line_note, caller,
                              line_ref],
                        **_DURABLE_IO)
                    evidence_ids.append(ev.get("id"))
                # CONDITIONAL approval: the committee's conditions are governance evidence
                # on the line, verified against the SAME per-line decision the sanction
                # cites — the conditions travel with the sanction, immutably.
                if fac.get("conditions"):
                    ev = await workflow.execute_activity(
                        activities.attach_evidence,
                        args=["Lending", line_id, "sanction_conditions",
                              f"conditions/{line_ref}", None, fac["conditions"], caller,
                              line_ref],
                        **_DURABLE_IO)
                    evidence_ids.append(ev.get("id"))
                self._stage = "Sanctioning lending facility"
                # product_type is a DEAL field — LendingUpdate is extra="forbid", so send only
                # fields the lending line actually has.
                line_extra = {k: v for k, v in {"rm": inp.rm}.items() if v is not None}
                await workflow.execute_activity(
                    activities.advance_stage,
                    args=["lending", line_id, "stage", "Sanctioned", line_extra or None,
                          caller],
                    **_DURABLE_IO)
                line_outcomes[line_id] = "Sanctioned"
                # A validity window makes the sanction PERISHABLE: an abandoned child
                # monitor outlives this run, reminds ops before the deadline, and files
                # the expiry evidence if the facility never progresses past 'Sanctioned'.
                if fac.get("valid_days"):
                    await workflow.start_child_workflow(
                        SanctionExpiryMonitorWorkflow.run,
                        SanctionExpiryInput(
                            lending_id=line_id, deal_id=inp.deal_id,
                            valid_days=int(fac["valid_days"]),
                            decision_ref=line_ref, caller=caller,
                            emit_search_attributes=inp.emit_search_attributes),
                        id=f"{wf_id}:expiry:{line_id}",
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON)
            else:
                self._stage = "Recording facility rejection"
                await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["Lending", line_id, "credit_committee_rejection", committee_ref,
                          None, line_note or "Committee rejected", caller, line_ref],
                    **_DURABLE_IO)
                await workflow.execute_activity(
                    activities.advance_stage,
                    args=["lending", line_id, "stage", "Rejected", None, caller],
                    **_DURABLE_IO)
                line_outcomes[line_id] = "Rejected"

        sanctioned = [lid for lid, o in line_outcomes.items() if o == "Sanctioned"]
        if not sanctioned:
            self._stage = "Rejected"
            self._fnd.business_status = "Rejected"
            _upsert_search(inp.emit_search_attributes, "Rejected", subject)
            return DealStructuringResult(
                workflow_id=wf_id, deal_id=inp.deal_id, status="Rejected",
                decided_by=decided_by, stage="Rejected", evidence_ids=evidence_ids,
                note=note, line_outcomes=line_outcomes,
                credit_note_version=self._note_version)

        # -- 5. Record the sanction basics on the DEAL as plain data (product_type/rm carried on
        #       the structuring request). This is NOT a lifecycle change — the deal's stage is the
        #       commercial funnel, owned by the RM, and the workflow never moves it.
        deal_fields = {k: v for k, v in
                       {"product_type": inp.product_type, "rm": inp.rm}.items() if v is not None}
        if deal_fields:
            await workflow.execute_activity(
                activities.update_fields,
                args=["deals", inp.deal_id, deal_fields, caller], **_DURABLE_IO)

        status = "Sanctioned" if len(sanctioned) == len(line_outcomes) else "PartiallySanctioned"
        self._stage = status
        self._fnd.business_status = status
        _upsert_search(inp.emit_search_attributes, status, subject)
        return DealStructuringResult(
            workflow_id=wf_id, deal_id=inp.deal_id, status=status,
            decided_by=decided_by, stage="Sanctioned",
            evidence_ids=evidence_ids, note=note, line_outcomes=line_outcomes,
            credit_note_version=self._note_version)


# Ordered position of a SYNDICATION status, for walking the mandate up its pipeline one
# policy-checked step at a time (parallel to the lending _DEAL_ORDER walk).
_SYN_ORDER = ["Deal Sourced", "Docs Pending", "IM in Prep", "IM Circulated",
              "Queries Received", "IP Received", "Sanctioned", "Disbursed"]


def _syn_before(current: str | None, target: str) -> bool:
    if current not in _SYN_ORDER or target not in _SYN_ORDER:
        return False
    return _SYN_ORDER.index(current) < _SYN_ORDER.index(target)


@workflow.defn
class SyndicationMandateWorkflow:
    """The syndication mandate's journey: IM preparation → circulation (VERSIONED — every
    circulation is immutable evidence) → lender activity (queries / proposals, landing on
    the deal's lender rows through the policy-enforcing API) → the Syn Head's recorded
    decision (persist-before-signal, verified fail-closed) → sanction (the Register's
    syndication_sanction evidence gate accepts the advance ONLY because the verified
    evidence is now on file) → lender allocation (validated against the mandate amount) →
    done. Inherits the full run-control + SLA foundation."""

    def __init__(self) -> None:
        self._stage = "Starting"
        self._fnd = _Foundation()
        self._notified = False
        self._im_version = 0
        self._im_queue: list[tuple] = []          # (reference, sha, by)
        self._lender_ids: list[str] = []
        self._lender_queue: list[tuple] = []      # (row_id, status, note, by)
        self._allocation: dict | None = None

    # ---- signals (all UNTRUSTED; whitelisted / verified before any effect) ----
    @workflow.signal
    def syndication_decision(self, decision_ref: str = "") -> None:
        """A wake-up only — the run re-reads the AUTHORITATIVE persisted decision."""
        self._notified = True

    @workflow.signal
    def control(self, action: str, control_ref: str = "") -> None:
        self._fnd.controls.append((action, control_ref))

    @workflow.signal
    def circulate_im(self, reference: str, sha256: str = "", by: str = "") -> None:
        """Circulate the (next version of the) IM: filed as immutable im_document evidence
        on the mandate; the first circulation advances the mandate to 'IM Circulated'."""
        if reference.strip():
            self._im_queue.append((reference.strip(), sha256 or None, by))

    @workflow.signal
    def lender_update(self, row_id: str, status: str, note: str = "", by: str = "") -> None:
        """Lender-level activity (queries received, IP received, dropped …) for ONE of the
        deal's lender rows. Whitelisted to the rows this run discovered; the move itself
        goes through the Register's policy API, which enforces transition legality."""
        if row_id in self._lender_ids:
            self._lender_queue.append((row_id, status, note, by))

    @workflow.signal
    def allocate(self, allocations: dict, by: str = "") -> None:
        """The post-sanction lender allocation: {lender_row_id: amount_cr}. Whitelisted to
        the run's lender rows and validated against the mandate amount before it lands."""
        if self._allocation is None and allocations:
            self._allocation = {"by": by, "amounts": dict(allocations)}

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.query
    def state(self) -> dict:
        return {**self._fnd.state(self._stage), "im_version": self._im_version,
                "lender_rows": self._lender_ids}

    async def _advance_mandate(self, sid: str, target: str, current: str | None,
                               caller: Any) -> str:
        """Walk the mandate one policy-checked step at a time up to ``target``; returns the
        resulting status."""
        while current in _SYN_ORDER and _syn_before(current, target):
            nxt = _SYN_ORDER[_SYN_ORDER.index(current) + 1]
            row = await workflow.execute_activity(
                activities.advance_stage,
                args=["syndication", sid, "status", nxt, None, caller], **_DURABLE_IO)
            current = row.get("status")
        return current or target

    async def _file_im(self, sid: str, reference: str, sha: str | None, by: str,
                       caller: Any, evidence_ids: list) -> None:
        self._im_version += 1
        ev = await workflow.execute_activity(
            activities.attach_evidence,
            args=["Syndication", sid, "im_document", reference, sha,
                  f"IM circulated to lenders (v{self._im_version}"
                  + (f", by {by}" if by else "") + ")", caller],
            **_DURABLE_IO)
        evidence_ids.append(ev.get("id"))

    @workflow.run
    async def run(self, inp: SyndicationMandateInput) -> SyndicationMandateResult:
        wf_id = workflow.info().workflow_id
        caller = inp.caller
        subject = f"Syndication:{inp.syndication_id}"
        evidence_ids: list = []
        _upsert_search(inp.emit_search_attributes, self._fnd.business_status, subject)

        # -- 0. The mandate row + the deal's lender rows (everything else on the deal).
        mandate = await workflow.execute_activity(
            activities.get_resource, args=["syndication", inp.syndication_id, caller], **_IO)
        rows = await workflow.execute_activity(
            activities.find_lines_for_deal, args=["syndication", inp.deal_id, caller], **_IO)
        self._lender_ids = [str(r.get("id")) for r in rows
                            if str(r.get("id")) != str(inp.syndication_id)]
        current = mandate.get("status")

        # -- 1. First IM circulation (input, or the first circulate signal later).
        if inp.im_reference:
            self._stage = "Circulating IM"
            await self._file_im(inp.syndication_id, inp.im_reference, inp.im_sha256, "",
                                caller, evidence_ids)
            current = await self._advance_mandate(inp.syndication_id, "IM Circulated",
                                                  current, caller)

        # -- 2. Await the Syn Head's decision; lender activity + IM revisions keep landing.
        self._stage = "Awaiting syndication decision"
        total = timedelta(hours=inp.decision_timeout_hours)
        start = workflow.now() - timedelta(hours=inp.resumed_elapsed_hours)
        # Tell the deployment's approvers the mandate now awaits their decision
        # (fallback: the requester) — the parked run is the queryable work item.
        await _emit_ops("awaiting_syndication_decision", {
            "subject": subject, "requested_by": inp.requested_by,
            "business_status": self._fnd.business_status},
            notify_to=(inp.approver_notify or [inp.requested_by]))
        verified: dict[str, Any] | None = None
        while verified is None:
            waited = workflow.now() - start
            remaining = total - waited
            if remaining <= timedelta(0):
                break
            if (due := self._fnd.due_sla_event(
                    waited, inp.sla_reminder_hours, inp.sla_escalation_hours)) is not None:
                await _emit_ops(due, {
                    "subject": subject, "requested_by": inp.requested_by,
                    "business_status": self._fnd.business_status,
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=[inp.requested_by],
                    severity="warning" if due == "sla_escalation" else "info")
                continue
            if workflow.info().is_continue_as_new_suggested():
                import dataclasses
                workflow.continue_as_new(dataclasses.replace(
                    inp, resumed_elapsed_hours=waited.total_seconds() / 3600))
            try:
                await workflow.wait_condition(
                    lambda: (self._notified or bool(self._fnd.controls)
                             or bool(self._im_queue) or bool(self._lender_queue)),
                    timeout=self._fnd.next_wakeup(waited, remaining,
                                                  inp.sla_reminder_hours,
                                                  inp.sla_escalation_hours))
            except asyncio.TimeoutError:
                continue
            # IM revisions: each is the next immutable version.
            while self._im_queue:
                ref, sha, by = self._im_queue.pop(0)
                self._stage = f"Circulating IM v{self._im_version + 1}"
                await self._file_im(inp.syndication_id, ref, sha, by, caller, evidence_ids)
                current = await self._advance_mandate(inp.syndication_id, "IM Circulated",
                                                      current, caller)
                self._stage = "Awaiting syndication decision"
            # Lender activity: policy-enforced per-row moves; an illegal transition is
            # REFUSED by the Register and surfaced as an ops event, never a crashed run.
            while self._lender_queue:
                row_id, status, note, by = self._lender_queue.pop(0)
                self._stage = "Recording lender update"
                try:
                    await workflow.execute_activity(
                        activities.advance_stage,
                        args=["syndication", row_id, "status", status,
                              ({"remarks": note} if note else None), caller], **_IO)
                    await _emit_ops("lender_update", {
                        "subject": subject, "lender_row": row_id, "status": status,
                        "by": by, "note": note})
                except Exception:  # noqa: BLE001 — an illegal move must not kill the mandate run
                    await _emit_ops("lender_update_rejected", {
                        "subject": subject, "lender_row": row_id, "status": status,
                        "by": by})
                self._stage = "Awaiting syndication decision"
            # Run-control (verified fail-closed against the durable control record).
            while self._fnd.controls:
                action, ref = self._fnd.controls.pop(0)
                self._stage = "Verifying control"
                v = await workflow.execute_activity(
                    activities.verify_control, args=[ref, caller], **_DURABLE_IO)
                self._stage = "Awaiting syndication decision"
                if not v.get("valid"):
                    continue
                verified_action = v["action"]
                if verified_action == "Cancelled":
                    self._fnd.business_status = "Cancelled"
                    self._fnd.cancelled_by = v.get("by")
                    self._fnd.cancel_note = v.get("note")
                elif verified_action == "ReturnedForInformation":
                    self._fnd.business_status = "ReturnedForInformation"
                elif verified_action == "Resubmitted":
                    self._fnd.business_status = "AwaitingDecision"
                    start = workflow.now()
                    self._fnd.reminders_sent = 0
                    self._fnd.escalated = False
                _upsert_search(inp.emit_search_attributes, self._fnd.business_status,
                               subject)
                # A RETURN is the maker's to-do (amend and resubmit); a RESUBMIT puts
                # the run back in the approvers' queue; a CANCEL confirms to the maker.
                await _emit_ops("run_control", {
                    "subject": subject, "action": verified_action, "by": v.get("by"),
                    "note": v.get("note")},
                    notify_to=(inp.approver_notify or [inp.requested_by])
                    if verified_action == "Resubmitted" else [inp.requested_by])
            if self._fnd.business_status == "Cancelled":
                self._stage = "Cancelled"
                return SyndicationMandateResult(
                    workflow_id=wf_id, syndication_id=inp.syndication_id,
                    status="Cancelled", decided_by=self._fnd.cancelled_by,
                    im_version=self._im_version, evidence_ids=evidence_ids,
                    note=self._fnd.cancel_note)
            if not self._notified:
                continue
            self._notified = False
            self._stage = "Verifying syndication decision"
            v = await workflow.execute_activity(
                activities.verify_syndication_decision,
                args=[inp.syndication_id, caller], **_DURABLE_IO)
            if v.get("valid"):
                verified = v
            else:
                self._stage = "Awaiting syndication decision"

        if verified is None:
            self._stage = "TimedOut"
            self._fnd.business_status = "TimedOut"
            _upsert_search(inp.emit_search_attributes, "TimedOut", subject)
            await _emit_ops("decision_timeout", {
                "subject": subject, "requested_by": inp.requested_by,
                "window_hours": inp.decision_timeout_hours},
                notify_to=[inp.requested_by], severity="warning")
            return SyndicationMandateResult(
                workflow_id=wf_id, syndication_id=inp.syndication_id, status="TimedOut",
                im_version=self._im_version, evidence_ids=evidence_ids,
                note="No syndication decision within the window.")

        decided_by = verified.get("decided_by")
        note = verified.get("note")

        if verified["outcome"] == "Rejected":
            self._stage = "Rejected"
            self._fnd.business_status = "Rejected"
            _upsert_search(inp.emit_search_attributes, "Rejected", subject)
            await workflow.execute_activity(
                activities.advance_stage,
                args=["syndication", inp.syndication_id, "status", "Rejected", None,
                      caller], **_DURABLE_IO)
            return SyndicationMandateResult(
                workflow_id=wf_id, syndication_id=inp.syndication_id, status="Rejected",
                decided_by=decided_by, im_version=self._im_version,
                evidence_ids=evidence_ids, note=note)

        # -- 3. Approved → file the VERIFIED sanction evidence (bound to the recorded
        #       decision), then walk the mandate to 'Sanctioned' — an advance the
        #       Register's evidence gate accepts only because that evidence is on file.
        self._stage = "Filing sanction evidence"
        sanction_ref = verified.get("sanction_reference") or f"syn-sanction/{wf_id}"
        ev = await workflow.execute_activity(
            activities.attach_evidence,
            args=["Syndication", inp.syndication_id, "syndication_sanction", sanction_ref,
                  None, note, caller, wf_id],
            **_DURABLE_IO)
        evidence_ids.append(ev.get("id"))
        self._stage = "Sanctioning mandate"
        current = await self._advance_mandate(inp.syndication_id, "Sanctioned", current,
                                              caller)

        # -- 4. Lender allocation (bounded wait; the run completes without one rather than
        #       blocking the sanction forever — allocation can still be recorded later).
        self._stage = "Awaiting lender allocation"
        self._fnd.business_status = "Sanctioned"
        _upsert_search(inp.emit_search_attributes, "Sanctioned", subject)
        allocations: dict = {}
        try:
            await workflow.wait_condition(
                lambda: self._allocation is not None,
                timeout=timedelta(hours=inp.allocation_timeout_hours))
        except asyncio.TimeoutError:
            await _emit_ops("allocation_pending", {
                "subject": subject,
                "note": "Sanctioned mandate has no lender allocation yet."})
        if self._allocation is not None:
            amounts = {k: float(v) for k, v in self._allocation["amounts"].items()
                       if k in self._lender_ids}
            mandate_amount = mandate.get("amount_cr")
            total_alloc = sum(amounts.values())
            if mandate_amount is not None and total_alloc > float(mandate_amount) + 1e-9:
                await _emit_ops("allocation_rejected", {
                    "subject": subject, "total": total_alloc,
                    "mandate_amount": float(mandate_amount),
                    "note": "allocation exceeds the mandate amount; not applied"})
            elif amounts:
                self._stage = "Recording allocation"
                for row_id, amount in sorted(amounts.items()):
                    await workflow.execute_activity(
                        activities.update_fields,
                        args=["syndication", row_id, {"amount_cr": amount}, caller],
                        **_DURABLE_IO)
                alloc_note = ", ".join(f"{k}={v}" for k, v in sorted(amounts.items()))
                ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["Syndication", inp.syndication_id, "syndication_allocation",
                          f"allocation/{wf_id}", None,
                          f"Lender allocation ({self._allocation.get('by') or 'ops'}): "
                          f"{alloc_note}", caller],
                    **_DURABLE_IO)
                evidence_ids.append(ev.get("id"))
                allocations = amounts

        self._stage = "Sanctioned"
        return SyndicationMandateResult(
            workflow_id=wf_id, syndication_id=inp.syndication_id, status="Sanctioned",
            decided_by=decided_by, im_version=self._im_version, allocations=allocations,
            evidence_ids=evidence_ids, note=note)


_AM_ORDER = ["Teaser Prepared", "Teaser Shared", "In Discussion", "NBO Received",
             "BO Received", "SPA / Documentation", "Closed"]


def _am_before(current: str | None, target: str) -> bool:
    if current not in _AM_ORDER or target not in _AM_ORDER:
        return False
    return _AM_ORDER.index(current) < _AM_ORDER.index(target)


@workflow.defn
class AssetMonetisationWorkflow:
    """The asset-monetisation mandate's journey: teaser (VERSIONED evidence) → buyer
    outreach (buyer-level activity on the deal's buyer rows, policy-enforced) → NDA /
    data-room records (immutable evidence per buyer) → offers (NBO / binding — the offer
    comparison's immutable inputs; a binding offer advances the mandate) → the AM Head's
    recorded CLOSURE decision (persist-before-signal, verified fail-closed; the Register's
    am_closure_approval evidence gate accepts 'Closed' only because that verified evidence
    is on file) → Closed, or Lost. Inherits the full run-control + SLA foundation."""

    def __init__(self) -> None:
        self._stage = "Starting"
        self._fnd = _Foundation()
        self._notified = False
        self._teaser_version = 0
        self._teaser_queue: list[tuple] = []      # (reference, sha, by)
        self._buyer_ids: list[str] = []
        self._buyer_queue: list[tuple] = []       # (row_id, status, note, by)
        self._nda_queue: list[tuple] = []         # (row_id, reference, data_room, by)
        self._offer_queue: list[tuple] = []       # (row_id, kind, amount, reference, by)
        self._offers: list[dict] = []

    # ---- signals (UNTRUSTED; whitelisted / policy-checked / verified) ----
    @workflow.signal
    def am_decision(self, decision_ref: str = "") -> None:
        """Wake-up only — the run re-reads the AUTHORITATIVE persisted decision."""
        self._notified = True

    @workflow.signal
    def control(self, action: str, control_ref: str = "") -> None:
        self._fnd.controls.append((action, control_ref))

    @workflow.signal
    def circulate_teaser(self, reference: str, sha256: str = "", by: str = "") -> None:
        if reference.strip():
            self._teaser_queue.append((reference.strip(), sha256 or None, by))

    @workflow.signal
    def buyer_update(self, row_id: str, status: str, note: str = "", by: str = "") -> None:
        """Buyer-level pipeline movement on ONE of the deal's buyer rows (whitelisted; the
        move itself is policy-enforced by the Register)."""
        if row_id in self._buyer_ids:
            self._buyer_queue.append((row_id, status, note, by))

    @workflow.signal
    def record_nda(self, row_id: str, reference: str, data_room: bool = False,
                   by: str = "") -> None:
        """An NDA signed (and optionally data-room access granted) for ONE buyer —
        filed as immutable am_nda evidence on the mandate."""
        if row_id in self._buyer_ids and reference.strip():
            self._nda_queue.append((row_id, reference.strip(), data_room, by))

    @workflow.signal
    def record_offer(self, row_id: str, kind: str, amount_cr: float,
                     reference: str = "", by: str = "") -> None:
        """An offer from ONE buyer: kind 'nbo' (non-binding) or 'binding'. Every offer is
        immutable am_offer evidence — the comparison set can never be quietly edited."""
        if row_id in self._buyer_ids and kind in ("nbo", "binding") and amount_cr > 0:
            self._offer_queue.append((row_id, kind, float(amount_cr), reference, by))

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.query
    def state(self) -> dict:
        return {**self._fnd.state(self._stage), "teaser_version": self._teaser_version,
                "buyer_rows": self._buyer_ids}

    @workflow.query
    def offer_comparison(self) -> list:
        """Every offer this run collected, in arrival order — the comparison a closure
        decision is made on (each is also on file as evidence)."""
        return self._offers

    async def _advance_mandate(self, mid: str, target: str, current: str | None,
                               caller: Any) -> str:
        while current in _AM_ORDER and _am_before(current, target):
            nxt = _AM_ORDER[_AM_ORDER.index(current) + 1]
            row = await workflow.execute_activity(
                activities.advance_stage,
                args=["asset-monetisation", mid, "status", nxt, None, caller],
                **_DURABLE_IO)
            current = row.get("status")
        return current or target

    async def _file_teaser(self, mid: str, reference: str, sha: str | None, by: str,
                           caller: Any, evidence_ids: list) -> None:
        self._teaser_version += 1
        ev = await workflow.execute_activity(
            activities.attach_evidence,
            args=["AssetMonetisation", mid, "teaser_document", reference, sha,
                  f"Teaser circulated to buyers (v{self._teaser_version}"
                  + (f", by {by}" if by else "") + ")", caller],
            **_DURABLE_IO)
        evidence_ids.append(ev.get("id"))

    @workflow.run
    async def run(self, inp: AssetMonetisationInput) -> AssetMonetisationResult:  # noqa: PLR0915
        wf_id = workflow.info().workflow_id
        caller = inp.caller
        subject = f"AssetMonetisation:{inp.asset_mon_id}"
        evidence_ids: list = []
        _upsert_search(inp.emit_search_attributes, self._fnd.business_status, subject)

        mandate = await workflow.execute_activity(
            activities.get_resource,
            args=["asset-monetisation", inp.asset_mon_id, caller], **_IO)
        rows = await workflow.execute_activity(
            activities.find_lines_for_deal,
            args=["asset-monetisation", inp.deal_id, caller], **_IO)
        self._buyer_ids = [str(r.get("id")) for r in rows
                           if str(r.get("id")) != str(inp.asset_mon_id)]
        current = mandate.get("status")

        if inp.teaser_reference:
            self._stage = "Circulating teaser"
            await self._file_teaser(inp.asset_mon_id, inp.teaser_reference,
                                    inp.teaser_sha256, "", caller, evidence_ids)
            current = await self._advance_mandate(inp.asset_mon_id, "Teaser Shared",
                                                  current, caller)

        self._stage = "Awaiting closure decision"
        total = timedelta(hours=inp.decision_timeout_hours)
        start = workflow.now() - timedelta(hours=inp.resumed_elapsed_hours)
        # Tell the deployment's approvers the monetisation now awaits the closure
        # decision (fallback: the requester) — the parked run is the work item.
        await _emit_ops("awaiting_am_decision", {
            "subject": subject, "requested_by": inp.requested_by,
            "business_status": self._fnd.business_status},
            notify_to=(inp.approver_notify or [inp.requested_by]))
        verified: dict[str, Any] | None = None
        while verified is None:
            waited = workflow.now() - start
            remaining = total - waited
            if remaining <= timedelta(0):
                break
            if (due := self._fnd.due_sla_event(
                    waited, inp.sla_reminder_hours, inp.sla_escalation_hours)) is not None:
                await _emit_ops(due, {
                    "subject": subject, "requested_by": inp.requested_by,
                    "business_status": self._fnd.business_status,
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=[inp.requested_by],
                    severity="warning" if due == "sla_escalation" else "info")
                continue
            if workflow.info().is_continue_as_new_suggested():
                import dataclasses
                workflow.continue_as_new(dataclasses.replace(
                    inp, resumed_elapsed_hours=waited.total_seconds() / 3600))
            try:
                await workflow.wait_condition(
                    lambda: (self._notified or bool(self._fnd.controls)
                             or bool(self._teaser_queue) or bool(self._buyer_queue)
                             or bool(self._nda_queue) or bool(self._offer_queue)),
                    timeout=self._fnd.next_wakeup(waited, remaining,
                                                  inp.sla_reminder_hours,
                                                  inp.sla_escalation_hours))
            except asyncio.TimeoutError:
                continue
            while self._teaser_queue:
                ref, sha, by = self._teaser_queue.pop(0)
                self._stage = f"Circulating teaser v{self._teaser_version + 1}"
                await self._file_teaser(inp.asset_mon_id, ref, sha, by, caller,
                                        evidence_ids)
                current = await self._advance_mandate(inp.asset_mon_id, "Teaser Shared",
                                                      current, caller)
                self._stage = "Awaiting closure decision"
            while self._nda_queue:
                row_id, ref, data_room, by = self._nda_queue.pop(0)
                self._stage = "Recording NDA"
                ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["AssetMonetisation", inp.asset_mon_id, "am_nda", ref, None,
                          f"NDA signed for buyer row {row_id}"
                          + (" — data-room access GRANTED" if data_room else "")
                          + (f" (by {by})" if by else ""), caller],
                    **_DURABLE_IO)
                evidence_ids.append(ev.get("id"))
                await _emit_ops("am_nda", {"subject": subject, "buyer_row": row_id,
                                           "data_room": data_room, "by": by})
                self._stage = "Awaiting closure decision"
            while self._offer_queue:
                row_id, kind, amount, ref, by = self._offer_queue.pop(0)
                self._stage = "Recording offer"
                ev = await workflow.execute_activity(
                    activities.attach_evidence,
                    args=["AssetMonetisation", inp.asset_mon_id, "am_offer",
                          ref or f"offer/{wf_id}/{len(self._offers) + 1}", None,
                          f"{'Binding offer' if kind == 'binding' else 'NBO'} from buyer "
                          f"row {row_id}: {amount} Cr"
                          + (f" (by {by})" if by else ""), caller],
                    **_DURABLE_IO)
                evidence_ids.append(ev.get("id"))
                self._offers.append({"buyer_row": row_id, "kind": kind,
                                     "amount_cr": amount, "reference": ref})
                # An offer moves the MANDATE forward (policy-checked walk): any offer
                # reaches 'NBO Received'; a binding one reaches 'BO Received'.
                target = "BO Received" if kind == "binding" else "NBO Received"
                current = await self._advance_mandate(inp.asset_mon_id, target, current,
                                                      caller)
                await _emit_ops("am_offer", {"subject": subject, "buyer_row": row_id,
                                             "kind": kind, "amount_cr": amount})
                self._stage = "Awaiting closure decision"
            while self._buyer_queue:
                row_id, status, note, by = self._buyer_queue.pop(0)
                self._stage = "Recording buyer update"
                try:
                    await workflow.execute_activity(
                        activities.advance_stage,
                        args=["asset-monetisation", row_id, "status", status,
                              ({"notes": note} if note else None), caller], **_IO)
                    await _emit_ops("buyer_update", {
                        "subject": subject, "buyer_row": row_id, "status": status,
                        "by": by})
                except Exception:  # noqa: BLE001 — an illegal move must not kill the run
                    await _emit_ops("buyer_update_rejected", {
                        "subject": subject, "buyer_row": row_id, "status": status,
                        "by": by})
                self._stage = "Awaiting closure decision"
            while self._fnd.controls:
                action, ref = self._fnd.controls.pop(0)
                self._stage = "Verifying control"
                v = await workflow.execute_activity(
                    activities.verify_control, args=[ref, caller], **_DURABLE_IO)
                self._stage = "Awaiting closure decision"
                if not v.get("valid"):
                    continue
                verified_action = v["action"]
                if verified_action == "Cancelled":
                    self._fnd.business_status = "Cancelled"
                    self._fnd.cancelled_by = v.get("by")
                    self._fnd.cancel_note = v.get("note")
                elif verified_action == "ReturnedForInformation":
                    self._fnd.business_status = "ReturnedForInformation"
                elif verified_action == "Resubmitted":
                    self._fnd.business_status = "AwaitingDecision"
                    start = workflow.now()
                    self._fnd.reminders_sent = 0
                    self._fnd.escalated = False
                _upsert_search(inp.emit_search_attributes, self._fnd.business_status,
                               subject)
                # A RETURN is the maker's to-do (amend and resubmit); a RESUBMIT puts
                # the run back in the approvers' queue; a CANCEL confirms to the maker.
                await _emit_ops("run_control", {
                    "subject": subject, "action": verified_action, "by": v.get("by"),
                    "note": v.get("note")},
                    notify_to=(inp.approver_notify or [inp.requested_by])
                    if verified_action == "Resubmitted" else [inp.requested_by])
            if self._fnd.business_status == "Cancelled":
                self._stage = "Cancelled"
                return AssetMonetisationResult(
                    workflow_id=wf_id, asset_mon_id=inp.asset_mon_id, status="Cancelled",
                    decided_by=self._fnd.cancelled_by,
                    teaser_version=self._teaser_version, offers=self._offers,
                    evidence_ids=evidence_ids, note=self._fnd.cancel_note)
            if not self._notified:
                continue
            self._notified = False
            self._stage = "Verifying closure decision"
            v = await workflow.execute_activity(
                activities.verify_am_decision,
                args=[inp.asset_mon_id, caller], **_DURABLE_IO)
            if v.get("valid"):
                verified = v
            else:
                self._stage = "Awaiting closure decision"

        if verified is None:
            self._stage = "TimedOut"
            self._fnd.business_status = "TimedOut"
            _upsert_search(inp.emit_search_attributes, "TimedOut", subject)
            await _emit_ops("decision_timeout", {
                "subject": subject, "requested_by": inp.requested_by,
                "window_hours": inp.decision_timeout_hours},
                notify_to=[inp.requested_by], severity="warning")
            return AssetMonetisationResult(
                workflow_id=wf_id, asset_mon_id=inp.asset_mon_id, status="TimedOut",
                teaser_version=self._teaser_version, offers=self._offers,
                evidence_ids=evidence_ids,
                note="No closure decision within the window.")

        decided_by = verified.get("decided_by")
        note = verified.get("note")

        if verified["outcome"] == "Rejected":
            # A LOST / cancelled sale: the mandate drops, with the reason on record.
            self._stage = "Lost"
            self._fnd.business_status = "Lost"
            _upsert_search(inp.emit_search_attributes, "Lost", subject)
            await workflow.execute_activity(
                activities.advance_stage,
                args=["asset-monetisation", inp.asset_mon_id, "status", "Dropped", None,
                      caller], **_DURABLE_IO)
            return AssetMonetisationResult(
                workflow_id=wf_id, asset_mon_id=inp.asset_mon_id, status="Lost",
                decided_by=decided_by, teaser_version=self._teaser_version,
                offers=self._offers, evidence_ids=evidence_ids, note=note)

        # Approved → file the VERIFIED closure evidence, then walk to 'Closed' — an
        # advance the Register's evidence gate accepts only because it is on file.
        self._stage = "Filing closure evidence"
        closure_ref = verified.get("closure_reference") or f"am-closure/{wf_id}"
        ev = await workflow.execute_activity(
            activities.attach_evidence,
            args=["AssetMonetisation", inp.asset_mon_id, "am_closure_approval",
                  closure_ref, None, note, caller, wf_id],
            **_DURABLE_IO)
        evidence_ids.append(ev.get("id"))
        self._stage = "Closing mandate"
        await self._advance_mandate(inp.asset_mon_id, "Closed", current, caller)
        self._stage = "Closed"
        self._fnd.business_status = "Closed"
        _upsert_search(inp.emit_search_attributes, "Closed", subject)
        return AssetMonetisationResult(
            workflow_id=wf_id, asset_mon_id=inp.asset_mon_id, status="Closed",
            decided_by=decided_by, teaser_version=self._teaser_version,
            offers=self._offers, evidence_ids=evidence_ids, note=note)


@workflow.defn
class SanctionExpiryMonitorWorkflow:
    """A sanction with a validity window is PERISHABLE — this monitor is the clock.

    Started as an ABANDONED child by the structuring workflow for every facility whose
    committee approval set ``valid_days``. It sleeps to the reminder point (default 7 days
    before expiry), checks the line, and reminds ops if it still sits at 'Sanctioned';
    at expiry it checks once more and — if the facility NEVER progressed — files the
    ``sanction_expired`` evidence (immutable audit) and raises the ops event. A facility
    that moved on ends the run quietly as Progressed. The monitor only ever OBSERVES and
    RECORDS: it never moves the line itself — what happens to a lapsed sanction (re-table,
    extend, close) is a committee/RM call, on the record this run created."""

    def __init__(self) -> None:
        self._stage = "Watching validity window"

    @workflow.query
    def status(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: SanctionExpiryInput) -> SanctionExpiryResult:
        wf_id = workflow.info().workflow_id
        subject = f"Lending:{inp.lending_id}"
        total = timedelta(days=inp.valid_days)
        remind_at = total - timedelta(days=inp.remind_before_days)
        _upsert_search(inp.emit_search_attributes, "SanctionValid", subject)

        async def _line_stage() -> str | None:
            row = await workflow.execute_activity(
                activities.get_resource, args=["lending", inp.lending_id, inp.caller],
                **_DURABLE_IO)
            return row.get("stage")

        if inp.remind_before_days > 0 and remind_at > timedelta(0):
            await workflow.sleep(remind_at)
            if (stage := await _line_stage()) == "Sanctioned":
                self._stage = "Reminder raised"
                await _emit_ops("sanction_expiry_reminder", {
                    "subject": subject, "deal_id": inp.deal_id,
                    "days_left": inp.remind_before_days,
                    "decision_ref": inp.decision_ref},
                    notify_to=[inp.caller.email], severity="warning")
                await workflow.sleep(total - remind_at)
            elif stage is not None:
                self._stage = "Progressed"
                return SanctionExpiryResult(workflow_id=wf_id, lending_id=inp.lending_id,
                                            status="Progressed", stage_at_close=stage)
        else:
            await workflow.sleep(total)

        stage = await _line_stage()
        if stage != "Sanctioned":
            self._stage = "Progressed"
            return SanctionExpiryResult(workflow_id=wf_id, lending_id=inp.lending_id,
                                        status="Progressed", stage_at_close=stage)
        # The window lapsed unprogressed: put it on the record, loudly.
        self._stage = "Expired"
        ev = await workflow.execute_activity(
            activities.attach_evidence,
            args=["Lending", inp.lending_id, "sanction_expired",
                  f"expiry/{inp.decision_ref or wf_id}", None,
                  f"Sanction validity window ({inp.valid_days} days) lapsed with the "
                  f"facility still at 'Sanctioned'.", inp.caller],
            **_DURABLE_IO)
        _upsert_search(inp.emit_search_attributes, "SanctionExpired", subject)
        await _emit_ops("sanction_expired", {
            "subject": subject, "deal_id": inp.deal_id, "valid_days": inp.valid_days,
            "decision_ref": inp.decision_ref, "evidence_id": ev.get("id")},
            notify_to=[inp.caller.email], severity="critical")
        return SanctionExpiryResult(workflow_id=wf_id, lending_id=inp.lending_id,
                                    status="Expired", stage_at_close=stage,
                                    evidence_id=ev.get("id"))


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
    'Disbursed'. This workflow (the MAKER's action) creates the durable handover PACKAGE
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
        # Tell the checkers a package awaits their approval (fallback: the requester).
        # The Prepared register row is the durable work item the Today list serves.
        await _emit_ops("awaiting_checker_approval", {
            "subject": f"Lending:{inp.lending_id}", "requested_by": inp.requested_by,
            "package_id": pkg.get("id")},
            notify_to=(inp.approver_notify or [inp.requested_by]))
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
        # Tell the checkers a checklist awaits their approval (fallback: the requester).
        await _emit_ops("awaiting_checker_approval", {
            "subject": f"Lending:{inp.lending_id}", "requested_by": inp.requested_by,
            "checklist_id": chk.get("id"),
            "checklist_version": inp.checklist_version},
            notify_to=(inp.approver_notify or [inp.requested_by]))
        return CpcsChecklistResult(
            workflow_id=wf_id, lending_id=inp.lending_id, checklist_id=chk.get("id"),
            status=chk.get("status") or "Completed",
            note="CP/CS checklist prepared; a different checker must approve it before "
                 "cp_cs_completion can be filed.")


@workflow.defn
class DocumentExpiryMonitorWorkflow:
    """The tenant-wide document-expiry clock (increment 7). One run per tenant
    (workflow id ``doc-expiry-{tenant}``), looping forever:

    sweep → the Register marks lapsed documents 'Expired' (idempotent) and reports
            the newly expired + soon-to-expire sets
          → every NEWLY EXPIRED document raises a critical ops event + durable
            notifications (the document's uploader + the deployment's ops recipients)
          → every document ENTERING the warn window gets a warning the same way
          → sleep ``interval_hours`` (or until ``sweep_now``), continue-as-new
            periodically to bound history.

    The monitor only OBSERVES and RECORDS — replacing or re-validating an expired
    document is a human call, made on the record this run created. ``stop`` ends the
    monitor cleanly (e.g. before decommissioning a tenant)."""

    def __init__(self) -> None:
        self._stopped = False
        self._sweep_now = False
        self._last: dict[str, Any] = {}

    @workflow.signal
    def stop(self) -> None:
        self._stopped = True

    @workflow.signal
    def sweep_now(self) -> None:
        """Ops convenience: run the next sweep immediately instead of on the timer."""
        self._sweep_now = True

    @workflow.query
    def state(self) -> dict:
        return dict(self._last)

    @workflow.run
    async def run(self, inp: DocumentExpiryInput) -> DocumentExpiryResult:
        wf_id = workflow.info().workflow_id
        sweeps = expired_total = warned_total = 0
        _upsert_search(inp.emit_search_attributes, "Monitoring",
                       f"DocumentExpiry:{inp.caller.tenant or 'default'}")
        while not self._stopped:
            report = await workflow.execute_activity(
                activities.sweep_document_expiry, args=[inp.warn_days, inp.caller],
                start_to_close_timeout=timedelta(minutes=2), retry_policy=_DURABLE)
            sweeps += 1
            expired = list(report.get("expired") or [])
            expiring = list(report.get("expiring") or [])
            expired_total += len(expired)
            self._last = {"swept_on": report.get("swept_on"), "sweeps": sweeps,
                          "expired": len(expired), "expiring": len(expiring)}
            for doc in expired:
                await _emit_ops("document_expired", {
                    "subject": f"{doc.get('subject_type')}:{doc.get('subject_id')}",
                    "document_id": doc.get("id"), "title": doc.get("title"),
                    "slot_key": doc.get("slot_key"), "expires_on": doc.get("expires_on")},
                    notify_to=[doc.get("uploaded_by"), *inp.notify], severity="critical")
            for doc in expiring:
                # Warn each owner ONCE per document+date: the dedupe key derived from the
                # event+subject+discriminator makes repeats no-ops at the Register.
                warned_total += 1
                await _emit_ops("document_expiring", {
                    "subject": f"{doc.get('subject_type')}:{doc.get('subject_id')}",
                    "document_id": doc.get("id"), "title": doc.get("title"),
                    "slot_key": doc.get("slot_key"), "expires_on": doc.get("expires_on")},
                    notify_to=[doc.get("uploaded_by"), *inp.notify], severity="warning")
            if self._stopped:
                break
            # Sleep to the next sweep (or an ops nudge), then bound history.
            self._sweep_now = False
            try:
                await workflow.wait_condition(
                    lambda: self._stopped or self._sweep_now,
                    timeout=timedelta(hours=inp.interval_hours))
            except asyncio.TimeoutError:
                pass
            if not self._stopped and (sweeps >= inp.max_iterations
                                      or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new(inp)
        return DocumentExpiryResult(workflow_id=wf_id, sweeps=sweeps,
                                    expired_total=expired_total,
                                    warned_total=warned_total)


@workflow.defn
class CovenantMonitorWorkflow:
    """The tenant-wide covenant clock (increment 8). One run per tenant
    (``cov-monitor-{tenant}``), looping forever:

    sweep → the Register generates each schedule's due observations (idempotent per
            covenant+period), flags newly-OVERDUE submissions (due + grace lapsed,
            nothing filed), and expires lapsed waivers — which flips the breach LIVE
            again and re-opens its EWS case
          → every newly-overdue observation raises a warning (ops recipients)
          → every lapsed waiver raises a CRITICAL alert
          → sleep ``interval_hours`` (or until ``sweep_now``), continue-as-new.

    The RECURRING path is the point: the same sweep, run forever, generates every
    period exactly once and reports every lapse exactly once — the Register's partial
    unique index and status flips make replays no-ops."""

    def __init__(self) -> None:
        self._stopped = False
        self._sweep_now = False
        self._last: dict[str, Any] = {}

    @workflow.signal
    def stop(self) -> None:
        self._stopped = True

    @workflow.signal
    def sweep_now(self) -> None:
        """Ops convenience: run the next sweep immediately instead of on the timer."""
        self._sweep_now = True

    @workflow.query
    def state(self) -> dict:
        return dict(self._last)

    @workflow.run
    async def run(self, inp: CovenantMonitorInput) -> CovenantMonitorResult:
        wf_id = workflow.info().workflow_id
        sweeps = generated_total = overdue_total = expired_total = 0
        _upsert_search(inp.emit_search_attributes, "Monitoring",
                       f"Covenants:{inp.caller.tenant or 'default'}")
        while not self._stopped:
            report = await workflow.execute_activity(
                activities.sweep_covenants, args=[inp.horizon_days, inp.caller],
                start_to_close_timeout=timedelta(minutes=2), retry_policy=_DURABLE)
            sweeps += 1
            overdue = list(report.get("overdue") or [])
            expired = list(report.get("waivers_expired") or [])
            generated_total += int(report.get("generated") or 0)
            overdue_total += len(overdue)
            expired_total += len(expired)
            self._last = {"swept_on": report.get("swept_on"), "sweeps": sweeps,
                          "generated": report.get("generated"),
                          "overdue": len(overdue), "waivers_expired": len(expired)}
            for obs in overdue:
                await _emit_ops("covenant_overdue", {
                    "subject": f"Monitoring:{obs.get('id')}",
                    "covenant": obs.get("covenant_name"), "period": obs.get("period"),
                    "due_date": obs.get("due_date"), "deal_id": obs.get("deal_id")},
                    notify_to=list(inp.notify), severity="warning")
            for obs in expired:
                await _emit_ops("covenant_waiver_expired", {
                    "subject": f"Monitoring:{obs.get('id')}",
                    "covenant": obs.get("covenant_name"), "period": obs.get("period"),
                    "deal_id": obs.get("deal_id")},
                    notify_to=list(inp.notify), severity="critical")
            if self._stopped:
                break
            self._sweep_now = False
            try:
                await workflow.wait_condition(
                    lambda: self._stopped or self._sweep_now,
                    timeout=timedelta(hours=inp.interval_hours))
            except asyncio.TimeoutError:
                pass
            if not self._stopped and (sweeps >= inp.max_iterations
                                      or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new(inp)
        return CovenantMonitorResult(workflow_id=wf_id, sweeps=sweeps,
                                     generated_total=generated_total,
                                     overdue_total=overdue_total,
                                     waivers_expired_total=expired_total)


@workflow.defn
class EwsCaseWorkflow:
    """One EWS case's clock (increment 8). The DURABLE case record in the Register is
    the single source of truth: every wake-up RE-READS it — the ``case_updated`` signal
    is only a nudge and carries nothing trusted, so a forged signal can at worst make
    the run look at the real record sooner.

    The run keeps the case honest against its SLAs:

    * still unassigned past ``assign_sla_hours``            → ops reminder (once);
    * not escalated past ``investigation_sla_hours``        → AUTO-ESCALATED through the
      Register's audited service route (idempotent — a race with a human action is
      harmless), with a critical alert;
    * escalated but not closed                              → re-alert every
      ``escalated_reminder_hours`` until someone with authority closes it.

    The run completes when the record reaches 'Closed', returning the disposition."""

    def __init__(self) -> None:
        self._nudge = False
        self._status = "Open"
        self._reminded_unassigned = False
        self._auto_escalated = False
        self._escalation_reminders = 0

    @workflow.signal
    def case_updated(self) -> None:
        """A register-side action happened — look at the record now."""
        self._nudge = True

    @workflow.query
    def state(self) -> dict:
        return {"case_status": self._status,
                "reminded_unassigned": self._reminded_unassigned,
                "auto_escalated": self._auto_escalated,
                "escalation_reminders": self._escalation_reminders}

    @workflow.run
    async def run(self, inp: EwsCaseInput) -> EwsCaseResult:
        wf_id = workflow.info().workflow_id
        subject = f"EwsCase:{inp.case_id}"
        start = workflow.now() - timedelta(hours=inp.resumed_elapsed_hours)
        _upsert_search(inp.emit_search_attributes, "OpenCase", subject)
        while True:
            case = await workflow.execute_activity(
                activities.get_resource, args=["ews-cases", inp.case_id, inp.caller],
                **_DURABLE_IO)
            self._status = str(case.get("status"))
            recipients = [*inp.notify, case.get("assigned_to"), case.get("opened_by")]
            if self._status == "Closed":
                _upsert_search(inp.emit_search_attributes, "CaseClosed", subject)
                return EwsCaseResult(
                    workflow_id=wf_id, case_id=inp.case_id, status="Closed",
                    disposition=case.get("disposition"),
                    closed_by=case.get("closed_by"),
                    auto_escalated=self._auto_escalated)
            waited = workflow.now() - start

            # --- the SLA ladder (each rung fires once; escalation re-alerts) -------
            if (self._status == "Open" and not self._reminded_unassigned
                    and inp.assign_sla_hours > 0
                    and waited >= timedelta(hours=inp.assign_sla_hours)):
                self._reminded_unassigned = True
                await _emit_ops("ews_unassigned", {
                    "subject": subject, "severity_flag": case.get("severity"),
                    "title": case.get("title"),
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=recipients, severity="warning")
            if (self._status in ("Open", "UnderInvestigation")
                    and not self._auto_escalated and inp.investigation_sla_hours > 0
                    and waited >= timedelta(hours=inp.investigation_sla_hours)):
                self._auto_escalated = True
                escalated = await workflow.execute_activity(
                    activities.auto_escalate_ews_case,
                    args=[inp.case_id,
                          f"Investigation SLA ({inp.investigation_sla_hours}h) lapsed "
                          f"with the case still {self._status}.", inp.caller],
                    **_DURABLE_IO)
                self._status = str(escalated.get("status"))
                await _emit_ops("ews_auto_escalated", {
                    "subject": subject, "severity_flag": case.get("severity"),
                    "title": case.get("title"),
                    "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                    notify_to=recipients, severity="critical")
            if (self._status == "Escalated" and inp.escalated_reminder_hours > 0):
                esc_wait = waited - timedelta(hours=inp.investigation_sla_hours)
                due_reminders = int(esc_wait / timedelta(
                    hours=inp.escalated_reminder_hours)) if esc_wait > timedelta(0) else 0
                if due_reminders > self._escalation_reminders:
                    self._escalation_reminders = due_reminders
                    await _emit_ops("ews_escalation_pending", {
                        "subject": subject, "severity_flag": case.get("severity"),
                        "title": case.get("title"),
                        "reminder": self._escalation_reminders,
                        "waiting_hours": round(waited.total_seconds() / 3600, 1)},
                        notify_to=recipients, severity="warning")

            # --- sleep to the NEXT deadline (or a register-side nudge) -------------
            candidates = []
            if self._status == "Open" and not self._reminded_unassigned:
                candidates.append(timedelta(hours=inp.assign_sla_hours) - waited)
            if self._status in ("Open", "UnderInvestigation") and not self._auto_escalated:
                candidates.append(timedelta(hours=inp.investigation_sla_hours) - waited)
            if self._status == "Escalated" and inp.escalated_reminder_hours > 0:
                nxt = (timedelta(hours=inp.investigation_sla_hours)
                       + timedelta(hours=inp.escalated_reminder_hours
                                   * (self._escalation_reminders + 1)))
                candidates.append(nxt - waited)
            wake = min([c for c in candidates if c > timedelta(0)],
                       default=timedelta(hours=12))
            if workflow.info().is_continue_as_new_suggested():
                import dataclasses
                workflow.continue_as_new(dataclasses.replace(
                    inp, resumed_elapsed_hours=waited.total_seconds() / 3600))
            self._nudge = False
            try:
                await workflow.wait_condition(lambda: self._nudge,
                                              timeout=max(wake, timedelta(seconds=1)))
            except asyncio.TimeoutError:
                pass


# Ordered position of a LENDING credit-pipeline stage, for "is X before Y" checks in
# structuring. (A deal's own stage is the commercial funnel and is never walked here.)
_DEAL_ORDER = ["Data Awaited", "Diligence", "Note Circulated", "Sanctioned",
               "CP/CS Completed", "Ready for Disbursement", "Disbursed",
               "Disbursement Pending"]


def _stage_before(current: str | None, target: str) -> bool:
    """True when ``current`` is earlier than ``target`` in the credit pipeline (so a forward hop is
    needed). Unknown/None current → treat as earliest (advance). Off-track stages (On Hold /
    Rejected) are not ordered — never auto-advanced from here."""
    if current == target:
        return False
    if current is None:
        return True
    if current not in _DEAL_ORDER or target not in _DEAL_ORDER:
        return False
    return _DEAL_ORDER.index(current) < _DEAL_ORDER.index(target)
