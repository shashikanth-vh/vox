"""The monitor, the official sources and the evidence reading.

Upstreams are stubbed: a fake search engine that counts Google requests like the real
gate, and saved pages for SEBI and IBBI. Run standalone with PULSE_TESTS_NO_REGISTER=1.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.news import monitor as mon
from app.news import official
from app.news import schedules as sched
from app.news.evidence import read_evidence
from app.news.search import strict_queries

TODAY = datetime.now().strftime("%Y%m%d")


# --------------------------------------------------------------------------- #
# Google: names in one OR query, people in one more, the adverse words in a third
# --------------------------------------------------------------------------- #
def test_google_takes_a_firms_names_in_one_query_and_bing_one_each():
    google, bing = strict_queries(["Enerparc Energy", "Enerparc", "EEPL"])
    assert google == ['"Enerparc" OR "EEPL"'], "the longer name adds nothing"
    assert bing == ['"Enerparc"', '"EEPL"']


def test_people_with_cities_are_grouped_so_or_binds_the_pairs():
    google, bing = strict_queries(["Ravi Rao", "Anil Kumar"],
                                  {"Ravi Rao": "Pune", "Anil Kumar": "Delhi"})
    assert google == ['("Ravi Rao" Pune) OR ("Anil Kumar" Delhi)']
    assert bing == ['"Ravi Rao" Pune', '"Anil Kumar" Delhi']


def test_the_adverse_query_asks_google_for_the_names_with_the_adverse_words():
    google, bing = strict_queries(["Acme Solar"], adverse=True)
    assert google[0] == '"Acme Solar"'
    assert google[1].startswith('("Acme Solar") (fraud OR default OR insolvency')
    assert bing == ['"Acme Solar"'], "Bing has no OR, so no adverse query"


def test_a_long_list_of_people_is_chunked():
    names = [f"Person Number{i}" for i in range(11)]
    google, _ = strict_queries(names)
    assert len(google) == 2 and google[0].count(" OR ") == 7


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title,nature,stage", [
    ("SEBI probes Acme Solar promoter over alleged fund diversion", "allegation", "pending"),
    ("SEBI bars Acme Solar promoter from markets for two years", "action", "pending"),
    ("NCLT admits insolvency plea against Acme Solar", "action", "pending"),
    ("Acme Solar promoter acquitted in loan fraud case", "outcome", "final"),
    ("Court upholds conviction of Acme Solar director", "outcome", "final"),
    ("Acme Solar appeals SEBI order before SAT", "action", "appeal"),
    ("Acme Solar commissions 50 MW plant in Rajasthan", "report", "unknown"),
])
def test_evidence_tells_an_allegation_from_an_order_from_an_outcome(title, nature, stage):
    ev = read_evidence(title)
    assert (ev["nature"], ev["stage"]) == (nature, stage), ev


def test_evidence_names_the_forum_the_amount_and_a_favourable_outcome():
    ev = read_evidence("ED attaches assets worth Rs 1,200 crore of Acme Solar promoter")
    assert ev["forums"] == ["ED"]
    assert ev["amount"] == "Rs 1,200 crore" and ev["amount_cr"] == 1200.0
    assert read_evidence("High Court quashes FIR against Acme Solar MD")["favourable"]
    assert read_evidence("Acme bags Rs 80 lakh order")["amount_cr"] == 0.8
    assert read_evidence("Acme to raise $12 million")["amount_cr"] is None, \
        "dollars are printed, not converted at a guessed rate"
    assert read_evidence("Acme Solar results")["basis"] == "headline"
    # cipher-test, 16 Oct: "arbitrator" contains "rbi".
    ev = read_evidence("Supreme Court Appoints Justice R V Raveendran As Sole Arbitrator "
                       "In Kinetic Green-Saera Dispute")
    assert ev["forums"] == ["Supreme Court"]


# --------------------------------------------------------------------------- #
# Official sources
# --------------------------------------------------------------------------- #
SEBI_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>SEBI</title>
<item><title>Order in the matter of Acme Solar Ltd</title>
<description>Order in the matter of Acme Solar Ltd</description>
<link>https://www.sebi.gov.in/enforcement/orders/oct-2026/order-in-the-matter_1.html</link>
<pubDate>15 Oct, 2026 +0530</pubDate></item>
<item><title>Notice of Demand against Ravi Kumar Rao</title>
<description>x</description>
<link>https://www.sebi.gov.in/enforcement/recovery-proceedings/oct-2026/n_2.html</link>
<pubDate>14 Oct, 2026 +0530</pubDate></item>
<item><title>Release Order in respect of Beta Power Limited</title><description>x</description>
<link>https://www.sebi.gov.in/enforcement/recovery-proceedings/oct-2026/r_3.html</link>
<pubDate>13 Oct, 2026 +0530</pubDate></item>
<item><title>Circular on mutual fund disclosures - Acme Solar</title><description>x</description>
<link>https://www.sebi.gov.in/legal/circulars/oct-2026/c_4.html</link>
<pubDate>13 Oct, 2026 +0530</pubDate></item>
</channel></rss>"""

