"""The News Radar: search, classify, email, schedule.

Nothing here touches the network. The three upstreams are stubbed at the HTTP layer, so
these tests pin the behaviour that matters — the merge, the de-duplication, the severity
rules, the coalescing, and every route's contract — without depending on what Google News
happens to be serving today.
"""

from __future__ import annotations

import asyncio
import json
import time as time_mod
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.news import schedules as sched
from app.news.mailer import SmtpConfig, digest_html, send_email
from app.news.search import NewsSearch, classify
from app.news.triage import triage

GOOGLE_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Acme Solar wins 300 MW order - Mint</title>
<link>https://mint.example/acme-order</link><source>Mint</source>
<pubDate>Tue, 05 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>Acme Solar faces CBI probe over land deal</title>
<link>https://et.example/acme-probe</link><source>ET</source>
<pubDate>Mon, 04 Aug 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

BING_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Acme Solar wins 300 MW order</title>
<link>https://other.example/same-story</link>
<pubDate>Tue, 05 Aug 2026 10:00:00 GMT</pubDate></item>
<item><title>Acme Solar commissions new line</title>
<link>https://biz.example/acme-line</link>
<pubDate>Sun, 03 Aug 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

GDELT_JSON = json.dumps({"articles": [
    {"title": "Acme Solar signs PPA", "url": "https://gd.example/acme-ppa",
     "domain": "gd.example", "seendate": "20260802T000000Z"}]})


# The genuine class, captured before any test patches the name. A helper that reads
# httpx.AsyncClient at call time can pick up ANOTHER test's stub and quietly wrap it —
# which is how a "no upstream answers" test ended up answering.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _stub_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "news.google.com" in host:
            return httpx.Response(200, text=GOOGLE_RSS)
        if "bing.com" in host:
            return httpx.Response(200, text=BING_RSS)
        if "gdeltproject.org" in host:
            return httpx.Response(200, text=GDELT_JSON)
        return httpx.Response(404, text="unexpected host")
    return httpx.MockTransport(handler)


@pytest.fixture()
def stub_upstreams(monkeypatch):
    """Point every outbound search at the canned feeds above."""
    transport = _stub_transport()
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr("app.news.search.httpx.AsyncClient", patched)
    return transport


# --------------------------------------------------------------------------- #
# The classifier — whole words, in the desk's vocabulary
# --------------------------------------------------------------------------- #
def test_severity_reads_the_headline_the_way_the_desk_would():
    # Hard-adverse wins outright, even when the sentence also sounds like good news.
    assert classify("Acme Solar faces CBI probe over land deal") == "UGLY"
    assert classify("Promoter arrested in loan fraud case") == "UGLY"
    # A clear positive outranks a routine watch word ("order" beats "delay" here).
    assert classify("Acme wins 300 MW order despite delay") == "GOOD"
    assert classify("Court summons issued to Acme") == "BAD"


def test_a_word_inside_another_word_is_not_a_signal():
    """The reason the lists are whole-word: substring matching turned "firm" into an FIR
    and "afraid" into a raid, which mislabels a company on the desk's screen."""
    assert classify("Acme, a firm in Pune, expands capacity") != "UGLY"
    assert classify("Investors were afraid of the tariff change") != "UGLY"
    assert classify("The first phase is complete") != "UGLY"


# --------------------------------------------------------------------------- #
# Triage: the guards and the context flip
# --------------------------------------------------------------------------- #
def test_a_cleared_name_is_not_an_adverse_hit():
    """Without this, every exoneration files as UGLY — and a radar that cries fraud over
    "cleared of fraud" is one the desk stops reading."""
    assert classify("Promoter cleared of fraud charges by court") != "UGLY"
    assert classify("Acme acquitted in the siphoning case") != "UGLY"
    assert classify("Company denies default on term loan") != "UGLY"
    assert classify("Regulator gives clean chit on related-party dealings") != "UGLY"
    # But the negator must be NEAR the keyword, not merely somewhere in the sentence.
    far = ("Court cleared the merger last year, and in a separate matter the promoter "
           "was arrested this week over an alleged loan fraud at the group")
    assert classify(far) == "UGLY"


def test_the_wrong_sense_of_a_word_is_not_a_signal():
    """Half this book is charging infrastructure. "Charge" has to mean a criminal charge,
    not a plug."""
    assert classify("Zeon opens 40 new EV charging stations in Pune") != "UGLY"
    assert classify("Battery charging capacity doubled at the depot") != "UGLY"
    assert classify("Tariff applies by default to open access consumers") != "UGLY"
    assert classify("The plant is doing fine after the monsoon") != "BAD"
    # A blocked sense must not shadow a real hit elsewhere in the same headline.
    assert classify("EV charging firm arrested over loan fraud") == "UGLY"


def test_good_looking_news_is_a_review_flag_on_a_live_borrower():
    """The heart of it: polarity is keyword x OUR RELATIONSHIP. Fresh debt is a win for a
    name we are chasing and a warning about a name we have lent to."""
    head = "Acme Solar raises fresh debt of Rs 400 crore"
    assert triage(head, live=False).severity == "GOOD"
    v = triage(head, live=True)
    assert (v.severity, v.category) == ("BAD", "context-review")
    assert "dilutes our cover" in v.reason          # the screen shows this on the flag

    ots = triage("Acme settles with lender in one-time settlement", live=True)
    assert ots.category == "context-review"
    assert "haircut" in ots.reason


def test_a_hard_adverse_hit_outranks_the_context_flip():
    """A borrower under enforcement is UGLY, not a polite review flag."""
    assert classify("Acme raises fresh debt weeks after ED probe", live=True) == "UGLY"


def test_policy_news_fires_without_a_company_name():
    """The item that re-prices a portfolio never mentions the borrower."""
    assert classify("SERC issues revised tariff order for FY27") == "POLICY"
    assert classify("MNRE notification revises ALMM list") == "POLICY"
    assert classify("Net metering policy changed for rooftop consumers") == "POLICY"


def test_the_stress_ladder_reads_as_a_warning():
    assert classify("Lender flags covenant breach at Acme") == "BAD"
    assert classify("Acme classified as SMA-2 by two banks") == "BAD"
    assert classify("Auditor resigns at Acme weeks before results") == "BAD"
    assert classify("Promoter pledge rises to 62 per cent") == "BAD"


def test_enforcement_euphemisms_read_as_adverse():
    """These are how recovery is actually reported, long before anyone writes "default"."""
    for h in ["Lookout circular issued against the promoter",
              "Bank invokes SARFAESI against Acme's Gujarat plant",
              "NCLT admits insolvency plea against Acme",
              "DGGI alleges fake ITC claims at the group"]:
        assert classify(h) == "UGLY", h


def test_a_phrase_matches_however_it_is_punctuated():
    """The press writes it both ways; a rule that knows one spelling misses half."""
    assert classify("Acme agrees one-time settlement with lenders", live=True) == "BAD"
    assert classify("Acme agrees one time settlement with lenders", live=True) == "BAD"


# --------------------------------------------------------------------------- #
# Search: merge, de-duplicate, cache, coalesce
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_merges_three_sources_and_dedupes_the_same_story(stub_upstreams):
    engine = NewsSearch(cache_ttl_s=0)
    arts = await engine.search("Acme Solar")
    titles = [a["title"] for a in arts]
    # Google's " - Mint" suffix is stripped: the source has its own column.
    assert "Acme Solar wins 300 MW order" in titles
    # The SAME story from Google and Bing under two URLs is listed once.
    assert titles.count("Acme Solar wins 300 MW order") == 1
    # All three sources contributed.
    assert {a["via"] for a in arts} == {"Google News", "Bing News", "GDELT"}
    # Severity rides along, newest first.
    probe = next(a for a in arts if "probe" in a["title"])
    assert probe["severity"] == "UGLY"
    assert [a["when"] for a in arts] == sorted((a["when"] for a in arts), reverse=True)


@pytest.mark.asyncio
async def test_a_disabled_gdelt_is_a_clean_skip_not_a_failure(stub_upstreams):
    engine = NewsSearch(disable_gdelt=True, cache_ttl_s=0)
    arts = await engine.search("Acme Solar")
    assert arts, "Google News + Bing still answer"
    assert "GDELT" not in {a["via"] for a in arts}


