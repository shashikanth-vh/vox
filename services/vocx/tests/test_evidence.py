"""Evidence quotes, one amount per lane, two readings reconciled, lender names
resolved — the structuring-output discipline added after the 1 Oct 2026
bake-off take."""

from __future__ import annotations

import json

from app.vocx.pipeline.evidence import (
    amount_lane_guard,
    carries_evidence,
    evidence_guard,
    quote_supported,
)
from app.vocx.pipeline.names import resolve_lender_names, resolve_one
from app.vocx.pipeline.reconcile import reconcile_readings
from app.vocx.spec.contract import build_tool_schema, validate_report

TRANSCRIPT = ("Pallavi and Tech met Rohan Maitha at the Whitefield office. Reported revenue of "
              "eighty five crore and an order book of sixty crore. They need a two crore working "
              "capital loan for raw material purchase. A manufacturing expansion loan is proposed for "
              "syndication via Axis Bank, Kotak Mahindra Bank, Bajaj Finance and Aditya Birla Finance. "
              "The promoters are exploring the sale of a thirty megawatt operational solar project in "
              "Karnataka valued at about one hundred and twenty crore.")


def _cell(value, confidence="high", evidence=None):
    c = {"value": value, "confidence": confidence}
    if evidence is not None:
        c["evidence"] = evidence
    return c


# ----------------------------------------------------------------- quotes

def test_a_quote_is_supported_verbatim_or_by_most_of_its_words():
    assert quote_supported("two crore working capital loan", TRANSCRIPT)
    assert quote_supported("a 2 crore working capital loan for raw material", TRANSCRIPT)  # STT drift
    assert not quote_supported("deal size of two crore syndicated", TRANSCRIPT)
    assert not quote_supported("", TRANSCRIPT) and not quote_supported(None, TRANSCRIPT)


def test_a_fact_without_words_behind_it_drops_to_low_and_is_flagged():
    report = {
        "common": {"location": _cell("Whitefield, Bangalore", "high", "met Rohan Maitha at the Whitefield office")},
        "lending": {"requirement_quantum_cr": _cell(2, "high", "two crore working capital loan")},
        "syndication": {"deal_size_cr": _cell(7, "high"),                                  # no quote, no 7 anywhere
                        "existing_lenders": _cell("Nowhere Bank", "high", "nothing like this was said")},
    }
    notes = evidence_guard(report, TRANSCRIPT)
    assert report["common"]["location"]["confidence"] == "high"
    assert report["lending"]["requirement_quantum_cr"]["confidence"] == "high"
    assert report["syndication"]["deal_size_cr"]["confidence"] == "low"
    assert report["syndication"]["existing_lenders"]["confidence"] == "low"
    assert "evidence" not in report["syndication"]["existing_lenders"]       # a false quote is dropped
    assert any("Deal size" in n or "deal_size" in n for n in notes)
    assert any("no transcript words behind" in n for n in notes)


def test_the_guard_stands_down_for_a_reading_that_never_quoted():
    report = {"lending": {"requirement_quantum_cr": _cell(25, "high")}}
    assert not carries_evidence(report)
    assert evidence_guard(report, "twenty five crore ask") == []
    assert report["lending"]["requirement_quantum_cr"]["confidence"] == "high"


def test_a_reviewer_override_is_never_downgraded():
    report = {"lending": {"requirement_quantum_cr": {"value": 9, "confidence": "high",
                                                     "user_override": True},
                          "existing_bankers": _cell("SBI", "high", "banking with SBI")}}
    evidence_guard(report, "banking with SBI")
    assert report["lending"]["requirement_quantum_cr"]["confidence"] == "high"


# ------------------------------------------------------------ one lane

def test_the_same_amount_on_both_lanes_is_cleared_from_syndication_unless_quoted_as_syndicated():
    report = {"lending": {"requirement_quantum_cr": _cell(2, "high", "two crore working capital loan")},
              "syndication": {"deal_size_cr": _cell(2, "high", "two crore working capital loan")}}
    notes = amount_lane_guard(report)
    assert report["syndication"]["deal_size_cr"] == {"value": None, "confidence": "n/a"}
    assert report["lending"]["requirement_quantum_cr"]["value"] == 2
    assert notes and "both the lending ask and the syndication size" in notes[0]

    tied = {"lending": {"requirement_quantum_cr": _cell(5, "high", "five crore working capital")},
            "syndication": {"deal_size_cr": _cell(5, "high", "5 crore, we are taking it to syndication")}}
    assert amount_lane_guard(tied) == []
    assert tied["syndication"]["deal_size_cr"]["value"] == 5


