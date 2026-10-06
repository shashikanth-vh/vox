"""Intake — the enquiries that arrive through a door other than the desk.

``POST /v1/intake/enquiries`` takes the JSON the website form (or the WhatsApp
bot) sends and decides what the register makes of it. Two ways in:

* **PRISM-hosted approval** (``status: submitted``): the website posts the
  enquiry the moment it is submitted. The register parks it, mints one Approve
  and one Reject link per approver the website names, and answers with those
  links; the website puts them in the e-mail it already sends the RM. The RM's
  click lands on a PRISM page (``GET …/{token}/approve``) that shows the enquiry
  and ONE button; pressing it (``POST``) runs the rule below. Nothing happens on
  merely opening the link, so a mail scanner that previews it approves nothing.
* **Website-decided** (``status: approved`` | ``rejected``): the website ran the
  approval itself and posts the result; the rule runs at once. This is what the
  WhatsApp bot uses too.

One rule, applied in order, so the same company never ends up with two stories:

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
the same enquiry number answers with the stored outcome and writes nothing (a
repeat of a still-parked one re-issues its links).

Authentication of the delivery is the sender's, not a user's: it carries
``X-Intake-Timestamp`` (unix seconds) and ``X-Intake-Signature`` =
``sha256=<hex HMAC-SHA256(secret, timestamp + "." + body)>`` with the shared
secret in REGISTER_INTAKE_WEBHOOK_SECRET. A stale timestamp or a bad signature
is refused before the body is even parsed; an unset secret switches intake off.
The links are their own credential: a long random token, stored only as its
SHA-256, bound to one enquiry, one action and the address it was issued to,
expiring, and spent by the first decision.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from evam_backend_core.company_identity import canonical_name
from fastapi import Depends, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.core.clock import tenant_today
from app.core.config import get_settings
from app.core.errors import ForbiddenError, ValidationAppError
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog
from app.models import Entity, Lead, LeadEnquiry, LeadEnquiryToken, Person
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
    status: str = Field(max_length=20)                 # submitted | approved | rejected
    intent: str | None = Field(default=None, max_length=20)   # capital | assets
    submitted_at: datetime | None = None
    approved_by: str | None = Field(default=None, max_length=200)
    approved_at: datetime | None = None
    # PRISM-hosted approval: the addresses the website will e-mail. One Approve /
    # Reject pair is minted per address, so the click says who approved.
    approvers: list[str] | None = None
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


def _result(row: LeadEnquiry, *, replayed: bool, lead_no: str | None = None,
            links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {"enquiry_no": row.enquiry_no, "status": row.status, "outcome": row.outcome,
           "lead_id": str(row.lead_id) if row.lead_id else None, "lead_no": lead_no,
           "deal_id": str(row.deal_id) if row.deal_id else None,
           "interaction_id": str(row.interaction_id) if row.interaction_id else None,
           "rm": row.rm, "note": row.note, "replayed": replayed}
    if links is not None:
        out["links"] = links
        if links:
            out["approve_url"], out["reject_url"] = links[0]["approve_url"], links[0]["reject_url"]
    return out


async def _lead_no_of(ctx: RequestContext, row: LeadEnquiry) -> str | None:
    if not row.lead_id:
        return None
    return (await ctx.session.execute(select(Lead.lead_no).where(Lead.id == row.lead_id))).scalar()


class _Decision:
    """What the rule did with an approved enquiry, for the answer, the audit row
    and the RM's notification."""

    lead_no: str | None = None
    recipient: str | None = None
    title: str = ""
    body: str = ""


