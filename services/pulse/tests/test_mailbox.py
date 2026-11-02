"""The mailbox reader: a Gmail thread as the reader sees it, which conversations it
keeps, which company it attaches them to, what it files, and the queue for the
rest. Google and the model are faked; the filing goes to a fake Register in the
unit tests and to the real one in the last test."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.mail import reader as mailr
from app.mail.extract import ExtractError, Extractor, _parse, normalise_note, stub_note
from app.mail.google import GoogleError, TokenDeadError
from app.mail.match import Firm, match_thread
from app.mail.store import MailStore
from app.mail.threads import own_words, parse_thread, skip_reason

INTERNAL = {"evamfinance.com"}
LABELS = {"Private", "HR"}


# --------------------------------------------------------------------------- #
# Fixtures: Gmail JSON the way users.threads.get?format=full returns it
# --------------------------------------------------------------------------- #
def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid: str, sender: str, to: str, when: datetime, subject: str, text: str, *,
         rfc: str | None = None, cc: str = "", labels: list[str] | None = None,
         html: bool = False, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to},
               {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
               {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": rfc or f"<{mid}@mail.example>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    mime = "text/html" if html else "text/plain"
    return {"id": mid, "threadId": "t-" + mid.split("-")[0], "labelIds": labels or ["INBOX"],
            "internalDate": str(int(when.timestamp() * 1000)), "snippet": text[:60],
            "payload": {"mimeType": "multipart/alternative", "headers": headers,
                        "parts": [{"mimeType": mime, "body": {"data": _b64(text)}}]}}


def _thread(tid: str, *messages: dict[str, Any]) -> dict[str, Any]:
    return {"id": tid, "messages": list(messages)}


D0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

ACME_THREAD = _thread(
    "t1",
    _msg("m1", "Ravi Shah <ravi@acmesolar.in>", "madhu@evamfinance.com", D0,
         "Term loan for the 50 MW Jodhpur project",
         "Hi Madhu,\n\nWe are looking at a term loan of Rs 25 crore for the Jodhpur plant. "
         "Can you share indicative terms by Friday?\n\nRegards,\nRavi"),
    _msg("m2", "Madhu Goud <madhu@evamfinance.com>", "ravi@acmesolar.in", D0 + timedelta(hours=5),
         "Re: Term loan for the 50 MW Jodhpur project",
         "Sure Ravi, indicative terms by Friday. We will need the audited financials.\n\n"
         "On Thu, 10 Sep 2026 Ravi Shah wrote:\n> We are looking at a term loan"),
)


class FakeGoogle:
    """Enough of Google to drive the reader: a mailbox with a few threads, a history
    cursor, and a switch that makes the token die."""

    def __init__(self, mailbox: str, threads: dict[str, dict[str, Any]]) -> None:
        self.mailbox = mailbox
        self.threads = threads
        self.configured = True
        self.dead = False
        self.history_id = "100"
        self.added: list[str] = []        # thread ids "added" since the last history read
        self.calls: list[str] = []
        self.code_profile = mailbox

    async def aclose(self) -> None:
        pass

    async def exchange_code(self, code: str, redirect_uri: str = "postmessage") -> dict[str, Any]:
        self.calls.append(f"exchange:{code}")
        if code == "bad":
            raise GoogleError("Google refused the code: invalid_grant")
        return {"access_token": "acc", "refresh_token": "refr-" + code,
                "scope": "https://www.googleapis.com/auth/gmail.readonly"}

    async def access_token(self, refresh_token: str) -> str:
        if self.dead:
            raise TokenDeadError("invalid_grant")
        return "acc"

    async def profile(self, access: str) -> dict[str, Any]:
        return {"emailAddress": self.code_profile, "historyId": self.history_id}

    async def list_threads(self, access, query, *, page_token=None, max_results=100):
        self.calls.append(f"list:{query}:{page_token}")
        ids = sorted(self.threads)
        if page_token == "p2":
            return ids[1:], None
        return ids[:1], ("p2" if len(ids) > 1 else None)

    async def history(self, access, start_history_id, *, page_token=None):
        self.calls.append(f"history:{start_history_id}")
        if start_history_id == "old":
            raise GoogleError("not found")
        added, self.added = set(self.added), []
        return added, self.history_id, None

    async def thread(self, access, thread_id):
        self.calls.append(f"thread:{thread_id}")
        if thread_id not in self.threads:
            raise GoogleError("not found")
        return self.threads[thread_id]


class FakeRegister:
    def __init__(self, firms: list[Firm], subject=("Entity", None)) -> None:
        self.firms = firms
        self.subject_kind, self.subject_id = subject
        self.filed: list[tuple[str, dict[str, Any]]] = []
        self.intel: list[tuple[str, dict[str, Any]]] = []
        self.domains: list[tuple[str, str]] = []

    async def firms_fn(self, tenant: str) -> list[Firm]:
        return self.firms

    async def subject_fn(self, tenant: str, entity_id: str) -> tuple[str, str]:
        return self.subject_kind, self.subject_id or entity_id

    async def file_fn(self, tenant: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.filed.append((key, payload))
        return {"id": f"int-{len(self.filed)}", **payload}

    async def intel_fn(self, tenant: str, key: str, row: dict[str, Any]) -> Any:
        self.intel.append((key, row))
        return {"id": f"intel-{len(self.intel)}"}

    async def add_domain(self, tenant: str, entity_id: str, domain: str) -> None:
        self.domains.append((entity_id, domain))


def _reader(tmp_path, google, register, **cfg) -> mailr.MailReader:
    store = MailStore(str(tmp_path / "mail.json"))
    config = mailr.MailConfig(internal_domains=INTERNAL, excluded_labels=LABELS, **cfg)
    return mailr.MailReader(store, google, Extractor("stub"), register.firms_fn, register.subject_fn,
                            register.file_fn, register.intel_fn, config, ["EVAM"],
                            add_domain=register.add_domain)


def _connected(reader: mailr.MailReader, email: str, *, since: str | None = None,
               history_id: str = "100") -> None:
    reader.store.put_mailbox("EVAM", email, {
        "email": email, "token": reader.store.seal("refr"), "connected_at": time.time(),
        "since": since, "history_id": history_id,
        "backfill": {"done": since is None, "page": None, "pages": 0, "threads": 0},
        "status": "connected", "error": None, "last_run": None, "last_ok": None,
        "counts": {"seen": 0, "filed": 0, "queued": 0, "skipped": {}, "errors": 0}})


ACME = Firm(id="e-acme", name="Acme Solar", legal_name="Acme Solar Private Limited",
            domains=["acmesolar.in"])
BETA = Firm(id="e-beta", name="Beta Power", legal_name="Beta Power Limited", aliases=["BetaP"])


# --------------------------------------------------------------------------- #
# A thread as the reader sees it
# --------------------------------------------------------------------------- #
def test_a_thread_is_parsed_in_date_order_with_its_own_words_only():
    t = parse_thread(ACME_THREAD)
    assert t is not None and [m.id for m in t.messages] == ["m1", "m2"]
    assert t.subject == "Term loan for the 50 MW Jodhpur project"
    assert "Can you share indicative terms" in t.first.text
    assert "wrote:" not in t.last.text and ">" not in t.last.text, "the quoted history is cut"
    assert "Regards" not in t.first.text, "the signature is cut"
    assert {p.email for p in t.outsiders(INTERNAL)} == {"ravi@acmesolar.in"}
    assert {p.email for p in t.insiders(INTERNAL)} == {"madhu@evamfinance.com"}
    assert t.parties()[0].name == "Ravi Shah"


def test_the_same_conversation_has_one_key_in_every_mailbox():
    """Gmail numbers threads per mailbox; the first message's Message-ID is shared."""
    a = parse_thread(ACME_THREAD)
    other = json.loads(json.dumps(ACME_THREAD))
    other["id"] = "t-in-ravis-colleagues-box"
    for m in other["messages"]:
        m["id"] = "x" + m["id"]
        m["threadId"] = other["id"]
    b = parse_thread(other)
    assert a is not None and b is not None and a.key == b.key