IBBI_HTML = """<html><body><table class="views-table">
<thead><tr><th>Type of PA</th><th>Date of Announcement</th><th>Last date of Submission</th>
<th>Name of Corporate Debtor</th><th>Name of Applicant</th>
<th>Name of Insolvency Professional</th><th>Public Announcement</th><th>Remarks</th></tr></thead>
<tbody>
<tr><td>Public Announcement of Corporate Insolvency Resolution Process</td><td>15-10-2026</td>
<td>23-10-2026</td><td>VATIKA IT PARKS PRIVATE LIMITED</td><td>BEACON TRUSTEESHIP LIMITED</td>
<td>Mrs. Megha Agrawal</td><td><a href="/uploads/announcement/pa1.pdf">(1.64 MB)</a></td><td></td></tr>
<tr><td>Public Announcement of Voluntary Liquidation Process</td><td>15-10-2026</td>
<td>12-11-2026</td><td>CONQOLL TECHNOLOGIES PRIVATE LIMITED</td>
<td>CONQOLL TECHNOLOGIES PRIVATE LIMITED</td><td>Sriram K</td>
<td><a href="/uploads/announcement/pa2.pdf">(574 KB)</a></td><td></td></tr>
</tbody></table></body></html>"""


@pytest.mark.parametrize("title,nature", [
    # The Gensol run, cipher-test 16 Oct: these read as plain "report" before.
    ("NCLT Ahmedabad Refuses Blanket Warranty Coverage For Gensol's EV Fleet, Orders "
     "Vehicle Return", "action"),
    ("CBI registers case against BluSmart's founders Anmol Singh Jaggi and Puneet Singh "
     "Jaggi", "action"),
    ("NCLT Ahmedabad Appoints Forensic Auditor To Verify Rs 1.34 Crore Deposit", "action"),
    # Without a forum, the same words are not an act.
    ("Acme Solar bags orders worth Rs 200 crore", "report"),
])
def test_a_named_forum_that_orders_or_registers_a_case_has_acted(title, nature):
    assert read_evidence(title)["nature"] == nature


NSE_ROWS = [
    {"sm_name": "Gensol Engineering Limited", "desc": "Disclosure of defaults on payment "
     "of interest/ repayment of principal amount", "attchmntText": "Default in payment",
     "an_dt": "15-Oct-2026 18:01:02", "attchmntFile": "https://nsearchives.nseindia.com/a.pdf"},
    {"sm_name": "Acme Solar Holdings Limited", "desc": "Resignation of Director/KMP/SMP",
     "attchmntText": "Resignation of Mr X as Independent Director", "an_dt": "14-Oct-2026"},
    {"sm_name": "Acme Solar Holdings Limited", "desc": "Credit Rating",
     "attchmntText": "CRISIL has reaffirmed its rating", "an_dt": "14-Oct-2026"},
    {"sm_name": "Beta Power Limited", "desc": "Credit Rating",
     "attchmntText": "ICRA downgraded the long-term rating", "an_dt": "14-Oct-2026"},
    {"sm_name": "Beta Power Limited", "desc": "Copy of Newspaper Publication",
     "attchmntText": "Notice of court convened meeting", "an_dt": "14-Oct-2026"},
    {"sm_name": "Beta Power Limited", "desc": "Outcome of Board Meeting",
     "attchmntText": "Results approved", "an_dt": "14-Oct-2026"},
]


def test_nse_keeps_only_adverse_filings():
    recs = official.parse_nse(NSE_ROWS)
    assert [(r["company"], r["severity"]) for r in recs] == [
        ("Gensol Engineering Limited", "UGLY"),
        ("Acme Solar Holdings Limited", "BAD"),
        ("Beta Power Limited", "BAD"),          # the downgrade, read from the text
    ], "a reaffirmation, a court-convened meeting and a board meeting are not news"
    assert recs[0]["when"] == "20261015"
    assert recs[0]["url"] == "https://nsearchives.nseindia.com/a.pdf"


