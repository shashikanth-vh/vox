"""The canonical correction pass — the diagnostic's garbles, fixed before any model.

Fixtures are the observed corruptions from the two-test report (appendices B/D)
and the staging A/B: this pass is what stops both engines inheriting them.
"""

from __future__ import annotations

from app.vocx.pipeline.canonical import canonicalize_transcript

GARBLED = ("The company's existing bankers are HDFC Bank and ICAC Bank. The company "
           "requires a 2 crore working capital loan from AVM Finance. Pallavi will "
           "collect the USD returns. We may approach Access Bank, Kotak Menindra Bank, "
           "Bajaj Finance and Ajit Berla. The company has asked AVM to manage the "
           "entire dexindication process. Greenco Industrial Solutions placed the "
           "order; Exact Climate Solutions declined. This requirement consists of a "
           "5 crore performance by guarantee. AU asked for 100% collateral curve. "
           "The creation of first order over new machinery. We have to leverage our "
           "existing reprimand obligations.")


def test_the_diagnostics_garbles_all_become_canonical():
    out, notes = canonicalize_transcript(GARBLED)
    for fixed in ("ICICI Bank", "Evam Finance", "GST returns", "Axis Bank",
                  "Kotak Mahindra Bank", "Aditya Birla", "debt syndication",
                  "Greenko", "Hexa Climate Solutions",
                  "performance bank guarantee", "collateral cover",
                  "first charge over", "repayment obligations"):
        assert fixed in out, fixed
    for gone in ("ICAC", "AVM", "USD returns", "Access Bank", "Menindra",
                 "Berla", "dexindication", "Greenco", "reprimand"):
        assert gone not in out, gone
    # Every replacement is flagged, once, naming both sides.
    assert any("ICAC Bank" in n and "ICICI Bank" in n for n in notes)
    assert any("AVM" in n and "Evam" in n for n in notes)
    assert all(n.startswith("canonical:") for n in notes)


def test_fuzzy_lender_matching_is_conservative():
    # A fresh mishearing of a KNOWN lender is caught by the roster...
    out, notes = canonicalize_transcript("We spoke to Kotak Mahendru Bank yesterday.")
    assert "Kotak Mahindra Bank" in out
    assert any("roster" in n for n in notes)
    # ...but an unknown institution is left exactly as heard: never guessed.
    keep, notes2 = canonicalize_transcript(
        "They also bank with Suryoday Small Finance Bank and Jana Bank.")
    assert "Jana Bank" in keep
    assert "Suryoday" in keep


def test_ordinary_english_is_never_touched():
    text = ("Access to the site was granted. The first order of business is the "
            "document list; the USD exposure is hedged.")
    out, notes = canonicalize_transcript(text)
    assert out == text
    assert notes == []


def test_config_aliases_extend_the_table():
    out, notes = canonicalize_transcript(
        "We met Sungrower Energies today.",
        extra_aliases={"Sungrower Energies": "SunGarner Energies"})
    assert "SunGarner Energies" in out
    assert any("Sungrower" in n for n in notes)