def test_html_mail_is_read_as_text():
    raw = _thread("t9", _msg("h1", "Asha <asha@betapower.com>", "madhu@evamfinance.com", D0,
                             "Sanction letter",
                             "<p>Dear Madhu,</p><p>Sanction letter <b>received</b> today.</p>", html=True))
    t = parse_thread(raw)
    assert t is not None and "Sanction letter received today." in t.first.text


def test_own_words_cuts_outlook_and_gmail_quote_markers():
    assert own_words("Yes, agreed.\n\nFrom: Ravi\nSent: Monday\n> old") == "Yes, agreed."
    assert own_words("Done.\n-----Original Message-----\nblah") == "Done."


@pytest.mark.parametrize("raw,why", [
    (_thread("i1", _msg("a", "Madhu <madhu@evamfinance.com>", "chetan@evamfinance.com", D0,
                        "Lunch", "Canteen at 1?")), "internal only"),
    (_thread("i2", _msg("b", "LinkedIn <notifications-noreply@linkedin.com>", "madhu@evamfinance.com",
                        D0, "You appeared in 9 searches", "Great news")), "automated sender"),
    (_thread("i3", _msg("c", "Ravi <ravi@acmesolar.in>", "madhu@evamfinance.com", D0,
                        "Weekly digest", "News", extra_headers={"List-Unsubscribe": "<x>"})),
     "automated sender"),
    (_thread("i4", _msg("d", "Ravi <ravi@acmesolar.in>", "madhu@evamfinance.com", D0,
                        "Personal", "about my loan", labels=["INBOX", "Private"])), "excluded label"),
    (ACME_THREAD, None),
])
def test_which_threads_the_reader_keeps(raw, why):
    t = parse_thread(raw)
    assert t is not None
    assert skip_reason(t, INTERNAL, LABELS) == why


