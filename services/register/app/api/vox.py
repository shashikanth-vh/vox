"""VOX conversations — store, workflow and the atomic edit path.

    POST  /v1/vox/consents                      write-once consent certification (D5)
    POST  /v1/vox/conversations                 create (idempotent on capture_id)
    GET   /v1/vox/conversations                 firm-wide list: Queue, feed, dossier, Mine/All
    GET   /v1/vox/conversations/{id}            read one
    PATCH /v1/vox/conversations/{id}/pipeline   machine path: the vocx worker advances the row
    POST  /v1/vox/conversations/{id}/edits      the atomic edit path (one transaction)
    POST  /v1/vox/conversations/{id}/approve    ready -> submitted (recorder or authority)
    POST  /v1/vox/conversations/{id}/erase      delete a draft (recorder/Management) or
                                                erase an approved record (Admin); the
                                                consent record survives either way

Access, per the spec's D2 adapted to PRISM: every authenticated user in the tenant
reads every conversation (the Queue is a workflow view, not a privacy tier); writing
is personal; edits are recorder-or-authority and always audited; consent records and
the edit trail are immutable in the database itself.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text, update

from app.core.logging import request_id_ctx
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models.vox import (
    CONVERSATION_STATUSES,
    VoxConsentRecord,
    VoxConversation,
    VoxConversationEdit,
    VoxConversationUseCase,
)

router = api_router()

_EDIT_AUTHORITY = {"Management", "Admin"}
_USE_CASES = {"lending", "syndication", "asset_monetisation", "credit_diligence",
              "investor_relations", "banking_relations", "operations", "other"}

# The pipeline's legal moves. Anything else is a bug surfacing as a 409, never a
# silent overwrite. `submitted` is reached only through /approve.
_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"uploading", "processing", "processing_failed"},
    "uploading": {"processing", "processing_failed"},
    "processing": {"ready", "processing_failed"},
    "processing_failed": {"processing", "failed_permanently"},
    "failed_permanently": {"processing"},          # an admin-triggered manual retry
    "ready": {"processing"},                       # re-run structuring on demand
    "submitted": set(),
}


# --------------------------------------------------------------------------- helpers

def _caller_email(ctx: RequestContext) -> str:
    return (ctx.user.email if ctx.user else ctx.actor) or "service"


def _may_edit(ctx: RequestContext, row: VoxConversation) -> bool:
    """Writing is personal: the recorder edits their own record; Management/Admin may
    edit any (today that is everyone — it tightens automatically if roles narrow).
    A machine caller (no user context) is the vocx service acting under its key."""
    if ctx.user is None:
        return True
    return (ctx.user.email.lower() == row.recorder_email.lower()
            or bool(set(ctx.user.roles or []) & _EDIT_AUTHORITY))


def _row_dict(c: VoxConversation, *, full: bool) -> dict[str, Any]:
    out = {
        "id": str(c.id),
        "recorder_email": c.recorder_email,
        "recorder_name": c.recorder_name,
        "entity_id": str(c.entity_id) if c.entity_id else None,
        "lead_id": str(c.lead_id) if c.lead_id else None,
        "deal_id": str(c.deal_id) if c.deal_id else None,
        "interaction_id": str(c.interaction_id) if c.interaction_id else None,
        "recording_mode": c.recording_mode,
        "capture_id": c.capture_id,
        "status": c.status,
        "processing_stage": c.processing_stage,
        "processing_error": c.processing_error,
        "retry_count": c.retry_count,
        "duration_seconds": c.duration_seconds,
        "latitude": c.latitude,
        "longitude": c.longitude,
        "sector": c.sector,
        "subsector": c.subsector,
        "meeting_date": c.meeting_date.isoformat() if c.meeting_date else None,
        "language_detected": c.language_detected,
        "entity_candidates": c.entity_candidates,
        "proposed_lead_company": c.proposed_lead_company,
        "proposed_lead_rm": c.proposed_lead_rm,
        "prompt_version": c.prompt_version,
        "registry_version": c.registry_version,
        "audio_ref": c.audio_ref,
        "audio_deleted_at": c.audio_deleted_at.isoformat() if c.audio_deleted_at else None,
        "erased_at": c.erased_at.isoformat() if c.erased_at else None,
        "consent_id": str(c.consent_id) if c.consent_id else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    # the feed's one-line story, cheap enough for every listing row
    report = c.structured_report or {}
    kdp = (((report.get("common") or {}).get("key_discussion_points") or {}).get("value") or [])
    out["snippet"] = ". ".join(x for x in kdp[:2] if isinstance(x, str))
    if full:
        out["raw_transcript"] = c.raw_transcript
        out["transcript_segments"] = c.transcript_segments
        out["structured_report"] = c.structured_report
        out["corrected_transcript"] = c.corrected_transcript
    return out


async def _use_cases_of(ctx: RequestContext, ids: list[uuid.UUID]) -> dict[str, list[str]]:
    if not ids:
        return {}
    rows = (await ctx.session.execute(
        select(VoxConversationUseCase).where(VoxConversationUseCase.conversation_id.in_(ids))
    )).scalars().all()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r.conversation_id), []).append(r.use_case)
    return out


async def _get_row(ctx: RequestContext, conversation_id: str,
                   *, for_update: bool = False) -> VoxConversation:
    """``for_update=True`` takes the row lock, so two simultaneous mutations of
    the same conversation serialize and the second SEES the first's outcome
    (idempotent replay) instead of racing it — the concurrency finding was two
    parallel approves both materialising the same proposed lead."""
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError as exc:
        raise NotFoundError("No such conversation.") from exc
    stmt = select(VoxConversation).where(VoxConversation.id == cid,
                                         VoxConversation.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("No such conversation.")
    return row


def _denormalise(row: VoxConversation) -> None:
    """Keep the filter columns honest whenever the JSONB changes — one code path,
    used by the pipeline write and the edit path alike."""
    report = row.structured_report or {}
    common = report.get("common") or {}

    def _val(key: str) -> Any:
        cell = common.get(key)
        return cell.get("value") if isinstance(cell, dict) else None

    row.sector = _val("sector")
    row.subsector = _val("subsector")
    md = _val("meeting_date")
    if isinstance(md, str):
        try:
            row.meeting_date = date.fromisoformat(md)
        except ValueError:
            pass
    elif md is None:
        row.meeting_date = row.meeting_date


def _uuid_or_422(value: str | None, name: str) -> uuid.UUID | None:
    """Foreign ids arrive from clients and manifests; a malformed one is the
    CALLER'S error (422), never an unhandled 500."""
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationAppError(f"{name} is not a valid id.") from exc


