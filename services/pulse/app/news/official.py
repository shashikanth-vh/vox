"""Official sources — what a regulator has put on record, read once for every firm.

News is what someone wrote about a firm; these are what a regulator DID. Two sources a
server can read without a login or a captcha:

  * SEBI's RSS feed (sebi.gov.in/sebirss.xml) — the latest orders, recovery proceedings,
    circulars and press releases. Only the ENFORCEMENT items are kept (orders, notices
    of demand and attachment, recovery): a circular names no firm.
  * IBBI's public announcements (ibbi.gov.in/en/public-announcement) — every CIRP,
    liquidation, voluntary liquidation and pre-pack started in India, newest first,
    twenty a page. Paging back through it is the history; the first pages are the news.

THE ECONOMICS ARE THE POINT. A news search costs one request per firm; these cost one
request for the whole book, because the feed is fetched once and every watched firm is
matched against it. They never touch Google's hourly budget.

MATCHING IS EXACT, because an official record about the wrong company is worse than
none. IBBI names the corporate debtor by its registered name ("VATIKA IT PARKS PRIVATE
LIMITED"), so a firm matches only when its legal name (or a desk alias) is the SAME
name once the company-form words are stripped — never because a short name appears
inside a longer one. SEBI orders are titled "in the matter of <name>", so a firm or a
key person matches on the whole name as words, and a person only on a name of two or
more words.

  * NSE's corporate announcements — every listed company's filings for a date range in
    one request, each naming the company. Only the ADVERSE kinds are kept (defaults,
    insolvency, regulatory actions and orders, auditor and director resignations,
    credit-rating actions, pledges): a board-meeting notice is not news.

Sources a server cannot read are not pretended: checked from the test box on 16 Oct,
MCA and BSE refuse a server outright, and NCLT's order and case search needs a captcha.
They are named as not covered (``NOT_COVERED``) so the screen can say so.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 - parsing responses from named sources
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import httpx
from evam_backend_core.logging import get_logger

from app.news.evidence import read_evidence
from app.news.triage import triage

log = get_logger("pulse.official")

SEBI_RSS = "https://www.sebi.gov.in/sebirss.xml"
IBBI_PA = "https://ibbi.gov.in/en/public-announcement"
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           "&from_date=%s&to_date=%s")
UA = {"User-Agent": "Mozilla/5.0 (PRISM-NewsRadar/1.0)"}
# NSE answers its API only to a browser-like client that says where it came from.
NSE_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Referer": "https://www.nseindia.com/", "Accept": "application/json"}

# Official records a server cannot read today, and why — shown, never silently absent.
NOT_COVERED = {
    "MCA": "refuses requests from servers; company status needs a data.gov.in key or a "
           "paid MCA data provider",
    "NCLT / eCourts": "order and case search needs a captcha (insolvency admissions are "
                      "covered through IBBI)",
    "BSE": "refuses requests from servers (listed firms are covered through NSE)",
}

# The words that say what FORM a company takes, not which company it is.
_FORM = re.compile(
    r"\b(private|pvt|limited|ltd|llp|plc|inc|incorporated|corporation|corp|company|co|"
    r"opc|the)\b", re.I)


def norm_company(name: str) -> str:
    """A registered name reduced to what identifies the company: lower case, the
    company-form words and punctuation gone, single spaces. "Vatika IT Parks Pvt. Ltd."
    and "VATIKA IT PARKS PRIVATE LIMITED" are the same string here."""
    s = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower().replace("(india)", " india "))
    s = _FORM.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


def names_in(text: str, name: str) -> bool:
    """``name`` printed in ``text`` as whole words."""
    needle = _words(name)
    return needle.strip() != "" and needle in _words(text)


# --------------------------------------------------------------------------- #
# SEBI
# --------------------------------------------------------------------------- #
def _sebi_when(pub: str) -> str:
    """SEBI writes "15 Oct, 2026 +0530" — not RFC 822, so the stdlib parser refuses it."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s+(\d{4})", pub or "")
    if not m:
        return ""
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                 "%d %b %Y").strftime("%Y%m%d")
    except ValueError:
        return ""


