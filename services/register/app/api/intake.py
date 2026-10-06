"""Intake — the enquiries that arrive through a door other than the desk.

``POST /v1/intake/enquiries`` takes the JSON the website's approve handler (or the
WhatsApp bot) sends once an RM has approved an enquiry, and decides what the
register makes of it. One rule, applied in order, so the same company never
ends up with two stories:

1. the company is already a client with a LIVE deal → an interaction on that
   deal, the RM told; no new lead (a second lead would split the story);
2. the company already has an ACTIVE lead → an interaction on that lead and a
   fresh next action; no duplicate;
3. the company is known but idle → a new lead linked to the existing client;
4. the company is new → a new lead, and the client master gains its Prospect
   row exactly as a desk-typed lead does;
5. the contact's mobile or e-mail is already on another company's live lead →
   the lead is created but flagged, so the desk decides;
6. the approver is not on the RM roster → the BD Head owns it, flagged.

A rejected enquiry is stored with outcome 'rejected' and creates nothing, so the
funnel is complete. Every delivery is recorded in ``lead_enquiries``; a repeat of
the same enquiry number answers with the stored outcome and writes nothing.

Authentication is the sender's, not a user's: each delivery carries
``X-Intake-Timestamp`` (unix seconds) and ``X-Intake-Signature`` =
``sha256=<hex HMAC-SHA256(secret, timestamp + "." + body)>`` with the shared
secret in REGISTER_INTAKE_WEBHOOK_SECRET. A stale timestamp or a bad signature
is refused before the body is even parsed; an unset secret switches intake off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from evam_backend_core.company_identity import canonical_name
from fastapi import Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.core.clock import tenant_today
from app.core.config import get_settings
from app.core.errors import ForbiddenError, ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models import Entity, Lead, LeadEnquiry, Person
from app.repositories.crud import CRUDRepository
from app.repositories.interactions import create_interaction

router = api_router()

# --------------------------------------------------------------------------- #
# The contract (what the website / bot send). Unknown keys are kept in the
# stored payload and ignored here, so a form field added later never breaks
# a delivery.
# --------------------------------------------------------------------------- #


class ContactIn(BaseModel, extra="allow"):
    name: str = Field(max_length=200)
    mobile: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    consent: bool | None = None


class CompanyIn(BaseModel, extra="allow"):
    name: str = Field(max_length=300)
    cin: str | None = Field(default=None, max_length=40)
    address: str | None = None
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=60)


class EnquiryIn(BaseModel, extra="allow"):
    enquiry_no: str = Field(min_length=1, max_length=40)
    channel: str = Field(default="website", max_length=20)
    status: str = Field(max_length=20)                 # approved | rejected
    intent: str | None = Field(default=None, max_length=20)   # capital | assets
    submitted_at: datetime | None = None
    approved_by: str | None = Field(default=None, max_length=200)
    approved_at: datetime | None = None
    contact: ContactIn
    company: CompanyIn | None = None
    capital: dict[str, Any] | None = None
    assets: dict[str, Any] | None = None
    business: dict[str, Any] | None = None
    message: str | None = None


# Website vocabulary → the register's Sector list (seed/refdata.py). Keys are
# matched after lowercasing and dropping everything but letters, so
# "electric-mobility", "Electric Mobility" and "ev_mobility" all land.
_SECTORS = {
    "electricmobility": "EV Mobility", "evmobility": "EV Mobility", "ev": "EV Mobility",
    "evfleets": "EV Mobility", "charging": "EV Mobility",
    "solar": "Solar - General", "solargeneral": "Solar - General",
    "solarepc": "Solar - EPC", "epc": "Solar - EPC",
    "solardeveloper": "Solar - Developer", "developer": "Solar - Developer",
    "solarrooftop": "Solar - Rooftop", "rooftop": "Solar - Rooftop",
    "solaroem": "Solar - OEM", "oem": "Solar - OEM",
    "bess": "BESS / Energy Storage", "energystorage": "BESS / Energy Storage",
    "storage": "BESS / Energy Storage", "battery": "BESS / Energy Storage",
    "biofuels": "Biofuels / Biogas / CBG", "biogas": "Biofuels / Biogas / CBG",
    "cbg": "Biofuels / Biogas / CBG", "bioenergy": "Biofuels / Biogas / CBG",
    "industrialdecarbonisation": "Industrial Decarbonisation",
    "decarbonisation": "Industrial Decarbonisation", "decarbonization": "Industrial Decarbonisation",
    "industrialwater": "Industrial Water", "water": "Industrial Water",
    "watertreatment": "Water Treatment / WASH", "wash": "Water Treatment / WASH",
    "climatedata": "Climate Data & IoT", "iot": "Climate Data & IoT",
    "industrialefficiency": "Industrial Efficiency", "efficiency": "Industrial Efficiency",
    "agri": "Agri / Drone", "agriculture": "Agri / Drone", "drone": "Agri / Drone",
}
_ADAPTATION = {"Industrial Water", "Water Treatment / WASH", "Climate Data & IoT", "Agri / Drone"}


def _sector_of(raw: str | None, hints: list[str]) -> str:
    for cand in [raw, *hints]:
        key = re.sub(r"[^a-z]", "", str(cand or "").lower())
        if key in _SECTORS:
            return _SECTORS[key]
    for cand in hints:
        low = str(cand or "").lower()
        if "solar" in low:
            return "Solar - General"
        if "wind" in low or "renewable" in low:
            return "Other"
    return "Other"


def _mobile(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return "+" + digits if not str(raw).strip().startswith("+") else str(raw).strip()


def _place(company: CompanyIn | None, assets: dict | None) -> tuple[str | None, str | None]:
    """(state, city) from the company block, else parsed from the address
    ("…, Hyderabad, Telangana 500081"), else from assets.location ("Tamil Nadu /
    Coimbatore")."""
    state = (company.state if company else None) or None
    city = (company.city if company else None) or None
    if company and company.address and not (state and city):
        parts = [p.strip() for p in company.address.split(",") if p.strip()]
        if parts:
            last = re.sub(r"\s*\d{6}\s*$", "", parts[-1]).strip()
            state = state or (last or None)
            if len(parts) >= 2:
                city = city or parts[-2]
    loc = str((assets or {}).get("location") or "").strip()
    if loc and not (state and city):
        bits = [b.strip() for b in re.split(r"[/,]", loc) if b.strip()]
        if bits:
            state = state or bits[0]
            if len(bits) >= 2:
                city = city or bits[1]
    return (state[:60] if state else None), (city[:120] if city else None)


def _lines(e: EnquiryIn) -> list[str]:
    """The enquiry as the desk should read it in the lead's notes."""
    out = [f"{e.channel.capitalize()} enquiry {e.enquiry_no}"
           + (f" · submitted {e.submitted_at.date().isoformat()}" if e.submitted_at else "")
           + (f" · approved by {e.approved_by}" if e.approved_by else "")]
    c = e.contact
    out.append("Contact: " + ", ".join(x for x in (c.name, _mobile(c.mobile), c.email) if x))
    if e.intent == "capital" and e.capital:
        cap = e.capital
        bits = [f"{k.replace('_', ' ')}: {', '.join(v) if isinstance(v, list) else v}"
                for k, v in cap.items() if v not in (None, "", [])]
        out.append("Capital ask — " + "; ".join(bits))
    if e.intent == "assets" and e.assets:
        a = e.assets
        role = a.get("role") or ""
        bits = [f"role: {role}"]
        if a.get("on_behalf_of_client"):
            bits.append("on behalf of a client")
        for k in ("offerings", "status", "status_other", "location", "buyer_size",
                  "buyer_value_basis", "buyer_location", "buyer_criteria", "criteria"):
            v = a.get(k)
            if v not in (None, "", []):
                bits.append(f"{k.replace('_', ' ')}: {', '.join(v) if isinstance(v, list) else v}")
        out.append("Asset monetisation — " + "; ".join(bits))
    if e.business:
        bits = [f"{k.replace('_', ' ')}: {v}" for k, v in e.business.items() if v not in (None, "", [])]
        if bits:
            out.append("Business — " + "; ".join(bits))
    if e.message:
        out.append(f"Message: {e.message}")
    return out


def _need(e: EnquiryIn) -> str:
    """One line for a summary: what they asked for."""
    if e.business and e.business.get("need"):
        return str(e.business["need"])
    if e.message:
        return str(e.message)
    if e.intent == "capital" and e.capital:
        req = e.capital.get("requirement") or e.capital.get("sector") or "capital"
        size = e.capital.get("ticket_size")
        return f"{req}" + (f", ticket {size}" if size else "")
    if e.intent == "assets" and e.assets:
        return f"asset monetisation ({e.assets.get('role') or 'enquiry'})"
    return "enquiry"


# --------------------------------------------------------------------------- #
# The delivery
# --------------------------------------------------------------------------- #


def _verify(secret: str, ts: str | None, sig: str | None, body: bytes, skew: int) -> None:
    if not secret:
        from app.core.errors import AppError

        class _Off(AppError):
            status_code = 503
        raise _Off("Intake is not configured on this register (REGISTER_INTAKE_WEBHOOK_SECRET).")
    if not ts or not sig:
        raise ForbiddenError("Intake delivery is not signed (X-Intake-Timestamp, X-Intake-Signature).")
    try:
        age = abs(time.time() - float(ts))
    except ValueError:
        raise ForbiddenError("X-Intake-Timestamp must be unix seconds.") from None
    if age > skew:
        raise ForbiddenError("Intake delivery is too old or from the future; re-send.")
    want = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body,
                    hashlib.sha256).hexdigest()
    got = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
    if not hmac.compare_digest(want, got.strip().lower()):
        raise ForbiddenError("Intake signature does not match.")