def test_two_live_nse_filings_a_grant_is_not_ugly_and_an_inspection_is():
    """cipher-test, 16 Oct, the first two NSE matches: a USDA attestation filed under
    "Granting/withdrawal/…/suspension of key licenses" was read as UGLY; a DGGI
    inspection said so only in its PDF's file name."""
    rows = [
        {"sm_name": "Mukka Proteins Limited",
         "desc": "Granting/withdrawal/surrender/cancellation/suspension of key licenses/ "
                 "regulatory approvals",
         "attchmntText": "Mukka Proteins Limited has informed the Exchange about receipt "
                         "of a USDA NOP Inputs Attestation.", "an_dt": "06-Oct-2026",
         "attchmntFile": "https://nsearchives.nseindia.com/corporate/"
                         "MUKKA_06102026152536_Intimation_of_NOP_Attestation.pdf"},
        {"sm_name": "Jet Freight Logistics Limited", "desc": "Action(s) taken or orders passed",
         "attchmntText": "Jet Freight Logistics Limited has informed the Exchange about "
                         "Action(s) taken or orders passed", "an_dt": "15-Oct-2026",
         "attchmntFile": "https://nsearchives.nseindia.com/corporate/"
                         "JETFREIGHT_15102026194909_Reg_30_Intimation_DGGI_Inspection_Signed.pdf"},
        {"sm_name": "Beta Power Limited",
         "desc": "Granting/withdrawal/surrender/cancellation/suspension of key licenses",
         "attchmntText": "Cancellation of the factory licence by the state board",
         "an_dt": "15-Oct-2026"},
    ]
    recs = official.parse_nse(rows)
    assert [(r["company"], r["severity"]) for r in recs] == [
        ("Jet Freight Logistics Limited", "UGLY"), ("Beta Power Limited", "BAD")]
    hit = official.match_official(recs, {"id": "j", "name": "Jet Freight Logistics",
                                         "legal_name": "Jet Freight Logistics Limited"})[0]
    ev = hit["evidence"]
    assert (ev["nature"], ev["stage"]) == ("action", "pending")
    assert ev["forums"] == ["GST / DGGI", "Exchange"], "the file name names the forum"


def test_nse_filings_match_the_registered_name_exactly():
    recs = official.parse_nse(NSE_ROWS)
    gensol = {"id": "g", "name": "Gensol Engineering",
              "legal_name": "Gensol Engineering Ltd"}
    hits = official.match_official(recs, gensol)
    assert len(hits) == 1 and hits[0]["source"] == "NSE"
    assert hits[0]["evidence"]["forums"] == ["Exchange"]
    assert official.match_official(recs, {"id": "x", "name": "Gensol",
                                          "legal_name": "Gensol Ltd"}) == []


def test_sebi_keeps_enforcement_items_and_reads_its_date():
    recs = official.parse_sebi(SEBI_XML)
    assert [r["title"][:12] for r in recs] == ["Order in the", "Notice of De", "Release Orde"]
    assert recs[0]["when"] == "20261015" and recs[0]["severity"] == "UGLY"
    assert recs[1]["record_type"] == "recovery"
    assert recs[2]["severity"] == "BAD", "a release order is a record, not a new action"


def test_ibbi_is_read_by_header_and_links_the_announcement():
    recs = official.parse_ibbi(IBBI_HTML)
    assert len(recs) == 2
    assert recs[0]["debtor"] == "VATIKA IT PARKS PRIVATE LIMITED"
    assert recs[0]["when"] == "20261015"
    assert recs[0]["url"] == "https://ibbi.gov.in/uploads/announcement/pa1.pdf"
    assert recs[0]["severity"] == "UGLY" and recs[1]["severity"] == "BAD"
    assert recs[0]["title"].startswith("Corporate Insolvency Resolution Process: VATIKA")


def test_a_changed_ibbi_layout_is_an_error_not_a_quiet_day():
    with pytest.raises(ValueError, match="layout changed"):
        official.parse_ibbi("<html><table><tr><td>nothing</td></tr></table></html>")


def test_official_records_match_the_exact_registered_name_only():
    recs = official.parse_ibbi(IBBI_HTML) + official.parse_sebi(SEBI_XML)
    vatika = {"id": "1", "name": "Vatika IT Parks", "legal_name": "Vatika IT Parks Pvt. Ltd."}
    hits = official.match_official(recs, vatika)
    assert [h["source"] for h in hits] == ["IBBI"]
    assert hits[0]["evidence"]["nature"] == "action"
    assert hits[0]["evidence"]["basis"] == "official record"
    # A short name inside a longer registered name is not the same company.
    assert official.match_official(recs, {"id": "2", "name": "Vatika",
                                          "legal_name": "Vatika Limited"}) == []


def test_sebi_matches_the_firm_or_a_key_person_by_full_name():
    recs = official.parse_sebi(SEBI_XML)
    acme = {"id": "1", "name": "Acme Solar", "legal_name": "Acme Solar Limited",
            "people": [{"name": "Ravi Kumar Rao", "role": "Promoter"}, {"name": "Ravi"}]}
    hits = official.match_official(recs, acme)
    assert [h["title"][:12] for h in hits] == ["Order in the", "Notice of De"]
    assert hits[1]["about"] == "Promoter · Ravi Kumar Rao"
    assert official.match_official(recs, {"id": "3", "name": "Beta Power",
                                          "legal_name": "Beta Power Ltd"})[0]["severity"] == "BAD"


