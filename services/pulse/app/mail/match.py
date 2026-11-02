"""Which company a conversation is about.

Rules, strongest first; the best rule's score is the confidence:
  0.95  an outside party writes from a domain on the company's ``email_domains``
  0.90  an outside party's address is a contact e-mail the Register knows
  0.70  the company's name (or a news alias) is in the subject
  0.55  the company's name is in the conversation's text
Public mail domains (gmail.com, …) never count as a domain match. A match below
the configured confidence goes to the review queue with the best guess attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.mail.threads import PUBLIC_DOMAINS, Thread

_CORP_RX = re.compile(r"\b(private|pvt|limited|ltd|llp|inc|corp|corporation|company|co|plc|"
                      r"industries|enterprises?|solutions|services|technologies|india)\b\.?", re.I)
_WS_RX = re.compile(r"[^a-z0-9]+")


@dataclass
class Firm:
    id: str
    name: str
    legal_name: str = ""
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    contacts: set[str] = field(default_factory=set)   # known e-mail addresses
    watch: bool = True


@dataclass
class Match:
    firm: Firm
    confidence: float
    how: str


def _norm(s: str) -> str:
    return _WS_RX.sub(" ", s.lower()).strip()


def _short_names(firm: Firm) -> list[str]:
    """The names worth searching for: the display name, the legal name without the
    corporate tail, every alias — each at least two words or six characters so
    'Land' or 'Sun' does not match every mail."""
    out: list[str] = []
    for raw in (firm.name, _CORP_RX.sub("", firm.legal_name)):
        n = _norm(raw)
        if n and (len(n) >= 6 or len(n.split()) >= 2):
            out.append(n)
    # An alias is the desk's deliberate choice ("BetaP"): trusted from four characters.
    for raw in firm.aliases:
        n = _norm(raw)
        if len(n) >= 4:
            out.append(n)
    return list(dict.fromkeys(out))


def _domain_index(firms: list[Firm]) -> dict[str, Firm]:
    idx: dict[str, Firm] = {}
    for f in firms:
        for d in f.domains:
            d = d.strip().lower()
            if d and d not in PUBLIC_DOMAINS:
                idx.setdefault(d, f)
    return idx


def _contact_index(firms: list[Firm]) -> dict[str, Firm]:
    idx: dict[str, Firm] = {}
    for f in firms:
        for e in f.contacts:
            idx.setdefault(e.lower(), f)
    return idx


def match_thread(t: Thread, firms: list[Firm], internal_domains: set[str]) -> Match | None:
    outsiders = t.outsiders(internal_domains)
    by_domain = _domain_index(firms)
    by_contact = _contact_index(firms)
    for p in outsiders:
        d = p.domain
        if d in by_domain:
            return Match(by_domain[d], 0.95, f"domain {d}")
        # a sub-domain of a listed one (mail.acmesolar.in)
        for listed, f in by_domain.items():
            if d.endswith("." + listed):
                return Match(f, 0.95, f"domain {d}")
    for p in outsiders:
        if p.email in by_contact:
            return Match(by_contact[p.email], 0.90, f"contact {p.email}")
    subject = _norm(t.subject)
    body = _norm(t.text(6000))
    best: Match | None = None
    for f in firms:
        for n in _short_names(f):
            if f" {n} " in f" {subject} ":
                cand = Match(f, 0.70, f"name in subject: {n}")
            elif f" {n} " in f" {body} ":
                cand = Match(f, 0.55, f"name in text: {n}")
            else:
                continue
            if best is None or cand.confidence > best.confidence or (
                    cand.confidence == best.confidence and len(n) > len(best.how)):
                best = cand
    return best


def firm_from_row(row: dict[str, Any]) -> Firm | None:
    name = (row.get("display_name") or row.get("legal_name") or "").strip()
    if not name:
        return None
    return Firm(id=str(row["id"]), name=name, legal_name=row.get("legal_name") or "",
                aliases=[str(a) for a in (row.get("news_aliases") or [])],
                domains=[str(d) for d in (row.get("email_domains") or [])],
                watch=row.get("news_watch") is not False)