def parse_sebi(xml_text: str) -> list[dict[str, Any]]:
    """The enforcement items of SEBI's feed, as records. Circulars and press releases
    are dropped: they name no firm, and a match on one would be a coincidence."""
    out: list[dict[str, Any]] = []
    for item in ET.fromstring(xml_text).iter("item"):  # noqa: S314 - SEBI's own feed
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or "/enforcement/" not in link:
            continue
        favourable = bool(re.search(r"\b(release|exemption|revocation|withdrawal)\b",
                                    title, re.I))
        out.append({
            "title": title, "url": link, "source": "SEBI", "via": "SEBI",
            "when": _sebi_when(item.findtext("pubDate") or ""), "snippet": "",
            "official": True, "record_type": ("recovery" if "/recovery-" in link
                                              else "order"),
            # An order lifting an attachment is still a record that there was one.
            "severity": "BAD" if favourable else "UGLY",
            "category": "regulatory",
        })
    return out


# --------------------------------------------------------------------------- #
# IBBI
# --------------------------------------------------------------------------- #
class _Rows(HTMLParser):
    """Every table row on the page as (cell texts, first link in each cell). The page is
    a Drupal view; parsing by HEADER TEXT rather than position keeps a column added or
    reordered from silently shifting a debtor's name into the applicant's place."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[str], list[str]]] = []
        self._row: tuple[list[str], list[str]] | None = None
        self._cell: list[str] | None = None
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = ([], [])
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._href = [], ""
        elif tag == "a" and self._cell is not None and not self._href:
            self._href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row[0].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._row[1].append(self._href)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row[0]:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _col(header: list[str], *words: str) -> int:
    for i, h in enumerate(header):
        low = h.lower()
        if all(w in low for w in words):
            return i
    return -1


def _ibbi_when(s: str) -> str:
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s or "")
    return f"{m.group(3)}{int(m.group(2)):02d}{int(m.group(1)):02d}" if m else ""


def parse_ibbi(html_text: str, base: str = IBBI_PA) -> list[dict[str, Any]]:
    """One record per public announcement on the page. Raises ValueError when the page
    has no announcement table — a changed layout must be reported, not read as "no
    insolvencies today"."""
    p = _Rows()
    p.feed(html_text or "")
    header_at = next((i for i, (cells, _) in enumerate(p.rows)
                      if _col(cells, "corporate debtor") >= 0), -1)
    if header_at < 0:
        raise ValueError("IBBI page has no 'Name of Corporate Debtor' column — layout changed?")
    header = p.rows[header_at][0]
    c_type, c_date = _col(header, "type"), _col(header, "date of announcement")
    c_debtor, c_app = _col(header, "corporate debtor"), _col(header, "applicant")
    c_ip = _col(header, "insolvency professional")
    c_pa = _col(header, "public announcement")
    out: list[dict[str, Any]] = []
    for cells, links in p.rows[header_at + 1:]:
        if len(cells) <= c_debtor or not cells[c_debtor]:
            continue
        kind = cells[c_type] if 0 <= c_type < len(cells) else ""
        debtor = cells[c_debtor]
        href = next((links[i] for i in ([c_pa] if c_pa >= 0 else []) + list(range(len(links)))
                     if 0 <= i < len(links) and links[i]), "")
        url = urllib.parse.urljoin(base, href) if href else base
        voluntary = "voluntary" in kind.lower()
        applicant = cells[c_app] if 0 <= c_app < len(cells) else ""
        ip = cells[c_ip] if 0 <= c_ip < len(cells) else ""
        short_kind = re.sub(r"^public announcement of\s+", "", kind, flags=re.I) or "Announcement"
        out.append({
            "title": f"{short_kind}: {debtor}",
            "url": url, "source": "IBBI", "via": "IBBI",
            "when": _ibbi_when(cells[c_date] if 0 <= c_date < len(cells) else ""),
            "snippet": "; ".join(x for x in (f"applicant {applicant}" if applicant else "",
                                             f"insolvency professional {ip}" if ip else "")
                                 if x),
            "debtor": debtor, "official": True, "record_type": "insolvency",
            # A firm winding itself up is not a creditor forcing it to — still material.
            "severity": "BAD" if voluntary else "UGLY",
            "category": "insolvency",
        })
    return out