@pytest.mark.asyncio
async def test_one_dead_source_never_fails_the_search(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "news.google.com" in request.url.host:
            raise httpx.ConnectTimeout("google is down")
        if "bing.com" in request.url.host:
            return httpx.Response(200, text=BING_RSS)
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": transport}))
    arts = await NewsSearch(cache_ttl_s=0).search("Acme Solar")
    assert arts, "Bing alone still answers"
    assert {a["via"] for a in arts} == {"Bing News"}


def _feeds_ok(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=GOOGLE_RSS if "google" in request.url.host
                              else BING_RSS)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))


def _all_upstreams_dead(monkeypatch, exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc or httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))


@pytest.mark.asyncio
async def test_a_total_outage_is_reported_not_returned_as_no_news(monkeypatch):
    """The whole point of the source report. "Nothing found" and "nothing reached" are
    the same empty list, and a desk sweeping 400 firms cannot tell them apart — one is
    a quiet week, the other is a container with no way out."""
    _all_upstreams_dead(monkeypatch)
    arts, sources = await NewsSearch(cache_ttl_s=0).search_detail("Acme Solar")
    assert arts == []
    assert [s["name"] for s in sources] == ["Google News", "GDELT", "Bing News"]
    assert all(s["ok"] is False for s in sources)
    # The reason has to be a sentence someone can act on, not a class name.
    assert "cannot reach the source" in sources[0]["error"]
    assert "name resolution" in sources[0]["error"]


@pytest.mark.asyncio
async def test_a_timeout_still_says_something_when_it_stringifies_to_nothing(monkeypatch):
    """httpx.ReadTimeout('') is the commonest container failure and str() gives ''. A
    blank reason on the screen is no better than no reason at all."""
    _all_upstreams_dead(monkeypatch, httpx.ReadTimeout(""))
    _, sources = await NewsSearch(cache_ttl_s=0).search_detail("Acme Solar")
    assert all("did not answer in time" in s["error"] for s in sources)


@pytest.mark.asyncio
async def test_a_partial_answer_is_not_an_outage(monkeypatch):
    """One source down while another answers is normal — it must not read as a fault."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "bing.com" in request.url.host:
            return httpx.Response(200, text=BING_RSS)
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": transport}))
    arts, sources = await NewsSearch(cache_ttl_s=0).search_detail("Acme Solar")
    assert arts
    by = {s["name"]: s for s in sources}
    assert by["Bing News"]["ok"] and by["Bing News"]["count"] == 2
    assert by["Google News"]["ok"] is False


@pytest.mark.asyncio
async def test_an_outage_is_never_cached(monkeypatch):
    """Caching a total failure for the TTL would keep the radar blind for fifteen
    minutes after the network came back — the one case where a retry must go out."""
    state = {"dead": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["dead"]:
            raise httpx.ConnectError("down")
        if "news.google.com" in request.url.host:
            return httpx.Response(200, text=GOOGLE_RSS)
        return httpx.Response(200, text="<rss><channel></channel></rss>")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": transport}))
    engine = NewsSearch(disable_gdelt=True, cache_ttl_s=900)
    assert await engine.search("Acme Solar") == []
    state["dead"] = False
    assert await engine.search("Acme Solar"), "the retry must reach the network again"


@pytest.mark.asyncio
async def test_concurrent_searches_for_one_term_cost_one_fetch(monkeypatch):
    """An all-firms sweep asks for the same names at once. Twenty callers must not
    become twenty upstream fetches."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "news.google.com" in request.url.host:
            calls["n"] += 1
            return httpx.Response(200, text=GOOGLE_RSS)
        return httpx.Response(200, text="<rss><channel></channel></rss>")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": transport}))
    engine = NewsSearch(disable_gdelt=True)
    results = await asyncio.gather(*[engine.search("Acme Solar") for _ in range(5)])
    assert all(r == results[0] for r in results)
    assert calls["n"] == 1, f"expected one upstream fetch, got {calls['n']}"


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def test_email_is_off_until_it_is_configured_and_says_so():
    ok, msg = send_email(SmtpConfig(), ["a@b.com"], "hi", "<p>x</p>")
    assert ok is False
    assert "not configured" in msg.lower()


def test_a_digest_names_the_source_and_links_every_article():
    html = digest_html("Acme Solar", [
        {"title": "Acme wins order", "url": "https://x.example/1", "source": "Mint",
         "via": "Google News", "when": "20260805", "severity": "GOOD"}])
    assert "https://x.example/1" in html and "Acme wins order" in html
    assert "Mint" in html and "Google News" in html


def test_a_digest_of_nothing_says_so_rather_than_rendering_an_empty_table():
    assert "No articles in this window." in digest_html("Acme", [])


def test_the_digest_names_the_brand_once_and_leads_each_card_with_the_firm():
    """"company name should be bigger than EVAM or PRISM": the masthead appears ONCE
    per email (it used to repeat above every firm), and each firm's name is the
    largest text on its card."""
    from app.news.mailer import digest_page
    page = digest_page([
        ("Beta Wind", [{"title": "Beta Wind fined", "url": "https://x/2", "source": "ET",
                        "via": "Google News", "when": "20261009", "severity": "BAD"}]),
        ("Acme Solar", [{"title": "Acme probe", "url": "https://x/1", "source": "Mint",
                         "via": "Bing News", "when": "20261008", "severity": "UGLY"}]),
    ], "2026-10-06", "2026-10-13", adverse_only=True)
    assert page.count("NEWS RADAR") == 1 and "News digest &middot;" not in page
    assert page.index(">Acme Solar</div>") < page.index(">Beta Wind</div>"), \
        "the firm with UGLY news comes first"
    import re as _re
    assert _re.search(r'font-size:20px;font-weight:700;line-height:1\.25">(<a [^>]*>[^<]*</a>)?'
                      r'Acme Solar</div>', page), "the firm name is the card's headline"
    assert "6 Oct 2026 to 13 Oct 2026" in page and "8 Oct 2026" in page
    assert "Adverse news digest" in page and "2 firms" in page


def test_an_old_story_the_search_resurfaced_is_marked_older_not_passed_off_as_new():
    from app.news.mailer import digest_page
    page = digest_page([("Tarak Trivedi", [
        {"title": "Unadkat seeks Trivedi's help", "url": "https://x/3", "source": "bing.com",
         "via": "Bing News", "when": "20150617", "severity": "BAD"}])],
        "2026-10-06", "2026-10-13")
    assert "17 Jun 2015" in page and "older story" in page
    tagged = digest_page([("Acme", [{"title": "t", "url": "u", "when": "20261012",
                                     "severity": "UGLY", "about": "Director · Asha Rao"}])],
                         "2026-10-06", "2026-10-13")
    assert "Director · Asha Rao" in tagged and "1 about its directors / promoters" in tagged
    for when in ("20261012", "20260930"):    # in the window, or the usual search lag
        fresh = digest_page([("Acme", [{"title": "t", "url": "u", "when": when,
                                        "severity": "BAD"}])], "2026-10-06", "2026-10-13")
        assert "older story" not in fresh, when


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
def test_next_run_lands_on_the_next_future_slot():
    base = datetime(2026, 8, 9, 10, 0)          # a Sunday, 10:00
    daily = datetime.fromtimestamp(sched.next_run("daily", 8, 0, base))
    assert daily == base.replace(hour=8) + timedelta(days=1)   # 08:00 already passed
    later_today = datetime.fromtimestamp(sched.next_run("daily", 18, 0, base))
    assert later_today == base.replace(hour=18)
    weekly = datetime.fromtimestamp(sched.next_run("weekly", 8, 0, base))  # Monday 08:00
    assert weekly.weekday() == 0 and weekly > base


def test_a_restart_never_fires_a_catch_up_digest(tmp_path):
    """The rule that keeps a desk trusting its alerts: a slot missed while the service
    was down is moved FORWARD, not fired the moment it comes back."""
    path = tmp_path / "schedules.json"
    stale = datetime.now() - timedelta(days=3)
    path.write_text(json.dumps([{"id": "S1", "q": "Acme", "recipients": ["a@b.com"],
                                 "cadence": "daily", "hour": 8, "weekday": 0,
                                 "next_run": stale.timestamp(), "active": True}]))
    store = sched.ScheduleStore(str(path))
    assert store.rearm_stale() == 1
    assert store.all()[0]["next_run"] > datetime.now().timestamp()