# --------------------------------------------------------------------------- #
# The monitor
# --------------------------------------------------------------------------- #
class FakeSearch:
    """Counts Google requests like SourceGate.sent, one per Google query."""

    def __init__(self, articles: dict[str, list[dict[str, Any]]] | None = None,
                 down: set[str] | None = None) -> None:
        self.articles = articles or {}
        self.down = down or set()
        self.google = 0
        self.calls: list[dict[str, Any]] = []

    def sent(self, source: str) -> int:
        return self.google

    async def http(self) -> Any:
        return None

    async def search_detail(self, term, dfrom, dto, limit=40, live=False, **kw):
        self.calls.append({"term": term, "dfrom": dfrom, **kw})
        g, _ = strict_queries(kw.get("names") or [term], kw.get("contexts"),
                              kw.get("adverse", False))
        self.google += len(g)
        if term in self.down:
            return [], [{"name": "Google News", "ok": False,
                         "error": "HTTP 503 from the source", "throttled": True},
                        {"name": "Bing News", "ok": True}]
        return list(self.articles.get(term, [])), [{"name": "Google News", "ok": True},
                                                    {"name": "Bing News", "ok": True}]


def _art(title: str, severity: str = "UGLY", **kw) -> dict[str, Any]:
    return {"title": title, "url": f"https://n/{abs(hash(title))}", "source": "ET",
            "when": TODAY, "severity": severity, "category": "adverse", **kw}


def _monitor(tmp_path, firms, search, *, per_hour=300, sebi=False, ibbi=False, nse=False):
    filed: list[tuple[str, str, dict[str, Any]]] = []

    async def firms_fn(tenant):
        return firms

    async def file_fn(tenant, key, row):
        filed.append((tenant, key, row))

    cfg = mon.MonitorConfig(google_per_hour=per_hour, sebi=sebi, ibbi=ibbi, nse=nse)
    m = mon.Monitor(mon.MonitorStore(str(tmp_path / "monitor.json")), search,
                    firms_fn, file_fn, cfg, ["EVAM"])
    return m, filed


FIRMS = [{"id": f"id{i}", "name": f"Firm{i} Solar", "legal_name": f"Firm{i} Solar Pvt Ltd",
          "people": [], "watch": True} for i in range(6)]


@pytest.mark.asyncio
async def test_an_hourly_pass_spends_only_the_google_budget(tmp_path):
    search = FakeSearch()
    m, _ = _monitor(tmp_path, FIRMS, search, per_hour=6)   # 2 Google queries a firm
    run = await m.run("EVAM")
    assert run["done"] == 3 and run["skipped_for_budget"] == 3
    assert "3 firms wait for the next hour" in run["note"]
    # The next hour takes the three that waited, not the three just searched.
    run2 = await m.run("EVAM")
    assert run2["total"] == 3 and run2["done"] == 3
    st = m.status("EVAM")
    assert st["searched_fresh"] == 6 and st["never_searched"] == 0


# --------------------------------------------------------------------------- #
# The night window: batches start only inside it, the digest reads them at 7
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec,hours", [
    ("0-6", (0, 6)), ("22-5", (22, 5)), (" 1 - 7 ", (1, 7)), ("20-24", (20, 0)),
    ("", None), ("all", None), ("0-24", None),
])
def test_monitor_hours_parse(spec, hours):
    assert mon.parse_hours(spec) == hours


@pytest.mark.parametrize("spec", ["6", "0-25", "24-6", "5-5", "night", "0:00-6:00"])
def test_a_monitor_hours_typo_is_refused(spec):
    with pytest.raises(ValueError):
        mon.parse_hours(spec)


@pytest.mark.parametrize("hour,inside", [(23, False), (0, True), (5, True), (6, False),
                                         (12, False)])
def test_batches_start_only_inside_the_night_window(hour, inside):
    assert mon.in_hours((0, 6), datetime(2026, 10, 17, hour, 59)) is inside


def test_a_window_across_midnight():
    assert mon.in_hours((22, 5), datetime(2026, 10, 17, 23, 0))
    assert mon.in_hours((22, 5), datetime(2026, 10, 17, 4, 0))
    assert not mon.in_hours((22, 5), datetime(2026, 10, 17, 5, 0))


def test_outside_the_window_the_next_batch_is_when_it_opens(tmp_path):
    m, _ = _monitor(tmp_path, FIRMS, FakeSearch())
    m.config.hours = (0, 6)
    noon = datetime(2026, 10, 17, 12, 0)
    assert m.opens_at(noon) == datetime(2026, 10, 18, 0, 0).timestamp()
    assert m.opens_at(datetime(2026, 10, 17, 3, 0)) == datetime(2026, 10, 17, 3, 0).timestamp()
    m.config.hours = None
    assert m.opens_at(noon) == noon.timestamp()