# --------------------------------------------------------------------------- #
# Which company
# --------------------------------------------------------------------------- #
def test_matching_by_domain_then_name_and_never_by_a_public_domain():
    t = parse_thread(ACME_THREAD)
    m = match_thread(t, [ACME, BETA], INTERNAL)
    assert m and m.firm.id == "e-acme" and m.confidence == 0.95 and m.how == "domain acmesolar.in"
    # the same mail from a Gmail address: only the name in the subject can place it
    gm = _thread("g1", _msg("g", "Ravi <ravi.shah@gmail.com>", "madhu@evamfinance.com", D0,
                            "Beta Power — sanction timelines",
                            "When do we hear from the bank?"))
    m2 = match_thread(parse_thread(gm), [ACME, BETA], INTERNAL)
    assert m2 and m2.firm.id == "e-beta" and m2.confidence == 0.70
    # an alias in the body
    gb = _thread("g2", _msg("g2", "Ravi <ravi.shah@gmail.com>", "madhu@evamfinance.com", D0,
                            "Timelines", "Following up on the BetaP mandate discussion."))
    m3 = match_thread(parse_thread(gb), [ACME, BETA], INTERNAL)
    assert m3 and m3.firm.id == "e-beta" and m3.confidence == 0.55
    # nothing: a short common word never matches
    nm = _thread("g3", _msg("g3", "x@someone.org", "madhu@evamfinance.com", D0, "Hello", "Sun is up."))
    assert match_thread(parse_thread(nm), [ACME, BETA, Firm(id="e-sun", name="Sun")], INTERNAL) is None