def test_schedules_survive_a_container_replacement(tmp_path):
    path = str(tmp_path / "s.json")
    store = sched.ScheduleStore(path)
    store.add({"id": "S1", "tenant": "EVAM", "q": "Acme", "recipients": ["a@b.com"],
               "cadence": "daily", "hour": 8, "weekday": 0, "next_run": 0})
    assert sched.ScheduleStore(path).all() == store.all()   # a fresh process reads it back


def test_a_schedule_belongs_to_its_tenant(tmp_path):
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    store.add({"id": "A", "tenant": "EVAM", "q": "x", "recipients": ["a@b.com"]})
    store.add({"id": "B", "tenant": "OTHER", "q": "y", "recipients": ["b@c.com"]})
    assert [s["id"] for s in store.all("EVAM")] == ["A"]
    assert store.remove("A", "OTHER") is False, "one tenant cannot delete another's"
    assert store.remove("A", "EVAM") is True


def test_editing_a_schedule_keeps_its_identity_and_history(tmp_path):
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    store.add({"id": "S1", "tenant": "EVAM", "q": "Acme", "recipients": ["a@b.com"],
               "cadence": "daily", "hour": 8, "weekday": 0, "last_run": 12345})
    assert store.update("S1", "OTHER", {"hour": 7}) is None, \
        "one tenant cannot edit another's schedule"
    edited = store.update("S1", "EVAM",
                          {"hour": 7, "recipients": ["a@b.com", "c@d.com"]})
    assert edited["hour"] == 7 and edited["recipients"] == ["a@b.com", "c@d.com"]
    assert edited["id"] == "S1" and edited["last_run"] == 12345
    fresh = sched.ScheduleStore(str(tmp_path / "s.json"))   # the edit reached disk
    assert fresh.get("S1")["hour"] == 7


def test_a_schedule_that_cannot_reach_disk_fails_loudly(tmp_path):
    """The failure mode that erased the desk's digests: the volume was not writable,
    save() swallowed the error, and every schedule lived only in memory until the next
    restart wiped it. An API mutation that cannot persist must now FAIL the request."""
    (tmp_path / "blocker").write_text("a file where the store needs a directory")
    store = sched.ScheduleStore(str(tmp_path / "blocker" / "s.json"))
    with pytest.raises(sched.ScheduleStoreError) as err:
        store.add({"id": "S1", "tenant": "EVAM", "q": "x", "recipients": ["a@b.com"]})
    assert "writable" in str(err.value)
    # The scheduler loop's run-stamps stay best-effort: a broken disk must not kill it.
    store.touch("S1", last_run=1.0)


def test_the_hour_is_the_desks_hour_not_the_containers(monkeypatch):
    """"if i schedule to send mail it 7 am it should sent at 7 am only" — the container
    runs UTC, so a naive next_run() fired IST 7s at 12:30 in the afternoon."""
    from zoneinfo import ZoneInfo
    monkeypatch.setenv("PULSE_DIGEST_TZ", "Asia/Kolkata")
    when = datetime.fromtimestamp(sched.next_run("daily", 7, 0),
                                  tz=ZoneInfo("Asia/Kolkata"))
    assert (when.hour, when.minute) == (7, 0)
    monkeypatch.setenv("PULSE_DIGEST_TZ", "not/a-zone")     # bad zone: server time, not a crash
    assert sched.next_run("daily", 7, 0) > time_mod.time()


