"""The News Radar: search, classify, email, schedule.

Nothing here touches the network. The three upstreams are stubbed at the HTTP layer, so
these tests pin the behaviour that matters — the merge, the de-duplication, the severity
rules, the coalescing, and every route's contract — without depending on what Google News
happens to be serving today.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.news import schedules as sched
from app.news.mailer import SmtpConfig, digest_html, send_email
from app.news.search import NewsSearch, classify

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


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch, tmp_path, stub_upstreams):
    monkeypatch.setenv("PULSE_SCHEDULE_FILE", str(tmp_path / "schedules.json"))
    monkeypatch.setenv("PULSE_SCHEDULER_ENABLED", "false")   # no loop inside a test
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
    assert set(body) == {"email", "from", "gdelt", "scheduler"}


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
