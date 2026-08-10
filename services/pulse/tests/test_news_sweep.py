"""The SWEEP and the PROBE — the two things the radar needed to be usable at desk scale.

The sweep existed already, but in the wrong place: the BROWSER ran it, one HTTP round
trip per firm per watch term, strictly one after another, because a `for` loop that
awaits cannot overlap. Four hundred firms is then four hundred serial crossings of the
gateway, each paying its own TLS handshakes to the same three hosts. It was not that
any single search was slow — it was that none of them ever ran at the same time.

The probe answers the other half. "No articles" reads identically whether the desk
picked a quiet company or the container has no outbound HTTPS, and on a locked-down
host it is always the second.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.news.search import NewsSearch

pytestmark = pytest.mark.asyncio

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>{t} wins 300 MW order - Mint</title><link>https://ex.com/{k}-win</link>
<source>Mint</source><pubDate>Sat, 02 Aug 2026 06:00:00 GMT</pubDate></item>
<item><title>{t} promoter arrested in loan fraud case - ET</title>
<link>https://ex.com/{k}-fraud</link><source>ET</source>
<pubDate>Fri, 01 Aug 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _feeds(monkeypatch, *, delay: float = 0.0, dead_hosts: set[str] | None = None):
    """Every upstream answers about whatever term was asked for, optionally slowly."""
    dead = dead_hosts or set()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        if any(d in host for d in dead):
            raise httpx.ConnectError("nope", request=request)
        if delay:
            await asyncio.sleep(delay)
        q = request.url.params.get("q") or request.url.params.get("query") or "x"
        term = str(q).split(" after:")[0].strip('"')
        key = term.replace(" ", "-")
        if "gdeltproject.org" in host:
            return httpx.Response(200, json={"articles": []})
        return httpx.Response(200, text=RSS.format(t=term.title(), k=key))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.news.search.httpx.AsyncClient",
        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))
    return calls


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
async def test_a_sweep_answers_every_term_in_the_order_it_was_asked(monkeypatch):
    _feeds(monkeypatch)
    terms = ["Acme Solar", "Vayu Power", "Suryodaya Energy"]
    rows = await NewsSearch().sweep(terms)

    assert [r["term"] for r in rows] == terms, "a sweep must stay alignable to its input"
    assert all(r["count"] > 0 for r in rows)
    # Each term got ITS OWN news, not the first term's answer handed round.
    assert "Acme Solar" in rows[0]["articles"][0]["title"]
    assert "Vayu Power" in rows[1]["articles"][0]["title"]


async def test_the_terms_actually_overlap(monkeypatch):
    """THE POINT OF THE WHOLE CHANGE. Eight terms against an upstream that takes 200ms
    must not cost 8 x 200ms. Sequentially this is >=1.6s; overlapped at six at a time it
    is two waves, well under a second."""
    _feeds(monkeypatch, delay=0.2)
    t0 = time.monotonic()
    rows = await NewsSearch().sweep([f"Firm {i}" for i in range(8)], concurrency=6)
    elapsed = time.monotonic() - t0

    assert len(rows) == 8 and all(r["count"] > 0 for r in rows)
    assert elapsed < 1.0, f"terms did not overlap — {elapsed:.2f}s for 8 x 0.2s"


async def test_the_severity_of_a_term_depends_on_whether_we_have_lent_to_it(monkeypatch):
    """Polarity is the caller's to declare, per term, inside one sweep: fresh funding is
    a win for a name we are chasing and a review flag for one that already owes us."""
    _feeds(monkeypatch)
    rows = await NewsSearch().sweep(["Acme Solar", "Vayu Power"],
                                    live_terms={"Vayu Power"})
    by_term = {r["term"]: r for r in rows}
    prospect = [a for a in by_term["Acme Solar"]["articles"] if "wins" in a["title"]]
    borrower = [a for a in by_term["Vayu Power"]["articles"] if "wins" in a["title"]]
    assert prospect and borrower
    assert prospect[0]["severity"] == "GOOD"
    # The adverse headline is adverse for both — exposure never softens a fraud.
    for row in rows:
        fraud = [a for a in row["articles"] if "fraud" in a["title"]]
        assert fraud and fraud[0]["severity"] == "UGLY"


async def test_one_unreachable_term_never_ends_the_sweep(monkeypatch):
    """A sweep is four hundred independent questions. One that cannot be answered must
    cost exactly one row, not the run."""
    _feeds(monkeypatch)
    engine = NewsSearch()
    real = engine.search_detail

    async def sometimes_explode(term, *a, **k):
        if term == "Bad Term":
            raise RuntimeError("upstream exploded")
        return await real(term, *a, **k)

    engine.search_detail = sometimes_explode  # type: ignore[method-assign]
    rows = await engine.sweep(["Acme Solar", "Bad Term", "Vayu Power"])

    assert [r["term"] for r in rows] == ["Acme Solar", "Bad Term", "Vayu Power"]
    assert rows[0]["count"] > 0 and rows[2]["count"] > 0
    assert rows[1]["count"] == 0
    assert "upstream exploded" in rows[1]["error"]


async def test_a_dead_network_is_reported_per_term_not_as_quiet_news(monkeypatch):
    """0 items with every source down is a FAULT, and the sweep says so on the row —
    otherwise four hundred zeroes look like four hundred uneventful companies."""
    _feeds(monkeypatch, dead_hosts={"news.google.com", "bing.com", "gdeltproject.org"})
    rows = await NewsSearch().sweep(["Acme Solar", "Vayu Power"])
    assert all(r["count"] == 0 for r in rows)
    for row in rows:
        assert "no news source could be reached" in row["error"]
        assert "cannot reach the source" in row["error"]


async def test_blank_and_duplicate_input_is_handled_not_fetched(monkeypatch):
    calls = _feeds(monkeypatch)
    assert await NewsSearch().sweep([]) == []
    assert await NewsSearch().sweep(["", "   "]) == []
    assert calls == [], "an empty sweep must not touch the network"


async def test_two_firms_watching_the_same_name_cost_one_fetch(monkeypatch):
    """Coalescing and the cache already existed; the sweep has to actually USE them —
    a sweep is exactly where the same promoter appears on several firms' watch lists."""
    calls = _feeds(monkeypatch)
    engine = NewsSearch()
    await engine.sweep(["Shared Promoter"] * 5)
    google = [c for c in calls if "news.google.com" in c]
    assert len(google) == 1, f"expected one fetch, made {len(google)}"


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
async def test_the_probe_says_which_sources_answered_and_how_fast(monkeypatch):
    _feeds(monkeypatch)
    out = await NewsSearch().probe()
    assert out["ok"] is True
    names = [s["name"] for s in out["sources"]]
    assert names == ["Bing News", "GDELT", "Google News"]
    assert all("ms" in s for s in out["sources"])
    assert "3 of 3 sources answered" in out["summary"]