def test_the_lane_guard_stands_down_without_evidence_and_with_distinct_amounts():
    legacy = {"lending": {"requirement_quantum_cr": _cell(5)}, "syndication": {"deal_size_cr": _cell(5)}}
    assert amount_lane_guard(legacy) == [] and legacy["syndication"]["deal_size_cr"]["value"] == 5
    distinct = {"lending": {"requirement_quantum_cr": _cell(2, "high", "two crore")},
                "syndication": {"deal_size_cr": _cell(20, "high", "twenty crore syndication")}}
    assert amount_lane_guard(distinct) == []


# --------------------------------------------------------- two readings

def test_a_disagreement_caps_both_readings_at_medium_and_flags_it():
    a = {"common": {"location": _cell("Electronic City"), "sector": _cell("Renewables"),
                    "meeting_summary": _cell("Pallavi met Rohan…", "n/a"),
                    "data_quality_flags": {"value": [], "confidence": "n/a"}},
         "syndication": {"deal_size_cr": _cell(None, "n/a"),
                         "probable_lenders": _cell("Axis Bank, Kotak Mahindra Bank")}}
    b = {"common": {"location": _cell("Whitefield"), "sector": _cell("Renewables"),
                    "meeting_summary": _cell("Tech met Pallavi…", "n/a")},
         "syndication": {"deal_size_cr": _cell(2), "probable_lenders": _cell("Axis Bank, Kotak Bank")}}
    flags = reconcile_readings(a, b)
    # the number differs: capped on the side that had it, flagged on both
    assert b["syndication"]["deal_size_cr"]["confidence"] == "medium"
    assert any("Deal size" in f or "deal_size" in f for f in flags)
    assert any("not captured" in f and "'2'" in f for f in flags)
    # short free text differing: a disagreement
    assert a["common"]["location"]["confidence"] == "medium"
    assert b["common"]["location"]["confidence"] == "medium"
    # the summary is prose: never compared, never flagged
    assert not any("summary" in f.lower() for f in flags)
    # agreed sector untouched
    assert a["common"]["sector"]["confidence"] == "high"
    assert flags and set(a["common"]["data_quality_flags"]["value"]) >= set(flags)
    assert set(b["common"]["data_quality_flags"]["value"]) >= set(flags)


def test_the_same_number_worded_two_ways_is_not_a_disagreement():
    a = {"lending": {"requirement_quantum_cr": _cell(2.0), "present_requirement": _cell("₹2 Cr working capital loan for raw material purchase, sought from UN Finance")}}
    b = {"lending": {"requirement_quantum_cr": _cell(2), "present_requirement": _cell("2 crore working capital loan for raw material purchase and manufacturing expansion")}}
    assert reconcile_readings(a, b) == []
    assert a["lending"]["requirement_quantum_cr"]["confidence"] == "high"


# ------------------------------------------------------------ names

LENDERS = ["SBI (State Bank of India)", "Axis Bank", "Kotak Mahindra Bank", "Aditya Birla Finance",
           "Bajaj Finance", "Union Bank of India", "Tata Capital"]


def test_near_misses_resolve_to_the_desks_spelling_and_unknowns_are_flagged():
    assert resolve_one("Aditya Billan Finance", LENDERS) == ("Aditya Birla Finance", "resolved")
    assert resolve_one("Kotak Bank", LENDERS) == ("Kotak Mahindra Bank", "resolved")
    assert resolve_one("State Bank of India", LENDERS) == ("SBI", "resolved")
    assert resolve_one("Axis Bank", LENDERS) == ("Axis Bank", "exact")
    assert resolve_one("UN Finance", LENDERS)[1] == "unknown"


def test_name_fields_and_lender_updates_are_rewritten_with_notes():
    report = {"lending": {"existing_bankers": _cell("Union Bank, HDFC Bank")},
              "syndication": {"probable_lenders": _cell("Axis Bank, Kotak Bank, Bajaj Finance and Aditya Billan"),
                              "lender_updates": {"value": [{"lender": "Aditya Billan Finance", "kind": "chase", "note": "x"}],
                                                 "confidence": "high"}}}
    notes = resolve_lender_names(report, LENDERS)
    assert report["syndication"]["probable_lenders"]["value"] == "Axis Bank, Kotak Mahindra Bank, Bajaj Finance, Aditya Birla Finance"
    assert report["syndication"]["lender_updates"]["value"][0]["lender"] == "Aditya Birla Finance"
    assert report["lending"]["existing_bankers"]["value"].startswith("Union Bank of India, ")
    assert any("'Kotak Bank' → 'Kotak Mahindra Bank'" in n for n in notes)
    assert any("HDFC Bank" in n and "not a lender the desk knows" in n for n in notes)
    assert resolve_lender_names(report, None) == []


