"""Temporal activities — the side-effecting steps. Every Register write/read goes through
the shared ``evam-register-client``, so activities inherit auth, idempotency, optimistic
concurrency, retry and correlation for free.

An activity may run more than once (Temporal retries on failure). That is safe here
because every write carries an **idempotency key** derived from the workflow id, so a
replay never duplicates — see ``app.workflows``.

Layout: the legacy reference activities first, then the VOX touchpoint set (resolve /
create / update / log / follow-up), then the lead-conversion set. Each activity does ONE
Register call-cluster and returns plain JSON — orchestration decisions belong in the
workflow, not here.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from evam_register_client import AsyncRegisterClient
from evam_register_client.config import RegisterClientConfig
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import get_settings
from app.types import (
    CallerContext,
    InteractionInput,
    LeadConversionInput,
    VoxTouchpoint,
)


def _as_caller(caller: Any) -> CallerContext | None:
    """Normalise an activity's ``caller`` argument to a ``CallerContext``.

    Temporal's payload converter cannot always resolve the ``CallerContext`` annotation (with
    ``from __future__ import annotations`` every hint is a string), and then it hands the activity
    a plain ``dict`` instead — which used to blow up with
    ``AttributeError: 'dict' object has no attribute 'tenant'`` on the FIRST delegated call and
    stall the whole run in retries. Accept either shape, and ignore unknown keys so an older or
    newer payload can never break a running worker.
    """
    if caller is None or isinstance(caller, CallerContext):
        return caller
    if isinstance(caller, dict):
        allowed = CallerContext.__dataclass_fields__
        return CallerContext(**{k: v for k, v in caller.items() if k in allowed})
    return None


def _client(caller: CallerContext | None = None) -> AsyncRegisterClient:
    """A Register client for THIS activity.

    * **tenant** — always the CALLER's tenant when present (never the fixed default), so a
      tenant-B workflow can never operate against tenant A's data.
    * **delegation** — when the signed-context channel is configured and the workflow carries
      a known human, the worker re-mints a short-lived signed context (identity + the live
      grant) so the Register authorizes the activity's reads/writes as the HUMAN, with their
      scope, instead of the worker's full service grant. The token is minted fresh per
      activity and used immediately over the internal worker→Register hop; it is left route-
      UNBOUND (an activity legitimately does a read then a write in the same client), which
      is the deliberate difference from the gateway hop where a token is route-bound in
      transit. No signing configured (dev) → the service key, still tenant-scoped.
    """
    caller = _as_caller(caller)
    s = get_settings()
    tenant = (caller.tenant if caller and caller.tenant else s.register_tenant)
    extra: dict[str, str] | None = None
    if s.internal_signing_secret and caller and caller.email:
        from evam_backend_core.internal_token import mint_internal_context
        extra = {"X-Internal-Context": mint_internal_context(
            signing_key=s.internal_signing_secret,
            algorithm=s.internal_signing_algorithm,
            ttl_seconds=s.internal_token_ttl_seconds, tenant=tenant,
            email=caller.email, user_id=caller.user_id or caller.email,
            roles=list(caller.roles), report_ids=list(caller.report_ids),
            report_emails=list(caller.report_emails),
            effective_views=caller.effective_views,
            effective_operations=caller.effective_operations,
            matrix_version=0, decision=caller.decision)}
    return AsyncRegisterClient(config=RegisterClientConfig(
        base_url=s.register_base_url, api_key=s.register_api_key,
        tenant=tenant, actor=s.register_actor, extra_headers=extra or {}))


# --------------------------------------------------------------------------- #
# Canonical company-name matching (same philosophy as PULSE: explainable)
# --------------------------------------------------------------------------- #
_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|india|co|company)\b\.?", re.IGNORECASE)


def _canonical(name: str) -> str:
    """'EcoSoch Solar Pvt. Ltd' → 'ecosoch solar' — the comparison key for matching."""
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", name)).strip().lower()


def _entity_code(name: str) -> str:
    """A deterministic code for a NEW entity: name slug + a short stable hash, so a
    retried create derives the same code (and the idempotency key dedupes anyway)."""
    slug = re.sub(r"[^A-Z0-9]", "", _canonical(name).upper())[:12] or "ENTITY"
    return f"{slug}-{hashlib.sha256(name.encode()).hexdigest()[:4].upper()}"


# --------------------------------------------------------------------------- #
# Legacy reference activities
# --------------------------------------------------------------------------- #
@activity.defn
async def write_interaction(inp: InteractionInput, idempotency_key: str) -> dict[str, Any]:
    """Record the interaction against its entity. Idempotent on ``idempotency_key``."""
    async with _client(inp.caller) as reg:
        return await reg.log_interaction(
            "Entity", inp.entity_id, inp.interaction_type,
            source=inp.source, summary=inp.summary, notes=inp.notes,
            performed_by=inp.performed_by,
            idempotency_key=idempotency_key,
            request_id=activity.info().workflow_id,   # correlate to the workflow
        )


@activity.defn
async def fetch_dossier(entity_id: str,
                        caller: CallerContext | None = None) -> dict[str, Any]:
    """Read the entity's 360° dossier (deals, financials, interactions, open intel)."""
    async with _client(caller) as reg:
        return await reg.dossier(entity_id, request_id=activity.info().workflow_id)