async def _roster_person(session: Any, tenant_id: Any, email_or_name: str | None) -> Person | None:
    from app.core.people import find_people

    if not email_or_name:
        return None
    people = await find_people(session, tenant_id, email_or_name)
    return people[0] if len(people) == 1 else None


async def _bd_head(session: Any, tenant_id: Any) -> Person | None:
    rows = (await session.execute(
        select(Person).where(Person.tenant_id == tenant_id, Person.deleted_at.is_(None),
                             Person.inactive.is_(False), Person.role.ilike("%BD Head%"))
        .order_by(Person.full_name))).scalars().all()
    return rows[0] if rows else None


async def _match_entity(session: Any, tenant_id: Any, name: str, cin: str | None) -> Entity | None:
    rows = (await session.execute(
        select(Entity).where(Entity.tenant_id == tenant_id, Entity.deleted_at.is_(None)))).scalars().all()
    if cin:
        c = cin.strip().upper()
        for e in rows:
            if (e.cin or "").strip().upper() == c:
                return e
    wanted = canonical_name(name)
    hits = [e for e in rows if wanted and any(
        x and canonical_name(x) == wanted for x in (e.legal_name, e.display_name))]
    return hits[0] if len(hits) == 1 else None


async def _active_lead(session: Any, tenant_id: Any, name: str, entity_id: Any) -> Lead | None:
    wanted = canonical_name(name)
    conds = [Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None),
             Lead.status == "Active", Lead.converted_deal_id.is_(None)]
    rows = (await session.execute(select(Lead).where(*conds).order_by(Lead.created_at.desc()))).scalars().all()
    for l in rows:
        if (entity_id is not None and l.entity_id == entity_id) or (wanted and canonical_name(l.company or "") == wanted):
            return l
    return None