async def _approve(ctx: RequestContext, e: EnquiryIn, row: LeadEnquiry, *, channel: str,
                   actor: str, approver: str | None, when: datetime | None) -> _Decision:
    """Run the rule for an approved enquiry: fill ``row`` with the outcome, write
    the lead / interaction, and say what to tell whom."""
    d = _Decision()
    if not e.company or not e.company.name.strip():
        raise ValidationAppError("An approved enquiry needs company.name.")
    company = e.company.name.strip()
    notes_lines = _lines(e)
    # Who owns it: the approver when they are on the roster, else the BD Head.
    person = await _roster_person(ctx.session, ctx.tenant_id, approver)
    flags: list[str] = []
    if person is None:
        person = await _bd_head(ctx.session, ctx.tenant_id)
        flags.append(f"approver {approver or '(unknown)'} is not on the RM roster"
                     + (f"; assigned to {person.full_name}" if person else "; no RM assigned"))
    rm = person.name if person else None
    d.recipient = (person.email if person else None) or (approver or None)
    entity = await _match_entity(ctx.session, ctx.tenant_id, company, e.company.cin)
    need = _need(e)
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
        d.title = f"Existing client {company} sent a {channel} enquiry"
        d.body = f"{need}. Logged on deal {live['deal_no'] or ''}; no new lead was created."
        return d

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
        d.lead_no = active.lead_no
        d.title = f"Lead {active.lead_no} ({company}) got a new {channel} enquiry"
        d.body = f"{need}. Logged on the existing lead; next action set for tomorrow."
        return d

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
    d.lead_no = lead.lead_no
    d.title = f"New {channel} enquiry: {company}"
    d.body = f"{need}. Lead {d.lead_no} assigned to {person.full_name if person else 'nobody yet'}."
    return d


async def _record(ctx: RequestContext, row: LeadEnquiry, *, actor: str, channel: str,
                  d: _Decision | None) -> None:
    """The trail: the audit row for this decision, and the RM's notification."""
    ctx.session.add(row)
    await ctx.session.flush()
    ctx.session.add(AuditLog(
        tenant_id=ctx.tenant_id, actor=actor, action="intake.enquiry",
        resource_type="lead_enquiries", resource_id=str(row.id),
        changes={"enquiry_no": row.enquiry_no, "channel": channel, "status": row.status,
                 "outcome": row.outcome, "company": row.company_name,
                 "approved_by": row.approved_by,
                 "lead_id": str(row.lead_id) if row.lead_id else None,
                 "deal_id": str(row.deal_id) if row.deal_id else None, "label": row.enquiry_no}))
    if d is not None and d.recipient:
        from app.api.notify import notify_maker
        await notify_maker(ctx, recipient=d.recipient, event="intake.enquiry", title=d.title,
                           body=d.body, severity="info",
                           subject_type="Lead" if row.lead_id else ("Deal" if row.deal_id else None),
                           subject_id=str(row.lead_id or row.deal_id) if (row.lead_id or row.deal_id) else None,
                           dedupe_key=f"intake:{row.enquiry_no}")


# --------------------------------------------------------------------------- #
# The links
# --------------------------------------------------------------------------- #


def _base_url(request: Request) -> str:
    s = get_settings()
    if s.intake_public_base_url.strip():
        return s.intake_public_base_url.strip().rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        return ""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    return f"{scheme}://{host}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _mint_links(ctx: RequestContext, row: LeadEnquiry, approvers: list[str] | None,
                      base: str) -> list[dict[str, Any]]:
    """One Approve / Reject pair per approver the website will e-mail — or one
    anonymous pair when it named nobody (the BD Head then owns the lead)."""
    settings = get_settings()
    ttl = timedelta(days=max(1, int(settings.intake_token_ttl_days)))
    expires = datetime.now(timezone.utc) + ttl
    seen: set[str] = set()
    who: list[str | None] = []
    # Who decides: the sender's list, else the register's configured approvers,
    # else every active BD Head on the roster. Nobody at all → one unnamed pair.
    for a in list(approvers or []) or await _default_approvers(ctx):
        em = (a or "").strip().lower()[:200]
        if em and em not in seen:
            seen.add(em)
            who.append(em)
    if not who:
        who.append(None)
    out: list[dict[str, Any]] = []
    for recipient in who:
        urls: dict[str, str] = {}
        for kind in ("approve", "reject"):
            token = secrets.token_urlsafe(32)
            ctx.session.add(LeadEnquiryToken(
                tenant_id=ctx.tenant_id, enquiry_id=row.id, kind=kind, recipient=recipient,
                token_hash=_hash(token), expires_at=expires))
            urls[f"{kind}_url"] = f"{base}/v1/intake/enquiries/{token}/{kind}"
        out.append({"recipient": recipient, **urls, "expires_at": expires.isoformat()})
    await ctx.session.flush()
    if settings.intake_send_email:
        for link in out:
            if link["recipient"]:
                await _mail_approver(ctx, row, link, expires)
                link["emailed"] = True
    return out


