"""A Gmail thread as the reader sees it: its messages in order, who took part, the
plain text of each message without the quoted history, and the filters that keep
the reader to conversations with outsiders.

Nothing here talks to Google; it reads the JSON ``users.threads.get?format=full``
returns, which makes it testable from fixtures.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

# Senders that are machines, not people. Matched against the local part and the
# display name, lower-cased.
AUTOMATED_RX = re.compile(
    r"(no-?reply|donotreply|do-not-reply|notification|newsletter|mailer-daemon|postmaster|"
    r"bounce|unsubscribe|calendar-notification|noreply@|alerts?@|updates?@|digest@|"
    r"billing@|invoice@|receipt@|support@|helpdesk|jira|github|linkedin|zoom\.us|"
    r"teams\.microsoft|docusign|calendly|google\.com$)", re.I)
# Public mail providers: a domain match on them says nothing about the company.
PUBLIC_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in",
                  "outlook.com", "hotmail.com", "live.com", "rediffmail.com", "icloud.com",
                  "protonmail.com", "proton.me", "aol.com", "msn.com", "ymail.com"}

_QUOTE_HEAD_RX = re.compile(
    r"^(On .{5,120} wrote:|From: .+|-{2,} ?Original Message ?-{2,}|-{2,} ?Forwarded message ?-{2,}|"
    r"Sent from my .+|Get Outlook for .+|_{5,}|> ?.*)$", re.M)
_SIGNATURE_RX = re.compile(r"^(--\s*|regards,?|thanks( and|&) regards,?|best regards,?|warm regards,?|"
                           r"thanks,?|kind regards,?|sincerely,?)\s*$", re.I | re.M)
_WS_RX = re.compile(r"[ \t]+")


@dataclass
class Party:
    email: str
    name: str = ""

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


@dataclass
class Message:
    id: str
    message_id: str          # the RFC Message-ID header (identity across mailboxes)
    sender: Party
    to: list[Party]
    cc: list[Party]
    date: datetime
    subject: str
    text: str                # this message's own words, quoted history removed
    labels: list[str] = field(default_factory=list)
    automated: bool = False


@dataclass
class Thread:
    thread_id: str
    messages: list[Message]

    @property
    def subject(self) -> str:
        for m in self.messages:
            if m.subject.strip():
                return re.sub(r"^((re|fw|fwd|aw|wg)\s*:\s*)+", "", m.subject.strip(), flags=re.I).strip()
        return "(no subject)"

    @property
    def first(self) -> Message:
        return self.messages[0]

    @property
    def last(self) -> Message:
        return self.messages[-1]

    @property
    def key(self) -> str:
        """The conversation's identity ACROSS mailboxes: the first message's Message-ID.
        The same thread sits under a different Gmail thread id in every recipient's
        mailbox; the Message-ID header is the one thing they share."""
        root = self.first.message_id or f"{self.thread_id}:{self.first.id}"
        return hashlib.sha256(root.strip().lower().encode()).hexdigest()[:32]

    def parties(self) -> list[Party]:
        seen: dict[str, Party] = {}
        for m in self.messages:
            for p in (m.sender, *m.to, *m.cc):
                if p.email and p.email not in seen:
                    seen[p.email] = p
                elif p.email and p.name and not seen[p.email].name:
                    seen[p.email].name = p.name
        return list(seen.values())

    def outsiders(self, internal_domains: set[str]) -> list[Party]:
        return [p for p in self.parties() if p.domain and p.domain not in internal_domains]

    def insiders(self, internal_domains: set[str]) -> list[Party]:
        return [p for p in self.parties() if p.domain in internal_domains]

    def text(self, max_chars: int = 12_000) -> str:
        """The conversation as the model reads it, newest last, trimmed from the middle
        when long so the opening and the latest exchange both survive."""
        parts = []
        for m in self.messages:
            who = m.sender.name or m.sender.email
            parts.append(f"[{m.date.strftime('%Y-%m-%d %H:%M')}] {who} <{m.sender.email}>:\n{m.text.strip()}")
        full = "\n\n".join(parts)
        if len(full) <= max_chars:
            return full
        head = full[: max_chars // 3]
        tail = full[-(max_chars - len(head) - 40):]
        return head + "\n\n[... earlier messages trimmed ...]\n\n" + tail


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _headers(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = str(h.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(h.get("value") or "")
    return out


def _parties(value: str) -> list[Party]:
    out = []
    for name, addr in getaddresses([value or ""]):
        addr = addr.strip().lower()
        if "@" in addr:
            out.append(Party(email=addr, name=name.strip().strip('"')))
    return out


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _body(payload: dict[str, Any]) -> str:
    """text/plain if the message has one, else the HTML stripped to text."""
    plain, html = [], []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        return html_to_text("\n".join(html))
    return str(payload.get("snippet") or "")


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return "\n".join(_WS_RX.sub(" ", ln).strip() for ln in text.splitlines())


def own_words(text: str) -> str:
    """This message's words: cut at the first quoted-history marker, drop the
    signature tail, squeeze blank lines."""
    m = _QUOTE_HEAD_RX.search(text)
    if m:
        text = text[: m.start()]
    sig = _SIGNATURE_RX.search(text)
    if sig and sig.start() > 40:
        text = text[: sig.start()]
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln.strip() == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _when(headers: dict[str, str], internal_ms: Any) -> datetime:
    try:
        d = parsedate_to_datetime(headers.get("date", ""))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def is_automated(sender: Party, headers: dict[str, str]) -> bool:
    if AUTOMATED_RX.search(f"{sender.name} {sender.email}".lower()):
        return True
    if headers.get("list-unsubscribe") or headers.get("list-id"):
        return True
    if headers.get("auto-submitted", "no").lower() not in ("", "no"):
        return True
    return headers.get("precedence", "").lower() in ("bulk", "list", "junk")


def parse_thread(raw: dict[str, Any]) -> Thread | None:
    """``users.threads.get?format=full`` → Thread, messages in date order. None when
    the thread has no readable message."""
    messages: list[Message] = []
    for m in raw.get("messages") or []:
        payload = m.get("payload") or {}
        h = _headers(payload)
        senders = _parties(h.get("from", ""))
        sender = senders[0] if senders else Party(email="")
        text = own_words(_body({**payload, "snippet": m.get("snippet", "")}))
        messages.append(Message(
            id=str(m.get("id") or ""), message_id=h.get("message-id", "").strip(),
            sender=sender, to=_parties(h.get("to", "")), cc=_parties(h.get("cc", "")),
            date=_when(h, m.get("internalDate")), subject=h.get("subject", ""),
            text=text, labels=[str(x) for x in (m.get("labelIds") or [])],
            automated=is_automated(sender, h)))
    messages = [m for m in messages if m.sender.email]
    if not messages:
        return None
    messages.sort(key=lambda m: m.date)
    return Thread(thread_id=str(raw.get("id") or ""), messages=messages)


# --------------------------------------------------------------------------- #
# Filters — why a thread is left out
# --------------------------------------------------------------------------- #
def skip_reason(t: Thread, internal_domains: set[str], excluded_labels: set[str]) -> str | None:
    """None = read it. Otherwise the reason, for the status page's counts."""
    labels = {lb.lower() for m in t.messages for lb in m.labels}
    if any(lb.lower() in labels for lb in excluded_labels):
        return "excluded label"
    if "spam" in labels or "trash" in labels:
        return "spam or trash"
    human = [m for m in t.messages if not m.automated]
    if not human:
        return "automated sender"
    if not t.outsiders(internal_domains):
        return "internal only"
    if not any(m.text.strip() for m in human):
        return "no text"
    return None