@pytest.mark.asyncio
async def test_email_that_is_not_configured_is_not_retried(tmp_path):
    """No retry can configure SMTP: the old loop re-ran the whole all-firms search three
    more times for it, four Google sweeps in 40 minutes, every morning."""
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    calls = []

    async def runner(s, **kw):
        calls.append(s["id"])
        return False, "Email is not configured — set PULSE_SMTP_HOST", 3, 0

    task = asyncio.create_task(sched.scheduler_loop(store, runner, tick_seconds=0))
    await asyncio.sleep(0.02)
    store.add({"id": "S1", "tenant": "EVAM", "q": "Acme", "recipients": ["a@b.com"],
               "cadence": "daily", "hour": 8, "weekday": 0,
               "next_run": time_mod.time() - 5, "active": True})
    try:
        while not calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        s1 = store.get("S1")
        assert calls == ["S1"] and not s1.get("retry")
        # The next REGULAR slot, not a retry delay. Compared with the slot itself rather
        # than "more than an hour away": run between 07:00 and 08:00 IST, the 8 o'clock
        # slot is less than an hour off and the looser check failed on the clock alone.
        assert abs(s1["next_run"] - sched.next_run("daily", 8, 0)) < 5, "next regular slot, not minutes"
        assert s1["next_run"] > time_mod.time() + 60
        assert "not retried" in s1["history"][-1]["note"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_failed_send_is_retried_soon_not_lost_until_tomorrow(tmp_path):
    """One SMTP hiccup at 7:00 must not silently cost the desk the whole day's digest:
    the loop re-arms the schedule minutes out, a bounded number of times, and writes
    every outcome into the schedule's history."""
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    calls = []

    async def runner(s, **kw):
        calls.append(s["id"])
        if len(calls) < 2:
            return False, "Send failed: smtp timeout", 0, 0
        return True, "Sent to 1 recipient(s)", 2, 7

    task = asyncio.create_task(sched.scheduler_loop(store, runner, tick_seconds=0))
    await asyncio.sleep(0.02)          # let the startup rearm pass run first — it must
    store.add({"id": "S1", "tenant": "EVAM", "q": "Acme", "recipients": ["a@b.com"],
               "cadence": "daily", "hour": 8, "weekday": 0,
               "next_run": time_mod.time() - 5, "active": True})
    try:
        while len(calls) < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        s1 = store.get("S1")
        assert s1["retry"] == 1, "the failure armed a retry"
        assert s1["next_run"] <= time_mod.time() + sched.RETRY_DELAY_S + 1
        assert s1["next_run"] > time_mod.time() + 60, "spaced out, not a tight loop"
        assert s1["history"][-1]["ok"] is False
        assert "retry 1 of" in s1["history"][-1]["note"]

        store.touch("S1", next_run=time_mod.time() - 1)   # bring the retry slot forward
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    s1 = store.get("S1")
    assert s1["retry"] == 0, "success cleared the retry counter"
    last = s1["history"][-1]
    assert last["ok"] is True and last["firms"] == 2 and last["items"] == 7
    assert s1["next_run"] > time_mod.time(), "back on the regular cadence"


def test_history_is_capped_so_the_file_never_grows_without_bound(tmp_path):
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    store.add({"id": "S1", "tenant": "EVAM", "q": "x", "recipients": ["a@b.com"]})
    for i in range(sched.MAX_HISTORY + 5):
        store.record_run("S1", ok=True, note=f"run {i}")
    hist = store.get("S1")["history"]
    assert len(hist) == sched.MAX_HISTORY
    assert hist[-1]["note"] == f"run {sched.MAX_HISTORY + 4}", "newest kept, oldest dropped"


def test_the_store_probe_tells_the_truth_both_ways(tmp_path):
    good = sched.ScheduleStore(str(tmp_path / "s.json"))
    ok, why = good.probe()
    assert ok is True and why == ""
    (tmp_path / "blocker").write_text("a file where the store needs a directory")
    bad = sched.ScheduleStore(str(tmp_path / "blocker" / "s.json"))
    ok, why = bad.probe()
    assert ok is False and "writable" in why


def _digest_runner_parts():
    """A run_schedule harness: fake search/send/digest that record what was covered."""
    searched, sent = [], {}

    async def search(term, dfrom, dto, **kw):
        searched.append(term)
        return [{"title": f"{term} wins order", "url": f"https://x/{term}",
                 "severity": "GOOD"}]

    def send(recipients, subject, body):
        sent.update({"recipients": recipients, "subject": subject, "body": body})
        return True, "Sent to 1 recipient(s)"

    def digest(groups, dfrom, dto, *, adverse_only=False, coverage=None):
        return "".join(f"<b>{term}</b>" for term, _ in groups)

    return searched, sent, search, send, digest


@pytest.mark.asyncio
async def test_an_all_firms_digest_covers_the_register_as_of_send_time():
    """"any newly added it wont consider is it?" — it does now: the firm list is read
    fresh at send time and MERGED with the stored snapshot, so a firm added after the
    schedule was created is in the next digest with no one re-saving anything, and the
    snapshot's typed watch terms are kept."""
    searched, _, search, send, digest = _digest_runner_parts()

    async def firm_terms():
        return ["Acme Solar India Pvt. Ltd.", "Beta Wind", "Newly Added Energy Ltd"]

    ok, _, firms, _ = await sched.run_schedule(
        {"id": "S1", "scope": "all-firms", "q": "acme solar india, OldCo watch term",
         "recipients": ["a@b.com"], "window_days": 7},
        search=search, send=send, digest=digest, firm_terms=firm_terms)
    assert ok is True
    assert searched == ["Acme Solar India", "OldCo watch term",
                        "Beta Wind", "Newly Added Energy"], \
        "snapshot kept (watch term included), fresh firms appended, dupes and corporate " \
        "suffixes folded"
    assert firms == 4


@pytest.mark.asyncio
async def test_a_register_outage_costs_freshness_never_the_digest():
    searched, sent, search, send, digest = _digest_runner_parts()

    async def firm_terms():
        raise RuntimeError("register unreachable")

    ok, _, _, _ = await sched.run_schedule(
        {"id": "S1", "scope": "all-firms", "q": "Acme Solar",
         "recipients": ["a@b.com"], "window_days": 7},
        search=search, send=send, digest=digest, firm_terms=firm_terms)
    assert ok is True and searched == ["Acme Solar"] and sent, \
        "the stored snapshot alone still went out"


@pytest.mark.asyncio
async def test_a_plain_term_schedule_never_touches_the_register():
    searched, _, search, send, digest = _digest_runner_parts()
    called = []

    async def firm_terms():
        called.append(1)
        return ["Should Not Appear"]

    await sched.run_schedule(
        {"id": "S1", "scope": "terms", "q": "Acme Solar",
         "recipients": ["a@b.com"], "window_days": 7},
        search=search, send=send, digest=digest, firm_terms=firm_terms)
    assert not called and searched == ["Acme Solar"]


@pytest.mark.asyncio
async def test_a_firms_key_people_are_searched_with_it_and_named_on_its_card():
    """"check the directors of the companies and add it": each listed person is searched
    as a NAME (strict, with their city), and what they turn up lands on the firm's card
    tagged with who it is about — an article naming both appears once."""
    calls, sent = [], {}

    async def search(term, dfrom, dto, **kw):
        calls.append((term, kw))
        if term == "Asha Rao":
            return [{"title": "Asha Rao named in GST fraud probe", "url": "https://x/p",
                     "severity": "UGLY"},
                    {"title": "Keypeople Batteries wins order", "url": "https://x/f",
                     "severity": "GOOD"}]
        return [{"title": f"{term} wins order", "url": "https://x/f", "severity": "GOOD"}]

    def send(recipients, subject, body):
        sent.update(subject=subject, body=body)
        return True, "sent"

    async def firm_terms():
        return [{"name": "Keypeople Batteries Pvt Ltd",
                 "people": [{"name": "Asha Rao", "role": "Director", "city": "Coimbatore"},
                            {"role": "nameless rows are skipped"}]}]

    seen_groups = {}

    def digest(groups, dfrom, dto, *, adverse_only=False, coverage=None):
        seen_groups.update(dict(groups))
        return "<p>digest</p>"

    ok, _, firms, items = await sched.run_schedule(
        {"id": "S1", "scope": "all-firms", "q": "", "recipients": ["a@b.com"]},
        search=search, send=send, digest=digest, firm_terms=firm_terms)
    assert ok and firms == 1 and items == 2
    assert ("Keypeople Batteries", {"strict": True, "context": "",
                                    "names": ["Keypeople Batteries", "Keypeople"]}) in calls, \
        "searched under its full name and the short name the press would print"
    assert ("Asha Rao", {"strict": True, "context": "Coimbatore",
                         "names": ["Asha Rao"]}) in calls
    card = seen_groups["Keypeople Batteries"]
    assert [a.get("about") for a in card] == [None, "Director · Asha Rao"], \
        "the firm's own article once, the director's tagged"


@pytest.mark.asyncio
async def test_the_scheduled_digest_searches_firms_in_parallel_not_one_by_one():
    """"can we speed it up": the run used to await each firm in turn. Now several are in
    flight at once (bounded), so a 400-firm digest is not 400 serial waits."""
    inflight = peak = 0

    async def search(term, dfrom, dto, **kw):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return []

    t0 = time_mod.time()
    await sched.run_schedule(
        {"id": "S1", "scope": "terms", "q": ",".join(f"Firm {i}" for i in range(24)),
         "recipients": ["a@b.com"]},
        search=search, send=lambda *a: (True, "sent"), digest=lambda *a, **k: "")
    assert 1 < peak <= sched.DIGEST_CONCURRENCY
    assert time_mod.time() - t0 < 24 * 0.05 / 2, "well under the serial time"


def test_a_name_search_keeps_only_articles_that_print_the_name_inside_the_window():
    from app.news.search import in_window, mentions
    assert mentions({"title": "Atlanta System wins grid order"}, "Atlanta System")
    assert not mentions({"title": "Man shoots passenger on Atlanta bus"}, "Atlanta System")
    assert mentions({"title": "Probe widens", "snippet": "Pranav S. Kumar, a partner,"},
                    "Pranav S Kumar"), "punctuation in the article does not hide the name"
    assert not mentions({"title": "Landmark ruling"}, "Land"), "whole words only"
    assert in_window({"when": "20261012"}, "2026-10-06", "2026-10-13")
    assert in_window({"when": "20260930"}, "2026-10-06", "2026-10-13"), "a week's grace"
    assert not in_window({"when": "20150617"}, "2026-10-06", "2026-10-13")
    assert in_window({"when": ""}, "2026-10-06", "2026-10-13"), "undated is kept"


@pytest.mark.asyncio
async def test_strict_search_quotes_the_name_and_drops_what_does_not_name_it(monkeypatch):
    engine = NewsSearch(disable_gdelt=True)
    asked = []

    async def fake_rss(client, q, dfrom, dto, **kw):
        asked.append(q)
        return ([{"title": "Atlanta System wins grid order", "url": "https://a/1",
                  "source": "ET", "when": "20261010", "via": "Google News"},
                 {"title": "Bus shooting in Atlanta", "url": "https://a/2",
                  "source": "CBS", "when": "20261010", "via": "Google News"},
                 {"title": "Atlanta System profile", "url": "https://a/3",
                  "source": "bing.com", "when": "20160318", "via": "Bing News"}],
                {"name": "x", "ok": True, "count": 3})

    monkeypatch.setattr(engine, "_google", fake_rss)
    monkeypatch.setattr(engine, "_bing", fake_rss)
    loose = await engine.search("Atlanta System", "2026-10-06", "2026-10-13")
    strict = await engine.search("Atlanta System", "2026-10-06", "2026-10-13",
                                 strict=True, context="Pune")
    assert len(loose) == 3, "a topic search is left alone"
    assert [a["url"] for a in strict] == ["https://a/1"]
    assert '"Atlanta System" Pune' in asked


def test_a_firm_is_watched_under_the_short_name_the_press_prints_only_when_it_is_safe():
    """"Enerparc insolvency" never says "Enerparc Energy": the digest lost the biggest
    story in the 13 Oct email to a full-name match. A short name is taken only when
    what is left is distinctive; "Future Solar" must never become "Future"."""
    from app.news.names import short_name, watch_names
    assert short_name("Enerparc Energy") == "Enerparc"
    assert short_name("Saera Electric Auto") == "Saera"
    # A group is searched whole: "Pashupati" alone found a Kathmandu temple and an
    # unrelated listed company (cipher-test, 16 Oct).
    assert short_name("Pashupati Group") is None
    assert watch_names("Pashupati Group") == ["Pashupati Group"]
    assert short_name("Euroteck Environmental") == "Euroteck"
    for generic in ("Future Solar", "Capital Energy", "Atlanta System", "Rajasthan Solar",
                    "Surya Synergy", "Clear Water", "Green Next Energy", "Sada Energy",
                    "Okaya", "Land",
                    # the live run: a judge's surname, a business house, namesakes
                    "Datta Power Infra", "Adani Solar", "Orano Energy",
                    "Praveen Capital", "Terex India"):
        assert short_name(generic) is None, generic
    assert watch_names("Enerparc Energy") == ["Enerparc Energy", "Enerparc"]
    assert watch_names("Land", ["Land Renewables", "LRPL"]) == ["Land Renewables", "LRPL"], \
        "an analyst's names are the whole list: a firm named after a word is watchable"


@pytest.mark.asyncio
async def test_a_record_marked_not_watched_leaves_the_digest_even_from_the_snapshot():
    searched, _, search, send, digest = _digest_runner_parts()

    async def firm_terms():
        return [{"name": "Consultant 1", "watch": False}, {"name": "Beta Wind"}]

    await sched.run_schedule(
        {"id": "S1", "scope": "all-firms", "q": "Consultant 1, Acme Solar",
         "recipients": ["a@b.com"]},
        search=search, send=send, digest=digest, firm_terms=firm_terms)
    assert searched == ["Acme Solar", "Beta Wind"]


@pytest.mark.asyncio
async def test_the_whole_window_is_sent_with_what_is_new_marked_and_remembered():
    """A "7-day" digest shows the week every morning. It used to drop every story sent
    before, so a quiet morning said "0 items" with four adverse stories inside the week
    (production, 20–23 Oct). A story sent before stays in, marked; only the new ones are
    remembered — and only after a send that succeeded."""
    from app.news.mailer import digest_page as digest
    _, sent, search, send, _fake = _digest_runner_parts()
    remembered = []
    first = {"id": "S1", "q": "Acme Solar, Beta Wind", "recipients": ["a@b.com"]}
    ok, _, firms, total = await sched.run_schedule(
        first, search=search, send=send, digest=digest,
        already_sent={sched.story_key({"title": "Acme Solar wins order"})},
        on_sent=remembered.extend)
    assert ok and firms == 2 and total == 2
    assert "Beta Wind" in sent["body"] and "Acme" in sent["body"]
    assert "sent before" in sent["body"] and ">NEW<" in sent["body"]
    assert "1 new · 2 items in the last 7 days" in sent["subject"]
    # The firm with something new leads the subject.
    assert sent["subject"].index("Beta Wind") < sent["subject"].index("Acme Solar")
    assert remembered == [sched.story_key({"title": "Beta Wind wins order"})]
    remembered.clear()
    await sched.run_schedule(first, search=search, send=lambda *a: (False, "smtp down"),
                             digest=digest, on_sent=remembered.extend)
    assert remembered == [], "a failed send is not remembered as sent"


@pytest.mark.asyncio
async def test_a_quiet_morning_still_shows_the_week_and_a_desk_subject_gets_the_counts():
    from app.news.mailer import digest_page as digest
    _, sent, search, send, _fake = _digest_runner_parts()
    keys = {sched.story_key({"title": "Acme Solar wins order"}),
            sched.story_key({"title": "Beta Wind wins order"})}
    sched_ = {"id": "S1", "q": "Acme Solar, Beta Wind", "recipients": ["a@b.com"],
              "subject": "ATLAS news digest"}
    ok, _, firms, total = await sched.run_schedule(
        sched_, search=search, send=send, digest=digest, already_sent=keys)
    assert ok and firms == 2 and total == 2
    assert sent["subject"] == "ATLAS news digest — 0 new · 2 items in the last 7 days"
    assert ">NEW<" not in sent["body"] and sent["body"].count("sent before") == 2


def test_two_records_reporting_the_same_story_become_one_card():
    a = {"title": "Satyendar Jain arrested: 7 key details on DJB tenders, Euroteck"}
    b = {"title": "DJB Tender Scam: Court grants bail to ex-engineer"}
    merged = sched.merge_shared([("Euroteck", [a, b]), ("Euroteck Environmental", [a]),
                                 ("Beta Wind", [{"title": "Beta Wind wins order"}])])
    assert [n for n, _ in merged] == ["Euroteck · Euroteck Environmental", "Beta Wind"]
    assert len(merged[0][1]) == 2, "the shared story appears once"


def test_one_story_from_many_outlets_is_one_item_with_its_other_reports_beneath():
    from app.news.mailer import digest_page, stories
    arts = [{"title": "Enerparc insolvency: more than 100 inquiries, transaction targeted",
             "severity": "UGLY", "when": "20261007", "url": "u1"},
            {"title": "Enerparc insolvency administrator targets solar portfolio sale",
             "severity": "UGLY", "when": "20261007", "url": "u2"},
            {"title": "Enerparc attracts 100 potential investors as insolvency sale moves",
             "severity": "UGLY", "when": "20261005", "url": "u3"},
            {"title": "Enerparc commissions 50 MW plant in Gujarat", "severity": "GOOD",
             "when": "20261006", "url": "u4"}]
    assert [len(g) for g in stories(arts, "Enerparc Energy")] == [3, 1]
    page = digest_page([("Enerparc Energy", arts), ("Beta Wind", arts[3:])],
                       "2026-10-06", "2026-10-13")
    assert "2 more reports of this story" in page and "2 stories from 4 reports" in page
    assert 'href="#firm-enerparc-energy"' in page and 'id="firm-enerparc-energy"' in page, \
        "the contents line jumps to the card"
    assert page.count('href="#top"') == 2 and 'id="top"' in page, "and each card jumps back"


def test_bing_names_its_publisher_and_its_links_go_straight_to_the_article():
    from app.news.search import _rss_items
    xml = ('<rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>'
           '<item><title>Acme Solar wins order</title>'
           '<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2f'
           'www.livemint.com%2fstory%2facme&amp;c=1</link>'
           '<pubDate>Mon, 12 Oct 2026 20:12:44 GMT</pubDate>'
           '<News:Source>Mint</News:Source></item></channel></rss>')
    [item] = _rss_items(xml, "Bing News")
    assert item["source"] == "Mint" and item["url"] == "https://www.livemint.com/story/acme"


def test_headlines_against_fraud_or_a_military_strike_are_not_adverse_news():
    from app.news.triage import classify
    assert classify("Darwinium launches capabilities to catch fraud by AI agents") == "GOOD"
    assert classify("Fraud Prevention for AI Agents, Humans and Bots") == "GOOD"
    assert classify("Drone Strike Targets Oil Tanker off Oman") == "GOOD"
    assert classify("Accounting disputes do not amount to fraud, tribunal holds") != "UGLY"
    assert classify("Workers go on strike at Acme plant") == "BAD", "a labour strike still is"
    assert classify("Promoter arrested in loan fraud case") == "UGLY"


@pytest.mark.asyncio
async def test_a_source_is_asked_no_faster_than_it_allows():
    """The live run asked Google News for six feeds at once and got its automated-
    traffic block for 365 of 489 firms. Each source now has its own spacing."""
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 0.05)
    t0 = time_mod.monotonic()
    await asyncio.gather(*(gate.enter() for _ in range(4)))
    assert time_mod.monotonic() - t0 >= 0.14, "four requests, spaced three intervals apart"
    slow = SourceGate("GDELT", 5.0)
    assert await slow.enter(max_wait=0) is None
    why = await slow.enter(max_wait=0)
    assert why and "one request every 5 s" in why, "a bulk search skips, it does not queue"