# --------------------------------------------------------------------------- #
# NSE
# --------------------------------------------------------------------------- #
# NSE's announcement subjects (``desc``) that are adverse, and how much. Matched on the
# subject the company filed under; the attached text is read by triage as well, so a
# "General Updates" that reports a default is still caught.
NSE_UGLY = re.compile(
    r"default|insolvency|bankruptcy|liquidation|winding up|fraud|action\(s\) (taken|"
    r"initiated)|orders? passed|suspension|search and seizure|forensic", re.I)
# Not "credit rating": most are reaffirmations and upgrades. A downgrade or withdrawal
# is caught by triage reading the filing's text, below.
NSE_BAD = re.compile(
    r"resignation|cessation|auditor|pledge|encumbrance|litigation|dispute|"
    r"penalty|show cause|notice from|investigation|one time settlement|restructuring",
    re.I)


# Subjects that cover good and bad alike — "Granting/withdrawal/surrender/cancellation/
# suspension of key licenses" filed a USDA attestation as UGLY (cipher-test, 16 Oct).
# For these the filing's own text decides.
NSE_MIXED = re.compile(r"granting/withdrawal|grant(ing)? or withdrawal", re.I)


def _file_words(url: str) -> str:
    """The attachment's file name as words. NSE's text often says only "Action(s) taken
    or orders passed"; the file is "…_Reg_30_Intimation_DGGI_Inspection_Signed.pdf"."""
    name = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return " ".join(w for w in re.split(r"[_\-\s]+", name) if w.isalpha() and len(w) > 2)


def _nse_when(s: str) -> str:
    try:
        return datetime.strptime((s or "").split()[0], "%d-%b-%Y").strftime("%Y%m%d")
    except (ValueError, IndexError):
        return ""