# --------------------------------------------------------------------------- #
# The note
# --------------------------------------------------------------------------- #
def test_the_models_answer_is_normalised_and_fences_tolerated():
    note = _parse('```json\n{"summary": "Term loan of Rs 25 Cr for Jodhpur; terms due Friday.", '
                  '"discussed": "term loan", "decisions": [], "asks": ["indicative terms"], '
                  '"amounts": ["Rs 25 crore term loan"], "next_step": "Send terms", '
                  '"next_step_date": "2026-09-11", "signal": "green", "counterparty_name": "Ravi Shah", '
                  '"evidence_quote": "term loan of Rs 25 crore"}\n```')
    assert note["discussed"] == ["term loan"] and note["signal"] == "GREEN"
    assert note["next_step_date"] == "2026-09-11"
    with pytest.raises(ExtractError):
        _parse("Sorry, I cannot help with that.")
    with pytest.raises(ExtractError):
        normalise_note({"summary": ""})
    stub = stub_note(parse_thread(ACME_THREAD))
    assert stub["summary"].startswith("Term loan for the 50 MW Jodhpur project:")
    assert stub["signal"] == "NONE"


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
async def test_a_matched_thread_files_one_interaction_at_the_mails_date(tmp_path):
    google = FakeGoogle("madhu@evamfinance.com", {"t1": ACME_THREAD})
    google.added = ["t1"]
    reg = FakeRegister([ACME, BETA], subject=("Deal", "deal-7"))
    r = _reader(tmp_path, google, reg)
    _connected(r, "madhu@evamfinance.com")
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["filed"] == 1 and stats["queued"] == 0, stats
    key, payload = reg.filed[0]
    assert payload["subject_type"] == "Deal" and payload["subject_id"] == "deal-7"
    assert payload["interaction_type"] == "Email / Written Correspondence"
    assert payload["direction"] == "Outbound", "the last message was ours"
    assert payload["occurred_at"].startswith("2026-09-10T14:00")
    assert payload["source"] == "Email" and payload["source_ref"].startswith("mail:")
    assert payload["performed_by"] == "madhu@evamfinance.com"
    assert payload["contact_name"] == "Ravi Shah"
    assert payload["meta"]["matched_on"] == "domain acmesolar.in"
    assert key.startswith("pulse:mail:EVAM:")
    assert reg.intel == [], "no signal in the stub note, no intelligence row"
    # the mailbox record remembers the pass
    rec = r.store.mailbox("EVAM", "madhu@evamfinance.com")
    assert rec["counts"]["filed"] == 1 and rec["status"] == "connected" and rec["last_ok"]
    # a second pass with nothing new files nothing
    google.added = ["t1"]
    stats2 = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats2["filed"] == 0 and stats2["skipped"].get("already filed") == 1
    assert len(reg.filed) == 1


async def test_a_thread_that_grows_files_the_continuation_under_the_same_company(tmp_path):
    google = FakeGoogle("madhu@evamfinance.com", {"t1": ACME_THREAD})
    google.added = ["t1"]
    reg = FakeRegister([ACME])
    r = _reader(tmp_path, google, reg)
    _connected(r, "madhu@evamfinance.com")
    await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    grown = json.loads(json.dumps(ACME_THREAD))
    grown["messages"].append(_msg("m3", "Ravi Shah <ravi@acmesolar.in>", "madhu@evamfinance.com",
                                  D0 + timedelta(days=2), "Re: Term loan",
                                  "Financials attached. Also, SEBI has sent us a show-cause notice."))
    google.threads["t1"] = grown
    google.added = ["t1"]
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["filed"] == 1 and len(reg.filed) == 2
    key2, p2 = reg.filed[1]
    assert p2["summary"].startswith("Re: ") and p2["direction"] == "Inbound"
    assert "continues an earlier filing" in p2["notes"]
    assert key2 != reg.filed[0][0]
    conv = r.store.conversation("EVAM", parse_thread(grown).key)
    assert conv["interaction_ids"] == ["int-1", "int-2"] and conv["last_message_id"] == "m3"


