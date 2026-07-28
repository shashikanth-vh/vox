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
from temporalio import activity

from app.config import get_settings
from app.types import InteractionInput, LeadConversionInput, VoxTouchpoint


def _client() -> AsyncRegisterClient:
    s = get_settings()
    return AsyncRegisterClient(
        s.register_base_url, s.register_api_key,
        tenant=s.register_tenant, actor=s.register_actor,
    )


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
    async with _client() as reg:
        return await reg.log_interaction(
            "Entity", inp.entity_id, inp.interaction_type,
            source=inp.source, summary=inp.summary, notes=inp.notes,
            performed_by=inp.performed_by,
            idempotency_key=idempotency_key,
            request_id=activity.info().workflow_id,   # correlate to the workflow
        )


@activity.defn
async def fetch_dossier(entity_id: str) -> dict[str, Any]:
    """Read the entity's 360° dossier (deals, financials, interactions, open intel)."""
    async with _client() as reg:
        return await reg.dossier(entity_id, request_id=activity.info().workflow_id)


# --------------------------------------------------------------------------- #
# VOX touchpoint activities
# --------------------------------------------------------------------------- #
@activity.defn
async def resolve_entity(company_name: str) -> dict[str, Any] | None:
    """Find the company by CANONICAL name: search the Register, then compare
    suffix-stripped lowercase names so 'EcoSoch Solar' matches 'EcoSoch Solar Pvt Ltd'.
    Returns the entity row, or None when the company is genuinely new."""
    wanted = _canonical(company_name)
    async with _client() as reg:
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
async def create_entity(tp: VoxTouchpoint, idempotency_key: str) -> dict[str, Any]:
    """The 'new company' scenario: register the company from the capture's hints."""
    name = (tp.company_name or "").strip()
    async with _client() as reg:
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
async def find_active_lead(entity_id: str) -> dict[str, Any] | None:
    """The company's active lead, newest first, or None."""
    async with _client() as reg:
        page = await reg.list("leads", entity_id=entity_id, status="Active", limit=1,
                              request_id=activity.info().workflow_id)
        return page.items[0] if page.items else None


@activity.defn
async def create_lead(tp: VoxTouchpoint, entity_id: str,
                      idempotency_key: str) -> dict[str, Any]:
    """Open a lead for the company (new-company scenario, or existing company with no
    active lead). The assigned RM defaults to the acting RM who captured it."""
    async with _client() as reg:
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
    async with _client() as reg:
        lead = await reg.get("leads", lead_id, request_id=activity.info().workflow_id)
        if not lead.get("rm") and (tp.assigned_rm or tp.performed_by):
            fields["rm"] = tp.assigned_rm or tp.performed_by
        if not fields:
            return lead
        return await reg.update("leads", lead_id, fields,
                                request_id=activity.info().workflow_id)


@activity.defn
async def assign_lead_owner(lead_id: str, user_id: str) -> dict[str, Any]:
    """Create the BDRM primary-owner assignment for a VOX-created lead, so the actual
    RM owns it (scoped lists/reads/writes work) — not just the ``rm`` name string.

    Idempotent: the assignments endpoint doesn't honour the Idempotency-Key header, so a
    Temporal retry after a lost response could otherwise hit the active-assignment unique
    constraint. We first check for an existing active assignment and return it."""
    async with _client() as reg:
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
    async with _client() as reg:
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
@activity.defn
async def convert_lead_txn(inp: LeadConversionInput, idempotency_key: str) -> dict[str, Any]:
    """Apply the whole conversion in ONE Register transaction (deal + product lines +
    lead Converted). All-or-nothing on the server — the workflow no longer creates rows
    step-by-step and compensate on failure."""
    async with _client() as reg:
        return await reg.convert_lead(
            inp.lead_id, is_lending=inp.is_lending, is_syndication=inp.is_syndication,
            is_asset_mon=inp.is_asset_mon, product_type=inp.product_type,
            amount_cr=inp.amount_cr, rm=inp.rm, analyst=inp.analyst, note=inp.note,
            idempotency_key=idempotency_key, request_id=activity.info().workflow_id)


@activity.defn
async def get_lead(lead_id: str) -> dict[str, Any]:
    async with _client() as reg:
        return await reg.get("leads", lead_id, request_id=activity.info().workflow_id)


@activity.defn
async def create_deal(inp: LeadConversionInput, entity_id: str,
                      idempotency_key: str) -> dict[str, Any]:
    async with _client() as reg:
        return await reg.create("deals", {
            "entity_id": entity_id,
            "product_type": inp.product_type,
            "is_lending": inp.is_lending,
            "is_syndication": inp.is_syndication,
            "is_asset_mon": inp.is_asset_mon,
            "rm": inp.rm,
            "analyst": inp.analyst,
            "stage": "Data Awaited",
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
    async with _client() as reg:
        return await reg.create(resource, body, idempotency_key=idempotency_key,
                                request_id=activity.info().workflow_id)


@activity.defn
async def mark_lead_converted(lead_id: str, deal_id: str, decided_by: str) -> dict[str, Any]:
    async with _client() as reg:
        return await reg.update("leads", lead_id, {
            "status": "Converted",
            "converted_deal_id": deal_id,
            "conv": f"Converted by {decided_by}",
        }, request_id=activity.info().workflow_id)


@activity.defn
async def soft_delete_row(resource: str, obj_id: str) -> None:
    """Compensation: undo a row created earlier in a failed conversion. The Register's
    delete is soft (restorable) and idempotent enough for a rollback path."""
    async with _client() as reg:
        try:
            await reg.delete(resource, obj_id, request_id=activity.info().workflow_id)
        except Exception:  # noqa: BLE001 - best-effort rollback; already-gone is fine
            pass


@activity.defn
async def mark_lead_note(lead_id: str, note: str) -> dict[str, Any]:
    """Record a rejection/timeout outcome on the lead without changing its status."""
    async with _client() as reg:
        lead = await reg.get("leads", lead_id, request_id=activity.info().workflow_id)
        existing = (lead.get("notes") or "").strip()
        merged = f"{existing}\n{note}".strip() if existing else note
        return await reg.update("leads", lead_id, {"notes": merged},
                                request_id=activity.info().workflow_id)
