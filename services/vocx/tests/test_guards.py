"""Deterministic output guards — driven by the two-test diagnostic's own words.

The fixtures below are lifted from the report's appendices (the raw Test 2
transcript), so the guards are proven against the exact production sentences
that motivated them: the in-principle upgrade the correction should have
capped, the reversed collateral negation, the unspoken score.
"""

from __future__ import annotations

from app.vocx.pipeline.guards import (
    apply_extract_guards,
    apply_spec_guards,
    lender_log_flags,
    negation_sentences,
    score_suggestion_flag,
    stage_evidence_level,
    stage_guard_text,
)

# Condensed from the diagnostic's Appendix D — the sentences that matter.
TEST2 = (
    "Kotak Mahindra has shared an in principle approval for 12 crore rupees. "
    "The proposal is subject to satisfactory DD, promoter contribution of 3 crore "
    "rupees and the creation of first charge over new machinery. This is only "
    "indicative proposal and not a sanction. AU Small Finance Bank has asked for "
    "100% collateral cover which the promoters are unwilling to provide. The "
    "company has already approached State Bank of India for the bank guarantee "
    "but no formal sanction has been received."
)


def test_the_controlling_correction_caps_the_stage_ladder():
    """The diagnostic's exact case: 'in principle approval' was (mis)heard, the
    speaker corrected to 'only indicative proposal and not a sanction'. The
    evidence level is rung 1 — the correction controls."""
    assert stage_evidence_level(TEST2) == 1


def test_stage_wording_is_downgraded_never_silently_kept():
    value = "Kotak Mahindra has shared an in-principle approval for ₹12 Cr."
    new, note = stage_guard_text(value, stage_evidence_level(TEST2))
    assert "in-principle" not in new.lower()
    assert "indicative proposal" in new
    assert note and "downgraded" in note


def test_plain_stage_evidence_still_reads_each_rung():
    assert stage_evidence_level("Kotak shared an indicative proposal.") == 1
    assert stage_evidence_level("HDFC gave an in-principle approval.") == 2
    assert stage_evidence_level("The loan was sanctioned on Friday.") == 3
    # A negated sanction is evidence AGAINST rung 3.
    assert stage_evidence_level("No formal sanction has been received.") == 0


def test_a_true_sanction_is_never_downgraded():
    ev = stage_evidence_level("The facility was sanctioned yesterday.")
    new, note = stage_guard_text("Sanctioned: ₹5 Cr facility.", ev)
    assert new == "Sanctioned: ₹5 Cr facility." and note is None


def test_negation_sentences_catch_the_reversal_risks():
    got = " ".join(negation_sentences(TEST2))
    assert "unwilling to provide" in got
    assert "no formal sanction" in got
    assert "not a sanction" in got


def test_an_unspoken_score_is_labelled_a_suggestion():
    assert score_suggestion_flag(4, TEST2)                       # never spoken
    spoken = TEST2 + " I would currently rate this opportunity 4 out of 5."
    assert score_suggestion_flag(4, spoken) is None              # spoken → no flag
    assert score_suggestion_flag(None, TEST2) is None            # no score → no flag


def test_spec_report_guards_rewrite_cells_and_flag():
    report = {
        "common": {
            "meeting_summary": {"value": "Kotak shared an in-principle approval "
                                          "for ₹12 Cr.", "confidence": "high"},
            "opportunity_score": {"value": 4, "confidence": "medium"},
            "data_quality_flags": {"value": [], "confidence": "n/a"},
        },
        "syndication": {
            "remarks": {"value": "In-principle approval received from Kotak.",
                        "confidence": "high"},
        },
    }
    notes = apply_spec_guards(report, TEST2)
    assert "indicative proposal" in report["syndication"]["remarks"]["value"]
    assert "indicative proposal" in report["common"]["meeting_summary"]["value"]
    assert any("downgraded" in n for n in notes)
    assert any("AI suggestion" in n for n in notes)
    assert any("negation check" in n for n in notes)


def test_extract_report_gains_quality_flags():
    ext = {"report": {"summary": "Kotak gave an in-principle approval.",
                      "key_intel": ["In-principle approval of ₹12 Cr from Kotak"],
                      "nuances": [], "pipeline_stage": "Sanctioned",
                      "opportunity_score": 4}}
    apply_extract_guards(ext, TEST2)
    rep = ext["report"]
    assert "indicative proposal" in rep["summary"]
    assert "indicative proposal" in rep["key_intel"][0]
    flags = rep["quality_flags"]
    assert any("Sanctioned" in f for f in flags)      # chip flagged, not rewritten
    assert any("AI suggestion" in f for f in flags)
    assert any("negation check" in f for f in flags)


TEST1_ASK = ("The company presently requires a 2 crore working capital loan. The "
             "management would like the loan to be sanctioned within the next "
             "three weeks.")