# ---------------------------------------------------------- contract

def test_the_contract_accepts_and_schema_requires_the_evidence_quote():
    schema = build_tool_schema()
    lend = schema["properties"]["lending"]["properties"]["requirement_quantum_cr"]
    assert "evidence" in lend["properties"] and "evidence" in lend["required"]
    summ = schema["properties"]["common"]["properties"]["meeting_summary"]
    assert "evidence" in summ["properties"] and "evidence" not in summ["required"]
    from tests.test_spec_contract import _valid_report
    rep = _valid_report()
    rep["lending"]["requirement_quantum_cr"]["evidence"] = "twenty five crore project finance"
    validate_report(rep)                                           # accepted
    rep["lending"]["requirement_quantum_cr"]["evidence"] = ["not", "a", "quote"]
    try:
        validate_report(rep)
    except Exception as exc:  # noqa: BLE001
        assert "evidence must be a short transcript quote" in str(exc)
    else:
        raise AssertionError("a non-string evidence must be refused")


def test_the_scrub_keeps_a_quote_and_drops_a_structure():
    from app.vocx.pipeline.structure import _scrub_cell
    cell = {"value": 5, "confidence": "high", "evidence": "  five crore  ", "notes": "x"}
    _scrub_cell({"key": "requirement_quantum_cr", "type": "number"}, cell)
    assert cell == {"value": 5, "confidence": "high", "evidence": "five crore"}
    cell2 = {"value": 5, "confidence": "high", "evidence": {"quote": "five"}}
    _scrub_cell({"key": "requirement_quantum_cr", "type": "number"}, cell2)
    assert "evidence" not in cell2
    assert json.dumps(cell)  # serialisable


# ------------------------------------------------ relaxations, 1 Oct take 2

def test_a_value_plainly_in_the_transcript_survives_without_a_quote():
    """The batched Regional path quotes some cells and not others: a quantum of
    2 with 'two crore' in the transcript, a turnover of 85 with 'eighty five
    crore', a name list that is all there — none of these are unquoted facts."""
    report = {
        "common": {"location": _cell("Whitefield", "high", "a quote the transcript never said"),
                   "attendees_counterparty": _cell(["Rohan Maitha", "Pallavi", "Someone Else"], "high")},
        "lending": {"requirement_quantum_cr": _cell(2, "high"), "company_turnover_cr": _cell(85, "high"),
                    "existing_bankers": _cell("Nowhere Bank", "high")},
        "asset_monetisation": {"deal_size": _cell("120 crores", "high", "valued at about one hundred and twenty crore")},
    }
    notes = evidence_guard(report, TRANSCRIPT)
    assert report["lending"]["requirement_quantum_cr"]["confidence"] == "high"      # 'two crore'
    assert report["lending"]["company_turnover_cr"]["confidence"] == "high"         # 'eighty five crore'
    assert report["common"]["attendees_counterparty"]["confidence"] == "high"
    assert report["common"]["location"]["confidence"] == "high"                    # value itself is there
    assert "evidence" not in report["common"]["location"]                          # the false quote went
    assert report["lending"]["existing_bankers"]["confidence"] == "low"
    assert len(notes) == 1 and "Nowhere Bank" in notes[0]


def test_number_words_cover_the_spoken_forms():
    from app.vocx.pipeline.evidence import _number_words, value_supported
    assert "eighty five" in _number_words(85) and "eighty-five" in _number_words(85)
    assert "one hundred and twenty" in _number_words(120)
    assert value_supported(120, "valued at one hundred and twenty crore")
    assert value_supported(2.5, "two point five, i.e. 2.5 crore")
    assert not value_supported(7, "nothing with that number")


def test_wording_is_not_a_disagreement():
    a = {"common": {"location": _cell("Whitefield, Bangalore"), "opportunity_score": _cell(None, "n/a")},
         "asset_monetisation": {"deal_size": _cell("approximately ₹120 crore"),
                                "offer_components": _cell(["land", "ppa", "connectivity"])}}
    b = {"common": {"location": _cell("Whitefield"), "opportunity_score": _cell(4, "medium")},
         "asset_monetisation": {"deal_size": _cell("120 crores"),
                                "offer_components": _cell(["entire_project", "land", "ppa", "connectivity"])}}
    assert reconcile_readings(a, b) == []
    assert a["common"]["location"]["confidence"] == "high"
    c = {"asset_monetisation": {"offer_components": _cell(["land", "modules"])}}
    d = {"asset_monetisation": {"offer_components": _cell(["ppa", "connectivity"])}}
    assert any("Offer components" in f or "offer_components" in f for f in reconcile_readings(c, d))