# --------------------------------------------------------------------------- #
# VOX touchpoint activities
# --------------------------------------------------------------------------- #
@activity.defn
async def resolve_entity(company_name: str,
                         caller: CallerContext | None = None) -> dict[str, Any] | None:
    """Find the company by CANONICAL name: search the Register, then compare
    suffix-stripped lowercase names so 'EcoSoch Solar' matches 'EcoSoch Solar Pvt Ltd'.
    Returns the entity row, or None when the company is genuinely new."""
    wanted = _canonical(company_name)
    async with _client(caller) as reg:
        page = await reg.list("entities", q=company_name.strip()[:60], limit=50,
                              request_id=activity.info().workflow_id)
        for row in page.items:
            for candidate in (row.get("legal_name"), row.get("display_name")):
                if candidate and _canonical(candidate) == wanted:
                    return row
        # Second pass: search on the canonical form (catches suffix-only differences
        # where the raw string didn't hit the trigram search).
        if wanted and wanted != company_name.strip().lower():
            page = await reg.list("entities", q=wanted[:60], limit=50,
                                  request_id=activity.info().workflow_id)
            for row in page.items:
                for candidate in (row.get("legal_name"), row.get("display_name")):
                    if candidate and _canonical(candidate) == wanted:
                        return row
    return None


@activity.defn
async def resolve_entity_candidates(company_name: str,
                                    caller: CallerContext | None = None) -> dict[str, Any]:
    """Company resolution with the AMBIGUITY made explicit: the exact canonical match when
    one exists, plus the close-but-not-exact candidates (same search corpus) so the workflow
    can ask a human instead of silently creating a near-duplicate company.

    ``exact``      — the row whose suffix-stripped lowercase name equals the spoken name.
    ``candidates`` — other rows the search surfaced whose canonical name SHARES a leading
                     token with the spoken name ("EcoSoch" vs "EcoSoch Energy") — the ones
                     a human should adjudicate. Bounded, deduplicated, id-sorted."""
    wanted = _canonical(company_name)
    first_token = (wanted.split() or [""])[0]
    exact: dict[str, Any] | None = None
    near: dict[str, dict[str, Any]] = {}
    async with _client(caller) as reg:
        seen_pages = []
        page = await reg.list("entities", q=company_name.strip()[:60], limit=50,
                              request_id=activity.info().workflow_id)
        seen_pages.append(page.items)
        if wanted and wanted != company_name.strip().lower():
            page2 = await reg.list("entities", q=wanted[:60], limit=50,
                                   request_id=activity.info().workflow_id)
            seen_pages.append(page2.items)
    for items in seen_pages:
        for row in items:
            names = [n for n in (row.get("legal_name"), row.get("display_name")) if n]
            canon = [_canonical(n) for n in names]
            if exact is None and wanted in canon:
                exact = row
                continue
            if first_token and any(c.split()[:1] == [first_token] for c in canon if c):
                near.setdefault(str(row.get("id")), row)
    candidates = [near[k] for k in sorted(near)][:8]
    activity.logger.info("company_resolution",
                         extra={"company": company_name, "exact": bool(exact),
                                "candidates": len(candidates)})
    return {"exact": exact, "candidates": candidates}


@activity.defn
async def find_active_leads(entity_id: str,
                            caller: CallerContext | None = None) -> list[dict[str, Any]]:
    """ALL of the company's active leads (bounded), newest first — the workflow ranks them
    by owning RM / lens / sector / recency and, on a tie, can ask the capturing RM to pick
    instead of guessing."""
    async with _client(caller) as reg:
        page = await reg.list("leads", entity_id=entity_id, status="Active", limit=10,
                              request_id=activity.info().workflow_id)
        return list(page.items)


@activity.defn
async def create_entity(tp: VoxTouchpoint, idempotency_key: str) -> dict[str, Any]:
    """The 'new company' scenario: register the company from the capture's hints."""
    name = (tp.company_name or "").strip()
    async with _client(tp.caller) as reg:
        return await reg.create("entities", {
            "code": _entity_code(name),
            "legal_name": name,
            "display_name": name,
            "sector": tp.sector,
            "lens": tp.lens,
            "state": tp.state,
            "register_status": "Pipeline",
            "notes": f"Created by VOX capture {tp.capture_id or ''} "
                     f"(workflow {activity.info().workflow_id}).",
        }, idempotency_key=idempotency_key, request_id=activity.info().workflow_id)