@pytest.mark.asyncio
async def test_a_failing_source_is_paused_and_reported_not_hammered():
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 0.0, trip_after=3, cooldown=60)
    for _ in range(3):
        assert await gate.enter() is None
        gate.done(False)
    why = await gate.enter()
    assert why and "failing for a long stretch" in why


@pytest.mark.asyncio
async def test_the_digest_says_which_sources_could_not_be_searched_for_how_many_firms():
    """The live run's email looked complete with Google blocked for most firms. Now the
    run counts, per source, the firms it could not search, and the email says it."""
    got = {}

    async def search(term, dfrom, dto, **kw):
        ok = term != "Beta Wind"
        return ([{"title": f"{term} fined by regulator", "url": f"u/{term}",
                  "severity": "BAD"}],
                [{"name": "Google News", "ok": ok, "error": "" if ok else "HTTP 503"},
                 {"name": "GDELT", "ok": False, "skipped": True,
                  "error": "not asked: it allows one request every 5.5 s"},
                 {"name": "Bing News", "ok": True}])

    def digest(groups, dfrom, dto, *, adverse_only=False, coverage=None):
        got["coverage"] = coverage
        from app.news.mailer import digest_page
        return digest_page(groups, dfrom, dto, adverse_only=adverse_only, coverage=coverage)

    sent = {}
    ok, msg, _, _ = await sched.run_schedule(
        {"id": "S1", "q": "Acme Solar, Beta Wind", "recipients": ["a@b.com"]},
        search=search, send=lambda to, subj, body: (sent.update(body=body) or True, "sent"),
        digest=digest)
    assert got["coverage"]["down"] == {"Google News": 1, "GDELT": 2}
    assert "Google News for 1 of 2 firms" in msg
    body = sent["body"]
    assert "2 firms searched" in body
    assert "<b>Google News</b> could not be searched for 1 of 2 firms (HTTP 503)" in body
    assert "may be missing news" in body
    assert "GDELT was not asked for 2 of 2 firms" in body, "pacing is said, but not alarmed"