@pytest.mark.asyncio
async def test_the_loop_starts_no_batch_outside_the_window(tmp_path, monkeypatch):
    m, _ = _monitor(tmp_path, FIRMS, FakeSearch())
    started: list[str] = []
    monkeypatch.setattr(m, "start", lambda tenant, full: started.append(tenant) or True)

    async def one_tick(inside: bool) -> None:
        monkeypatch.setattr(m, "in_hours", lambda now=None: inside)
        task = asyncio.create_task(m.loop(tick_seconds=0))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await one_tick(False)
    assert started == [], "a batch due at noon waits for the night"
    await one_tick(True)
    assert started and started[0] == "EVAM"


def test_the_default_night_window_covers_a_pass_before_the_digest():
    s = get_settings()
    hours = mon.parse_hours(s.monitor_hours)
    assert hours == (0, 6), "ends an hour before the 7 AM digest"
    batches = hours[1] - hours[0]
    # ~2 Google searches a firm (+1 with people): ~500 firms is ~1,100 a pass.
    assert batches * s.monitor_google_per_hour >= 1100
    assert batches < s.monitor_refresh_hours < 24, "each firm once a night, never twice"


def test_a_garbled_monitor_hours_keeps_the_night_window(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("PULSE_MONITOR_HOURS", "midnight to six")
    assert Settings().monitor_hours == "0-6"
    monkeypatch.setenv("PULSE_MONITOR_HOURS", "1-7,")
    assert Settings().monitor_hours == "1-7"
    assert Settings().monitor_config().hours == (1, 7)


@pytest.mark.asyncio
async def test_a_firm_with_a_gap_is_not_marked_searched_and_goes_first_next_time(tmp_path):
    search = FakeSearch(down={"Firm2 Solar"})
    m, _ = _monitor(tmp_path, FIRMS, search)
    await m.run("EVAM")
    st = m.status("EVAM")
    assert st["searched_fresh"] == 5
    gap = next(g for g in st["gaps"] if g["id"] == "id2")
    assert gap["down"]["Google News"] == "HTTP 503 from the source"
    search.down.clear()
    run = await m.run("EVAM")
    assert run["total"] == 1, "only the firm with the gap is due"
    assert m.status("EVAM")["searched_fresh"] == 6


@pytest.mark.asyncio
async def test_a_story_is_filed_once_per_firm_however_often_it_is_found(tmp_path):
    a = _art("Firm0 Solar promoter arrested in Rs 40 crore loan fraud")
    twin = {**a, "url": "https://other/1", "source": "Bing"}   # same story, other URL
    search = FakeSearch({"Firm0 Solar": [a, twin]})
    m, filed = _monitor(tmp_path, FIRMS[:1], search)
    await m.run("EVAM", full=True)
    await m.run("EVAM", full=True)
    assert len(filed) == 1
    _, key, row = filed[0]
    assert key.startswith("pulse:EVAM:id0:")
    assert row["intel_type"] == "News" and row["signal"] == "RED"
    assert row["payload"]["evidence"]["nature"] == "action"
    assert row["payload"]["evidence"]["amount_cr"] == 40.0
    assert row["payload"]["monitor"] is True
    assert row["observed_at"].startswith(f"{TODAY[:4]}-{TODAY[4:6]}-{TODAY[6:]}")


@pytest.mark.asyncio
async def test_a_firm_named_by_one_common_word_is_not_searched_but_named_as_a_gap(tmp_path):
    """cipher-test, 16 Oct: firms registered as "Land", "Pure" and "Vikas" filed 47 of
    the first 50 adverse stories — land-fraud cases, an IAS officer called Vikas. Such
    a firm is searched only under news names an analyst sets."""
    firms = [{"id": "land", "name": "Land", "legal_name": "Land", "watch": True},
             {"id": "vikas", "name": "Vikas", "legal_name": "Vikas", "watch": True,
              "people": [{"name": "Ravi Kumar Rao"}]},
             {"id": "named", "name": "Land", "legal_name": "Land",
              "aliases": ["Land Renewables"], "watch": True},
             {"id": "ipec", "name": "IPEC", "legal_name": "IPEC", "watch": True}]
    search = FakeSearch()
    m, _ = _monitor(tmp_path, firms, search)
    await m.run("EVAM", full=True)
    asked = [c["names"] for c in search.calls]
    assert ["Land"] not in asked and ["Vikas"] not in asked
    assert ["Land Renewables"] in asked and ["IPEC"] in asked
    assert ["Ravi Kumar Rao"] in asked, "the firm's people are still searched"
    gaps = {g["id"]: g["down"] for g in m.status("EVAM")["gaps"]}
    assert "set its news names" in gaps["land"]["News search"]
    assert "named" not in gaps and "ipec" not in gaps
    from app.news.names import too_common
    assert too_common("Visakha") and too_common("Pure")
    assert not too_common("3eco") and not too_common("Savë") and not too_common("Trontek")
    assert not too_common("Land Renewables"), "two words are a name, not a word"


@pytest.mark.asyncio
async def test_a_full_pass_ignores_the_hourly_budget(tmp_path):
    search = FakeSearch()
    m, _ = _monitor(tmp_path, FIRMS, search, per_hour=1)
    run = await m.run("EVAM", full=True)
    assert run["done"] == 6 and run["skipped_for_budget"] == 0
    assert run["kind"] == "full"


@pytest.mark.asyncio
async def test_a_firms_people_are_one_search_and_their_stories_say_who(tmp_path):
    firm = {**FIRMS[0], "people": [{"name": "Ravi Kumar Rao", "role": "MD", "city": "Pune"},
                                   {"name": "Anil Mehta"}]}
    story = _art("Ravi Kumar Rao questioned by ED", matched="Ravi Kumar Rao")
    search = FakeSearch({"people of Firm0 Solar": [story]})
    m, filed = _monitor(tmp_path, [firm], search)
    await m.run("EVAM", full=True)
    people_call = next(c for c in search.calls if c["term"].startswith("people of"))
    assert people_call["names"] == ["Ravi Kumar Rao", "Anil Mehta"]
    assert people_call["contexts"] == {"Ravi Kumar Rao": "Pune"}
    assert filed[0][2]["payload"]["about"] == "MD · Ravi Kumar Rao"
    assert search.google == 2 + 1, "names + adverse, and ONE query for both people"


@pytest.mark.asyncio
async def test_official_sources_are_read_once_and_matched_to_every_firm(tmp_path, monkeypatch):
    calls = {"sebi": 0, "ibbi": 0}

    async def sebi(client):
        calls["sebi"] += 1
        return official.parse_sebi(SEBI_XML)

    async def ibbi(client, pages):
        calls["ibbi"] += 1
        return official.parse_ibbi(IBBI_HTML), 1

    monkeypatch.setattr(official, "fetch_sebi", sebi)
    monkeypatch.setattr(official, "fetch_ibbi", ibbi)
    firms = [{"id": "v", "name": "Vatika IT Parks", "legal_name": "Vatika IT Parks Pvt Ltd"},
             {"id": "a", "name": "Acme Solar", "legal_name": "Acme Solar Limited"},
             *FIRMS[:3]]
    m, filed = _monitor(tmp_path, firms, FakeSearch(), sebi=True, ibbi=True)
    await m.run("EVAM", full=True)
    assert calls == {"sebi": 1, "ibbi": 1}, "one read for the whole book"
    reg = [(row["entity_id"], row["intel_type"], row["source"]) for _, _, row in filed]
    assert ("v", "Regulatory", "PULSE:IBBI") in reg and ("a", "Regulatory", "PULSE:SEBI") in reg
    st = m.status("EVAM")["official"]
    assert st["IBBI"]["ok"] and st["IBBI"]["matched"] == 1
    assert "MCA" in m.status("EVAM")["not_covered"]


@pytest.mark.asyncio
async def test_an_official_source_that_fails_is_reported_and_the_pass_goes_on(tmp_path,
                                                                            monkeypatch):
    async def broken(client, *a, **kw):
        raise RuntimeError("HTTP 503 from the source")

    monkeypatch.setattr(official, "fetch_sebi", broken)
    monkeypatch.setattr(official, "fetch_ibbi", broken)
    m, _ = _monitor(tmp_path, FIRMS[:2], FakeSearch(), sebi=True, ibbi=True)
    run = await m.run("EVAM", full=True)
    assert run["done"] == 2
    o = m.status("EVAM")["official"]
    assert not o["SEBI"]["ok"] and "503" in o["SEBI"]["detail"]


@pytest.mark.asyncio
async def test_the_monitors_memory_survives_a_restart(tmp_path):
    search = FakeSearch({"Firm0 Solar": [_art("Firm0 Solar raided")]})
    m, filed = _monitor(tmp_path, FIRMS[:1], search)
    await m.run("EVAM", full=True)
    m2, filed2 = _monitor(tmp_path, FIRMS[:1], search)
    await m2.run("EVAM", full=True)
    assert len(filed) == 1 and filed2 == [], "the second process remembers what was filed"
    assert m2.status("EVAM")["searched_fresh"] == 1


# --------------------------------------------------------------------------- #
# Promoters and directors from the desk's leads
# --------------------------------------------------------------------------- #
def test_a_leads_contact_is_a_promoter_only_with_a_leadership_role_and_a_full_name():
    leads = [
        {"entity_id": "e1", "contact": "Sameer Patwa", "designation": "MD", "city": "Pune"},
        {"entity_id": "e1", "contact": "sameer patwa", "designation": "Director"},  # dup
        {"entity_id": "e1", "contact": "Prashant", "designation": "Founder"},       # 1 word
        {"entity_id": "e1", "contact": "Anil Mehta", "designation": "BD Manager"},
        {"entity_id": "e1", "contact": "Raj Kumar", "designation": ""},             # unknown
        {"entity_id": "e2", "contact": "inderjeet sanyal", "designation": "Co-Founder & CEO"},
        {"entity_id": "e2", "contact": "Pratik Kamdar", "designation": "Promotor"},
        {"entity_id": None, "contact": "No Firm", "designation": "MD"},
    ]
    got = mon.lead_people(leads)
    assert [p["name"] for p in got["e1"]] == ["Sameer Patwa"]
    assert got["e1"][0] == {"name": "Sameer Patwa", "role": "MD", "city": "Pune",
                            "source": "lead"}
    assert [p["name"] for p in got["e2"]] == ["Inderjeet Sanyal", "Pratik Kamdar"]
    merged = mon.merge_people([{"name": "Sameer Patwa", "role": "Promoter"}], got["e1"])
    assert merged == [{"name": "Sameer Patwa", "role": "Promoter"}], "the analyst's entry wins"


@pytest.mark.asyncio
async def test_a_story_about_a_lead_contact_must_also_name_the_firm_or_the_city(tmp_path):
    firm = {**FIRMS[0], "city": "Pune",
            "people": [{"name": "Sameer Patwa", "role": "MD", "source": "lead"},
                       {"name": "Ravi Kumar Rao", "role": "Promoter"}]}
    stories = [
        _art("Sameer Patwa arrested in Firm0 Solar loan fraud", matched="Sameer Patwa"),
        _art("Pune businessman Sameer Patwa questioned by ED", matched="Sameer Patwa"),
        _art("Sameer Patwa wins chess title", "GOOD", matched="Sameer Patwa"),  # namesake
        _art("Ravi Kumar Rao questioned by ED", matched="Ravi Kumar Rao"),     # curated
    ]
    search = FakeSearch({"people of Firm0 Solar": stories})
    m, filed = _monitor(tmp_path, [firm], search)
    await m.run("EVAM", full=True)
    titles = sorted(row["title"] for _, _, row in filed)
    assert titles == sorted([stories[0]["title"], stories[1]["title"], stories[3]["title"]])
    call = next(c for c in search.calls if c["term"].startswith("people of"))
    assert call["contexts"] == {"Sameer Patwa": "Pune"}, "a lead contact gets the firm's city"
    st = m.status("EVAM")["people"]
    assert st == {"firms_with_people": 1, "from_leads": 1, "people": 2}


@pytest.mark.asyncio
async def test_the_history_pass_searches_a_year_back(tmp_path):
    search = FakeSearch()
    m, _ = _monitor(tmp_path, FIRMS[:1], search)
    await m.run("EVAM", full=True)
    await m.run("EVAM", full=True, history=True)
    first, hist = search.calls[0]["dfrom"], search.calls[-1]["dfrom"]
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    assert hist == year_ago and first > year_ago
    assert m.store.tenant("EVAM")["run"]["kind"] == "history"


@pytest.mark.asyncio
async def test_ibbi_history_is_read_once_a_day_and_on_its_own_without_google(tmp_path,
                                                                           monkeypatch):
    pages_read: list[int] = []

    async def ibbi(client, pages):
        pages_read.append(pages)
        return official.parse_ibbi(IBBI_HTML), 1

    monkeypatch.setattr(official, "fetch_ibbi", ibbi)
    firms = [{"id": "v", "name": "Vatika IT Parks", "legal_name": "Vatika IT Parks Pvt Ltd"}]
    search = FakeSearch()
    m, filed = _monitor(tmp_path, firms, search, ibbi=True)
    await m.run("EVAM")                       # first batch of the day: the history
    await m.run("EVAM", full=True)            # same day: the newest pages
    assert pages_read == [100, 3]
    search.google = 0
    await m.run_official("EVAM")              # on demand: history, and no Google at all
    assert pages_read[-1] == 100 and search.google == 0
    assert m.store.tenant("EVAM")["run"]["kind"] == "official"
    assert len(filed) == 1, "the Vatika CIRP is filed once, however often it is read"


# --------------------------------------------------------------------------- #
# The digest reads what the monitor filed
# --------------------------------------------------------------------------- #
def _row(entity: str, title: str, when: str, **kw) -> dict[str, Any]:
    return {"entity_id": entity, "intel_type": "News", "signal": "RED", "title": title,
            "url": "https://n/" + title[:5], "source": "PULSE:ET",
            "payload": {"severity": "UGLY", "when": when, "monitor": True,
                        "evidence": {"nature": "allegation"}}, **kw}


def test_stored_findings_are_grouped_by_firm_inside_the_window():
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
    rows = [_row("id0", "Firm0 raided", TODAY), _row("id0", "Firm0 old story", old),
            _row("id1", "Firm1 dismissed", TODAY, is_dismissed=True),
            _row("gone", "Not a watched firm", TODAY)]
    state = {"firms": {"id0": {"last_ok": time.time()},
                       "id1": {"last_ok": 0, "down": {"Google News": "HTTP 503"}}},
             "official": {"SEBI": {"ok": False, "at": time.time(), "detail": "HTTP 503"}}}
    pairs, cov = mon.findings_digest(rows, FIRMS[:3], state, week_ago, today)
    assert pairs == [("Firm0 Solar", [pairs[0][1][0]])]
    assert pairs[0][1][0]["title"] == "Firm0 raided"
    assert pairs[0][1][0]["evidence"] == {"nature": "allegation"}
    assert cov["firms"] == 3
    assert cov["down"]["Google News"] == 1, "Firm1: searched, but Google refused"
    assert cov["down"]["News search"] == 1, "Firm2: still in the queue"
    assert cov["down"]["SEBI"] == 3


@pytest.mark.asyncio
async def test_an_all_firms_digest_with_the_monitor_does_not_search():
    sent: dict[str, Any] = {}

    async def search(*a, **kw):
        raise AssertionError("the digest must read the monitor's findings, not search")

    async def stored(dfrom, dto):
        return [("Firm0 Solar", [_art("Firm0 Solar raided")])], {"firms": 6, "down": {}}

    def send(to, subject, body):
        sent.update(subject=subject, body=body)
        return True, "Sent"

    ok, msg, firms, items = await sched.run_schedule(
        {"id": "S1", "scope": "all-firms", "recipients": ["a@b.c"], "q": ""},
        search=search, send=send, digest=sched_digest, stored=stored)
    assert ok and (firms, items) == (1, 1)
    assert "all 6 firms searched on every source" in msg
    assert "Firm0 Solar" in sent["subject"]


def test_the_email_says_what_each_adverse_story_asserts():
    from app.news.mailer import firm_card
    a = _art("SEBI bars Firm0 promoter", evidence=read_evidence(
        "SEBI bars Firm0 promoter, Rs 25 crore penalty"))
    card = firm_card("Firm0 Solar", [a])
    assert "Action taken · pending · SEBI · Rs 25 crore" in card
    rec = official.match_official(official.parse_ibbi(IBBI_HTML),
                                  {"id": "v", "name": "Vatika IT Parks",
                                   "legal_name": "Vatika IT Parks Private Limited"})
    assert "Official record — Action taken · pending · IBBI · NCLT" in firm_card("V", rec)
    assert "Allegation" not in firm_card("Firm0", [_art("Firm0 wins award", "GOOD")])


def sched_digest(groups, dfrom, dto, **kw):
    from app.news.mailer import digest_page
    return digest_page(groups, dfrom, dto, **kw)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PULSE_SCHEDULE_FILE", str(tmp_path / "schedules.json"))
    monkeypatch.setenv("PULSE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("PULSE_MONITOR_FILE", str(tmp_path / "monitor.json"))
    monkeypatch.setenv("PULSE_MONITOR_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_the_status_route_says_what_is_and_is_not_covered(client):
    r = client.get("/v1/news/monitor")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["store_ok"] is True
    assert body["google_per_hour"] == 300 and body["run"]["running"] is False
    assert body["hours"] == "00:00–06:00"
    assert "NCLT / eCourts" in body["not_covered"]


def test_scan_one_firm_and_scan_all_routes(client, monkeypatch):
    m = client.app.state.monitor

    async def scan_firm(tenant, entity_id):
        return 2 if entity_id == "id0" else None

    started: list[tuple[str, bool]] = []
    monkeypatch.setattr(m, "scan_firm", scan_firm)
    monkeypatch.setattr(m, "start", lambda tenant, full, history=False, official_only=False:
                        started.append((tenant, history)) or len(started) == 1)
    r = client.post("/v1/news/monitor/scan", json={"scope": "firm", "entity_id": "id0"})
    assert r.json() == {"ok": True, "done": True, "filed": 2}
    assert client.post("/v1/news/monitor/scan",
                       json={"scope": "firm", "entity_id": "nope"}).status_code == 404
    assert client.post("/v1/news/monitor/scan",
                       json={"scope": "all", "history": True}).status_code == 202
    assert client.post("/v1/news/monitor/scan", json={"scope": "all"}).status_code == 409
    assert started[0] == ("EVAM", True)
    assert client.post("/v1/news/monitor/scan",
                       json={"scope": "official"}).status_code == 409, "one pass at a time"
