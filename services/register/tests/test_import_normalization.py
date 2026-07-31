"""Pure-function tests for the MIS import normalisation layer — no database.

The value census below is EXACTLY the distinct lifecycle values (with counts) observed
in Evam_ATLAS_MIS_Consolidated_v4.xlsx. The contract these tests pin: every one of
those values imports (zero omissions) — funnel terms verbatim into deals.stage (the
funnel IS the deal's stage since the two-layer migration; a deal carries no credit
lifecycle), wording/case variants canonicalised (and reported), the Deal-Status overlay
mapping the syndication terminals — while a genuinely unknown value still fails the
vocabulary screen (fail-closed for future drift).
"""

from __future__ import annotations

import uuid

from evam_backend_core.rbac import DEAL_FUNNEL_STAGES, STAGE_VOCAB

from app.seed.from_xlsx import _canon_funnel, _canon_value, _map_credit_stage

# sheet value → expected outcome, straight from the v4 census
V4_DEALS_STAGE = {
    "In Pipeline": 45, "Screened Out": 38, "Closed Won": 15, "In Screening": 14,
    "New Inquiry": 10, "On Hold": 3, "Closed Lost": 2,
}
V4_SYN_BANK_STATUS = {
    "IM Circulated": "IM Circulated", "Rejected": "Rejected",
    "Queries Received": "Queries Received", "IP received": "IP Received",
    "IM under preparation": "IM in Prep", "Final sanction received": "Sanctioned",
    "IM Sent": "IM Circulated",
}
V4_LENDING_STAGE = ["Rejected", "Disbursed", "Data Awaited", "Sanctioned",
                    "Diligence", "On Hold"]
V4_AM_STATUS = ["In Discussion", "Dropped", "NBO Received", "Teaser Prepared"]


def test_every_v4_deals_stage_is_funnel_vocabulary():
    for value in V4_DEALS_STAGE:
        assert _canon_funnel(value) == value          # verbatim — no translation
        assert value in DEAL_FUNNEL_STAGES
    # case/spacing-insensitive
    assert _canon_funnel("in  pipeline") == "In Pipeline"
    assert _canon_funnel("SCREENED OUT") == "Screened Out"
    # a credit value is NOT funnel — routes to the credit path
    assert _canon_funnel("Diligence") is None
    assert _canon_funnel("Totally Unknown") is None


def test_v4_syndication_statuses_all_canonicalise():
    vocab = STAGE_VOCAB["Syndication"][1]
    for raw, want in V4_SYN_BANK_STATUS.items():
        got = _canon_value("Syndication", raw)
        assert got == want, (raw, got)
        assert got in vocab
    # the Deal-Status overlay terminals exist in the vocabulary
    assert {"Dropped", "Disbursed"} <= vocab
    # unknown wording still refuses (fail-closed for future drift)
    assert _canon_value("Syndication", "Vibes Good") == "Vibes Good"
    assert "Vibes Good" not in vocab


def test_v4_lending_and_am_values_screen_clean():
    lend_vocab = STAGE_VOCAB["Lending"][1]
    for v in V4_LENDING_STAGE:
        mapped = _map_credit_stage(_canon_value("Lending", v))
        assert mapped in lend_vocab, (v, mapped)
    am_vocab = STAGE_VOCAB["AssetMonetisation"][1]
    for v in V4_AM_STATUS:
        assert _canon_value("AssetMonetisation", v) in am_vocab


def test_deal_stage_is_the_funnel_and_credit_values_screen_out():
    # Since the two-layer migration the funnel IS deals.stage: STAGE_VOCAB screens the
    # Deals sheet against the funnel, so every v4 value passes and a credit-lifecycle
    # word landing on the Deals sheet quarantines by name instead of importing.
    field, vocab = STAGE_VOCAB["Deal"]
    assert field == "stage"
    assert vocab == frozenset(DEAL_FUNNEL_STAGES)
    for value in V4_DEALS_STAGE:
        assert value in vocab
    for credit_only in ("Data Awaited", "Diligence", "Note Circulated", "Sanctioned"):
        assert credit_only not in vocab


def test_funnel_vocabulary_matches_schema_literal():
    from app.schemas.resources import DealCreate
    for value in DEAL_FUNNEL_STAGES:
        assert DealCreate(entity_id=uuid.uuid4(), stage=value).stage == value