@pytest.mark.asyncio
async def test_bing_is_never_searched_with_or_but_google_takes_the_names_in_one(monkeypatch):
    """Live on cipher-test: Bing returned 0 items for '"Enerparc Energy" OR "Enerparc"'
    and 14 for '"Enerparc"', so every firm with a short name was blind on Bing — each
    name is its own Bing query. Google takes OR, and its hourly allowance is the scarce
    thing, so a firm's names are ONE Google query. Either way a name containing another
    one is not asked separately."""
    engine = NewsSearch(disable_gdelt=True, pace=False)
    asked = []
    asked_google = []

    async def fake(client, q, dfrom, dto, **kw):
        asked.append(q)
        return ([{"title": "Enerparc insolvency administrator targets sale", "url": "https://e/1",
                  "source": "PV", "when": "20261007", "via": "Bing News"}],
                {"name": "Bing News", "ok": True, "count": 1})

    async def fake_google(client, q, dfrom, dto, **kw):
        asked_google.append(q)
        return await fake(client, q, dfrom, dto, **kw)

    monkeypatch.setattr(engine, "_google", fake_google)
    monkeypatch.setattr(engine, "_bing", fake)
    got = await engine.search("Enerparc Energy", "2026-10-06", "2026-10-13", strict=True,
                              names=["Enerparc Energy", "Enerparc"])
    assert asked == ['"Enerparc"', '"Enerparc"'], "one query per source"
    assert len(got) == 1, "the same article from both sources appears once"
    assert got[0]["matched"] == "Enerparc"
    from app.news.mailer import digest_page
    page = digest_page([("Enerparc Energy", got)], "2026-10-06", "2026-10-13")
    assert "found as &ldquo;Enerparc&rdquo;" in page, "a short-name match says so"
    plain = digest_page([("Enerparc Energy", [{**got[0], "matched": "Enerparc Energy"}])],
                        "2026-10-06", "2026-10-13")
    assert "found as" not in plain, "a full-name match needs no remark"
    asked.clear()
    asked_google.clear()
    await engine.search("Land", "2026-10-06", "2026-10-13", strict=True,
                        names=["Land Renewables", "LRPL"])
    assert asked_google == ['"Land Renewables" OR "LRPL"'], "Google: one query, OR"
    bing = [q for q in asked if q not in asked_google]
    assert sorted(bing) == ['"LRPL"', '"Land Renewables"'], "Bing: one per name, no OR"
    assert not any(" OR " in q for q in bing)


def test_the_word_list_ships_inside_the_package_not_only_in_the_source_tree():
    """Live on cipher-test, a script that imported the INSTALLED package got an empty
    word list — so no short names, and Enerparc's insolvency went missing. The list is
    declared package data now; this checks the declaration and the loaded list."""
    import tomllib
    from pathlib import Path

    from app.news import names
    conf = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "data/*" in conf["tool"]["setuptools"]["package-data"]["app.news"]
    assert len(names._english()) > 50000


@pytest.mark.asyncio
async def test_a_throttled_source_is_waited_out_and_asked_again_not_given_up(monkeypatch):
    """The 14 Oct production digest: "Google News could not be searched for 335 of 525
    firms (HTTP 503)". A 503 now pauses Google's lane and the SAME search is retried,
    so the firm is searched, not reported missing."""
    from app.news import search as srch
    engine = NewsSearch(disable_gdelt=True, pace=False)
    engine._gates["Google News"] = srch.SourceGate("Google News", 0.0, pause=0.02,
                                                   max_pause=0.05)
    answers = iter([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if "news.google.com" in request.url.host:
            code = next(answers)
            return (httpx.Response(200, text=GOOGLE_RSS) if code == 200
                    else httpx.Response(code, text="<html>Sorry...</html>"))
        return httpx.Response(200, text=BING_RSS)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))
    arts, sources = await engine.search_detail("Acme Solar", "2026-08-01", "2026-08-10",
                                               strict=True, names=["Acme Solar"])
    google = next(x for x in sources if x["name"] == "Google News")
    assert google["ok"] is True, "searched after two throttles, not given up"
    assert engine._gates["Google News"].throttles == 2
    assert any(a["via"] == "Google News" for a in arts)


@pytest.mark.asyncio
async def test_a_person_at_the_search_box_is_not_held_through_a_long_throttle(monkeypatch):
    from app.news import search as srch
    engine = NewsSearch(disable_gdelt=True, pace=False)
    gate = srch.SourceGate("Google News", 0.0, pause=600)
    engine._gates["Google News"] = gate
    gate.throttled()                                   # Google said "wait 10 minutes"
    _feeds_ok(monkeypatch)
    t0 = time_mod.monotonic()
    _, sources = await engine.search_detail("Acme Solar")
    assert time_mod.monotonic() - t0 < 5, "an interactive search does not sit it out"
    google = next(x for x in sources if x["name"] == "Google News")
    assert google.get("skipped") and "too fast" in google["error"]


@pytest.mark.asyncio
async def test_a_request_queued_before_a_throttle_does_not_go_out_during_the_pause():
    """Live on cipher-test: callers that already held a slot sent anyway while Google's
    pause was on, and each refusal doubled the pause. They now wait the pause out."""
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 0.05, pause=0.3, max_pause=1.0)
    t0 = time_mod.monotonic()
    first = await gate.enter()                       # takes the first slot now
    waiting = asyncio.create_task(gate.enter())       # holds the next slot (t0 + 0.05)
    await asyncio.sleep(0.01)
    gate.throttled(time_mod.monotonic())              # Google says stop, before slot 2
    assert first is None and await waiting is None
    assert time_mod.monotonic() - t0 >= 0.3, "slot 2 waited for the pause, not just its turn"


@pytest.mark.asyncio
async def test_retry_passes_stop_when_the_run_has_spent_its_budget(monkeypatch):
    monkeypatch.setattr(sched, "RETRY_PASS_BUDGET_S", -1)     # budget already spent
    calls = []

    async def search(term, dfrom, dto, **kw):
        calls.append(term)
        return ([], [{"name": "Google News", "ok": False, "error": "HTTP 503",
                      "throttled": True}, {"name": "Bing News", "ok": True}])

    sent = {}
    ok, msg, _, _ = await sched.run_schedule(
        {"id": "S1", "q": "Acme Solar", "recipients": ["a@b.com"]},
        search=search, send=lambda to, subj, body: (sent.update(body=body) or True, "sent"),
        digest=lambda groups, dfrom, dto, **k: __import__(
            "app.news.mailer", fromlist=["digest_page"]).digest_page(groups, dfrom, dto, **k))
    assert calls == ["Acme Solar"], "no retry pass after the budget"
    assert "Google News for 1 of 1 firms" in msg and "could not be searched" in sent["body"]


@pytest.mark.asyncio
async def test_a_source_that_refuses_for_twenty_minutes_is_given_up_for_the_run():
    """Live: Google refused for 2.5 hours and the run sat out 15-minute pauses for its
    last nine firms. Past the give-up time the source is reported, not waited for."""
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 0.0, pause=0.01, max_pause=0.01)
    gate.give_up_after = 0.05
    gate.throttled(time_mod.monotonic())
    await asyncio.sleep(0.06)
    why = await gate.enter()
    assert why and "long stretch" in why
    fresh = SourceGate("Google News", 0.0, pause=0.01)
    fresh.give_up_after = 0.05
    fresh.throttled(time_mod.monotonic())
    fresh.done(True)                                  # an answer in between resets it
    await asyncio.sleep(0.06)
    assert await fresh.enter() is None