@activity.defn
async def find_active_lead(entity_id: str,
                           caller: CallerContext | None = None) -> dict[str, Any] | None:
    """The company's active lead, newest first, or None."""
    async with _client(caller) as reg:
        page = await reg.list("leads", entity_id=entity_id, status="Active", limit=1,
                              request_id=activity.info().workflow_id)
        return page.items[0] if page.items else None


@activity.defn
async def create_lead(tp: VoxTouchpoint, entity_id: str,
                      idempotency_key: str) -> dict[str, Any]:
    """Open a lead for the company (new-company scenario, or existing company with no
    active lead). The assigned RM defaults to the acting RM who captured it."""
    async with _client(tp.caller) as reg:
        return await reg.create("leads", {
            "entity_id": entity_id,
            "company": (tp.company_name or "").strip() or "(unknown)",
            "sector": tp.sector,
            "lens": tp.lens,
            "source": "RM",
            "rm": tp.assigned_rm or tp.performed_by,
            "status": "Active",
            "contact": tp.contact_name,
            "last_interaction_date": (tp.occurred_at or "")[:10] or None,
            "next_action": tp.next_action,
            "next_action_date": tp.next_action_date,
            "notes": tp.summary,
        }, idempotency_key=idempotency_key, request_id=activity.info().workflow_id)


@activity.defn
async def update_lead_touch(lead_id: str, tp: VoxTouchpoint) -> dict[str, Any]:
    """Roll the touchpoint into an EXISTING active lead: last-interaction date,
    follow-up action, and (only if unset) the assigned RM. Retry-safe: the same values
    applied twice converge to the same row."""
    fields: dict[str, Any] = {}
    if tp.occurred_at:
        fields["last_interaction_date"] = tp.occurred_at[:10]
    if tp.next_action:
        fields["next_action"] = tp.next_action
    if tp.next_action_date:
        fields["next_action_date"] = tp.next_action_date
    async with _client(tp.caller) as reg:
        lead = await reg.get("leads", lead_id, request_id=activity.info().workflow_id)
        if not lead.get("rm") and (tp.assigned_rm or tp.performed_by):
            fields["rm"] = tp.assigned_rm or tp.performed_by
        if not fields:
            return lead
        return await reg.update("leads", lead_id, fields,
                                request_id=activity.info().workflow_id)


@activity.defn
async def assign_lead_owner(lead_id: str, user_id: str,
                            caller: CallerContext | None = None) -> dict[str, Any]:
    """Create the BDRM primary-owner assignment for a VOX-created lead, so the actual
    RM owns it (scoped lists/reads/writes work) — not just the ``rm`` name string.

    Idempotent: the assignments endpoint doesn't honour the Idempotency-Key header, so a
    Temporal retry after a lost response could otherwise hit the active-assignment unique
    constraint. We first check for an existing active assignment and return it."""
    async with _client(caller) as reg:
        page = await reg.list("assignments", subject_type="Lead", subject_id=lead_id,
                              request_id=activity.info().workflow_id)
        for row in page.items:
            if str(row.get("user_id")) == str(user_id) and row.get("ended_at") is None:
                return row
        return await reg.create("assignments", {
            "user_id": user_id, "subject_type": "Lead", "subject_id": lead_id,
            "assignment_role": "BDRM",
            "note": "Auto-assigned from a VOX capture (primary owner).",
        })


@activity.defn
async def log_touchpoint(tp: VoxTouchpoint, entity_id: str, lead_id: str | None,
                         idempotency_key: str) -> dict[str, Any]:
    """The full-fidelity interaction: transcript, audio reference, GPS, attendees,
    structured intel, both RMs, follow-up dates — and the Temporal workflow id in
    ``source_ref`` so any row can be traced back to its run."""
    wf_id = activity.info().workflow_id
    attachments = ([{"kind": "audio", "uri": tp.audio_ref}] if tp.audio_ref else None)
    meta: dict[str, Any] = {"assigned_rm": tp.assigned_rm, "acting_rm": tp.performed_by,
                            "capture_id": tp.capture_id}
    if tp.next_meeting_date:
        # The calendar hand-off record: a calendar integration (Google/Outlook) polls
        # interactions with meta.calendar.status == "pending" and writes back the event id.
        meta["calendar"] = {"status": "pending", "date": tp.next_meeting_date,
                            "title": tp.next_action or "Follow-up meeting"}
    async with _client(tp.caller) as reg:
        return await reg.log_interaction(
            "Entity", entity_id, tp.interaction_type,
            source="VOX",
            direction=tp.direction,
            occurred_at=tp.occurred_at,
            summary=tp.summary,
            notes=tp.notes,
            transcript=tp.transcript,
            language=tp.language,
            gps_lat=tp.gps_lat, gps_lng=tp.gps_lng, location=tp.location,
            attendees=tp.attendees, key_intel=tp.key_intel, next_steps=tp.next_steps,
            contact_name=tp.contact_name, performed_by=tp.performed_by,
            next_action=tp.next_action, next_action_date=tp.next_action_date,
            next_meeting_date=tp.next_meeting_date,
            attachments=attachments,
            source_ref=wf_id,
            meta={k: v for k, v in meta.items() if v is not None},
            idempotency_key=idempotency_key,
            request_id=wf_id,
        )