async def _default_approvers(ctx: RequestContext) -> list[str]:
    """Who decides when the sender names nobody: the employees ticked "Enquiry
    approver" in the Employees master; else REGISTER_INTAKE_APPROVERS; else every
    active BD Head on the roster."""
    base = select(Person.email).where(Person.tenant_id == ctx.tenant_id, Person.deleted_at.is_(None),
                                      Person.inactive.is_(False), Person.email.isnot(None))
    ticked = (await ctx.session.execute(
        base.where(Person.enquiry_approver.is_(True)).order_by(Person.full_name))).scalars().all()
    if ticked:
        return [r for r in ticked if r]
    configured = [x.strip() for x in get_settings().intake_approvers.split(",") if x.strip()]
    if configured:
        return configured
    rows = (await ctx.session.execute(
        base.where(Person.role.ilike("%BD Head%")).order_by(Person.full_name))).scalars().all()
    return [r for r in rows if r]


def _mail_html(row: LeadEnquiry, e: EnquiryIn | None, approve_url: str, reject_url: str,
               expires: datetime) -> str:
    """The approval e-mail as Gmail renders it: a summary table and two buttons.
    Table layout and inline styles only — mail clients honour nothing else."""
    c = e.contact if e else None
    when = row.submitted_at or row.received_at
    rows = [("Enquiry", f"{row.enquiry_no} · {row.channel}"), ("Company", row.company_name),
            ("Contact", ", ".join(x for x in ((c.name if c else row.contact_name),
                                               _mobile(c.mobile) if c else None,
                                               (c.email if c else None)) if x) or None),
            ("Ask", (("Capital" if e.intent == "capital" else "Asset monetisation" if e.intent == "assets" else "Enquiry")
                     + " — " + _need(e)) if e else row.intent),
            ("Submitted", when.strftime("%d %b %Y, %H:%M") if when else None)]
    if e and e.company and e.company.address:
        rows.insert(2, ("Address", e.company.address))
    trs = "".join(
        f"<tr><td style='padding:6px 10px 6px 0;color:#5b6b7a;vertical-align:top;white-space:nowrap'>{_h(k)}</td>"
        f"<td style='padding:6px 0;color:#1d2733'>{_h(v)}</td></tr>" for k, v in rows)
    btn = ("display:inline-block;padding:12px 22px;border-radius:8px;font-weight:600;"
           "text-decoration:none;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif")
    return (
        "<!doctype html><html><body style='margin:0;background:#f4f6f8;font-family:-apple-system,Segoe UI,Roboto,"
        "Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1d2733'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f4f6f8'><tr><td align='center' style='padding:24px 12px'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='max-width:560px;background:#ffffff;border:1px solid #dfe5ea;border-radius:12px'>"
        "<tr><td style='padding:22px 22px 6px'><div style='font-size:12px;font-weight:700;letter-spacing:.08em;color:#0b5d4b'>PRISM · EVAM FINANCE</div>"
        f"<h1 style='font-size:20px;margin:10px 0 4px'>New {_h(row.channel)} enquiry: {_h(row.company_name)}</h1>"
        "<p style='margin:0 0 14px;color:#5b6b7a'>Approve to create the lead in PRISM, or reject it. Nothing happens until you confirm on the page that opens.</p></td></tr>"
        f"<tr><td style='padding:0 22px'><table role='presentation' cellpadding='0' cellspacing='0' width='100%' style='border-top:1px solid #eef1f4;border-bottom:1px solid #eef1f4'>{trs}</table></td></tr>"
        "<tr><td style='padding:18px 22px 6px'><table role='presentation' cellpadding='0' cellspacing='0'><tr>"
        f"<td style='padding-right:12px'><a href='{html.escape(approve_url, quote=True)}' style='{btn};background:#0b5d4b;color:#ffffff'>Approve</a></td>"
        f"<td><a href='{html.escape(reject_url, quote=True)}' style='{btn};background:#b42318;color:#ffffff'>Reject</a></td>"
        "</tr></table></td></tr>"
        f"<tr><td style='padding:8px 22px 22px;color:#7a8794;font-size:12px'>These links are for you only and work until {expires.strftime('%d %b %Y')}. "
        "If the buttons do not show, copy the link below into your browser.<br>"
        f"Approve: {_h(approve_url)}<br>Reject: {_h(reject_url)}</td></tr>"
        "</table></td></tr></table></body></html>")