@pytest.mark.asyncio
async def test_an_early_start_rearms_to_the_next_slot_not_the_same_one(tmp_path, monkeypatch):
    """cipher-test, 16 Oct: the 01:30 digest started at 01:10, re-armed to "the next
    slot from now" — 01:30 again — and ran a second time at 04:09."""
    store = sched.ScheduleStore(str(tmp_path / "s.json"))
    calls = []

    async def runner(s, **kw):
        calls.append(kw.get("send_at"))
        return True, "Sent", 1, 1

    monkeypatch.setattr(sched, "lead_seconds", lambda s: 3600)
    due = sched.next_run("daily", (datetime.now(sched.desk_tz()) + timedelta(hours=1)).hour, 0)
    task = asyncio.create_task(sched.scheduler_loop(store, runner, tick_seconds=0))
    await asyncio.sleep(0.02)
    store.add({"id": "S1", "tenant": "EVAM", "q": "Acme", "recipients": ["a@b.com"],
               "cadence": "daily", "hour": datetime.fromtimestamp(due, sched.desk_tz()).hour,
               "weekday": 0, "next_run": due, "active": True})
    try:
        while not calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert calls == [due], "started early, once, holding the email for its slot"
        assert store.get("S1")["next_run"] >= due + 23 * 3600, "re-armed to tomorrow's slot"
    finally:
        task.cancel()


def test_the_gate_slows_on_a_throttle_and_eases_back_after_a_clean_run():
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 2.0, pause=60, max_pause=900)
    assert gate.throttled() == 60 and gate.interval == 3.0
    assert gate.throttled() == 120 and gate.interval == 4.5
    for _ in range(25):
        gate.done(True)
    assert gate.interval < 4.5 and gate._pause == 60, "eases back, pause step resets"
    for _ in range(200):
        gate.done(True)
    assert gate.interval == 2.0, "never below the measured base"


def test_one_refusal_episode_escalates_the_pause_once_not_once_per_request_in_flight():
    """Six requests in flight when Google starts refusing used to double the pause six
    times (60 s -> 15 min in a second). Requests sent before the throttle only honour
    it; the next refusal of a request sent AFTER it is what escalates."""
    from app.news.search import SourceGate
    gate = SourceGate("Google News", 2.0, pause=60, max_pause=900)
    t_sent = time_mod.monotonic()
    assert gate.throttled(t_sent) == 60
    for _ in range(5):                      # the rest of the burst, sent at the same time
        gate.throttled(t_sent)
    assert gate.throttles == 1 and gate._pause == 120
    assert gate.throttled(time_mod.monotonic() + 1) == 120, "a later refusal escalates"


@pytest.mark.asyncio
async def test_a_partial_answer_is_not_cached_so_a_retry_really_asks_again(monkeypatch):
    calls = {"google": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "news.google.com" in request.url.host:
            calls["google"] += 1
            if calls["google"] == 1:
                raise httpx.ConnectTimeout("blip")
            return httpx.Response(200, text=GOOGLE_RSS)
        return httpx.Response(200, text=BING_RSS)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))
    engine = NewsSearch(disable_gdelt=True, pace=False)
    _, first = await engine.search_detail("Acme Solar")
    _, second = await engine.search_detail("Acme Solar")
    assert not next(x for x in first if x["name"] == "Google News")["ok"]
    assert next(x for x in second if x["name"] == "Google News")["ok"], \
        "the gap was not served back from the cache"
    _, third = await engine.search_detail("Acme Solar")
    assert calls["google"] == 2, "a complete answer IS cached"


@pytest.mark.asyncio
async def test_bulk_searches_leave_gdelt_out_entirely(monkeypatch):
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        return httpx.Response(200, text=GOOGLE_RSS if "google" in request.url.host
                              else BING_RSS)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.news.search.httpx.AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))
    _, sources = await NewsSearch(pace=False).search_detail(
        "Acme Solar", strict=True, names=["Acme Solar"])
    assert not any("gdelt" in h for h in asked)
    assert all(x["ok"] for x in sources), "nothing to report about GDELT in a digest"


@pytest.mark.asyncio
async def test_no_firm_is_left_unsearched_when_a_source_has_a_bad_minute():
    """THE ACCEPTANCE TEST for "test such that nothing fails": Google throttles a third
    of the firms on the first pass. The retry passes search them again, every firm ends
    up searched on every source, and the email carries no "could not be searched"."""
    first_time: set[str] = set()

    async def search(term, dfrom, dto, **kw):
        flaky = term.endswith(("0", "3", "6"))
        google_ok = not (flaky and term not in first_time)
        first_time.add(term)
        return ([{"title": f"{term} fined by regulator", "url": f"u/{term}",
                  "severity": "BAD"}],
                [{"name": "Google News", "ok": google_ok,
                  **({} if google_ok else {"error": "HTTP 503 from the source",
                                           "throttled": True})},
                 {"name": "GDELT", "ok": True, "count": 0, "note": "bulk: not used"},
                 {"name": "Bing News", "ok": True}])

    sent = {}
    names = [f"Firm {i}" for i in range(30)]
    ok, msg, firms, _ = await sched.run_schedule(
        {"id": "S1", "q": ",".join(names), "recipients": ["a@b.com"]},
        search=search, send=lambda to, subj, body: (sent.update(body=body) or True, "sent"),
        digest=lambda groups, dfrom, dto, **k: __import__(
            "app.news.mailer", fromlist=["digest_page"]).digest_page(groups, dfrom, dto, **k))
    assert ok and firms == 30
    assert "not searched" not in msg
    assert "could not be searched" not in sent["body"]
    assert "30 firms searched" in sent["body"]


@pytest.mark.asyncio
async def test_a_digest_is_searched_early_and_sent_at_its_minute():
    sent_at = []
    t0 = time_mod.time()
    await sched.run_schedule(
        {"id": "S1", "q": "Acme Solar", "recipients": ["a@b.com"]},
        search=lambda *a, **k: asyncio.sleep(0, result=[]),
        send=lambda *a: (sent_at.append(time_mod.time()) or True, "sent"),
        digest=lambda *a, **k: "", send_at=t0 + 0.3)
    assert sent_at and sent_at[0] >= t0 + 0.3, "held until the slot"


def test_the_lead_time_is_learnt_from_how_long_the_last_searches_took():
    assert sched.lead_seconds({}) == sched.DEFAULT_LEAD_S
    s = {"history": [{"seconds": 900}, {"seconds": 1500}, {"seconds": 1200}]}
    assert sched.lead_seconds(s) == 1500 * 1.25 + 300
    assert sched.lead_seconds({"history": [{"seconds": 99999}]}) == sched.MAX_LEAD_S