# --------------------------------------------------------------------------- #
# Lead-conversion activities
# --------------------------------------------------------------------------- #
async def _read_decision_record(workflow_id: str, kind: str,
                                tenant: str) -> dict[str, Any] | None:
    """Fetch the IMMUTABLE decision from the dedicated single-winner decision resource and
    confirm it is genuinely for THIS decision. Read as the workflow service principal, scoped
    to the run's tenant (RLS enforces isolation).

    CRITICAL failure semantics (P0): a genuine ``NotFoundError`` means no decision was ever
    recorded — return None (invalid reference). Any OTHER error — a transport failure, 429 or
    5xx while the Register is briefly unavailable — is RE-RAISED, so Temporal retries the
    activity WITHOUT consuming the decision. Swallowing those would silently discard a
    legitimate, already-acknowledged decision."""
    from evam_register_client.errors import NotFoundError

    async with _client(CallerContext(tenant=tenant)) as reg:
        try:
            rec = await reg.get_decision(workflow_id, request_id=workflow_id)
        except NotFoundError:
            return None   # no decision recorded → invalid reference
        # Every other error (transport / 429 / 5xx / conflict) propagates → Temporal retries.
    if rec.get("workflow_id") == workflow_id and rec.get("decision") == kind:
        return rec
    return None


@activity.defn
async def verify_decision(kind: str, by: str, approval_token: str, workflow_id: str,
                          lead_id: str = "", tenant: str = "",
                          decision_ref: str = "") -> dict[str, Any]:
    """Validate a lead-conversion approve/reject signal BEFORE the workflow lets it count, and
    return the AUTHORITATIVE decision. Fail-closed against a DIRECT Temporal signal — for BOTH
    approve and reject.

    The single-winner decision resource is the SOLE authority. The worker always derives the
    outcome, the approver identity AND the note from the persisted record — NEVER from the
    signal's (latest-caller) token or note. This means that when two approvers submit the same
    outcome, the run and the database always name the SAME (first) approver and note, and there
    is no dependence on a short-lived bearer token at all.

    Failure semantics (P0): the record read distinguishes a genuine ``NotFound`` (no decision
    → ``valid: false``, discard the spoofed/premature signal) from a transient transport / 429
    / 5xx error, which RAISES so Temporal retries WITHOUT consuming the decision. In dev (no
    signing) the signal is trusted, as elsewhere."""
    s = get_settings()
    if not s.internal_signing_secret:
        return {"valid": True, "email": by, "tenant": tenant, "user_id": by,
                "roles": [], "operations": {}, "views": {}, "note": None, "decision": kind}

    # The persisted single-winner record is the authority for identity, note AND outcome. A
    # transient Register failure RAISES from here (Temporal retries; decision not consumed).
    rec = await _read_decision_record(workflow_id, kind, tenant or s.register_tenant)
    if rec is None:
        return {"valid": False, "reason": "no durable decision record for this decision"}
    return {"valid": True, "email": rec.get("decided_by") or by,
            "tenant": tenant, "user_id": rec.get("decided_by_id") or by,
            "roles": list(rec.get("roles", [])), "operations": rec.get("operations", {}),
            "views": rec.get("views", {}), "note": rec.get("note"), "decision": kind}


@activity.defn
async def convert_lead_txn(inp: LeadConversionInput, idempotency_key: str,
                           approver: CallerContext | None = None,
                           approved_by: str | None = None) -> dict[str, Any]:
    """Apply the whole conversion in ONE Register transaction (deal + product lines +
    lead Converted, PLUS the RM/analyst line assignments). All-or-nothing on the server.

    AUTHORIZATION: the workflow passes the VERIFIED APPROVER context (already validated by
    ``verify_decision`` at decision time and recorded durably in history — so this does NOT
    depend on a short-lived JWT surviving a worker outage). It is authorized as the approver,
    NEVER the requester.

    FAIL CLOSED: in production (signing configured) a conversion with no verified approver is
    REFUSED — there is no fallback to ``inp.caller`` (the original requester), which would let
    a run convert on the requester's own authority and defeat the human-in-the-loop control.
    Only in dev (no signing) does the requester context stand in."""
    s = get_settings()
    approver_ok = approver is not None and bool(approver.email)
    if s.internal_signing_secret and not approver_ok:
        raise ApplicationError(
            "Refusing to convert without a verified approver identity.",
            non_retryable=True)
    caller = approver if approver_ok else inp.caller
    # Provenance = the VERIFIED approver identity (from the token). approved_by is a dev-only
    # fallback; it is never used to authorize, only to label.
    provenance = (approver.email if approver_ok and approver else approved_by)
    async with _client(caller) as reg:
        return await reg.convert_lead(
            inp.lead_id, is_lending=inp.is_lending, is_syndication=inp.is_syndication,
            is_asset_mon=inp.is_asset_mon, product_type=inp.product_type,
            amount_cr=inp.amount_cr, rm=inp.rm, analyst=inp.analyst,
            rm_id=inp.rm_id, analyst_id=inp.analyst_id, note=inp.note,
            approved_by=provenance,
            idempotency_key=idempotency_key, request_id=activity.info().workflow_id)


