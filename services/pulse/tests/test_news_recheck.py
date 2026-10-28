"""The radar's precision fixes and the re-check of filed stories (Oct 2026: cipher-test
showed more UGLY than production — namesakes and NCLT merger hearings filed by a
12-month history pass and never judged again; and news printed under a firm's legal
name was never searched). Every firm here is made up; the headline shapes are real."""

from __future__ import annotations

import asyncio

from app.news import monitor as mon
from app.news.names import too_common, watch_names
from app.news.triage import triage


# ------------------------------------------------------------------------ names
def test_an_everyday_phrase_is_not_a_name_but_a_coined_word_makes_one():
    for phrase in ("Bright Solar", "Prime Consultants", "Green Energy", "Sun Power"):
        assert too_common(phrase), phrase
    # Three ordinary words together are a company's name, not a phrase.
    for name in ("Corvane Enviro", "Velstra Energy", "IPEC Solar", "Concord Systems",
                 "Blue Power Motors"):
        assert not too_common(name), name


def test_the_full_and_legal_names_are_always_searched_with_the_analysts_names():
    assert watch_names("Velstra Batteries", ["Velstra"], legal="Quorin Lithium Power") == [
        "Velstra Batteries", "Quorin Lithium Power", "Velstra"]
    # An everyday full name is searched only under the analyst's names.
    assert watch_names("Bright Solar", ["Bright Solar Ventures BSV"]) == [
        "Bright Solar Ventures BSV"]


def test_the_monitor_searches_without_limited_and_under_the_legal_name():
    names, short, aliases = mon.firm_names({"name": "Velstra Energy Limited",
                                            "legal_name": "Quorin Power Private Limited"})
    assert names == ["Velstra Energy", "Quorin Power", "Velstra"]
    assert short == "Velstra" and aliases == []


# ----------------------------------------------------------------------- triage
def test_probe_is_adverse_only_as_an_investigation():
    assert triage("Velstra plans to double probe capacities on AI boom").severity != "UGLY"
    assert triage("Structural insights revealed through an organic-framework probe"
                  ).severity != "UGLY"
    assert triage("ED launches probe into Velstra Energy").severity == "UGLY"
    assert triage("Police probe land deal involving Velstra").severity == "UGLY"


def test_an_nclt_merger_scheme_is_restructuring_not_insolvency():
    assert triage("Corvane Enviro NCLT scheme hearing set for July 23").severity != "UGLY"
    assert triage("Velstra announces NCLT hearing for Buildco merger").severity != "UGLY"
    assert triage("NCLT admits insolvency plea against Velstra Energy").severity == "UGLY"
    assert triage("Velstra Energy dragged to NCLT by lender").severity == "UGLY"


# ------------------------------------------------------------- who it is about
FIRM = {"id": "e1", "name": "Velstra Energy", "city": "Coimbatore"}


def _about(title: str, firm: dict = FIRM) -> bool:  # noqa: B006
    names, short, _a = mon.firm_names(firm)
    return mon.about_firm({"title": title}, firm, names, short)


def test_a_story_must_print_the_firm_as_a_name():
    generic = {"id": "e2", "name": "Corvane"}
    assert not _about("Structural insights through a corvane organic-framework reagent", generic)
    assert _about("Corvane wins state award", generic)


def test_a_short_name_alone_needs_the_firms_sector_or_city():
    assert not _about("Velstra semiconductor strike exceeds 40 days")       # a namesake
    assert _about("Velstra commissions 40 MW solar plant")                  # sector
    assert _about("Velstra opens new office in Coimbatore")                 # city
    assert _about("Velstra Energy posts record quarter")                    # full name


# ------------------------------------------------------------------- the re-check
def _row(title: str, severity: str = "UGLY", **payload: str) -> dict:
    return {"id": "r1", "entity_id": "e1", "intel_type": "News", "signal": "RED",
            "title": title, "row_version": 3,
            "payload": {"monitor": True, "severity": severity, "when": "20260601", **payload}}