async def test_an_unplaced_thread_waits_in_the_queue_and_a_person_places_it(tmp_path):
    unknown = _thread("t5", _msg("u1", "Priya <priya@novawind.co.in>", "madhu@evamfinance.com", D0,
                                 "Wind farm refinancing", "We would like to discuss refinancing of our "
                                 "60 MW asset. Can we meet next week?"))
    google = FakeGoogle("madhu@evamfinance.com", {"t5": unknown})
    google.added = ["t5"]
    reg = FakeRegister([ACME, BETA])
    r = _reader(tmp_path, google, reg)
    _connected(r, "madhu@evamfinance.com")
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["queued"] == 1 and reg.filed == []
    q = r.queue("EVAM")
    assert len(q) == 1 and q[0]["subject"] == "Wind farm refinancing"
    assert q[0]["parties"] == [{"email": "priya@novawind.co.in", "name": "Priya"}]
    assert "text" not in q[0], "the queue listing carries an excerpt, not the mail"
    assert r.queue("EVAM", mailbox="someone@evamfinance.com") == []
    # placed by a person, teaching the Register the domain
    row = await r.attach("EVAM", q[0]["key"], "e-beta", by="admin@evamfinance.com", add_domain=True)
    assert row["subject_type"] == "Entity" and row["subject_id"] == "e-beta"
    assert reg.domains == [("e-beta", "novawind.co.in")]
    assert reg.filed[0][1]["meta"]["matched_on"] == "attached by admin@evamfinance.com"
    assert r.queue("EVAM") == []
    # the next pass knows the domain and files a new thread from them by itself
    reg.firms = [ACME, Firm(id="e-beta", name="Beta Power", domains=["novawind.co.in"])]
    second = _thread("t6", _msg("u2", "Priya <priya@novawind.co.in>", "madhu@evamfinance.com",
                                D0 + timedelta(days=1), "Site visit", "Thursday works for the site visit."))
    google.threads["t6"] = second
    google.added = ["t6"]
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["filed"] == 1 and stats["queued"] == 0


async def test_a_refused_filing_is_counted_shown_and_retried_not_fatal(tmp_path):
    """A Register (or model) refusal on one thread must not end the pass silently:
    it is counted, its message is kept for the dialog, the thread is tried again on
    the next passes, and the pass still records success and moves the history on."""
    google = FakeGoogle("madhu@evamfinance.com", {"t1": ACME_THREAD})
    google.added = ["t1"]
    reg = FakeRegister([ACME])
    calls = {"n": 0}

    async def refusing(tenant, key, payload):
        calls["n"] += 1
        raise RuntimeError("422: attendees: value is not a valid list")

    r = _reader(tmp_path, google, reg)
    r._file = refusing
    _connected(r, "madhu@evamfinance.com", history_id="100")
    google.history_id = "101"
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    rec = r.store.mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["errors"] == 1 and stats["filed"] == 0
    assert rec["last_ok"] and rec["error"] is None, "the pass itself succeeded"
    assert rec["history_id"] == "101", "history moves on; the thread is kept on the retry list"
    assert "422" in rec["last_error"]["error"] and rec["last_error"]["thread"] == "t1"
    assert rec["retry"] == {"t1": 1}
    # the next pass tries it again even though history has nothing new
    await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert calls["n"] == 2 and rec["retry"] == {"t1": 2}
    # once the Register accepts, it is filed and the retry entry goes
    r._file = reg.file_fn
    await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert len(reg.filed) == 1 and r.store.mailbox("EVAM", "madhu@evamfinance.com")["retry"] == {}


async def test_ignored_conversations_stay_ignored(tmp_path):
    unknown = _thread("t7", _msg("v1", "x@random.org", "madhu@evamfinance.com", D0, "Hi", "Long time."))
    google = FakeGoogle("madhu@evamfinance.com", {"t7": unknown})
    google.added = ["t7"]
    reg = FakeRegister([ACME])
    r = _reader(tmp_path, google, reg)
    _connected(r, "madhu@evamfinance.com")
    await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    key = r.queue("EVAM")[0]["key"]
    assert r.ignore("EVAM", key) is True
    google.added = ["t7"]
    stats = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert stats["skipped"].get("ignored") == 1 and r.queue("EVAM") == []