@activity.defn
async def get_lead(lead_id: str,
                   caller: CallerContext | None = None) -> dict[str, Any]:
    async with _client(caller) as reg:
        return await reg.get("leads", lead_id, request_id=activity.info().workflow_id)


@activity.defn
async def create_deal(inp: LeadConversionInput, entity_id: str,
                      idempotency_key: str) -> dict[str, Any]:
    async with _client(inp.caller) as reg:
        return await reg.create("deals", {
            "entity_id": entity_id,
            "product_type": inp.product_type,
            "is_lending": inp.is_lending,
            "is_syndication": inp.is_syndication,
            "is_asset_mon": inp.is_asset_mon,
            "rm": inp.rm,
            "analyst": inp.analyst,
            # The deal's stage is the COMMERCIAL funnel: an approved conversion enters at
            # 'In Pipeline'. Credit execution lives on the lending line (created separately
            # at 'Data Awaited').
            "stage": "In Pipeline",
            "source": "RM",
            "source_detail": f"Converted from lead {inp.lead_id}",
            "remarks": inp.note,
        }, idempotency_key=idempotency_key, request_id=activity.info().workflow_id)


@activity.defn
async def create_line(resource: str, entity_id: str, deal_id: str,
                      inp: LeadConversionInput, idempotency_key: str) -> dict[str, Any]:
    """One product-line tracker row (lending / syndication / asset-monetisation)."""
    body: dict[str, Any] = {"entity_id": entity_id, "deal_id": deal_id,
                            "rm": inp.rm, "analyst": inp.analyst}
    if resource == "lending":
        body |= {"amount_cr": inp.amount_cr, "stage": "Data Awaited"}
    elif resource == "syndication":
        body |= {"amount_cr": inp.amount_cr, "status": "Deal Sourced"}
    elif resource == "asset-monetisation":
        body |= {"status": "Teaser Prepared"}
    async with _client(inp.caller) as reg:
        return await reg.create(resource, body, idempotency_key=idempotency_key,
                                request_id=activity.info().workflow_id)


@activity.defn
async def mark_lead_converted(lead_id: str, deal_id: str, decided_by: str,
                              caller: CallerContext | None = None) -> dict[str, Any]:
    async with _client(caller) as reg:
        return await reg.update("leads", lead_id, {
            "status": "Converted",
            "converted_deal_id": deal_id,
            "conv": f"Converted by {decided_by}",
        }, request_id=activity.info().workflow_id)


@activity.defn
async def soft_delete_row(resource: str, obj_id: str,
                          caller: CallerContext | None = None) -> None:
    """Compensation: undo a row created earlier in a failed conversion. The Register's
    delete is soft (restorable) and idempotent enough for a rollback path."""
    async with _client(caller) as reg:
        try:
            await reg.delete(resource, obj_id, request_id=activity.info().workflow_id)
        except Exception:  # noqa: BLE001 - best-effort rollback; already-gone is fine
            pass


# --------------------------------------------------------------------------- #
# Business-lifecycle activities — evidence, stage advance, qualification, documents
# --------------------------------------------------------------------------- #
@activity.defn
async def attach_evidence(subject_type: str, subject_id: str, evidence_kind: str,
                          reference: str, sha256: str | None = None, note: str | None = None,
                          caller: CallerContext | None = None,
                          decision_ref: str | None = None) -> dict[str, Any]:
    """File an IMMUTABLE governance-evidence record for a subject (a committee approval, a sanction
    letter, an executed agreement, a qualification memo). This is the artefact the Register's
    evidence gate checks before it will accept a sensitive transition.

    The Register AUTHORISES this per kind (the workflow service holds the attach operations), so a
    governance kind carries the provenance the Register demands: this run's ``workflow_id`` +
    ``run_id`` and a ``decision_ref`` (the committee-decision reference), plus an integrity digest
    (derived from the reference when the caller didn't supply one).

    IDEMPOTENT: evidence is append-only (a retry must not file a second copy). We first list the
    subject's evidence and return the existing record if this exact (kind, reference) is already on
    file, so a Temporal retry after a lost response never duplicates it."""
    info = activity.info()
    digest = sha256 or hashlib.sha256((reference or "").encode()).hexdigest()
    async with _client(caller) as reg:
        page = await reg.list("evidence", subject_type=subject_type, subject_id=subject_id,
                              request_id=info.workflow_id)
        for row in page.items:
            if row.get("evidence_kind") == evidence_kind and row.get("reference") == reference:
                return row
        return await reg.create("evidence", {
            "subject_type": subject_type, "subject_id": subject_id,
            "evidence_kind": evidence_kind, "reference": reference,
            "sha256": digest, "note": note,
            "workflow_id": info.workflow_id, "run_id": info.workflow_run_id,
            "decision_ref": decision_ref or info.workflow_id,
        }, request_id=info.workflow_id)