async def _same_contact(session: Any, tenant_id: Any, mobile: str | None, email: str | None,
                        company: str) -> Lead | None:
    """A live lead for ANOTHER company carrying this phone (last ten digits) or e-mail."""
    digits = re.sub(r"\D", "", mobile or "")[-10:]
    em = (email or "").strip().lower()
    if not digits and not em:
        return None
    wanted = canonical_name(company)
    rows = (await session.execute(select(Lead).where(
        Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None), Lead.status == "Active",
        or_(Lead.phone.isnot(None), Lead.notes.isnot(None))))).scalars().all()
    for l in rows:
        if canonical_name(l.company or "") == wanted:
            continue
        if digits and re.sub(r"\D", "", l.phone or "")[-10:] == digits:
            return l
        if em and em in (l.notes or "").lower():
            return l
    return None


def _result(row: LeadEnquiry, *, replayed: bool, lead_no: str | None = None) -> dict[str, Any]:
    return {"enquiry_no": row.enquiry_no, "status": row.status, "outcome": row.outcome,
            "lead_id": str(row.lead_id) if row.lead_id else None, "lead_no": lead_no,
            "deal_id": str(row.deal_id) if row.deal_id else None,
            "interaction_id": str(row.interaction_id) if row.interaction_id else None,
            "rm": row.rm, "note": row.note, "replayed": replayed}


@router.post("/v1/intake/enquiries", status_code=201, tags=["Intake"],
             summary="An approved (or rejected) enquiry from the website / WhatsApp door")