async def test_a_dead_token_stops_the_mailbox_and_asks_for_a_reconnect(tmp_path):
    google = FakeGoogle("madhu@evamfinance.com", {"t1": ACME_THREAD})
    google.dead = True
    reg = FakeRegister([ACME])
    r = _reader(tmp_path, google, reg)
    _connected(r, "madhu@evamfinance.com")
    await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    st = r.status("EVAM", "madhu@evamfinance.com")
    assert st["status"] == "reconnect" and "Reconnect" in st["error"] and reg.filed == []
    # the loop leaves it alone until the person reconnects
    assert await r.run_tenant("EVAM") == {}


async def test_the_backfill_pages_through_by_date_and_history_takes_over(tmp_path):
    old = json.loads(json.dumps(ACME_THREAD))
    google = FakeGoogle("madhu@evamfinance.com", {"t1": old, "t2": _thread(
        "t2", _msg("w1", "Asha <asha@betapower.com>", "madhu@evamfinance.com", D0 - timedelta(days=100),
                   "Beta Power — mandate letter", "Please find the signed mandate letter."))})
    reg = FakeRegister([ACME, BETA])
    r = _reader(tmp_path, google, reg, backfill_pages_per_run=1)
    _connected(r, "madhu@evamfinance.com", since="2026-01-01")
    s1 = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    rec = r.store.mailbox("EVAM", "madhu@evamfinance.com")
    assert s1["filed"] == 1 and rec["backfill"]["done"] is False and rec["status"] == "backfill"
    assert any(c.startswith("list:after:2026/01/01") for c in google.calls)
    s2 = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    rec = r.store.mailbox("EVAM", "madhu@evamfinance.com")
    assert s2["filed"] == 1 and rec["backfill"]["done"] is True and rec["status"] == "connected"
    assert {p["subject_type"] for _, p in reg.filed} == {"Entity"}
    # an expired history id falls back to a dated listing instead of failing
    rec["history_id"] = "old"
    r.store.put_mailbox("EVAM", "madhu@evamfinance.com", rec)
    s3 = await r.run_mailbox("EVAM", "madhu@evamfinance.com")
    assert s3["errors"] == 0 and r.store.mailbox("EVAM", "madhu@evamfinance.com")["history_id"] == "100"


async def test_connect_refuses_another_persons_mailbox_and_seals_the_token(tmp_path):
    google = FakeGoogle("madhu@evamfinance.com", {})
    reg = FakeRegister([])
    r = _reader(tmp_path, google, reg)
    google.code_profile = "someone.else@gmail.com"
    with pytest.raises(mailr.MailError):
        await r.connect("EVAM", "madhu@evamfinance.com", "code-1")
    google.code_profile = "madhu@evamfinance.com"
    st = await r.connect("EVAM", "madhu@evamfinance.com", "code-2", since_months=6, display_name="Madhu")
    assert st["connected"] and st["status"] == "connected" and st["since"]
    rec = r.store.mailbox("EVAM", "madhu@evamfinance.com")
    assert rec["token"] != "refr-code-2" and r.store.open_token(rec["token"]) == "refr-code-2"
    # the store round-trips with the same key file
    again = MailStore(str(tmp_path / "mail.json"))
    assert again.open_token(again.mailbox("EVAM", "madhu@evamfinance.com")["token"]) == "refr-code-2"
    with pytest.raises(GoogleError):
        await r.connect("EVAM", "madhu@evamfinance.com", "bad")
    assert r.disconnect("EVAM", "madhu@evamfinance.com") is True
    assert r.status("EVAM", "madhu@evamfinance.com")["connected"] is False


# --------------------------------------------------------------------------- #
# The routes, and a real filing
# --------------------------------------------------------------------------- #
H = {"X-Tenant": "EVAM"}