@activity.defn
async def verify_committee_decision(deal_id: str,
                                    caller: CallerContext | None = None) -> dict[str, Any]:
    """Read the AUTHORITATIVE Credit Committee decision the orchestrator persisted for THIS run
    (single-winner, fresh-authorized, provenance server-set) and derive the outcome, approver, note
    AND references from it — NEVER from the untrusted Temporal signal.

    FAIL CLOSED: a missing record (a spoofed / direct Temporal signal, or one arriving before the
    orchestrator persisted the decision) returns ``valid=False`` so the workflow ignores the wake-up
    and keeps waiting; a record for a DIFFERENT subject, or with no terminal outcome, is likewise
    rejected. A genuine ``NotFound`` distinguishes "no decision" from a transient Register outage:
    only the former returns invalid; any other error propagates so Temporal retries."""
    from evam_register_client.errors import NotFoundError
    wf_id = str(activity.info().workflow_id)
    async with _client(caller) as reg:
        try:
            rec = await reg.get_decision(wf_id, request_id=wf_id)
        except NotFoundError:
            return {"valid": False, "reason": "no committee decision recorded for this run"}
        # Any other error (transport / 5xx / 429) propagates → Temporal retries, decision intact.
    if rec.get("subject_type") != "Deal" or str(rec.get("subject_id")) != str(deal_id):
        return {"valid": False, "reason": "committee decision is for a different subject"}
    if rec.get("decision") not in ("Approved", "Rejected"):
        return {"valid": False, "reason": "committee decision has no terminal outcome"}
    return {"valid": True, "outcome": rec.get("decision"), "decided_by": rec.get("decided_by"),
            "note": rec.get("note"),
            "committee_reference": rec.get("committee_reference"),
            "sanction_letter_reference": rec.get("sanction_letter_reference")}


@activity.defn
async def verify_control(control_ref: str,
                         caller: CallerContext | None = None) -> dict[str, Any]:
    """Verify a run-control signal (cancel / return-for-information / resubmit) against the
    DURABLE control record the orchestrator persisted BEFORE signalling.

    The signal itself is untrusted (anyone with Temporal access could send one); the record
    is not — it can only be written through the Register's service-principal endpoint with a
    verified human identity. FAIL CLOSED: no record, a record for a different run, or a
    non-control outcome → ``valid=False`` and the workflow ignores the wake-up."""
    from evam_register_client.errors import NotFoundError
    wf_id = str(activity.info().workflow_id)
    if not control_ref.startswith(f"{wf_id}:control:"):
        return {"valid": False, "reason": "control reference is not bound to this run"}
    async with _client(caller) as reg:
        try:
            rec = await reg.get_decision(control_ref, request_id=wf_id)
        except NotFoundError:
            return {"valid": False, "reason": "no control record for this reference"}
    action = rec.get("decision")
    if action not in ("Cancelled", "ReturnedForInformation", "Resubmitted"):
        return {"valid": False, "reason": "referenced record is not a control record"}
    activity.logger.info("control_verified", extra={"workflow": wf_id, "action": action,
                                                    "by": rec.get("decided_by")})
    return {"valid": True, "action": action, "by": rec.get("decided_by"),
            "note": rec.get("note")}


@activity.defn
async def emit_operational_event(event: str, detail: dict[str, Any]) -> dict[str, Any]:
    """Emit an operational event (SLA reminder, escalation, control action, business
    milestone) for the humans who run PRISM.

    Always lands in the structured log (the ops baseline every deployment has). When
    ``WORKFLOWS_OPS_WEBHOOK_URL`` is set, the event is ALSO posted as JSON to it — Slack,
    Teams, PagerDuty, or any receiver — with a short bounded retry. Delivery is deliberately
    BEST-EFFORT: an unreachable webhook logs a warning and returns delivered=False; it never
    fails the workflow that raised the event (increment 7 replaces this seam with the full
    notification service)."""
    import asyncio as _asyncio

    import httpx

    s = get_settings()
    wf_id = str(activity.info().workflow_id)
    payload = {"event": event, "workflow_id": wf_id, **detail}
    activity.logger.info("operational_event", extra={"ops": payload})
    if not s.ops_webhook_url:
        return {"delivered": False, "channel": "log"}
    last_error = ""
    for attempt in range(1 + max(0, s.ops_webhook_retries)):
        try:
            async with httpx.AsyncClient(timeout=s.ops_webhook_timeout_s) as client:
                r = await client.post(s.ops_webhook_url, json=payload)
            if r.status_code < 300:
                return {"delivered": True, "channel": "webhook"}
            last_error = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        await _asyncio.sleep(min(2.0 * (attempt + 1), 5.0))
    activity.logger.warning("operational_event_undelivered",
                            extra={"ops": payload, "error": last_error})
    return {"delivered": False, "channel": "webhook", "error": last_error}