async def _sync_lane_remarks(ctx: RequestContext, row: VoxConversation) -> None:
    """VOX → the product lines' STATUS fields. Each lane block carries a reviewed
    ``remarks`` cell; on approval (and on post-approval edits) it REPLACES the
    matching tracker's status field — lending remarks, syndication latest remarks,
    asset-monetisation notes — the way the desk uses them: one current line, with
    the old→new history on the audit trail and the full story on the interaction.

    Rules the desk relies on:
    * only for lanes the conversation DETECTED, and only when the reviewed cell is
      non-empty — an empty cell never blanks a hand-written status;
    * the target must be unambiguous: the conversation's deal pins it, else the
      company must have exactly ONE active line in that lane — never a guess
      between credit lines;
    * same-value replays write nothing (approve is idempotent).
    """
    if row.status != "submitted" or not row.entity_id:
        return
    from app.models.trackers import (AssetMonetisation, LendingTracker,
                                     SyndicationTracker)
    targets = {"lending": (LendingTracker, "remarks"),
               "syndication": (SyndicationTracker, "remarks"),
               "asset_monetisation": (AssetMonetisation, "notes")}
    report = row.structured_report or {}
    lanes = [u for u in (report.get("detected_use_cases") or []) if u in targets]
    for lane in lanes:
        cell = (report.get(lane) or {}).get("remarks")
        value = cell.get("value") if isinstance(cell, dict) else None
        text = str(value).strip() if value else ""
        if not text:
            continue
        model, field = targets[lane]
        candidates = (await ctx.session.execute(
            select(model).where(model.tenant_id == ctx.tenant_id,
                                model.entity_id == row.entity_id,
                                model.deleted_at.is_(None))
        )).scalars().all()
        if row.deal_id:
            pinned = [c for c in candidates
                      if getattr(c, "deal_id", None) == row.deal_id]
            if pinned:
                candidates = pinned
        if len(candidates) != 1:
            continue
        obj = candidates[0]
        old_text = (getattr(obj, field) or "").strip()
        if old_text == text:
            continue
        setattr(obj, field, text[:4000])
        obj.updated_by = ctx.actor
        if getattr(obj, "version", None) is not None:
            obj.version = obj.version + 1
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=ctx.actor, action="update",
            resource_type=model.__tablename__, resource_id=str(obj.id),
            request_id=request_id_ctx.get(),
            changes={"label": getattr(obj, "tracker_no", None) or "",
                     "source": "vox.approve",
                     "values": {field: {"from": old_text[:300] or None,
                                        "to": text[:300]}}}))


def _merge_lender_updates(preserved: dict, fresh: Any) -> dict:
    """Regeneration keeps both sides of this list: the reviewer's corrected rows
    stay AS CONFIRMED, and newly spoken events the fresh extraction found (a
    reply added to the corrected transcript) append after them. Coverage is by
    lender name (folded, containment either way — a corrected spelling must not
    resurrect its mangled twin) plus kind, so a lender's reply still appends
    when only its chase was confirmed. The result stays user-override so the
    NEXT regeneration merges again instead of discarding."""
    from difflib import SequenceMatcher

    def _f(n: Any) -> str:
        return " ".join(str(n or "").casefold().split())

    def _same_lender(a: str, b: str) -> bool:
        if a == b or a in b or b in a:
            return True
        # The extraction may re-emit the STT mangling of a name the reviewer
        # already corrected ("Gurdwaj" beside "Godrej Capital") — the same
        # closeness bar the filing tier uses keeps the twin out.
        ab, bb = a.split(" - ")[0].strip(), b.split(" - ")[0].strip()
        return max(SequenceMatcher(None, a, b).ratio(),
                   SequenceMatcher(None, ab, bb).ratio()) >= 0.72

    kept = [e for e in (preserved.get("value") or []) if isinstance(e, dict)]
    fresh_rows = (fresh or {}).get("value") if isinstance(fresh, dict) else None
    out = list(kept)
    for e in fresh_rows or []:
        if not isinstance(e, dict):
            continue
        name = _f(e.get("lender") or e.get("owner"))
        kind = str(e.get("kind") or "").strip().lower()
        if not name:
            continue
        covered = any(
            str(k.get("kind") or "").strip().lower() == kind
            and (kn := _f(k.get("lender") or k.get("owner")))
            and _same_lender(kn, name)
            for k in kept)
        if not covered:
            out.append(e)
    return {"value": out,
            "confidence": preserved.get("confidence") or "medium",
            "user_override": True}