def test_a_filed_namesake_is_dismissed_and_a_merger_hearing_regraded():
    verdict = mon.rejudge(_row("Velstra semiconductor strike exceeds 40 days"), FIRM)
    assert verdict is not None and "namesake" in verdict["dismiss"]
    verdict = mon.rejudge(_row("Velstra Energy NCLT scheme hearing set for July 23"), FIRM)
    assert verdict == {"regrade": "GOOD"}
    assert mon.rejudge(_row("Velstra Energy wins 40 MW order", "GOOD"), FIRM) is None
    # A firm named by an everyday phrase cannot own a story until it has news names.
    plain = {"id": "e3", "name": "Bright Solar"}
    verdict = mon.rejudge(_row("Bright Solar customers face delays"), plain)
    assert verdict is not None and "set its news names" in verdict["dismiss"]
    named = {**plain, "aliases": ["Bright Solar Ventures"]}
    assert mon.rejudge(_row("Bright Solar Ventures wins order", "GOOD"), named) is None
    # A person's story and an official record keep their own matching.
    assert mon.rejudge(_row("Strike exceeds 40 days", about="Director · A Kumar"), FIRM) is None
    official = {**_row("Strike"), "intel_type": "Regulatory"}
    assert mon.rejudge(official, FIRM) is None


def test_the_recheck_writes_each_verdict_with_its_reason_at_the_version_read(tmp_path):
    rows = [_row("Velstra semiconductor strike exceeds 40 days"),
            {**_row("Velstra Energy NCLT scheme hearing set for July 23"), "id": "r2"},
            {**_row("Velstra Energy wins 40 MW order", "GOOD"), "id": "r3"}]
    updates: list[tuple[str, dict, dict]] = []

    async def read(tenant: str) -> list[dict]:
        return rows

    async def write(tenant: str, row: dict, data: dict) -> None:
        updates.append((tenant, row, data))

    async def firms(tenant: str) -> list[dict]:
        return [FIRM]

    m = mon.Monitor(mon.MonitorStore(str(tmp_path / "monitor.json")), None, firms,
                    None, mon.MonitorConfig(), ["EVAM"],  # type: ignore[arg-type]
                    rows=read, update=write)
    got = asyncio.run(m.recheck("evam"))
    assert got["checked"] == 3 and got["dismissed"] == 1 and got["regraded"] == 1
    (_, r1, d1), (_, r2, d2) = updates
    assert r1["row_version"] == 3 and d1["is_dismissed"] is True
    assert "namesake" in d1["payload"]["recheck"] and d1["payload"]["rechecked"]
    assert d2["signal"] == "GREEN" and d2["payload"]["severity"] == "GOOD"
    assert d2["payload"]["recheck"] == "re-graded UGLY → GOOD by today's rules"
    assert m.status("EVAM", [FIRM])["recheck"]["dismissed"] == 1


def test_a_stylised_name_a_registered_name_and_the_sector_keep_a_real_story():
    assert _about("e-Velstra G1 on-road price in Pune", {"id": "e4", "name": "e-Velstra"})
    reg = {"id": "e5", "name": "Prime Energy", "legal_name": "Prime Energy Private Limited"}
    names, short, _a = mon.firm_names(reg)
    assert names == []                                   # an everyday phrase: not searched
    assert mon.about_firm({"title": "Module order from Prime Energy Private Limited"}, reg,
                          names, short)
    assert mon.rejudge(_row("Module order from Prime Energy Private Limited", "GOOD"),
                       reg) is None
    assert _about("e-VELSTRA launches G1 electric scooter", {"id": "e4", "name": "e-Velstra"})
    assert _about("Velstra, Corvane lead 3W retail sales in April")      # sector words
    # Everyday words are no sector: a namesake's wedding behind "barbed wire".
    assert not _about("Barbed wire, metal barricades ahead of Velstra Cohen's wedding")


def test_the_recheck_restores_what_it_dismissed_but_never_a_persons_dismissal():
    wrongly = {**_row("e-Velstra G1 on-road price in Pune", "GOOD"), "is_dismissed": True}
    wrongly["payload"] = {**wrongly["payload"], "recheck": "not about e-Velstra …",
                          "dismissed_by": "recheck"}
    assert mon.rejudge(wrongly, {"id": "e1", "name": "e-Velstra"}) == {"restore": ""}
    by_desk = {**_row("e-Velstra G1 on-road price in Pune", "GOOD"), "is_dismissed": True}
    assert mon.rejudge(by_desk, {"id": "e1", "name": "e-Velstra"}) is None
    still_noise = {**wrongly, "title": "Velstra semiconductor strike exceeds 40 days"}
    assert mon.rejudge(still_noise, FIRM) is None        # stays dismissed