def test_trading_name_sheds_the_suffixes_the_press_never_prints():
    assert sched.trading_name("Acme Solar India Pvt. Ltd.") == "Acme Solar India"
    assert sched.trading_name("Beta Wind Limited") == "Beta Wind"
    assert sched.trading_name("Reliance Ltd") == "Reliance"
    assert sched.trading_name("SA") == "SA", "never stripped down to nothing"


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch, tmp_path, stub_upstreams):
    monkeypatch.setenv("PULSE_SCHEDULE_FILE", str(tmp_path / "schedules.json"))
    monkeypatch.setenv("PULSE_SCHEDULER_ENABLED", "false")   # no loop inside a test
    monkeypatch.setenv("PULSE_MONITOR_FILE", str(tmp_path / "monitor.json"))
    monkeypatch.setenv("PULSE_MONITOR_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_search_route_answers_articles(client):
    r = client.get("/v1/news/search", params={"q": "Acme Solar"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["articles"]) > 0
    assert {"title", "url", "source", "when", "via", "severity"} <= set(body["articles"][0])
    # A healthy search reports its sources too, and says nothing failed.
    assert all(s["ok"] for s in body["sources"])
    assert "error" not in body


def test_the_route_says_when_nothing_could_be_reached(client, monkeypatch):
    """What the desk actually needed on a live deployment: 200 with zero articles read
    as "no news about this firm" whether the sources answered or the container had no
    way out. The screen can only tell the difference if the server says."""
    _all_upstreams_dead(monkeypatch, httpx.ConnectError("connection refused"))
    body = client.get("/v1/news/search", params={"q": "Acme Solar"}).json()
    assert body["articles"] == [] and body["count"] == 0
    assert "no news source could be reached" in body["error"]
    assert "Google News" in body["error"] and "Bing News" in body["error"]


def test_an_empty_term_is_an_empty_answer_not_a_search(client):
    assert client.get("/v1/news/search", params={"q": "  "}).json() == {"articles": []}


def test_config_route_reports_what_is_actually_wired(client):
    body = client.get("/v1/news/config").json()
    assert body["email"] is False          # no SMTP in the test environment
    assert body["from"] == ""              # never echo a sender we cannot send from
    assert set(body) == {"email", "from", "gdelt", "scheduler", "policy_themes"}
    # The screen builds its policy sweep from these, so they have to arrive.
    assert "ALMM" in body["policy_themes"]


def test_emailing_without_smtp_fails_with_a_reason_a_person_can_act_on(client):
    r = client.post("/v1/news/email", json={"q": "Acme", "recipients": "a@b.com"})
    assert r.status_code == 400
    assert "not configured" in r.json()["message"].lower()


def test_schedule_round_trip_through_the_api(client):
    made = client.post("/v1/news/schedules", json={
        "q": "Acme Solar, Beta Wind", "recipients": "desk@evamfinance.com",
        "cadence": "weekly", "hour": 7, "weekday": 1, "adverse_only": True})
    assert made.status_code == 200, made.text
    schedule = made.json()["schedule"]
    assert schedule["recipients"] == ["desk@evamfinance.com"]   # the string was split
    assert schedule["next_run"] > 0

    listed = client.get("/v1/news/schedules").json()
    assert [s["id"] for s in listed["schedules"]] == [schedule["id"]]
    assert listed["smtp"] is False

    assert client.post("/v1/news/schedules/delete",
                       json={"id": schedule["id"]}).json() == {"ok": True}
    assert client.get("/v1/news/schedules").json()["schedules"] == []


def test_a_schedule_needs_a_term_and_a_recipient(client):
    r = client.post("/v1/news/schedules", json={"q": "", "recipients": ""})
    assert r.status_code == 400
    assert "recipient" in r.json()["message"].lower()


def test_running_an_unknown_schedule_says_so(client):
    r = client.post("/v1/news/schedules/run", json={"id": "nope"})
    assert r.status_code == 404


def test_run_now_answers_at_once_and_the_outcome_lands_under_last_run(client, monkeypatch):
    """"Run now" on an all-firms digest takes ~20 minutes at a pace Google accepts; held
    in the request it met the edge's 65 s limit. It starts in the background, a second
    click while it runs is refused, and the result is the schedule's last history entry."""
    gate = asyncio.Event()
    from app import main as main_mod

    async def slow_run(schedule, **kw):
        await gate.wait()
        return False, "Email is not configured — all 2 firms searched on every source", 2, 3

    monkeypatch.setattr(main_mod.sched, "run_schedule", slow_run)
    made = client.post("/v1/news/schedules", json={
        "q": "Acme Solar, Beta Wind", "recipients": "desk@evamfinance.com"}).json()["schedule"]
    t0 = time_mod.monotonic()
    r = client.post("/v1/news/schedules/run", json={"id": made["id"]})
    assert r.status_code == 202 and r.json()["started"] is True
    assert time_mod.monotonic() - t0 < 2, "answered without waiting for the run"
    again = client.post("/v1/news/schedules/run", json={"id": made["id"]})
    assert again.status_code == 409, "no second run while one is going"
    client.portal.call(gate.set)            # let the run finish on the app's own loop
    for _ in range(100):
        hist = client.get("/v1/news/schedules").json()["schedules"][0].get("history") or []
        if hist:
            break
        time_mod.sleep(0.02)
    assert hist[-1]["ok"] is False and "all 2 firms searched" in hist[-1]["note"]
    assert client.post("/v1/news/schedules/run", json={"id": made["id"]}).status_code == 202, \
        "free to run again once it finished"


def test_the_desk_edits_a_schedule_in_place(client):
    made = client.post("/v1/news/schedules", json={
        "q": "Acme Solar", "recipients": "desk@evamfinance.com",
        "cadence": "daily", "hour": 7}).json()["schedule"]

    r = client.post("/v1/news/schedules/update", json={
        "id": made["id"], "q": "Acme Solar",
        "recipients": "desk@evamfinance.com, risk@evamfinance.com",
        "cadence": "weekly", "hour": 9, "weekday": 2, "window_days": 14})
    assert r.status_code == 200, r.text
    edited = r.json()["schedule"]
    assert edited["id"] == made["id"]
    assert edited["recipients"] == ["desk@evamfinance.com", "risk@evamfinance.com"]
    assert edited["cadence"] == "weekly" and edited["hour"] == 9
    assert edited["next_run"] != made["next_run"], "the slot moves with the new clock"

    listed = client.get("/v1/news/schedules").json()["schedules"]
    assert listed == [edited], "the list answers the edited schedule, same identity"

    r = client.post("/v1/news/schedules/update",
                    json={"id": "nope", "q": "x", "recipients": "a@b.com"})
    assert r.status_code == 404
    r = client.post("/v1/news/schedules/update",
                    json={"id": made["id"], "q": "", "recipients": ""})
    assert r.status_code == 400


def test_the_dialog_is_told_whether_the_store_can_save(client):
    body = client.get("/v1/news/schedules").json()
    assert body["store_ok"] is True and body["store_error"] == ""


def test_an_all_firms_schedule_needs_no_typed_terms(client):
    """The dialog once showed "(0 firms)" with an empty local book — creating an
    all-firms schedule then failed for want of terms it no longer needs, since the
    register answers at send time."""
    r = client.post("/v1/news/schedules", json={
        "q": "", "recipients": "desk@evamfinance.com", "scope": "all-firms",
        "cadence": "daily", "hour": 7})
    assert r.status_code == 200, r.text
    r = client.post("/v1/news/schedules", json={
        "q": "", "recipients": "desk@evamfinance.com", "scope": "terms"})
    assert r.status_code == 400, "a plain term schedule still needs terms"


# --------------------------------------------------------------------------- #
# Configuration read from a hand-edited .env
# --------------------------------------------------------------------------- #
def test_a_stray_comma_in_env_does_not_crash_loop_the_service(monkeypatch):
    """`PULSE_SMTP_PORT=587,` — one comma carried over from a copied block — used to
    take the container down on boot, over and over, with a pydantic traceback. No port
    is "587," and no flag is "1,": on a number or a boolean that is unambiguously a
    typo, and absorbing it beats a crash loop nobody can read."""
    monkeypatch.setenv("PULSE_SMTP_PORT", "587,")
    monkeypatch.setenv("PULSE_DISABLE_GDELT", "1,")
    monkeypatch.setenv("PULSE_SCHEDULER_ENABLED", " false ")
    monkeypatch.setenv("PULSE_UPSTREAM_CONCURRENCY", '"8"')
    get_settings.cache_clear()
    s = get_settings()
    assert s.smtp_port == 587
    assert s.disable_gdelt is True
    assert s.scheduler_enabled is False
    assert s.upstream_concurrency == 8
    get_settings.cache_clear()


def test_a_blank_setting_means_the_default_not_a_failure(monkeypatch):
    """`PULSE_SMTP_PORT=` with nothing after it is how a stubbed-out block reads. It
    means "unset" — the default — not None, which fails validation just as loudly."""
    monkeypatch.setenv("PULSE_SMTP_PORT", "")
    monkeypatch.setenv("PULSE_SEARCH_CACHE_TTL_S", "   ")
    get_settings.cache_clear()
    s = get_settings()
    assert s.smtp_port == 587
    assert s.search_cache_ttl_s == 900
    get_settings.cache_clear()


def test_a_password_is_never_trimmed(monkeypatch):
    """The cleaning is deliberately limited to numbers and booleans: an SMTP key may
    legitimately end in a comma, and trimming it would break authentication in a way
    nobody could see from the outside."""
    monkeypatch.setenv("PULSE_SMTP_PASS", "key-with-a-trailing-comma,")
    monkeypatch.setenv("PULSE_SMTP_FROM_NAME", "PRISM, Notifications")
    get_settings.cache_clear()
    s = get_settings()
    assert s.smtp_pass == "key-with-a-trailing-comma,"
    assert s.smtp_from_name == "PRISM, Notifications"
    get_settings.cache_clear()