async def _mail_approver(ctx: RequestContext, row: LeadEnquiry, link: dict[str, Any],
                         expires: datetime) -> None:
    """One inbox notification + one e-mail delivery for this approver, in the same
    transaction as the links (the notifier sends it; see WORKFLOWS_SMTP_*)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.notifications import Notification, NotificationDelivery

    e = _parse(row)
    need = _need(e) if e else (row.intent or "enquiry")
    title = f"Approve {row.channel} enquiry {row.enquiry_no}: {row.company_name}"[:300]
    body = (f"{row.company_name} — {need}\nContact: {row.contact_name or '—'}\n\n"
            f"Approve (creates the lead in PRISM): {link['approve_url']}\n"
            f"Reject: {link['reject_url']}\n\n"
            f"The links are for you only and work until {expires.strftime('%d %b %Y')}. "
            "Opening a link shows the enquiry; the decision is the button on that page.")
    nid = uuid.uuid4()
    # One mail per issue: a re-issue (the website re-posting a waiting enquiry) is a
    # new dedupe key, so the RM gets the fresh links.
    dedupe = f"intake:{row.enquiry_no}:approval:{link['recipient']}:{_hash(link['approve_url'])[:12]}"[:240]
    won = (await ctx.session.execute(pg_insert(Notification).values(
        id=nid, tenant_id=ctx.tenant_id, recipient=link["recipient"], event="intake.approval",
        severity="info", title=title, body=body, subject_type="LeadEnquiry", subject_id=str(row.id),
        dedupe_key=dedupe, created_by=ctx.actor,
        meta={"html": _mail_html(row, e, link["approve_url"], link["reject_url"], expires),
              "approve_url": link["approve_url"], "reject_url": link["reject_url"],
              "enquiry_no": row.enquiry_no}).on_conflict_do_nothing()
        .returning(Notification.id))).scalar_one_or_none()
    if won is None:
        return
    await ctx.session.execute(pg_insert(NotificationDelivery).values(
        tenant_id=ctx.tenant_id, notification_id=nid, channel="email",
        target=link["recipient"], status="pending", created_by=ctx.actor)
        .on_conflict_do_nothing(constraint="notification_deliveries_unique"))


@router.post("/v1/intake/enquiries", status_code=201, tags=["Intake"],
             summary="An enquiry from the website / WhatsApp door — submitted, approved or rejected")
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
    status = (e.status or "").strip().lower()
    if status not in ("submitted", "approved", "rejected"):
        raise ValidationAppError("status must be 'submitted', 'approved' or 'rejected'.")

    # Idempotent on the enquiry number: a redelivery answers with what happened —
    # and a redelivery of a still-parked enquiry gets its links again.
    existing = (await ctx.session.execute(select(LeadEnquiry).where(
        LeadEnquiry.tenant_id == ctx.tenant_id, LeadEnquiry.enquiry_no == e.enquiry_no))).scalar_one_or_none()
    if existing is not None:
        links = None
        if existing.status == "submitted" and status == "submitted":
            links = await _mint_links(ctx, existing, e.approvers, _base_url(request))
        return _result(existing, replayed=True, lead_no=await _lead_no_of(ctx, existing), links=links)

    row = LeadEnquiry(
        tenant_id=ctx.tenant_id, enquiry_no=e.enquiry_no, channel=channel, status=status,
        intent=(e.intent or None), company_name=(e.company.name if e.company else None),
        contact_name=e.contact.name, approved_by=e.approved_by, approved_at=e.approved_at,
        submitted_at=e.submitted_at, payload=json.loads(body.decode("utf-8")), outcome="rejected")

    if status == "submitted":
        # PRISM-hosted approval: park it, hand back the links for the RM's e-mail.
        if not e.company or not e.company.name.strip():
            raise ValidationAppError("A submitted enquiry needs company.name.")
        row.outcome, row.approved_by, row.approved_at = "pending", None, None
        row.note = "Waiting for the RM's decision (Approve / Reject link)."
        ctx.session.add(row)
        await ctx.session.flush()
        links = await _mint_links(ctx, row, e.approvers, _base_url(request))
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=actor, action="intake.enquiry",
            resource_type="lead_enquiries", resource_id=str(row.id),
            changes={"enquiry_no": e.enquiry_no, "channel": channel, "status": "submitted",
                     "outcome": "pending", "company": row.company_name,
                     "approvers": [l["recipient"] for l in links if l["recipient"]],
                     "label": e.enquiry_no}))
        return _result(row, replayed=False, links=links)

    d: _Decision | None = None
    if status == "approved":
        d = await _approve(ctx, e, row, channel=channel, actor=actor, approver=e.approved_by,
                           when=e.approved_at or e.submitted_at)
    else:
        row.note = "Rejected at approval; nothing created."
    await _record(ctx, row, actor=actor, channel=channel, d=d)
    return _result(row, replayed=False, lead_no=d.lead_no if d else None)


# --------------------------------------------------------------------------- #
# The RM's click — a browser page, not an API answer
# --------------------------------------------------------------------------- #

_CSS = """
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:#f4f6f8;font:16px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d2733}
.wrap{max-width:560px;margin:0 auto;padding:28px 16px 48px}
.brand{font-weight:700;letter-spacing:.08em;color:#0b5d4b;font-size:13px;margin-bottom:14px}
.card{background:#fff;border:1px solid #dfe5ea;border-radius:12px;padding:22px}
h1{font-size:20px;margin:0 0 6px}
.sub{color:#5b6b7a;margin:0 0 18px}
table{border-collapse:collapse;width:100%;margin:0 0 18px}
td{padding:6px 0;vertical-align:top;border-bottom:1px solid #eef1f4;overflow-wrap:anywhere}
.sub b{overflow-wrap:anywhere}
td:first-child{color:#5b6b7a;width:36%;padding-right:10px}
.ok{background:#e8f6ef;border:1px solid #bfe5d1;color:#0b5d4b;border-radius:8px;padding:12px 14px;margin:0 0 14px}
.no{background:#fdecec;border:1px solid #f5c2c2;color:#8a1f1f;border-radius:8px;padding:12px 14px;margin:0 0 14px}
.warn{background:#fff7e6;border:1px solid #f3dcae;color:#6b4e00;border-radius:8px;padding:12px 14px;margin:0 0 14px}
button{appearance:none;border:0;border-radius:8px;padding:12px 18px;font:inherit;font-weight:600;cursor:pointer;width:100%}
.approve{background:#0b5d4b;color:#fff}.reject{background:#b42318;color:#fff}
textarea{width:100%;box-sizing:border-box;border:1px solid #cfd7de;border-radius:8px;padding:10px;font:inherit;min-height:84px;margin:0 0 12px}
.foot{color:#7a8794;font-size:13px;margin-top:16px}
a{color:#0b5d4b}
"""


def _page(title: str, inner: str, status: int = 200, *, pending: bool = False) -> HTMLResponse:
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<meta name='robots' content='noindex'><title>{html.escape(title)} · PRISM</title>"
           f"<style>{_CSS}</style></head><body><div class='wrap'><div class='brand'>PRISM · EVAM FINANCE</div>"
           f"<div class='card'>{inner}</div>"
           + ("<div class='foot'>This page was opened from the link in your e-mail. "
              "Nothing happens until you press the button.</div>" if pending else "")
           + "</div></body></html>")
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _h(v: Any) -> str:
    return html.escape(str(v)) if v not in (None, "") else "—"


def _summary(row: LeadEnquiry, e: EnquiryIn | None) -> str:
    c = e.contact if e else None
    when = row.submitted_at or row.received_at
    rows = [
        ("Enquiry", f"{row.enquiry_no} · {row.channel}"),
        ("Company", row.company_name),
        ("Contact", ", ".join(x for x in ((c.name if c else row.contact_name),
                                           _mobile(c.mobile) if c else None,
                                           (c.email if c else None)) if x) or None),
        ("Ask", (("Capital" if e.intent == "capital" else "Asset monetisation" if e.intent == "assets" else "Enquiry")
                 + " — " + _need(e)) if e else row.intent),
        ("Submitted", when.strftime("%d %b %Y, %H:%M") if when else None),
    ]
    if e and e.company and e.company.address:
        rows.insert(2, ("Address", e.company.address))
    return "<table>" + "".join(f"<tr><td>{_h(k)}</td><td>{_h(v)}</td></tr>" for k, v in rows) + "</table>"


def _parse(row: LeadEnquiry) -> EnquiryIn | None:
    try:
        return EnquiryIn.model_validate(row.payload or {})
    except ValueError:
        return None


async def _load(ctx: RequestContext, token: str, kind: str, *, lock: bool = False,
                ) -> tuple[LeadEnquiryToken | None, LeadEnquiry | None, HTMLResponse | None]:
    """The token's enquiry — or the page that says why there is none. ``lock``
    takes the enquiry row FOR UPDATE: two RMs pressing their buttons in the same
    second must serialise, so the second sees the first's decision and never
    creates a twin lead."""
    if kind not in ("approve", "reject") or not (20 <= len(token) <= 128):
        return None, None, _page("Link not recognised",
                                 "<h1>This link is not recognised</h1><p class='sub'>It may have been "
                                 "copied incompletely. Open it again from the e-mail.</p>", 404)
    tok = (await ctx.session.execute(select(LeadEnquiryToken).where(
        LeadEnquiryToken.tenant_id == ctx.tenant_id,
        LeadEnquiryToken.token_hash == _hash(token)))).scalar_one_or_none()
    if tok is None or tok.kind != kind:
        return None, None, _page("Link not recognised",
                                 "<h1>This link is not recognised</h1><p class='sub'>It may have been "
                                 "copied incompletely. Open it again from the e-mail.</p>", 404)
    q = select(LeadEnquiry).where(LeadEnquiry.id == tok.enquiry_id, LeadEnquiry.tenant_id == ctx.tenant_id)
    if lock:
        q = q.with_for_update()
    row = (await ctx.session.execute(q)).scalar_one_or_none()
    if row is None:
        return None, None, _page("Link not recognised", "<h1>This link is not recognised</h1>", 404)
    if row.status == "submitted" and tok.expires_at < datetime.now(timezone.utc):
        return tok, row, _page("Link expired",
                               f"<h1>This link has expired</h1><p class='sub'>Enquiry {_h(row.enquiry_no)} "
                               f"({_h(row.company_name)}) is still waiting. Ask for it to be re-sent, "
                               "or add the lead in PRISM.</p>", 410)
    return tok, row, None


def _already(row: LeadEnquiry) -> HTMLResponse:
    who = row.approved_by or "someone"
    when = row.approved_at.strftime("%d %b %Y, %H:%M") if row.approved_at else ""
    if row.status == "approved":
        what = {"lead_created": "a lead was created", "interaction_on_deal": "it was logged on the company's live deal",
                "interaction_on_lead": "it was logged on the company's open lead"}.get(row.outcome, "it was handled")
        return _page("Already approved",
                     f"<h1>Already approved</h1><p class='sub'>Enquiry {_h(row.enquiry_no)} ({_h(row.company_name)}) "
                     f"was approved by {_h(who)}{' on ' + when if when else ''}, and {what}.</p>"
                     f"<div class='ok'>{_h(row.note)}</div><p><a href='/ui/'>Open PRISM</a></p>")
    return _page("Already rejected",
                 f"<h1>Already rejected</h1><p class='sub'>Enquiry {_h(row.enquiry_no)} ({_h(row.company_name)}) "
                 f"was rejected by {_h(who)}{' on ' + when if when else ''}.</p>"
                 f"<div class='no'>{_h(row.note)}</div>")


def _real_click(request: Request) -> bool:
    """A navigation the person started — what a tap on the e-mail button looks
    like from Chrome, the Gmail app or a recent Safari — as opposed to a link
    scanner, a preview fetch or a script. Browsers set these fetch-metadata
    headers themselves; nothing in the mail can forge them."""
    h = request.headers
    return (h.get("sec-fetch-user") == "?1" and h.get("sec-fetch-mode") == "navigate"
            and h.get("sec-fetch-dest", "document") == "document")


@router.get("/v1/intake/enquiries/{token}/{kind}", tags=["Intake"], include_in_schema=False,
            response_class=HTMLResponse)
async def decision_page(token: str, kind: str, request: Request,
                        ctx: RequestContext = Depends(get_context)) -> HTMLResponse:
    """The page behind the Approve / Reject link. A real tap on Approve decides
    at once (one tap for the RM); anything else that opens the link — a mail
    scanner, a preview — gets the enquiry and one button, and changes nothing.
    Reject always asks for the reason first."""
    if kind == "approve" and get_settings().intake_one_tap and _real_click(request):
        return await decide(token, kind, request, ctx)
    tok, row, err = await _load(ctx, token, kind)
    if err is not None:
        return err
    assert tok is not None and row is not None
    if row.status != "submitted":
        return _already(row)
    e = _parse(row)
    who = f"<p class='sub'>You are deciding as <b>{_h(tok.recipient)}</b>.</p>" if tok.recipient else \
          "<div class='warn'>This link was not issued to a named RM: the lead will go to the BD Head.</div>"
    if kind == "approve":
        inner = (f"<h1>Approve this enquiry?</h1>{who}{_summary(row, e)}"
                 f"<form method='post'><button class='approve' type='submit'>Approve — create the lead in PRISM</button></form>")
    else:
        inner = (f"<h1>Reject this enquiry?</h1>{who}{_summary(row, e)}"
                 f"<form method='post'><textarea name='reason' placeholder='Why (optional, kept with the enquiry)'></textarea>"
                 f"<button class='reject' type='submit'>Reject — nothing will be created</button></form>")
    return _page("Approve enquiry" if kind == "approve" else "Reject enquiry", inner, pending=True)


@router.post("/v1/intake/enquiries/{token}/{kind}", tags=["Intake"], include_in_schema=False,
             response_class=HTMLResponse)
async def decide(token: str, kind: str, request: Request,
                 ctx: RequestContext = Depends(get_context)) -> HTMLResponse:
    """The button: run the rule (approve) or park the rejection, once."""
    tok, row, err = await _load(ctx, token, kind, lock=True)
    if err is not None:
        return err
    assert tok is not None and row is not None
    if row.status != "submitted":
        return _already(row)
    e = _parse(row)
    if e is None:
        return _page("Enquiry unreadable",
                     "<h1>This enquiry cannot be read</h1><p class='sub'>Its stored payload is not valid; "
                     "add the lead in PRISM and tell the website team.</p>", 500)
    now = datetime.now(timezone.utc)
    channel = row.channel or "website"
    approver = tok.recipient
    actor = approver or f"intake:{channel}"
    tok.used_at = now
    row.approved_by, row.approved_at = approver, now
    if kind == "approve":
        row.status = "approved"
        e = e.model_copy(update={"status": "approved", "approved_by": approver, "approved_at": now})
        d = await _approve(ctx, e, row, channel=channel, actor=actor, approver=approver, when=now)
        await _record(ctx, row, actor=actor, channel=channel, d=d)
        headline = {"lead_created": f"Lead {d.lead_no} created",
                    "interaction_on_deal": "Logged on the company's live deal",
                    "interaction_on_lead": f"Logged on open lead {d.lead_no}"}.get(row.outcome, "Approved")
        inner = (f"<h1>{_h(headline)}</h1><p class='sub'>Enquiry {_h(row.enquiry_no)} · {_h(row.company_name)}</p>"
                 f"<div class='ok'>{_h(d.body)}</div>{_summary(row, e)}"
                 f"<p><a href='/ui/'>Open PRISM</a></p>")
        return _page("Approved", inner)
    reason = ""
    try:
        form = await request.form()
        reason = str(form.get("reason") or "").strip()[:1000]
    except Exception:  # noqa: BLE001 - a bare POST without a form body is still a rejection
        reason = ""
    row.status, row.outcome = "rejected", "rejected"
    row.note = f"Rejected by {approver or 'the link holder'}" + (f": {reason}" if reason else ".")
    row.payload = {**(row.payload or {}), "decision": {"status": "rejected", "by": approver,
                                                       "at": now.isoformat(), "reason": reason or None}}
    await _record(ctx, row, actor=actor, channel=channel, d=None)
    inner = (f"<h1>Rejected</h1><p class='sub'>Enquiry {_h(row.enquiry_no)} · {_h(row.company_name)}</p>"
             f"<div class='no'>Nothing was created in PRISM." + (f" Reason kept: {_h(reason)}" if reason else "") + "</div>"
             f"{_summary(row, e)}")
    return _page("Rejected", inner)