@activity.defn
async def verify_syndication_decision(syndication_id: str,
                                      caller: CallerContext | None = None) -> dict[str, Any]:
    """The AUTHORITATIVE syndication decision for THIS run (persisted by the orchestrator
    with Syn Head authority, single-winner, subject-bound). FAIL CLOSED exactly like the
    committee verifier: no record / wrong subject / no terminal outcome → valid=False and
    the run keeps waiting."""
    from evam_register_client.errors import NotFoundError
    wf_id = str(activity.info().workflow_id)
    async with _client(caller) as reg:
        try:
            rec = await reg.get_decision(wf_id, request_id=wf_id)
        except NotFoundError:
            return {"valid": False, "reason": "no syndication decision recorded for this run"}
    if (rec.get("subject_type") != "Syndication"
            or str(rec.get("subject_id")) != str(syndication_id)):
        return {"valid": False, "reason": "syndication decision is for a different subject"}
    if rec.get("decision") not in ("Approved", "Rejected"):
        return {"valid": False, "reason": "syndication decision has no terminal outcome"}
    return {"valid": True, "outcome": rec.get("decision"), "decided_by": rec.get("decided_by"),
            "note": rec.get("note"),
            "sanction_reference": rec.get("committee_reference"),
            "conditions": rec.get("conditions")}


@activity.defn
async def verify_facility_decisions(line_ids: list[str],
                                    caller: CallerContext | None = None) -> dict[str, Any]:
    """Read the PER-FACILITY committee outcomes the orchestrator persisted under
    ``{workflow_id}:lending:{line_id}`` — committee approval is facility-specific, so the
    workflow acts on each line's own recorded outcome, never a blanket deal result.

    FAIL CLOSED like the deal-level verifier: a missing or non-terminal record for ANY line
    returns ``valid=False`` (the orchestrator persists every line's decision before it
    signals, so a gap means the wake-up did not come from the orchestrator)."""
    from evam_register_client.errors import NotFoundError
    wf_id = str(activity.info().workflow_id)
    outcomes: dict[str, dict[str, Any]] = {}
    async with _client(caller) as reg:
        for lid in line_ids:
            try:
                rec = await reg.get_decision(f"{wf_id}:lending:{lid}", request_id=wf_id)
            except NotFoundError:
                return {"valid": False,
                        "reason": f"no committee decision recorded for facility {lid}"}
            if (rec.get("subject_type") != "Lending"
                    or str(rec.get("subject_id")) != str(lid)
                    or rec.get("decision") not in ("Approved", "Rejected")):
                return {"valid": False,
                        "reason": f"facility decision for {lid} is invalid or non-terminal"}
            outcomes[str(lid)] = {"outcome": rec.get("decision"), "note": rec.get("note"),
                                  "conditions": rec.get("conditions"),
                                  "valid_days": rec.get("valid_days")}
    return {"valid": True, "facilities": outcomes}


@activity.defn
async def advance_stage(resource: str, obj_id: str, stage_field: str, stage_value: str,
                        extra: dict[str, Any] | None = None,
                        caller: CallerContext | None = None) -> dict[str, Any]:
    """Advance a line's lifecycle stage through the Register's normal, policy-enforcing update API
    (never a side-door write) — so ordered-transition, mandatory-field, field-lock and EVIDENCE
    gates all apply. ``extra`` carries the mandatory fields a target stage needs (e.g. product_type
    + rm for Sanctioned). Idempotent: re-applying the same target stage is a no-op the Register
    accepts."""
    body: dict[str, Any] = {stage_field: stage_value, **(extra or {})}
    async with _client(caller) as reg:
        lead = await reg.get(resource, obj_id, request_id=activity.info().workflow_id)
        if lead.get(stage_field) == stage_value and not extra:
            return lead   # already there — nothing to do
        return await reg.update(resource, obj_id, body,
                                request_id=activity.info().workflow_id)



