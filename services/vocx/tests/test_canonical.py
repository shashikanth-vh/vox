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


def test_the_fresh_take_variants_of_23_sep_become_canonical():
    # The first post-267 fresh recording surfaced four NEW renderings of already
    # known garbles — the alias store compounding exactly as designed.
    out, notes = canonicalize_transcript(
        "We may approach Access Bank, Kotak Menindra Bank, Bajaj Finance and "
        "Egypt Burla for this requirement. The company has asked AVM to manage "
        "the entire dex indication process. Pallavi will collect the audited "
        "financial statements, U.S. returns, bank statements, deteriorating and "
        "the details of the current order book.")
    assert "Aditya Birla" in out and "Egypt Burla" not in out
    assert "debt syndication" in out and "dex indication" not in out
    assert "GST returns" in out and "U.S." not in out
    assert "debtor ageing" in out and "deteriorating" not in out
    assert any("Egypt Burla" in n and "Aditya Birla" in n for n in notes)
    # The bare word stays untouchable: only the exact document-list bigram maps.
    keep, _ = canonicalize_transcript(
        "Asset quality is deteriorating. The fund gives us returns of 18%.")
    assert "deteriorating" in keep and "us returns" in keep


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