def parse_nse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ADVERSE announcements, as records. Routine filings (board meetings, ESOPs,
    investor meets) are dropped here, not filed and hidden later."""
    out: list[dict[str, Any]] = []
    for r in rows or []:
        company = str(r.get("sm_name") or "").strip()  # NSE's registered name
        subject = re.sub(r"\s+", " ", str(r.get("desc") or "")).strip()
        text = re.sub(r"\s+", " ", str(r.get("attchmntText") or "")).strip()
        url = str(r.get("attchmntFile") or "")
        if not company or not subject:
            continue
        # What the filing says, plus what its file is called ("DGGI Inspection").
        said = f"{text} {_file_words(url)}".strip()
        if NSE_MIXED.search(subject):
            v = triage(said)
            sev = v.severity if v.category in ("adverse", "stress") else ""
            if not sev and re.search(r"\b(withdraw\w*|surrender\w*|cancel\w*|suspen\w*|"
                                     r"revok\w*)\b", said, re.I):
                sev = "BAD"
        elif NSE_UGLY.search(subject):
            sev = "UGLY"
        elif NSE_BAD.search(subject):
            sev = "BAD"
        else:
            # A routine subject whose own text is adverse ("General Updates: intimation
            # of default in payment of interest"; "Credit Rating: downgraded to BB").
            # Only hard-adverse or the stress ladder: a filing's "court-convened meeting"
            # or "delay in submission" is routine, not news.
            v = triage(f"{subject}. {said}")
            sev = v.severity if v.category in ("adverse", "stress") else ""
        if not sev:
            continue
        out.append({
            "title": f"{company}: {subject}" + (f" — {text[:180]}" if text else ""),
            "url": url or "https://www.nseindia.com/",
            "source": "NSE", "via": "NSE", "when": _nse_when(str(r.get("an_dt") or "")),
            "snippet": said[:400], "company": company, "official": True,
            "record_type": "exchange filing", "severity": sev, "category": "exchange",
        })
    return out


async def fetch_nse(client: httpx.AsyncClient, days: int = 2,
                    timeout: float = 30.0) -> list[dict[str, Any]]:
    """The last ``days`` of announcements for every listed company, adverse only."""
    today = datetime.now()
    frm = (today - timedelta(days=max(1, days))).strftime("%d-%m-%Y")
    resp = await client.get(NSE_ANN % (frm, today.strftime("%d-%m-%Y")),
                            headers=NSE_HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError("NSE answered something other than a list of announcements")
    return parse_nse(data)


# --------------------------------------------------------------------------- #
# Fetch + match
# --------------------------------------------------------------------------- #
async def fetch_sebi(client: httpx.AsyncClient, timeout: float = 20.0) -> list[dict[str, Any]]:
    resp = await client.get(SEBI_RSS, headers=UA, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return parse_sebi(resp.text)


async def fetch_ibbi(client: httpx.AsyncClient, pages: int = 3, *, timeout: float = 25.0,
                     spacing_s: float = 2.0) -> tuple[list[dict[str, Any]], int]:
    """The newest ``pages`` pages of announcements, politely spaced. Returns the
    records and how many pages were read; a page that fails after the first ends the
    walk with what was read (the caller reports the shortfall)."""
    out: list[dict[str, Any]] = []
    read = 0
    for page in range(max(1, pages)):
        url = IBBI_PA if page == 0 else f"{IBBI_PA}?page={page}"
        try:
            resp = await client.get(url, headers=UA, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            rows = parse_ibbi(resp.text)
        except Exception:
            if page == 0:
                raise
            log.warning("pulse_ibbi_page_failed", extra={"page": page})
            break
        read += 1
        if not rows:
            break
        out.extend(rows)
        if page + 1 < pages and spacing_s:
            await asyncio.sleep(spacing_s)
    return out, read


def match_official(records: list[dict[str, Any]], firm: dict[str, Any]) -> list[dict[str, Any]]:
    """The records about THIS firm or one of its key people, each with ``matched`` (the
    name it was found under) and, for a person, ``about``.

    IBBI: the debtor's registered name must equal one of the firm's names once the
    company-form words are stripped. SEBI: the firm's full name, or a key person's
    full name of two or more words, printed in the order's title as whole words."""
    names = [n for n in [firm.get("legal_name"), firm.get("name"),
                         *(firm.get("aliases") or [])] if n and str(n).strip()]
    normed = {norm_company(str(n)): str(n) for n in names if len(norm_company(str(n))) >= 4}
    people = [p for p in (firm.get("people") or [])
              if len(str(p.get("name") or "").split()) >= 2]
    out: list[dict[str, Any]] = []
    for r in records:
        if r.get("source") in ("IBBI", "NSE"):
            # Both name the company by its registered name: exact, after the form words.
            hit = normed.get(norm_company(r.get("debtor") or r.get("company") or ""))
            if hit:
                out.append({**r, "matched": hit})
            continue
        title = r.get("title") or ""
        # Both sides without the company-form words: the order says "XYZ Ltd", the
        # register "XYZ Limited". A one-word name must be six letters or more.
        bare = norm_company(title)
        hit = next((orig for key, orig in normed.items()
                    if (len(key.split()) >= 2 or len(key) >= 6) and names_in(bare, key)),
                   None)
        if hit:
            out.append({**r, "matched": hit})
            continue
        person = next((p for p in people if names_in(title, str(p["name"]))), None)
        if person:
            role = str(person.get("role") or "").strip()
            label = f"{role} · {person['name']}" if role else str(person["name"])
            out.append({**r, "matched": str(person["name"]), "about": label})
    forums = {"IBBI": ["IBBI", "NCLT"], "SEBI": ["SEBI"], "NSE": ["Exchange"]}
    for r in out:
        ev = read_evidence(r.get("title", ""), r.get("snippet", ""))
        # The company's own disclosure that something WAS done to it is an action:
        # an IBBI announcement, an exchange filing of actions, orders or a default.
        acted = r.get("source") == "IBBI" or (
            r.get("source") == "NSE" and r.get("severity") == "UGLY"
            and ev["nature"] in ("report", "allegation"))
        r["evidence"] = {**ev, "basis": "official record",
                         # Named in the filing (DGGI, SEBI …) first, then where it was filed.
                         "forums": list(dict.fromkeys(
                             [*(ev["forums"] if r.get("source") == "NSE" else []),
                              *forums.get(str(r.get("source")), [])])),
                         **({"nature": "action", "stage": "pending"} if acted else {})}
    return out