@activity.defn
async def update_fields(resource: str, obj_id: str, fields: dict[str, Any],
                        caller: CallerContext | None = None) -> dict[str, Any]:
    """A plain, policy-enforced field update with NO lifecycle change (e.g. recording
    product_type/rm on the deal when its lending line is sanctioned). Idempotent: values
    already equal on the row are skipped, and an empty remainder is a no-op read."""
    async with _client(caller) as reg:
        row = await reg.get(resource, obj_id, request_id=activity.info().workflow_id)
        body = {k: v for k, v in fields.items() if v is not None and row.get(k) != v}
        if not body:
            return row
        return await reg.update(resource, obj_id, body,
                                request_id=activity.info().workflow_id)


@activity.defn
async def find_lines_for_deal(resource: str, deal_id: str,
                              caller: CallerContext | None = None) -> list[dict[str, Any]]:
    """The product lines of ``resource`` (lending / syndication / asset-monetisation) that belong
    to ``deal_id``. ``deal_id`` is a whitelisted filter on each line resource."""
    async with _client(caller) as reg:
        page = await reg.list(resource, deal_id=str(deal_id), limit=50,
                              request_id=activity.info().workflow_id)
        return list(page.items)


@activity.defn
async def get_resource(resource: str, obj_id: str,
                       caller: CallerContext | None = None) -> dict[str, Any]:
    async with _client(caller) as reg:
        return await reg.get(resource, obj_id, request_id=activity.info().workflow_id)


@activity.defn
async def prepare_cpcs_checklist(lending_id: str, payload: dict[str, Any],
                                 caller: CallerContext | None = None) -> dict[str, Any]:
    """Prepare (as the MAKER) the authoritative CP/CS checklist in the Register. The Register
    validates CP/CS structure + waiver / CS-deferment controls; a DIFFERENT checker approves it
    afterwards, and only then can cp_cs_completion be minted from it."""
    async with _client(caller) as reg:
        return await reg.create("internal/cpcs-checklists", {"lending_id": lending_id, **payload},
                                request_id=activity.info().workflow_id)


@activity.defn
async def create_handover_package(lending_id: str, package: dict[str, Any],
                                  caller: CallerContext | None = None) -> dict[str, Any]:
    """Create the durable, immutable Advaya handover PACKAGE in the Register and advance the line to
    'Disbursed' — TRANSACTIONALLY, so the stage moves only after the snapshot is durably
    written. Authoritative amounts (facility, proposed drawdown) are read from the Lending row
    server-side; ``package`` carries only the handover metadata (executed-doc refs, package
    ref/digest, CP/CS checklist version, delivery method/recipient, initiator/approver, note).
    Idempotent per Lending line — a retry returns the existing package."""
    info = activity.info()
    async with _client(caller) as reg:
        return await reg.create("internal/handover-packages",
                                {"lending_id": lending_id, **package},
                                request_id=info.workflow_id)


@activity.defn
async def record_advaya_handoff(handoff_key: str, lending_id: str, payload_sha256: str,
                                status: str, acknowledgement_id: str | None = None,
                                caller: CallerContext | None = None) -> dict[str, Any]:
    """Record the authoritative, immutable, single-winner Advaya-handoff OUTCOME in the Register.

    This is the FUTURE-Advaya-integration hook: when a real Advaya round-trip exists, its accepted
    response is recorded here and the ``advaya_acknowledgement`` evidence is VERIFIED against it
    before a line may reach 'Disbursement Pending'. It is NOT called on the current handover path
    (PRISM stops at 'Disbursed' — no Advaya call, no fabricated acceptance). Idempotent:
    a replay of the same outcome returns the original; a contradictory one is refused (409)."""
    info = activity.info()
    async with _client(caller) as reg:
        return await reg.create("internal/advaya-handoffs", {
            "handoff_key": handoff_key, "lending_id": lending_id,
            "payload_sha256": payload_sha256, "status": status,
            "acknowledgement_id": acknowledgement_id,
            "workflow_id": info.workflow_id, "run_id": info.workflow_run_id,
        }, request_id=info.workflow_id)


@activity.defn
async def mark_lead_note(lead_id: str, note: str,
                         caller: CallerContext | None = None) -> dict[str, Any]:
    """Record a rejection/timeout outcome on the lead without changing its status.

    IDEMPOTENT: this runs under the durable (unlimited-retry) policy, so it may execute more
    than once. It appends the note only if that exact line is not already present, so a retry
    (or a re-signalled outcome) never double-appends.

    CONCURRENCY-SAFE: the update carries the row's ``version`` as an If-Match precondition, so
    a concurrent edit to the lead's notes cannot be silently overwritten (last-writer-wins) —
    a version conflict raises and Temporal retries against the fresh row."""
    async with _client(caller) as reg:
        lead = await reg.get("leads", lead_id, request_id=activity.info().workflow_id)
        existing = (lead.get("notes") or "").strip()
        if note.strip() in existing:
            return lead   # already recorded — nothing to do (idempotent)
        merged = f"{existing}\n{note}".strip() if existing else note
        return await reg.update("leads", lead_id, {"notes": merged},
                                expected_version=lead.get("version"),
                                request_id=activity.info().workflow_id)
