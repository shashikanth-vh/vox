"""Email a news digest — the desk's own reading, in their inbox.

SMTP settings come from the environment (``PULSE_SMTP_*``); nothing is read from a file
checked into the repo, and a blank user/password simply DISABLES email — search keeps
working, and every route says plainly that email is not configured rather than failing in
a way that reads like a bug.

Ported from the desk's atlas_serve.py, including the two details that took a real outage
to learn: the envelope-from must be a bare address (Brevo and other relays reject a
display-name string there) while the header From may carry the friendly name, and every
message goes out as BOTH plain text and HTML so a text-only client is not handed markup.
"""

from __future__ import annotations

import html as _html
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from typing import Any

from evam_backend_core.logging import get_logger

log = get_logger("pulse.mailer")

_SEV_COLOR = {"GOOD": "#2E7D4F", "BAD": "#C98A1B", "UGLY": "#B3432B",
               "POLICY": "#1F6FA8"}


@dataclass(frozen=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    from_name: str = ""

    @property
    def ready(self) -> bool:
        """Email is OFF unless a host, a login and a key are all present. Half-configured
        is treated as off, not as an error to discover at send time."""
        return bool(self.host and self.user and self.password)


# Ordered most-serious first: a firm's card is coloured by its worst article, and the
# digest leads with the firms carrying the most UGLY news.
_SEV_RANK = {"UGLY": 0, "BAD": 1, "POLICY": 2, "GOOD": 3}
_NAVY, _MUTED, _RULE = "#1B2A4A", "#5F6E76", "#E6EAEE"
_FONT = "font-family:Segoe UI,Helvetica,Arial,sans-serif"
_OLDER_GRACE_DAYS = 7
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(stamp: str) -> str:
    """'20261009' or '2026-10-09' -> '9 Oct 2026'; anything else is shown as given."""
    digits = re.sub(r"\D", "", stamp or "")[:8]
    if len(digits) != 8 or not 1 <= int(digits[4:6]) <= 12:
        return stamp or ""
    return f"{int(digits[6:])} {_MONTHS[int(digits[4:6]) - 1]} {digits[:4]}"


def _span(dfrom: str, dto: str) -> str:
    return f"{_day(dfrom)} to {_day(dto)}" if dfrom or dto else "last 3 months"


def _counts(articles: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in articles:
        sev = a.get("severity") or "GOOD"
        out[sev] = out.get(sev, 0) + 1
    return out


def _chip(sev: str, label: str | None = None) -> str:
    colour = _SEV_COLOR.get(sev, _SEV_COLOR["GOOD"])
    return (f'<span style="display:inline-block;background:{colour};color:#fff;'
            'font-size:10px;font-weight:700;letter-spacing:.4px;padding:2px 7px;'
            f'border-radius:9px;white-space:nowrap">{_html.escape(label or sev)}</span>')


def _tally(counts: dict[str, int]) -> str:
    """'1 UGLY · 2 BAD', most serious first, each count in its severity colour."""
    return " &middot; ".join(
        f'<span style="color:{_SEV_COLOR.get(sev, _MUTED)};font-weight:700">{n} {sev}</span>'
        for sev, n in sorted(counts.items(), key=lambda kv: _SEV_RANK.get(kv[0], 9)))


# Words that say nothing about WHICH story a headline tells.
_STOP = frozenset("""
about after again against also amid among before being below between could during
first from have into more most news over report reports said says than that their
them then there these they this those under until very what when where which while
will with would year years over says said into onto amid amidst week weeks today
""".split())


def _stems(title: str, skip: set[str]) -> set[str]:
    """The content words of a headline, cut to five letters so "targets" and
    "targeted" agree. The firm's own name is skipped: every one of its headlines
    shares that, and it says nothing about which story is which."""
    return {w[:5] for w in re.findall(r"[a-z]+", (title or "").lower())
            if len(w) >= 4 and w not in _STOP and w not in skip}


def stories(articles: list[dict[str, Any]], name: str = "") -> list[list[dict[str, Any]]]:
    """Group headlines that tell the same story: two that share two or more content
    words are joined, and joins chain ("arrested" links to "bail" through a third).
    Input order is kept, so the first of each group is its lead."""
    skip = set(re.findall(r"[a-z]+", name.lower()))
    stems = [_stems(a.get("title", ""), skip) for a in articles]
    parent = list(range(len(articles)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(articles)):
        for j in range(i):
            if len(stems[i] & stems[j]) >= 2:
                parent[root(i)] = root(j)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, a in enumerate(articles):
        groups.setdefault(root(i), []).append(a)
    return list(groups.values())


def anchor_of(term: str) -> str:
    """A stable in-email anchor for a firm's card."""
    return "firm-" + (re.sub(r"[^a-z0-9]+", "-", (term or "").lower()).strip("-") or "x")


def firm_card(term: str, articles: list[dict[str, Any]],
              dfrom: str = "", dto: str = "", *, anchor: str = "") -> str:
    """One firm: its NAME is the headline of the card, the stories sit under it.

    The card's left edge carries the colour of the worst article, so a scan down the
    email finds the serious ones without reading. Several outlets carrying one story
    show as ONE item — the most serious, newest headline — with the other reports
    listed small beneath it ("Satyendar Jain arrested" arrived as seven items). A
    story published before the window (search engines resurface old pieces) is
    marked as older rather than passed off as this week's news. An article found
    through one of the firm's key people names that person above the headline.
    ``anchor`` makes the card a target for the contents line, with a way back up."""
    arts = sorted(articles, key=lambda a: (_SEV_RANK.get(a.get("severity", ""), 9),
                                           -int(re.sub(r"\D", "", a.get("when") or "")[:8] or 0)))
    counts = _counts(arts)
    worst = min(counts, key=lambda s: _SEV_RANK.get(s, 9)) if counts else "GOOD"
    # Search engines date by first seen and lag a few days, so a piece a week before
    # the window is ordinary; older than that, it is an old story resurfacing.
    floor = re.sub(r"\D", "", dfrom or "")[:8]
    if len(floor) == 8:
        floor = (date(int(floor[:4]), int(floor[4:6]), int(floor[6:]))
                 - timedelta(days=_OLDER_GRACE_DAYS)).strftime("%Y%m%d")

    def is_older(a: dict[str, Any]) -> bool:
        when = re.sub(r"\D", "", a.get("when") or "")[:8]
        return bool(floor and when and when < floor)

    def meta(a: dict[str, Any], with_via: bool = True) -> str:
        out = " &middot; ".join(x for x in (
            _html.escape(a.get("source", "")),
            f'via {_html.escape(a["via"])}' if with_via and a.get("via") else "",
            _html.escape(_day(a.get("when", "")))) if x)
        # Found under a name other than the card's own (a short name, or an analyst's
        # news name): said, so a namesake ("nSure.ai" for NSure Energy) is seen as one.
        found = str(a.get("matched") or "")
        if found and not a.get("about") and found.casefold() != term.split(" · ")[0].casefold():
            out += (f' &middot; <span style="color:#1F6FA8">found as '
                    f'&ldquo;{_html.escape(found)}&rdquo;</span>')
        if is_older(a):
            out += ' &middot; <span style="color:#8A6D1F;font-weight:600">older story</span>'
        return out

    groups = stories(arts, term)
    rows = []
    for group in groups:
        a, rest = group[0], group[1:]
        also = ""
        if rest:
            also = (f'<div style="margin-top:6px;padding-left:10px;border-left:2px solid {_RULE}">'
                    f'<div style="color:{_MUTED};font-size:11px;font-weight:700;'
                    f'margin-bottom:2px">{len(rest)} more report{"" if len(rest) == 1 else "s"}'
                    ' of this story</div>'
                    + "".join(
                        f'<div style="font-size:12px;line-height:1.4;margin:2px 0">'
                        f'<a href="{_html.escape(r.get("url", ""))}" style="color:{_NAVY};'
                        f'text-decoration:none">{_html.escape(r.get("title", ""))}</a>'
                        f' <span style="color:{_MUTED}">&middot; {meta(r, False)}</span></div>'
                        for r in rest) + '</div>')
        rows.append(
            '<tr><td style="padding:10px 0 10px 0;border-top:1px solid ' + _RULE + ';'
            'vertical-align:top;width:58px">' + _chip(a.get("severity", "GOOD")) + '</td>'
            '<td style="padding:10px 0;border-top:1px solid ' + _RULE + '">'
            + (f'<div style="color:#1F6FA8;font-size:11px;font-weight:700;'
               f'letter-spacing:.3px;margin-bottom:2px">{_html.escape(a["about"])}</div>'
               if a.get("about") else '')
            + f'<a href="{_html.escape(a.get("url", ""))}" style="color:{_NAVY};'
            'text-decoration:none;font-size:14px;font-weight:600;line-height:1.35'
            + (';opacity:.75' if is_older(a) else '') + f'">{_html.escape(a.get("title", ""))}</a>'
            f'<div style="color:{_MUTED};font-size:12px;margin-top:3px">{meta(a)}</div>'
            + also + '</td></tr>')
    body = "".join(rows) or (
        f'<tr><td style="padding:10px 0;color:{_MUTED};border-top:1px solid {_RULE}">'
        'No articles in this window.</td></tr>')
    n, k = len(arts), len(groups)
    about_people = sum(1 for a in arts if a.get("about"))
    tally = (f'{k} stor{"y" if k == 1 else "ies"}'
             + (f' from {n} reports' if n != k else '')) if n else "0 articles"
    target = (f'<a name="{anchor}" id="{anchor}" style="text-decoration:none"></a>'
              if anchor else '')
    up = (f'<a href="#top" style="float:right;color:{_MUTED};font-size:11px;'
          'text-decoration:none;font-weight:400;margin-top:6px">&uarr; Top</a>'
          if anchor else '')
    return (
        f'{target}<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:separate;background:#fff;border:1px solid {_RULE};'
        f'border-left:4px solid {_SEV_COLOR.get(worst, _SEV_COLOR["GOOD"])};'
        'border-radius:8px;margin:0 0 14px 0"><tr><td style="padding:14px 18px 6px 18px">'
        f'<div style="{_FONT};color:{_NAVY};font-size:20px;font-weight:700;line-height:1.25">'
        f'{up}{_html.escape(term)}</div>'
        f'<div style="{_FONT};color:{_MUTED};font-size:12px;margin:4px 0 8px 0">'
        + tally + (f' &middot; {_tally(counts)}' if counts else '')
        + (f' &middot; {about_people} about its directors / promoters' if about_people else '')
        + '</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="{_FONT};border-collapse:collapse">{body}</table>'
        '</td></tr></table>')


def _coverage_note(coverage: dict[str, Any] | None, found: int) -> str:
    """The run's coverage, said in the email: how many firms were searched, and which
    source could not be searched for how many of them, with the reason. A short digest
    must never pass for a complete one — the 13 Oct live run had Google News blocked
    for 365 of 489 firms, and the email it produced said nothing."""
    if not coverage or not coverage.get("firms"):
        return ""
    n = int(coverage["firms"])
    lines = [f'<div style="color:{_MUTED};font-size:12px">{n} firm{"" if n == 1 else "s"} '
             f'searched &middot; {found} with news to report</div>']
    warn, info = [], []
    for src, k in sorted((coverage.get("down") or {}).items(), key=lambda kv: -kv[1]):
        why = str((coverage.get("why") or {}).get(src) or "")
        if why.startswith("not asked"):
            info.append(f'{_html.escape(src)} was not asked for {k} of {n} firms '
                        f'({_html.escape(why[len("not asked: "):])}); the other sources '
                        'covered them.')
        else:
            warn.append(f'<b>{_html.escape(src)}</b> could not be searched for {k} of {n} '
                        f'firms{f" ({_html.escape(why)})" if why else ""}.')
    if warn:
        lines.append(
            '<div style="margin-top:8px;padding:8px 10px;background:#FFF6E5;'
            'border:1px solid #F0D9A8;border-radius:6px;color:#6B4E0E;font-size:12px">'
            + " ".join(warn) + ' Those firms were searched on the other sources only, so '
            'this digest may be missing news about them.</div>')
    if info:
        lines.append(f'<div style="color:{_MUTED};font-size:11px;margin-top:6px">'
                     + " ".join(info) + '</div>')
    return "".join(lines)


def digest_page(groups: list[tuple[str, list[dict[str, Any]]]], dfrom: str = "",
                dto: str = "", *, adverse_only: bool = False,
                coverage: dict[str, Any] | None = None) -> str:
    """The whole email: ONE masthead (brand small, the window and the totals), a
    contents line naming the firms most-serious first — each a link to its card, and
    each card a link back up — then a card per firm. The brand is said once: the
    firms are what the reader opened the email for. (In-email links work in Outlook,
    Apple Mail and most phone clients; Gmail on the web ignores them, and the
    contents line still reads as a list there.)"""
    groups = sorted(groups, key=lambda g: (
        tuple(-_counts(g[1]).get(s, 0) for s in ("UGLY", "BAD", "POLICY")), g[0].casefold()))
    articles = [a for _, arts in groups for a in arts]
    totals = _counts(articles)
    title = "Adverse news digest" if adverse_only else "News digest"
    firms = len(groups)
    n_stories = sum(len(stories(a, t)) for t, a in groups)
    stats = (f'{firms} firm{"" if firms == 1 else "s"} &middot; {n_stories} '
             f'stor{"y" if n_stories == 1 else "ies"}'
             + (f' from {len(articles)} reports' if len(articles) != n_stories else '')
             + (f' &middot; {_tally(totals)}' if totals else ''))
    anchors = {t: anchor_of(t) for t, _ in groups} if firms > 1 else {}
    index = ""
    if firms > 1:
        cells = [f'<span style="white-space:nowrap;display:inline-block;margin:0 14px 6px 0">'
                 f'<a href="#{anchors[t]}" style="color:{_NAVY};font-weight:600;'
                 f'text-decoration:underline;text-decoration-color:{_RULE}">{_html.escape(t)}</a> '
                 f'<span style="color:{_MUTED}">({_tally(_counts(a))})</span></span>'
                 for t, a in groups]
        index = (f'<tr><td style="background:#fff;padding:14px 22px 6px 22px;font-size:12px;'
                 f'border-bottom:1px solid {_RULE}">'
                 f'<div style="color:{_MUTED};font-size:11px;font-weight:700;'
                 'letter-spacing:.8px;text-transform:uppercase;margin-bottom:8px">'
                 'In this digest, most serious first &middot; tap a name to jump to it</div>'
                 + "".join(cells) + '</td></tr>')
    cards = "".join(firm_card(t, a, dfrom, dto, anchor=anchors.get(t, ""))
                    for t, a in groups) or (
        f'<div style="{_FONT};color:{_MUTED};padding:16px">No matching news in this window.</div>')
    return (
        '<div style="background:#F3F5F8;padding:18px 0">'
        '<a name="top" id="top"></a>'
        '<table role="presentation" align="center" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:680px;margin:0 auto;border-collapse:collapse;{_FONT}">'
        f'<tr><td style="background:{_NAVY};padding:16px 22px;border-radius:10px 10px 0 0">'
        '<div style="color:#9FB3BA;font-size:11px;font-weight:700;letter-spacing:1.2px">'
        'EVAM PRISM &middot; NEWS RADAR</div>'
        f'<div style="color:#fff;font-size:18px;font-weight:600;margin-top:4px">{title}'
        f' <span style="color:#9FB3BA;font-weight:400;font-size:14px">&middot; '
        f'{_span(dfrom, dto)}</span></div></td></tr>'
        f'<tr><td style="background:#fff;padding:12px 22px;font-size:13px;color:{_MUTED};'
        f'border-bottom:1px solid {_RULE}">{stats}{_coverage_note(coverage, firms)}</td></tr>'
        + index
        + f'<tr><td style="padding:16px 0 0 0">{cards}</td></tr>'
        f'<tr><td style="color:#8A97A0;font-size:11px;padding:6px 4px 0 4px">'
        'Generated by the PRISM News Radar. Every headline links to its source; severity is '
        'PRISM\'s reading of the headline, so open the article before acting on it.</td></tr>'
        '</table></div>')


def digest_html(term: str, articles: list[dict[str, Any]],
                dfrom: str = "", dto: str = "") -> str:
    """A one-firm email (the "email this search" button): the same page, one card."""
    return digest_page([(term, articles)], dfrom, dto)


def _smtp_hint(exc: Exception, cfg: Any) -> str:
    """The SMTP layer's errors name a syscall, not a cause.

    "[Errno -2] Name or service not known" is the resolver refusing the host, and it
    reads on screen as a mail problem when it is a hostname or an egress problem. The
    library cannot say which of the two it is, but it CAN say what to look at — and the
    host it actually tried, which is the fact the desk needs and never has.
    """
    text = str(exc)
    host = f"{cfg.host}:{cfg.port}"
    if "Name or service not known" in text or "nodename nor servname" in text:
        return (f" — the SMTP host {host!r} could not be resolved from this container. "
                "Check PULSE_SMTP_HOST for a typo or a stray comma, and that the "
                "container is allowed to resolve and reach it.")
    if "Connection refused" in text or "Network is unreachable" in text:
        return (f" — nothing is listening at {host} from this container. Check the port "
                "(587 = STARTTLS, 465 = implicit SSL) and any egress firewall.")
    if "timed out" in text.lower():
        return (f" — {host} accepted no connection in time, which is usually an egress "
                "firewall silently dropping outbound SMTP.")
    if "authentication" in text.lower() or "5.7." in text or "535" in text:
        return (" — the host answered but rejected the credentials. For most providers "
                "PULSE_SMTP_PASS is an API/SMTP key, not the account password.")
    return ""

def send_email(cfg: SmtpConfig, recipients: list[str], subject: str,
               html_body: str) -> tuple[bool, str]:
    """Blocking on purpose — callers run it in a threadpool. Returns (ok, message) where
    the message is written to be shown to the person who asked, not only logged."""
    if not cfg.ready:
        return False, ("Email is not configured — set PULSE_SMTP_HOST / _USER / _PASS "
                       "(search works without it).")
    recipients = [r.strip() for r in recipients if r and r.strip()]
    if not recipients:
        return False, "No valid recipients."

    sender = cfg.sender or cfg.user
    # The envelope sender must be a BARE address: several relays (Brevo among them)
    # reject a display-name string here even though the header From accepts one.
    envelope_from = parseaddr(sender)[1] or sender
    header_from = (formataddr((cfg.from_name, envelope_from))
                   if cfg.from_name and "<" not in sender else sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = header_from
    msg["To"] = ", ".join(recipients)
    # Text part first: a client that prefers plain text must not be handed raw markup.
    msg.attach(MIMEText(re.sub("<[^>]+>", "", html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info("pulse_email_sending", extra={"subject": subject, "host": cfg.host,
                                           "port": cfg.port, "recipients": len(recipients)})
    try:
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port,
                                  context=ssl.create_default_context(), timeout=20) as s:
                s.login(cfg.user, cfg.password)
                s.sendmail(envelope_from, recipients, msg.as_string())
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(cfg.user, cfg.password)
                s.sendmail(envelope_from, recipients, msg.as_string())
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never raised at them
        log.error("pulse_email_failed", extra={"host": cfg.host, "port": cfg.port,
                                               "user": cfg.user, "error": str(exc)})
        return False, f"Send failed: {exc}{_smtp_hint(exc, cfg)}"
    log.info("pulse_email_sent", extra={"recipients": len(recipients)})
    return True, f"Sent to {len(recipients)} recipient(s)."