def test_a_requested_sanction_is_aspiration_not_evidence():
    """Test 1's own sentence: 'would like the loan to be sanctioned within three
    weeks' is a REQUEST. It must not unlock stage wording — this exact hole let
    Regional's invented 'indicative proposal stage' remark pass unflagged on the
    staging A/B (23 Sep)."""
    assert stage_evidence_level(TEST1_ASK) == 0
    value = "The 2 crore working capital ask is at the indicative proposal stage."
    _new, note = stage_guard_text(value, stage_evidence_level(TEST1_ASK))
    assert note and "does not evidence any stage" in note


def test_a_faithful_sanction_request_in_a_field_is_not_a_claim():
    """The mirror case: a field that reports 'sanction requested within three
    weeks' repeats the ask faithfully — it claims no rung and must not be
    flagged as one (the false positive the aspirational rule also prevents)."""
    value = "Working capital loan of ₹2 Cr; sanction requested within three weeks."
    new, note = stage_guard_text(value, stage_evidence_level(TEST1_ASK))
    assert new == value and note is None


def test_candidate_lenders_never_become_chase_entries():
    """The 23 Sep live failure verbatim: 'we may approach X, Y, Z' produced
    four 'chase' entries reading 'identified as a probable lender' — a fake
    interaction ledger that would pollute the deal's chase board on approval.
    The guard drops every entry with no spoken chase/reply behind it, and the
    aspirational 'sanctioned within three weeks' nearby must not evidence one."""
    transcript = (
        "Apart from Evam's loan, the company requires an additional 10 crore "
        "term loan for expanding its manufacturing facility. We may approach "
        "Axis Bank, Kotak Mahindra Bank, Bajaj Finance and Aditya Birla for "
        "this requirement. The company has asked Evam to manage the entire "
        "debt syndication process. The management would like the loan to be "
        "sanctioned within the next three weeks.")
    report = {"syndication": {"lender_updates": {"value": [
        {"lender": "Axis Bank", "kind": "chase",
         "note": "Identified as a probable lender for the 10 Cr term loan."},
        {"lender": "Kotak Mahindra Bank", "kind": "chase",
         "note": "Identified as a probable lender for the 10 Cr term loan."},
        {"lender": "Bajaj Finance", "kind": "chase",
         "note": "Identified as a probable lender."},
    ], "confidence": "medium"}}}
    notes = lender_log_flags(report, transcript)
    cell = report["syndication"]["lender_updates"]
    assert cell["value"] == [] and cell["confidence"] == "n/a"
    assert len(notes) == 3
    assert all("no chase or reply was spoken" in n for n in notes)
    assert any("Axis Bank" in n for n in notes)


def test_real_lender_interactions_survive_the_log_guard():
    """The ledger's legitimate content — a spoken chase and a spoken reply —
    passes untouched, including when the verb sits in the neighbouring
    sentence, and the fabricated entry beside them is the only one dropped."""
    transcript = (
        "Chased Godrej Capital for the IM response, no reply yet. I also "
        "spoke about Axis Finance. They reverted with queries on the security "
        "structure. We may approach Tata Capital next month.")
    report = {"syndication": {"lender_updates": {"value": [
        {"lender": "Godrej Capital", "kind": "chase", "note": "Chased for the IM."},
        {"lender": "Axis Finance", "kind": "reply", "note": "Queries on security."},
        {"lender": "Tata Capital", "kind": "chase", "note": "Identified as probable."},
    ], "confidence": "medium"}}}
    notes = lender_log_flags(report, transcript)
    kept = [e["lender"] for e in report["syndication"]["lender_updates"]["value"]]
    assert kept == ["Godrej Capital", "Axis Finance"]
    assert len(notes) == 1 and "Tata Capital" in notes[0]


def test_narrator_role_labels_are_scrubbed_from_prose():
    """The 23 Sep takes: 'Tech (narrator) and Pallavi met...' and 'Pallavi and
    the recorder (Tech) met...' — the name stands alone, exactly as any other
    attendee's. The scrub is deterministic label hygiene, never content."""
    from app.vocx.pipeline.guards import strip_narrator_labels
    assert (strip_narrator_labels("Tech (narrator) and Pallavi met Rohan.")
            == "Tech and Pallavi met Rohan.")
    assert (strip_narrator_labels("Pallavi and the recorder (Tech) met Rohan.")
            == "Pallavi and Tech met Rohan.")
    assert (strip_narrator_labels("The Recorder (Tech) rated it 4 out of 5.")
            == "Tech rated it 4 out of 5.")
    # Legitimate parentheticals survive untouched.
    keep = "Rohan Mehta (MD) and Priya Sharma (CFO) attended; PPA (25-year) discussed."
    assert strip_narrator_labels(keep) == keep
    # And the spec-report wrapper applies it to summary cells.
    report = {"common": {"meeting_summary": {
        "value": "Tech (narrator) and Pallavi met Rohan Mehta (MD).",
        "confidence": "high"}}}
    apply_spec_guards(report, "We met Rohan and discussed the site visit.")
    assert (report["common"]["meeting_summary"]["value"]
            == "Tech and Pallavi met Rohan Mehta (MD).")