async def receive_enquiry(
    request: Request,
    ctx: RequestContext = Depends(get_context),
    x_intake_timestamp: str | None = Header(default=None, alias="X-Intake-Timestamp"),
    x_intake_signature: str | None = Header(default=None, alias="X-Intake-Signature"),
) -> dict[str, Any]:
    settings = get_settings()
    body = await request.body()
    _verify(settings.intake_webhook_secret, x_intake_timestamp, x_intake_signature, body,
            int(settings.intake_max_skew_s))
    try:
        e = EnquiryIn.model_validate(json.loads(body.decode("utf-8") or "{}"))
    except ValueError as exc:
        raise ValidationAppError(f"Enquiry body is not valid: {exc}") from None
    channel = (e.channel or "website").strip().lower()[:20] or "website"
    actor = f"intake:{channel}"

    # Idempotent on the enquiry number: a redelivery answers with what happened.
    existing = (await ctx.session.execute(select(LeadEnquiry).where(
        LeadEnquiry.tenant_id == ctx.tenant_id, LeadEnquiry.enquiry_no == e.enquiry_no))).scalar_one_or_none()
    if existing is not None:
        lead_no = None
        if existing.lead_id:
            lead_no = (await ctx.session.execute(select(Lead.lead_no).where(Lead.id == existing.lead_id))).scalar()
        return _result(existing, replayed=True, lead_no=lead_no)

    status = (e.status or "").strip().lower()
    if status not in ("approved", "rejected"):
        raise ValidationAppError("status must be 'approved' or 'rejected'.")
    row = LeadEnquiry(
        tenant_id=ctx.tenant_id, enquiry_no=e.enquiry_no, channel=channel, status=status,
        intent=(e.intent or None), company_name=(e.company.name if e.company else None),
        contact_name=e.contact.name, approved_by=e.approved_by, approved_at=e.approved_at,
        submitted_at=e.submitted_at, payload=json.loads(body.decode("utf-8")), outcome="rejected")
    notes_lines = _lines(e)
    lead_no: str | None = None
    recipient: str | None = None
    title = body_text = ""

    if status == "approved":
        if not e.company or not e.company.name.strip():
            raise ValidationAppError("An approved enquiry needs company.name.")
        company = e.company.name.strip()
        # Who owns it: the approver when they are on the roster, else the BD Head.
        person = await _roster_person(ctx.session, ctx.tenant_id, e.approved_by)
        flags: list[str] = []
        if person is None:
            person = await _bd_head(ctx.session, ctx.tenant_id)
            flags.append(f"approver {e.approved_by or '(unknown)'} is not on the RM roster"
                         + (f"; assigned to {person.full_name}" if person else "; no RM assigned"))
        rm = person.name if person else None
        recipient = (person.email if person else None) or (e.approved_by or None)
        entity = await _match_entity(ctx.session, ctx.tenant_id, company, e.company.cin)
        need = _need(e)
        when = e.approved_at or e.submitted_at
        occurred = when if when else None

        from app.api.lead_lookup import live_deal_for
        live = await live_deal_for(ctx.session, ctx.tenant_id, entity.id) if entity is not None else None
        if live is not None:
            # 1. Already being worked: the enquiry joins the deal's conversation.
            inter = await create_interaction(ctx.session, ctx.tenant_id, actor, {
                "subject_type": "Deal", "subject_id": str(live["id"]),
                "interaction_type": "Email / Written Correspondence", "direction": "inbound",
                "summary": f"{channel.capitalize()} enquiry {e.enquiry_no}: {need}"[:300],
                "notes": "\n".join(notes_lines), "contact_name": e.contact.name,
                "performed_by": rm, **({"occurred_at": occurred} if occurred else {})})
            row.outcome, row.deal_id, row.interaction_id, row.rm = "interaction_on_deal", live["id"], inter.id, rm
            row.note = (f"{company} already has live deal {live['deal_no'] or ''} (RM {live['rm'] or '—'}); "
                        "logged on the deal, no new lead.")
            recipient = recipient or None
            title = f"Existing client {company} sent a {channel} enquiry"
            body_text = f"{need}. Logged on deal {live['deal_no'] or ''}; no new lead was created."
        else:
            active = await _active_lead(ctx.session, ctx.tenant_id, company, entity.id if entity else None)
            if active is not None:
                # 2. An open lead already: one more touch on it, not a twin.
                inter = await create_interaction(ctx.session, ctx.tenant_id, actor, {
                    "subject_type": "Lead", "subject_id": str(active.id),
                    "interaction_type": "Email / Written Correspondence", "direction": "inbound",
                    "summary": f"{channel.capitalize()} enquiry {e.enquiry_no}: {need}"[:300],
                    "notes": "\n".join(notes_lines), "contact_name": e.contact.name,
                    "performed_by": rm, "next_action": f"Call back on {channel} enquiry {e.enquiry_no}",
                    "next_action_date": tenant_today() + timedelta(days=1),
                    **({"occurred_at": occurred} if occurred else {})})
                row.outcome, row.lead_id, row.interaction_id, row.rm = "interaction_on_lead", active.id, inter.id, active.rm or rm
                row.note = f"Active lead {active.lead_no} already exists for {company}; logged on it, no duplicate."
                lead_no = active.lead_no
                recipient = recipient or None
                title = f"Lead {active.lead_no} ({company}) got a new {channel} enquiry"
                body_text = f"{need}. Logged on the existing lead; next action set for tomorrow."
            else:
                # 3 / 4. A new lead — linked to the known client, or born with its master row.
                twin = await _same_contact(ctx.session, ctx.tenant_id, e.contact.mobile, e.contact.email, company)
                if twin is not None:
                    flags.append(f"same contact as lead {twin.lead_no} ({twin.company}) — possible duplicate")
                hints = []
                if e.capital:
                    hints += [str(x) for x in (e.capital.get("sub_sectors") or [])] + [str(e.capital.get("requirement") or "")]
                if e.assets:
                    hints += [str(x) for x in (e.assets.get("offerings") or [])] + [str(x) for x in (e.assets.get("buyer_criteria") or e.assets.get("criteria") or [])]
                if e.business and e.business.get("need"):
                    hints.append(str(e.business["need"]))
                sector = _sector_of((e.capital or {}).get("sector"), hints)
                state, city = _place(e.company, e.assets)
                notes = ("\n".join(f"FLAG: {f}" for f in flags) + ("\n" if flags else "")) + "\n".join(notes_lines)
                data: dict[str, Any] = {
                    "company": company, "cin": (e.company.cin or None), "address": (e.company.address or None),
                    "city": city, "state": state, "country": "India",
                    "contact": e.contact.name, "phone": _mobile(e.contact.mobile),
                    "source": "Inbound", "source_name": f"{channel.capitalize()} {e.enquiry_no}",
                    "rm": rm, "status": "Active", "temperature": "Warm",
                    "sector": sector, "lens": "Adaptation" if sector in _ADAPTATION else "Mitigation",
                    "notes": notes, "next_action": f"Call back on {channel} enquiry {e.enquiry_no}: {need}"[:300],
                    "next_action_date": tenant_today() + timedelta(days=1),
                    "last_interaction_date": (when.date() if when else tenant_today()),
                }
                if entity is not None:
                    data["entity_id"] = entity.id
                else:
                    from app.api.lead_rules import lead_company_to_master
                    await lead_company_to_master(ctx, data)
                lead = await CRUDRepository(Lead).create(ctx.session, ctx.tenant_id, actor, data)
                await ctx.session.flush()
                from app.api.lead_rules import link_prospects_to_company
                await link_prospects_to_company(ctx.session, ctx.tenant_id, company, lead.entity_id, lead.id)
                row.outcome, row.lead_id, row.rm = "lead_created", lead.id, rm
                row.note = ("New lead" + (" linked to existing client" if entity is not None else " and client master row")
                            + (("; " + "; ".join(flags)) if flags else "") + ".")
                lead_no = lead.lead_no
                title = f"New {channel} enquiry: {company}"
                body_text = f"{need}. Lead {lead_no} assigned to {person.full_name if person else 'nobody yet'}."
    else:
        row.note = "Rejected at approval; nothing created."

    ctx.session.add(row)
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="intake.enquiry",
        resource_type="lead_enquiries", resource_id=str(row.id),
        changes={"enquiry_no": e.enquiry_no, "channel": channel, "status": status,
                 "outcome": row.outcome, "company": row.company_name,
                 "lead_id": str(row.lead_id) if row.lead_id else None,
                 "deal_id": str(row.deal_id) if row.deal_id else None, "label": e.enquiry_no}))
    if status == "approved" and recipient:
        from app.api.notify import notify_maker
        await notify_maker(ctx, recipient=recipient, event="intake.enquiry", title=title,
                           body=body_text, severity="info",
                           subject_type="Lead" if row.lead_id else ("Deal" if row.deal_id else None),
                           subject_id=str(row.lead_id or row.deal_id) if (row.lead_id or row.deal_id) else None,
                           dedupe_key=f"intake:{e.enquiry_no}")
    return _result(row, replayed=False, lead_no=lead_no)
