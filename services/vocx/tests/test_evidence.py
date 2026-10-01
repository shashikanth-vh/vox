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
        "syndication": {"deal_size_cr": _cell(2, "high"),                                  # no quote
                        "probable_lenders": _cell("Axis Bank, Kotak", "high", "nothing like this was said")},
    }
    notes = evidence_guard(report, TRANSCRIPT)
    assert report["common"]["location"]["confidence"] == "high"
    assert report["lending"]["requirement_quantum_cr"]["confidence"] == "high"
    assert report["syndication"]["deal_size_cr"]["confidence"] == "low"
    assert report["syndication"]["probable_lenders"]["confidence"] == "low"
    assert "evidence" not in report["syndication"]["probable_lenders"]       # a false quote is dropped
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
    a = {"common": {"location": _cell("Whitefield, Bangalore"), "sector": _cell("Renewables"),
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