async def _sync_lender_updates(ctx: RequestContext, row: VoxConversation) -> None:
    """VOX → the chase board. The reviewed ``syndication.lender_updates`` entries
    file as the SAME interactions the Log-chase / Log-reply buttons write — lender
    id + direction — so the existing roll-up stamps chased/response dates and the
    chase/reply note snapshots with no second mechanism.

    Rules, mirroring the desk's own: a chase is Outbound, a reply Inbound; the
    lender must match a mandate lender by name; the conversation's deal pins the
    mandate, else the named lender must sit on exactly ONE of the company's
    mandates — never a guess. Name matching forgives what STT does to names
    ("Gurdwaj" for "Godrej"): after exact/containment, the closest mandate
    lender files IF it is both close enough and clearly ahead of the runner-up;
    anything ambiguous still skips.

    Runs at approve, on post-approval edits, and when a re-analyzed approved
    record lands home — so a lender name corrected at review AFTER approval
    still reaches the board. Each filed interaction carries this conversation's
    id in key_intel, and that DB ledger (not a flag someone's edit could drop)
    is what prevents the same event ever filing twice."""
    if not row.entity_id:
        return
    report = row.structured_report or {}
    cell = (report.get("syndication") or {}).get("lender_updates")
    entries = cell.get("value") if isinstance(cell, dict) else None
    if not isinstance(entries, list) or not entries:
        return
    from difflib import SequenceMatcher

    from app.models import Interaction
    from app.models.trackers import SyndicationLender, SyndicationTracker
    from app.repositories.interactions import create_interaction

    conds = [SyndicationTracker.tenant_id == ctx.tenant_id,
             SyndicationTracker.entity_id == row.entity_id,
             SyndicationTracker.deleted_at.is_(None)]
    if row.deal_id:
        conds.append(SyndicationTracker.deal_id == row.deal_id)
    mandates = (await ctx.session.execute(
        select(SyndicationTracker).where(*conds))).scalars().all()
    if not mandates:
        return
    lenders = (await ctx.session.execute(
        select(SyndicationLender).where(
            SyndicationLender.tenant_id == ctx.tenant_id,
            SyndicationLender.syndication_id.in_([m.id for m in mandates]),
            SyndicationLender.deleted_at.is_(None))
    )).scalars().all()

    def _fold(name: str) -> str:
        return " ".join(str(name or "").casefold().split())

    # What this conversation has already put on the board — one query, then a
    # dict lookup per entry. Replays, edit re-runs and re-analysis resumes all
    # pass through here; the ledger makes every re-run idempotent. The mapped
    # ROW (not just the pair) lets a hit refresh its note: the review panel
    # autosaves while the reviewer types, so a fresh row often files before
    # its note exists — the words must still catch up with the event.
    already: dict[tuple[Any, str], Interaction] = {
        (i.syndication_lender_id, str(i.direction or "").lower()): i
        for i in (await ctx.session.execute(
            select(Interaction).where(
                Interaction.tenant_id == ctx.tenant_id,
                Interaction.source == "VOX",
                Interaction.deleted_at.is_(None),
                Interaction.key_intel["vox_conversation_id"].astext == str(row.id))
        )).scalars()}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Tolerate the model's borrowed action_items shape (owner/action) so a
        # report structured before the normalizer learned it still files.
        spoken = _fold(entry.get("lender") or entry.get("owner") or "")
        kind = str(entry.get("kind") or "").strip().lower()
        note = str(entry.get("note") or entry.get("action") or "").strip()
        if not kind:
            low = note.casefold()
            kind = ("reply" if any(w in low for w in (
                "raised", "reverted", "responded", "replied", "came back", "quer",
                "declined", "sanctioned", "committed", "confirmed", "agreed"))
                else "chase")
        if not spoken or kind not in ("chase", "reply"):
            continue
        hits = [ln for ln in lenders
                if spoken == _fold(ln.lender_name)
                or spoken in _fold(ln.lender_name) or _fold(ln.lender_name) in spoken]
        if not hits:
            # The forgiving tier. STT reliably mangles exactly these names —
            # "Gurdwaj Capital" for "Godrej Capital" — and an unknown-name skip
            # here is silent. Score every mandate lender (full name AND the
            # name with its branch suffix dropped, so "Axis Finance - Delhi"
            # competes as "axis finance"); the best files only when it is close
            # enough (≥0.72) AND clearly ahead of the runner-up (≥0.08) — a
            # genuinely unknown or ambiguous name still never guesses.
            def _score(ln: Any) -> float:
                whole = _fold(ln.lender_name)
                base = whole.split(" - ")[0].strip()
                return max(SequenceMatcher(None, spoken, whole).ratio(),
                           SequenceMatcher(None, spoken, base).ratio())
            scored = sorted(((_score(ln), ln) for ln in lenders),
                            key=lambda t: t[0], reverse=True)
            if scored and scored[0][0] >= 0.72 and (
                    len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
                hits = [scored[0][1]]
        if len(hits) != 1:
            continue                     # unknown or ambiguous name — never a guess
        lender = hits[0]
        direction = "Outbound" if kind == "chase" else "Inbound"
        verb = "Chased" if kind == "chase" else "Reply from"
        filed = already.get((lender.id, direction.lower()))
        if filed is not None:
            # The event is on the board; never a second row. But a note typed
            # AFTER the row first filed (the panel autosaves mid-typing) still
            # belongs to it — refresh the filed entry's words and the board's
            # note snapshot, dates untouched.
            if note and (filed.notes or "").strip() != note:
                filed.summary = note[:300]
                filed.notes = note
                filed.updated_by = ctx.actor
                await ctx.session.execute(
                    update(SyndicationLender).where(SyndicationLender.id == lender.id)
                    .values(**({"last_reply_note": note} if direction == "Inbound"
                               else {"last_chase_note": note})))
            continue
        itx = await create_interaction(ctx.session, ctx.tenant_id, ctx.actor, {
            "subject_type": "Syndication", "subject_id": str(lender.syndication_id),
            "syndication_lender_id": lender.id,
            "interaction_type": "VOX conversation",
            "direction": direction,
            "summary": (note or f"{verb} {lender.lender_name}")[:300],
            "notes": note or None,
            "performed_by": row.recorder_name or row.recorder_email,
            "source": "VOX",
            "key_intel": {"vox_conversation_id": str(row.id)},
        })
        already[(lender.id, direction.lower())] = itx


async def _sync_linked_interaction(ctx: RequestContext, row: VoxConversation) -> None:
    """Mirror an APPROVED conversation's current report onto its filed timeline
    entry. The public interaction surface is append-only by design; this service
    owns both tables and keeps them telling the same story — the conversation's
    audit rows record who changed what."""
    if row.status != "submitted" or not row.interaction_id:
        return
    from app.models import Interaction
    itx = (await ctx.session.execute(
        select(Interaction).where(Interaction.id == row.interaction_id,
                                  Interaction.tenant_id == ctx.tenant_id)
    )).scalar_one_or_none()
    if itx is None:
        return
    report = row.structured_report or {}
    common = report.get("common") or {}

    def _cv(key: str) -> Any:
        cell = common.get(key)
        return cell.get("value") if isinstance(cell, dict) else None

    kdp = [x for x in (_cv("key_discussion_points") or []) if isinstance(x, str)]
    lanes = [u for u in (report.get("detected_use_cases") or []) if isinstance(u, str)]

    def _block_val(block: str, key: str) -> str:
        cell = (report.get(block) or {}).get(key)
        val = cell.get("value") if isinstance(cell, dict) else None
        return str(val).strip() if val is not None else ""

    locs = {k: v for k, v in (
        ("lending", _block_val("lending", "project_location")),
        ("syndication", _block_val("syndication", "project_location")),
        ("asset_monetisation", _block_val("asset_monetisation", "asset_location")),
    ) if v}
    # summary is the 300-char HEADLINE; a meeting summary that outgrows it lands
    # WHOLE in notes (unlimited, shown by the row's "more" expansion) — the desk
    # read "…a proposal for SBI to take over the ICICI facility plus provide" and
    # never learned the ₹40 Cr that followed.
    full_summary = str(_cv("meeting_summary") or (kdp[0] if kdp else "")
                       or "VOX conversation")
    itx.summary = full_summary if len(full_summary) <= 300 else full_summary[:299] + "…"
    if len(full_summary) > 300 and not itx.notes:
        itx.notes = full_summary
    if kdp or lanes or locs:
        itx.key_intel = {**({"points": kdp} if kdp else {}),
                         **({"use_cases": lanes} if lanes else {}),
                         **({"locations": locs} if locs else {})}
    venue = str(_cv("location") or "").strip()
    loc_line = venue or " · ".join(locs.values())
    if loc_line:
        itx.location = loc_line[:200]
    if itx.transcript:
        itx.transcript = row.corrected_transcript or row.raw_transcript
    itx.updated_by = ctx.actor


# ----------------------------------------------------------------------- consents

class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    certification_text: str = Field(min_length=10, max_length=4000)
    conversation_id: str | None = None
    device_meta: dict | None = None


@router.post("/v1/vox/consents", status_code=201)
async def create_consent(payload: ConsentIn,
                         ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = VoxConsentRecord(
        tenant_id=ctx.tenant_id,
        conversation_id=uuid.UUID(payload.conversation_id) if payload.conversation_id else None,
        user_email=_caller_email(ctx),
        user_name=ctx.user.full_name if ctx.user else None,
        certification_text=payload.certification_text,
        device_meta=payload.device_meta,
        created_by=ctx.actor,
    )
    ctx.session.add(row)
    await ctx.session.flush()
    return {"id": str(row.id), "certified_at": row.certified_at.isoformat()
            if row.certified_at else datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ create / read

class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recording_mode: str = Field(pattern="^(post_meeting|live)$")
    capture_id: str | None = Field(default=None, max_length=120)
    audio_ref: str | None = Field(default=None, max_length=512)
    duration_seconds: int | None = Field(default=None, ge=0, le=6000)
    latitude: float | None = None
    longitude: float | None = None
    consent_id: str | None = None
    entity_id: str | None = None
    lead_id: str | None = None
    # Who actually SPOKE — honoured only on a machine call (the vocx service acting
    # for the recorder); a human caller's verified identity always wins.
    recorder_email: str | None = Field(default=None, max_length=200)
    recorder_name: str | None = Field(default=None, max_length=200)


@router.post("/v1/vox/conversations", status_code=201)
async def create_conversation(payload: ConversationIn,
                              ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    # Live mode without a consent certification cannot exist (the one hard gate).
    if payload.recording_mode == "live" and not payload.consent_id:
        raise ValidationAppError("A live recording requires its consent certification first.")

    if payload.capture_id:
        existing = (await ctx.session.execute(
            select(VoxConversation).where(
                VoxConversation.tenant_id == ctx.tenant_id,
                VoxConversation.capture_id == payload.capture_id)
        )).scalar_one_or_none()
        if existing is not None:
            # A retried upload REPLAYS, never duplicates.
            return {**_row_dict(existing, full=False), "replayed": True}

    if ctx.user is None and payload.recorder_email:
        recorder_email, recorder_name = payload.recorder_email, payload.recorder_name
    else:
        recorder_email = _caller_email(ctx)
        recorder_name = ctx.user.full_name if ctx.user else None
    row = VoxConversation(
        tenant_id=ctx.tenant_id,
        recorder_email=recorder_email,
        recorder_name=recorder_name,
        recording_mode=payload.recording_mode,
        capture_id=payload.capture_id,
        audio_ref=payload.audio_ref,
        duration_seconds=payload.duration_seconds,
        latitude=payload.latitude,
        longitude=payload.longitude,
        consent_id=_uuid_or_422(payload.consent_id, "consent_id"),
        entity_id=_uuid_or_422(payload.entity_id, "entity_id"),
        lead_id=_uuid_or_422(payload.lead_id, "lead_id"),
        status="queued",
        created_by=ctx.actor,
    )
    ctx.session.add(row)
    from sqlalchemy.exc import IntegrityError
    try:
        await ctx.session.flush()
    except IntegrityError:
        # Two simultaneous retries of the same upload raced the existence check.
        # The unique (tenant, capture_id) constraint kept the data single — the
        # loser REPLAYS the winner's row instead of surfacing a 500.
        await ctx.session.rollback()
        if payload.capture_id:
            existing = (await ctx.session.execute(
                select(VoxConversation).where(
                    VoxConversation.tenant_id == ctx.tenant_id,
                    VoxConversation.capture_id == payload.capture_id)
            )).scalar_one_or_none()
            if existing is not None:
                return {**_row_dict(existing, full=False), "replayed": True}
        raise
    return _row_dict(row, full=False)


@router.get("/v1/vox/conversations")
async def list_conversations(
        ctx: RequestContext = Depends(get_context),
        status: str | None = Query(default=None),
        recorder: str | None = Query(default=None),
        mine: bool = Query(default=False),
        entity_id: str | None = Query(default=None),
        lead_id: str | None = Query(default=None),
        capture_id: str | None = Query(default=None, max_length=120),
        use_case: str | None = Query(default=None),
        q: str | None = Query(default=None, max_length=200),
        date_from: date | None = Query(default=None),
        date_to: date | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        include_erased: bool = Query(default=False),
) -> dict[str, Any]:
    stmt = select(VoxConversation).where(VoxConversation.tenant_id == ctx.tenant_id)
    if not include_erased:
        # A deleted draft (or an Admin-erased record) leaves the feeds; the row
        # itself survives for audit and is reachable by id or with include_erased.
        stmt = stmt.where(VoxConversation.erased_at.is_(None))
    if status:
        wanted = [s for s in status.split(",") if s in CONVERSATION_STATUSES]
        if not wanted:
            raise ValidationAppError(f"status must be among {CONVERSATION_STATUSES}")
        stmt = stmt.where(VoxConversation.status.in_(wanted))
    if mine:
        stmt = stmt.where(func.lower(VoxConversation.recorder_email)
                          == _caller_email(ctx).lower())
    elif recorder:
        stmt = stmt.where(func.lower(VoxConversation.recorder_email) == recorder.lower())
    if entity_id:
        stmt = stmt.where(VoxConversation.entity_id == uuid.UUID(entity_id))
    if lead_id:
        # the dossier for a company that is still lead-only — no entity exists
        # yet, so its story is keyed by the lead itself
        stmt = stmt.where(VoxConversation.lead_id == uuid.UUID(lead_id))
    if capture_id:
        # "did this capture ever land?" — the recovery card asks before it shows,
        # so a take whose send DID complete is cleaned up instead of haunting the
        # recorder as an 'unsent' ghost (capture_id is unique per tenant).
        stmt = stmt.where(VoxConversation.capture_id == capture_id)
    if use_case:
        if use_case not in _USE_CASES:
            raise ValidationAppError(f"use_case must be among {sorted(_USE_CASES)}")
        stmt = stmt.where(VoxConversation.id.in_(
            select(VoxConversationUseCase.conversation_id)
            .where(VoxConversationUseCase.use_case == use_case)))
    if date_from:
        stmt = stmt.where(VoxConversation.meeting_date >= date_from)
    if date_to:
        stmt = stmt.where(VoxConversation.meeting_date <= date_to)
    if q:
        # Full-text over the verbatim transcript plus a plain scan of the structured
        # report. An empty result set is valid — search never invents matches.
        stmt = stmt.where(
            text("(to_tsvector('english', coalesce(raw_transcript,'')) "
                 "@@ plainto_tsquery('english', :q_fts)) "
                 "OR structured_report::text ILIKE :q_like").bindparams(
                q_fts=q, q_like=f"%{q}%"))

    total = (await ctx.session.execute(
        select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await ctx.session.execute(
        stmt.order_by(VoxConversation.created_at.desc())
        .offset(offset).limit(limit))).scalars().all()
    tags = await _use_cases_of(ctx, [r.id for r in rows])
    items = [{**_row_dict(r, full=False), "use_cases": tags.get(str(r.id), [])}
             for r in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/v1/vox/conversations/{conversation_id}")
async def get_conversation(conversation_id: str,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _get_row(ctx, conversation_id)
    tags = await _use_cases_of(ctx, [row.id])
    # The audit strip's "Edits: N" — cheap COUNT on the append-only trail.
    edits = (await ctx.session.execute(
        select(func.count()).select_from(VoxConversationEdit)
        .where(VoxConversationEdit.conversation_id == row.id))).scalar() or 0
    return {**_row_dict(row, full=True), "use_cases": tags.get(str(row.id), []),
            "edits_count": int(edits)}


# ------------------------------------------------------------- the pipeline writes

class PipelineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    processing_stage: str | None = Field(default=None, max_length=40)
    processing_error: str | None = None
    retry_increment: bool = False
    audio_ref: str | None = Field(default=None, max_length=512)
    duration_seconds: int | None = Field(default=None, ge=0, le=6000)
    raw_transcript: str | None = None
    transcript_segments: list | None = None
    structured_report: dict | None = None
    entity_candidates: list[str] | None = None
    language_detected: str | None = Field(default=None, max_length=40)
    prompt_version: str | None = Field(default=None, max_length=20)
    registry_version: str | None = Field(default=None, max_length=20)


@router.patch("/v1/vox/conversations/{conversation_id}/pipeline")
async def advance_pipeline(conversation_id: str, payload: PipelineIn,
                           ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _get_row(ctx, conversation_id, for_update=True)
    if row.erased_at is not None:
        raise ConflictError("This conversation was erased; its content cannot return.")

    if payload.status is not None:
        if payload.status not in CONVERSATION_STATUSES:
            raise ValidationAppError(f"status must be among {CONVERSATION_STATUSES}")
        if payload.status != row.status:
            if payload.status not in _TRANSITIONS.get(row.status, set()):
                raise ConflictError(
                    f"A conversation cannot move {row.status} -> {payload.status}.")
            row.status = payload.status
    if payload.retry_increment:
        row.retry_count = (row.retry_count or 0) + 1
    for field in ("processing_stage", "processing_error", "audio_ref", "duration_seconds",
                  "raw_transcript", "transcript_segments", "entity_candidates",
                  "language_detected", "prompt_version", "registry_version"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    if payload.status == "ready":
        # arriving clean: a fresh success clears the previous attempt's error
        row.processing_error = None
    if payload.structured_report is not None:
        incoming = payload.structured_report
        # A regeneration re-applies the reviewer's overridden cells on top of the
        # fresh AI report — their confirmed values outrank a re-extraction.
        # lender_updates is the one exception to wholesale replacement: it is a
        # LIST OF EVENTS, and the commonest reason to re-analyze is a corrected
        # transcript carrying a NEW event ("also got reply from Canara Bank").
        # The reviewer's corrected rows stay exactly as confirmed; freshly
        # extracted events for lenders they don't already cover append after.
        if row.preserved_overrides:
            incoming = dict(incoming)
            for path, cell in row.preserved_overrides.items():
                block_key, _, field_key = path.partition(".")
                block = dict(incoming.get(block_key) or {})
                if field_key == "lender_updates":
                    cell = _merge_lender_updates(cell, block.get(field_key))
                block[field_key] = cell
                incoming[block_key] = block
            row.preserved_overrides = None
        row.structured_report = incoming
        _denormalise(row)
        detected = incoming.get("detected_use_cases") or []
        await ctx.session.execute(delete(VoxConversationUseCase).where(
            VoxConversationUseCase.conversation_id == row.id))
        for uc in dict.fromkeys(u for u in detected if isinstance(u, str)):
            if uc in _USE_CASES:
                ctx.session.add(VoxConversationUseCase(conversation_id=row.id, use_case=uc))
    if row.status == "ready" and row.resume_status == "submitted":
        # A re-analyzed APPROVED record lands home: the approval stands, the
        # rebuilt report replaces the old one, and the filed timeline entry is
        # brought back in step — all in this same transaction.
        row.status = "submitted"
        row.resume_status = None
        await _sync_linked_interaction(ctx, row)
        await _sync_lane_remarks(ctx, row)
        await _sync_lender_updates(ctx, row)
    row.updated_by = ctx.actor
    await ctx.session.flush()
    await ctx.session.refresh(row)
    # FULL row: the pipeline worker resumes from whatever this response says already
    # exists — a compact answer here once made it re-transcribe stored audio.
    return _row_dict(row, full=True)


# ------------------------------------------------------------- the atomic edit path

class FieldEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_path: str = Field(min_length=3, max_length=200, pattern=r"^[a-z_]+\.[a-z_0-9]+$")
    new_value: dict = Field(description="The full {value, confidence[, user_override]} cell.")


class EditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[FieldEdit] = Field(default_factory=list)
    use_cases: list[str] | None = None
    entity_id: str | None = None
    lead_id: str | None = None
    deal_id: str | None = None
    interaction_id: str | None = None
    # "Create new lead" intent — recorded here, materialised by approve. '' clears.
    proposed_lead_company: str | None = Field(default=None, max_length=300)
    proposed_lead_rm: str | None = Field(default=None, max_length=120)
    # The reviewer's corrected transcript copy. The verbatim original is evidence
    # and never changes; this is what regeneration structures. '' clears.
    corrected_transcript: str | None = None


@router.post("/v1/vox/conversations/{conversation_id}/edits")
async def apply_edits(conversation_id: str, payload: EditsIn,
                      ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """ALL post-AI changes flow through here, in one transaction: the JSONB, the
    denormalised columns, the use-case join rows and one audit row per change move
    together or not at all (the spec's 12.3)."""
    row = await _get_row(ctx, conversation_id, for_update=True)
    if not _may_edit(ctx, row):
        raise ForbiddenError("Only the recorder or Management/Admin may edit this record.")
    if row.erased_at is not None:
        raise ConflictError("This conversation was erased; there is nothing to edit.")

    editor = _caller_email(ctx)
    editor_name = ctx.user.full_name if ctx.user else None
    changed = 0

    def _audit(path: str, old: Any, new: Any) -> None:
        ctx.session.add(VoxConversationEdit(
            tenant_id=ctx.tenant_id, conversation_id=row.id,
            editor_email=editor, editor_name=editor_name,
            field_path=path, old_value=old, new_value=new))

    if payload.edits:
        report = dict(row.structured_report or {})
        for e in payload.edits:
            block_key, field_key = e.field_path.split(".", 1)
            cell = e.new_value
            if "value" not in cell or "confidence" not in cell:
                raise ValidationAppError(
                    f"{e.field_path}: an edit carries the full {{value, confidence}} cell.")
            block = dict(report.get(block_key) or {})
            old_cell = block.get(field_key)
            if old_cell == cell:
                continue
            block[field_key] = cell
            report[block_key] = block
            _audit(e.field_path, old_cell, cell)
            changed += 1
        row.structured_report = report
        _denormalise(row)

    if payload.use_cases is not None:
        wanted = [u for u in dict.fromkeys(payload.use_cases) if u in _USE_CASES]
        if not wanted:
            raise ValidationAppError("A conversation carries at least one use case.")
        current = sorted((await _use_cases_of(ctx, [row.id])).get(str(row.id), []))
        if sorted(wanted) != current:
            await ctx.session.execute(delete(VoxConversationUseCase).where(
                VoxConversationUseCase.conversation_id == row.id))
            for uc in wanted:
                ctx.session.add(VoxConversationUseCase(conversation_id=row.id, use_case=uc))
            report = dict(row.structured_report or {})
            report["detected_use_cases"] = wanted
            row.structured_report = report
            _audit("use_cases", current, wanted)
            changed += 1

    for link in ("entity_id", "lead_id", "deal_id", "interaction_id"):
        raw = getattr(payload, link)
        if raw is not None:
            new_id = _uuid_or_422(raw, link)
            old = getattr(row, link)
            if old != new_id:
                setattr(row, link, new_id)
                _audit(f"links.{link}", str(old) if old else None, raw or None)
                changed += 1

    # The new-lead intent travels the same audited path as the link pins.
    for field in ("proposed_lead_company", "proposed_lead_rm"):
        raw = getattr(payload, field)
        if raw is not None:
            new_val = raw.strip() or None
            old = getattr(row, field)
            if old != new_val:
                setattr(row, field, new_val)
                _audit(f"links.{field}", old, new_val)
                changed += 1

    if payload.corrected_transcript is not None:
        # Draft stage: the correction is what regeneration structures. Approved
        # stage: regeneration stays closed (the report changes through field
        # edits), but the READING COPY is still the desk's to fix — a mis-heard
        # name in an approved record was otherwise wrong forever. Both are
        # audited; the verbatim original never changes.
        new_txt = payload.corrected_transcript.strip() or None
        if new_txt != row.corrected_transcript:
            # The audit keeps both copies in full — the correction is as
            # accountable as the evidence it annotates.
            _audit("transcript.corrected", row.corrected_transcript, new_txt)
            row.corrected_transcript = new_txt
            changed += 1

    # A post-approval content edit must not leave the FILED timeline row telling
    # yesterday's story. The public interaction surface is append-only by design,
    # but this service owns both tables: when an approved conversation's report
    # or lanes change, its linked interaction's summary/key_intel are re-derived
    # here, inside the same transaction — the conversation's audit rows above
    # already record who changed what.
    if changed and (payload.edits or payload.use_cases is not None
                    or payload.corrected_transcript is not None):
        await _sync_linked_interaction(ctx, row)
        await _sync_lane_remarks(ctx, row)
        await _sync_lender_updates(ctx, row)

    row.updated_by = ctx.actor
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return {**_row_dict(row, full=True), "changed": changed}


@router.post("/v1/vox/conversations/{conversation_id}/regenerate")
async def regenerate_conversation(conversation_id: str,
                                  ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """Rebuild the structured report from the (corrected) transcript.

    A name misheard once propagates into every field and bullet; after the
    reviewer corrects the transcript, this sends the conversation back through
    the STRUCTURING stage only — the audio is never re-transcribed, the
    verbatim transcript never changes. Cells the reviewer explicitly
    overrode (user_override) are stashed on the row and re-applied when the
    fresh report lands, so their work survives the rebuild."""
    row = await _get_row(ctx, conversation_id, for_update=True)
    if not _may_edit(ctx, row):
        raise ForbiddenError("Only the recorder or Management/Admin may regenerate this record.")
    if row.erased_at is not None:
        raise ConflictError("This conversation was erased; there is nothing to regenerate.")
    if row.status not in ("ready", "submitted"):
        raise ConflictError(
            f"Only a ready or approved conversation can be regenerated "
            f"(this one is {row.status}).")
    # An APPROVED record passes through the pipeline and RETURNS approved: the
    # pipeline write reads resume_status when the fresh report lands and puts
    # the row back, re-syncing the filed timeline entry.
    if row.status == "submitted":
        row.resume_status = "submitted"

    overrides: dict[str, Any] = {}
    report = row.structured_report or {}
    for block_key, block in report.items():
        if not isinstance(block, dict):
            continue
        for field_key, cell in block.items():
            if isinstance(cell, dict) and cell.get("user_override"):
                overrides[f"{block_key}.{field_key}"] = cell
    row.preserved_overrides = overrides or None
    row.structured_report = None
    row.status = "processing"
    row.processing_stage = "structuring"
    row.updated_by = ctx.actor
    ctx.session.add(AuditLog(tenant_id=ctx.tenant_id, actor=ctx.actor, action="vox.regenerate",
                             resource_type="vox_conversations", resource_id=str(row.id),
                             request_id=request_id_ctx.get(),
                             changes={"corrected": bool(row.corrected_transcript),
                                      "overrides_preserved": len(overrides)}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _row_dict(row, full=True)


# ------------------------------------------------------------------ approve / erase

@router.post("/v1/vox/conversations/{conversation_id}/approve")
async def approve_conversation(conversation_id: str,
                               ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    row = await _get_row(ctx, conversation_id, for_update=True)
    if not _may_edit(ctx, row):
        raise ForbiddenError("Only the recorder or Management/Admin may approve this record.")
    if row.status == "submitted":
        return {**_row_dict(row, full=False), "replayed": True}
    if row.status != "ready":
        raise ConflictError(f"Only a ready conversation can be approved (this one is {row.status}).")

    # A "create new lead" chosen at review time materialises HERE — the register
    # gains a lead exactly when the firm signs off on the conversation, never on
    # the tap that proposed it (a discarded take must leave no stray lead behind).
    created_lead_id: str | None = None
    if row.proposed_lead_company and not (row.lead_id or row.deal_id):
        from app.models.deals import Lead
        from app.repositories.crud import CRUDRepository
        lead = await CRUDRepository(Lead).create(ctx.session, ctx.tenant_id, ctx.actor, {
            "company": row.proposed_lead_company,
            "rm": row.proposed_lead_rm,
            "sector": row.sector,
            # A repeat opportunity for a company already in Atlas keeps its entity.
            "entity_id": row.entity_id,
            "source_name": "VOX conversation",
        })
        await ctx.session.flush()
        row.lead_id = lead.id
        created_lead_id = str(lead.id)
        ctx.session.add(VoxConversationEdit(
            tenant_id=ctx.tenant_id, conversation_id=row.id,
            editor_email=_caller_email(ctx),
            editor_name=ctx.user.full_name if ctx.user else None,
            field_path="links.lead_id", old_value=None, new_value=created_lead_id))
    if row.proposed_lead_company or row.proposed_lead_rm:
        row.proposed_lead_company = None
        row.proposed_lead_rm = None

    row.status = "submitted"
    row.updated_by = ctx.actor
    # The reviewed lane remarks become the product lines' current status (lending
    # remarks / syndication latest remarks / asset-mon notes) — audited, replace.
    await _sync_lane_remarks(ctx, row)
    # And the reviewed lender chases/replies land on the chase board — approve only.
    await _sync_lender_updates(ctx, row)
    ctx.session.add(AuditLog(tenant_id=ctx.tenant_id, actor=ctx.actor, action="vox.approve",
                             resource_type="vox_conversations", resource_id=str(row.id),
                             request_id=request_id_ctx.get(),
                             changes={"recorder": row.recorder_email,
                                      **({"created_lead_id": created_lead_id}
                                         if created_lead_id else {})}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _row_dict(row, full=False)


@router.post("/v1/vox/conversations/{conversation_id}/erase")
async def erase_conversation(conversation_id: str,
                             ctx: RequestContext = Depends(get_context)) -> dict[str, Any]:
    """Authorised erasure (16.3): hard-delete audio reference, transcript and
    structured content — while the consent record and an erasure log survive.

    Two doors, by lifecycle: BEFORE approval the recording is still the
    recorder's draft, so the recorder (or Management/Admin) may delete it.
    AFTER approval it is a firm record, and only Admin erasure — the
    authorised-request path — can remove its content."""
    row = await _get_row(ctx, conversation_id, for_update=True)
    if ctx.user is not None:
        roles = ctx.user.roles or set()
        if "Admin" not in roles:
            if row.status == "submitted":
                raise ForbiddenError(
                    "An approved conversation is a firm record — erasure is an Admin "
                    "action, executed on an authorised request.")
            if not (_may_edit(ctx, row) or "Management" in roles):
                raise ForbiddenError(
                    "Only the recorder or Management/Admin may delete this draft.")
    if row.erased_at is not None:
        return {**_row_dict(row, full=False), "replayed": True}
    now = datetime.now(timezone.utc)
    row.raw_transcript = None
    row.transcript_segments = None
    row.structured_report = None
    row.entity_candidates = None
    row.sector = None
    row.subsector = None
    if row.audio_ref:
        row.audio_deleted_at = now   # the object itself is removed by the storage sweep
    row.erased_at = now
    row.updated_by = ctx.actor
    # The edit trail holds field VALUES, so erasure must reach it too — through the
    # one sanctioned door: the append-only trigger yields only under this GUC, set
    # transaction-local right here and nowhere else.
    await ctx.session.execute(text("SELECT set_config('app.vox_erasure', 'on', true)"))
    await ctx.session.execute(delete(VoxConversationEdit).where(
        VoxConversationEdit.conversation_id == row.id))
    ctx.session.add(AuditLog(tenant_id=ctx.tenant_id, actor=ctx.actor, action="vox.erase",
                             resource_type="vox_conversations", resource_id=str(row.id),
                             request_id=request_id_ctx.get(),
                             changes={"recorder": row.recorder_email,
                                      "had_audio": bool(row.audio_ref)}))
    await ctx.session.flush()
    await ctx.session.refresh(row)
    return _row_dict(row, full=False)