async def test_the_probe_names_the_wall_when_there_is_no_egress(monkeypatch):
    """The whole reason this endpoint exists: on a container with no outbound HTTPS the
    radar looks broken and the network is at fault. Say so, and name the hosts to allow."""
    _feeds(monkeypatch, dead_hosts={"news.google.com", "bing.com", "gdeltproject.org"})
    out = await NewsSearch().probe()
    assert out["ok"] is False
    assert "NO source could be reached" in out["summary"]
    assert "news.google.com" in out["summary"] and "gdeltproject.org" in out["summary"]
    assert all(not s["ok"] for s in out["sources"])
    assert all("cannot reach the source" in s["error"] for s in out["sources"])


async def test_a_partly_reachable_container_is_ok_not_broken(monkeypatch):
    """One source down is normal — Bing throttles, GDELT rate-limits. The probe must not
    cry wolf, or nobody reads it the day it matters."""
    _feeds(monkeypatch, dead_hosts={"gdeltproject.org"})
    out = await NewsSearch().probe()
    assert out["ok"] is True
    assert "2 of 3 sources answered" in out["summary"]
    assert [s["ok"] for s in out["sources"] if s["name"] == "GDELT"] == [False]


async def test_the_probe_reads_the_network_now_not_the_cache(monkeypatch):
    """A probe served from the 15-minute search cache would report a network that has
    since fallen over as healthy — the one answer it must never give.

    The upstream flips mid-test through the SAME transport, because the engine keeps one
    connection pool for its lifetime (that is the point of it) and re-patching the class
    afterwards would never reach a client that already exists.
    """
    state = {"dead": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if state["dead"]:
            raise httpx.ConnectError("nope", request=request)
        if "gdeltproject.org" in request.url.host:
            return httpx.Response(200, json={"articles": []})
        return httpx.Response(200, text=RSS.format(t="Rbi", k="rbi"))

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.news.search.httpx.AsyncClient",
        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": transport}))

    engine = NewsSearch()
    assert (await engine.probe())["ok"] is True
    # The same term, again, after the network dies. A cached answer would still say ok.
    state["dead"] = True
    out = await engine.probe()
    assert out["ok"] is False, "the probe answered from cache"
    assert all("cannot reach the source" in s["error"] for s in out["sources"])


# --------------------------------------------------------------------------- #
# The SMTP hints
# --------------------------------------------------------------------------- #
def test_an_smtp_failure_names_the_cause_and_the_host():
    """"Send failed: [Errno -2] Name or service not known" is a syscall, not a cause.
    It reads as a mail problem when it is a hostname or an egress problem, and it never
    says which host was tried — the one fact the desk needs to fix it."""
    from types import SimpleNamespace

    from app.news.mailer import _smtp_hint

    cfg = SimpleNamespace(host="smtp-relay.brevo.com", port=587)
    dns = _smtp_hint(OSError("[Errno -2] Name or service not known"), cfg)
    assert "could not be resolved" in dns
    assert "smtp-relay.brevo.com:587" in dns, "the host tried must be named"
    assert "stray comma" in dns, "the .env fault that actually causes this"

    refused = _smtp_hint(ConnectionRefusedError("[Errno 111] Connection refused"), cfg)
    assert "nothing is listening" in refused and "465" in refused

    slow = _smtp_hint(TimeoutError("timed out"), cfg)
    assert "egress firewall" in slow

    auth = _smtp_hint(Exception("535 5.7.8 Authentication credentials invalid"), cfg)
    assert "API/SMTP key, not the account password" in auth

    # An error nobody has a hint for adds NOTHING — a guess would be worse than silence.
    assert _smtp_hint(Exception("some novel failure"), cfg) == ""


def test_a_trailing_comma_in_the_smtp_host_is_absorbed(monkeypatch):
    """The exact fault that broke email in the field: PULSE_SMTP_HOST=smtp.gmail.com,

    A hostname can never contain a comma, so this is always a copy-paste artefact —
    and left as written it reaches the resolver verbatim and comes back "[Errno -2]
    Name or service not known", which reads as a mail outage rather than a one-character
    typo. Numbers and booleans were already cleaned; names were not.
    """
    from app.config import Settings

    s = Settings(smtp_host="smtp.gmail.com,", smtp_port="587,",  # type: ignore[arg-type]
                 smtp_from="news@evamfinance.com,", smtp_user="u", smtp_pass="k")
    assert s.smtp_host == "smtp.gmail.com"
    assert s.smtp_port == 587
    assert s.smtp_from == "news@evamfinance.com"
    assert s.smtp().ready is True

    assert Settings(smtp_user="news@evamfinance.com,").smtp_user == "news@evamfinance.com"

    # A PASSWORD may legitimately end in a comma — trimming one would break auth
    # invisibly, which is far worse than the typo this absorbs. So it is left alone,
    # and FLAGGED instead: a config block where everything carries a trailing comma
    # gives the password one too, and the desk then sees "authentication failed" for a
    # password they can see is correct.
    assert Settings(smtp_pass="secret,").smtp_pass == "secret,"
    assert Settings(smtp_pass="hnwi okbt ggah blrw,").smtp_password_looks_mangled() is True
    assert Settings(smtp_pass="hnwi okbt ggah blrw").smtp_password_looks_mangled() is False
    assert Settings(smtp_pass="").smtp_password_looks_mangled() is False