@pytest.mark.asyncio
async def test_the_routes_know_the_caller_and_who_oversees(pulse):
    assert (await pulse.get("/v1/mail/me", headers=H)).status_code == 401
    me = await pulse.get("/v1/mail/me", headers={**H, "X-User-Email": "madhu@evamfinance.com"})
    assert me.status_code == 200 and me.json()["connected"] is False
    assert me.json()["configured"] is False, "no Google client on the test box"
    assert me.json()["engine"] == "stub"
    r = await pulse.post("/v1/mail/connect", json={"code": "x"},
                         headers={**H, "X-User-Email": "madhu@evamfinance.com"})
    assert r.status_code == 503 and "PULSE_GOOGLE_CLIENT_ID" in r.json()["message"]
    cov = await pulse.get("/v1/mail/coverage", headers={**H, "X-User-Email": "madhu@evamfinance.com",
                                                        "X-User-Roles": "BDRM"})
    assert cov.status_code == 403
    cov = await pulse.get("/v1/mail/coverage", headers={**H, "X-User-Email": "admin@evamfinance.com",
                                                        "X-User-Roles": "Admin"})
    assert cov.status_code == 200 and cov.json()["items"] == []
    q = await pulse.get("/v1/mail/queue", headers={**H, "X-User-Email": "madhu@evamfinance.com"})
    assert q.status_code == 200 and q.json()["total"] == 0


@pytest.mark.asyncio
async def test_a_conversation_lands_on_the_companys_timeline_in_the_register(pulse, register_direct):
    """End to end with the real Register: a company with the outside domain, a thread
    from that domain, one interaction on the company at the mail's date."""
    code = f"MAIL{uuid.uuid4().hex[:6].upper()}"
    ent = (await register_direct.post("/v1/entities", json={
        "code": code, "legal_name": f"Acme Solar {code} Private Limited",
        "display_name": f"Acme Solar {code}", "email_domains": [f"{code.lower()}.in"]})).json()
    # A fresh Message-ID per run: the Idempotency-Key is the conversation's identity,
    # and the test database keeps earlier runs' rows.
    raw = _thread("tr1", _msg("r1", f"Ravi <ravi@{code.lower()}.in>", "madhu@evamfinance.com", D0,
                              "Term loan for Jodhpur", "Looking for Rs 25 crore term loan. Terms by Friday?",
                              rfc=f"<{code.lower()}-r1@mail.example>"))
    app = pulse._transport.app  # noqa: SLF001 - the test reaches the app the fixture built
    reader: mailr.MailReader = app.state.mail_reader
    google = FakeGoogle("madhu@evamfinance.com", {"tr1": raw})
    google.added = ["tr1"]
    reader.google = google
    _connected(reader, "madhu@evamfinance.com")
    r = await pulse.post("/v1/mail/read-now", headers={**H, "X-User-Email": "madhu@evamfinance.com"})
    assert r.status_code == 202
    for _ in range(100):
        await asyncio.sleep(0.05)
        rec = reader.store.mailbox("EVAM", "madhu@evamfinance.com")
        if rec.get("last_pass"):
            break
    assert rec["last_pass"]["filed"] == 1, rec["last_pass"]
    rows = (await register_direct.get("/v1/interactions", params={"entity_id": ent["id"]})).json()
    items = rows.get("items") if isinstance(rows, dict) else rows
    assert len(items) == 1
    it = items[0]
    assert it["subject_type"] == "Entity" and it["source"] == "Email"
    assert it["interaction_type"] == "Email / Written Correspondence"
    assert it["occurred_at"].startswith("2026-09-10")
    assert it["performed_by"] == "madhu@evamfinance.com" and it["contact_name"] == "Ravi"
    assert it["key_intel"]["summary"].startswith("Term loan for Jodhpur:")
    reader.disconnect("EVAM", "madhu@evamfinance.com")
