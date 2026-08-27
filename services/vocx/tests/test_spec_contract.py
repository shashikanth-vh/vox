"""Phase 0 acceptance: the registry is the single source of truth, the contract
validates, and a broken object never best-efforts its way past the gate."""

import copy
import json

import pytest

from app.vocx.spec import (
    ContractError,
    compute_data_quality_flags,
    latest_prompt_version,
    latest_registry_version,
    load_prompt,
    load_registry,
    validate_report,
)


# ---------------------------------------------------------------- the registry

def test_the_registry_loads_and_is_v1():
    reg = load_registry()
    assert reg["registry_version"] == latest_registry_version() == "v1"
    assert set(reg["use_cases"]) == {
        "lending", "syndication", "asset_monetisation", "credit_diligence",
        "investor_relations", "banking_relations", "operations",
    }


def test_the_six_sector_taxonomy_is_locked_and_fully_canonicalised():
    reg = load_registry()
    assert len(reg["taxonomy"]) == 6
    subsectors = {s for subs in reg["taxonomy"].values() for s in subs}
    # every subsector owns its canonical data points, each with a hi/md marker
    assert set(reg["subsector_canonicals"]) == subsectors
    for sub, fields in reg["subsector_canonicals"].items():
        assert 2 <= len(fields) <= 3, sub
        assert any(f["conf"] == "hi" for f in fields), f"{sub} lacks a primary sizing metric"


def test_lending_speaks_quantum_not_ticket_size():
    reg = load_registry()
    labels = {f["key"]: f["label"] for f in reg["blocks"]["lending"]["fields"]}
    assert "Quantum" in labels["requirement_quantum_cr"]
    # no field is named or labelled ticket-anything (the ui_note MAY mention the ban)
    assert not any("ticket" in (f["key"] + f["label"]).lower() for f in reg["blocks"]["lending"]["fields"])


def test_thin_use_cases_ship_common_only():
    reg = load_registry()
    for uc in ("credit_diligence", "investor_relations", "banking_relations", "operations"):
        assert reg["blocks"][uc]["fields"] == []


def test_the_canonical_prompt_exists_with_its_anchor_rules():
    prompt = " ".join(load_prompt().split())  # anchors may span the spec's line breaks
    assert latest_prompt_version() == "v1"
    for anchor in (
        "Never fabricate",
        "party_role",
        "entity name",   # rule 5: no local normalisation
        "suspect segment",
        "lakh",
        "Return ONLY the single JSON object",
    ):
        assert anchor in prompt, f"prompt lost its anchor: {anchor}"


# ------------------------------------------------------------- a valid report

def _cell(value, confidence="high", **extra):
    return {"value": value, "confidence": confidence, **extra}


def _valid_report():
    return {
        "detected_use_cases": ["lending", "asset_monetisation"],
        "common": {
            "meeting_type": _cell("in_person"),
            "meeting_date": _cell("2026-08-20"),
            "location": _cell("Whitefield, Bangalore", "medium"),
            "sector": _cell("Renewables"),
            "subsector": _cell("Solar-Developer", "medium"),
            "attendees_counterparty": _cell(["R. Sharma (MD)"], "medium"),
            "key_discussion_points": _cell(["40 MW under construction"], "high"),
            "meeting_summary": _cell(None, "n/a"),
            "follow_up_time": _cell(None, "n/a"),
            "action_items": _cell([{"action": "Share DPR", "owner": "R. Sharma", "deadline": None}], "medium"),
            "next_steps": _cell("Review DPR together", "high"),
            "follow_up_date": _cell(None, "n/a"),
            "opportunity_assessment": _cell("Strong sponsor, real ask.", "n/a"),
            "opportunity_score": _cell(4, "medium", user_override=False),
            "opportunity_score_override_reason": _cell(None, "n/a"),
            "competitive_intelligence": _cell("", "n/a"),
            "data_quality_flags": _cell(["turnover not mentioned"], "n/a"),
        },
        "lending": {
            "requirement_nature": _cell("project_finance"),
            "requirement_quantum_cr": _cell(25, "low"),
            "company_turnover_cr": _cell(None, "n/a"),
            "existing_bankers": _cell("SBI", "medium"),
            "project_location": _cell("Karnataka", "medium"),
            "present_requirement": _cell("~25 Cr project finance for phase 1", "high"),
            "remarks": _cell(None, "n/a"),
        },
        "asset_monetisation": {
            "party_role": _cell("owner"),
            "deal_size": _cell("~180 Cr EV (indicative)", "medium"),
            "offer_components": _cell(["land", "ppa", "connectivity"], "high"),
            "asset_status": _cell("under_construction", "medium"),
            "asset_location": _cell("Chikkaballapur", "medium"),
            "offer_notes": _cell(None, "n/a"),
            "target_project_size": _cell(None, "n/a"),
            "valuation_approach": _cell(None, "n/a"),
            "buyer_criteria": _cell([], "n/a"),
            "remarks": _cell(None, "n/a"),
        },
        "entity_candidates": ["Suryodaya EPC", "SBI"],
    }


def test_the_contract_example_validates():
    assert validate_report(_valid_report()) is not None


def test_party_role_both_carries_owner_and_buyer_fields_together():
    r = _valid_report()
    am = r["asset_monetisation"]
    am["party_role"] = _cell("both")
    am["target_project_size"] = _cell("20-50 MW operational", "medium")
    am["valuation_approach"] = _cell("EV per MW", "low")
    am["buyer_criteria"] = _cell(["South India preferred"], "medium")
    validate_report(r)


# ------------------------------------------------- violations, all of them named

def _expect_error(report, needle):
    with pytest.raises(ContractError) as exc:
        validate_report(report)
    assert any(needle in e for e in exc.value.errors), exc.value.errors


def test_a_block_for_an_undetected_use_case_is_refused():
    r = _valid_report()
    r["syndication"] = {"facility_nature": _cell("ecb")}
    _expect_error(r, "absent means absent")


def test_a_detected_use_case_without_its_block_is_refused():
    r = _valid_report()
    del r["lending"]
    _expect_error(r, "lending: detected but its block is missing")


def test_missing_fields_must_be_null_not_omitted():
    r = _valid_report()
    del r["lending"]["remarks"]
    _expect_error(r, "lending.remarks: missing")


def test_unknown_fields_are_refused_not_absorbed():
    r = _valid_report()
    r["lending"]["ticket_size"] = _cell(10)
    _expect_error(r, "lending.ticket_size: unknown field")


def test_judgement_fields_must_carry_na():
    r = _valid_report()
    r["common"]["opportunity_assessment"] = _cell("Great!", "high")
    _expect_error(r, "judgement fields carry confidence 'n/a'")


def test_an_enum_outside_its_options_is_refused():
    r = _valid_report()
    r["lending"]["requirement_nature"] = _cell("venture_debt")
    _expect_error(r, "requirement_nature")


def test_the_subsector_must_live_under_its_sector():
    r = _valid_report()
    r["common"]["subsector"] = _cell("BESS-OEM", "medium")
    _expect_error(r, "not under 'Renewables'")


def test_score_override_shape_is_enforced():
    r = _valid_report()
    r["common"]["opportunity_score"] = {"value": 3, "confidence": "medium", "user_override": True}
    _expect_error(r, "overridden score carries confidence 'n/a'")
    r["common"]["opportunity_score"] = {"value": 3, "confidence": "n/a", "user_override": True}
    validate_report(r)


def test_score_bounds():
    r = _valid_report()
    r["common"]["opportunity_score"] = _cell(7, "high", user_override=False)
    _expect_error(r, "outside [1, 5]")


def test_no_use_cases_at_all_is_a_failure():
    r = _valid_report()
    r["detected_use_cases"] = []
    _expect_error(r, "at least one use case")


def test_a_non_object_never_reaches_the_database():
    with pytest.raises(ContractError):
        validate_report(["not", "a", "report"])


def test_every_violation_is_reported_not_just_the_first():
    r = _valid_report()
    del r["lending"]["remarks"]
    r["lending"]["ticket_size"] = _cell(10)
    r["common"]["opportunity_assessment"] = _cell("x", "high")
    with pytest.raises(ContractError) as exc:
        validate_report(r)
    assert len(exc.value.errors) >= 3


# ------------------------------------------------------------- quality nudges

def test_null_numerics_flag_but_never_block():
    r = _valid_report()
    r["lending"]["requirement_quantum_cr"] = _cell(None, "n/a")
    validate_report(r)  # still submits
    flags = compute_data_quality_flags(r)
    assert any("Quantum" in f for f in flags)


def test_lending_with_no_sector_raises_the_spec_flag():
    r = _valid_report()
    r["common"]["sector"] = _cell(None, "n/a")
    r["common"]["subsector"] = _cell(None, "n/a")
    validate_report(r)
    assert "sector not determinable" in compute_data_quality_flags(r)


def test_registry_bumps_never_mutate_old_rows():
    """Version migration posture: the validator runs against the version the row
    was processed under — load_registry is version-addressed, not latest-only."""
    reg_v1 = load_registry("v1")
    assert reg_v1["registry_version"] == "v1"
    # unknown versions fail loudly — a deploy problem, not a silent fallback
    from app.vocx.spec import RegistryError
    with pytest.raises(RegistryError):
        load_registry("v99")


# ------------------------------------------------- per-subsector canonicals (9.8)

def test_subsector_details_validate_against_the_chosen_subsector():
    r = _valid_report()
    r["subsector_details"] = {
        "operating_uc_capacity_mw": _cell("40 MW", "high"),
        "portfolio_stage": _cell("Under construction", "medium"),
    }
    validate_report(r)


def test_a_canonical_from_another_subsector_is_refused():
    r = _valid_report()
    r["subsector_details"] = {"chemistry": _cell("LFP")}   # BESS-OEM's, not Solar-Developer's
    _expect_error(r, "not a canonical data point of 'Solar-Developer'")


def test_details_without_a_subsector_are_refused():
    r = _valid_report()
    r["common"]["sector"] = _cell(None, "n/a")
    r["common"]["subsector"] = _cell(None, "n/a")
    r["subsector_details"] = {"operating_uc_capacity_mw": _cell("40 MW")}
    _expect_error(r, "present without a chosen subsector")


def test_dict_entries_in_detected_use_cases_are_named_not_crashed():
    """Field finding three: entries arrived as dicts and the duplicate check's set()
    raised a raw TypeError. The violation must be NAMED so the repair round can fix it."""
    r = _valid_report()
    r["detected_use_cases"] = [{"use_case": "lending"}, "asset_monetisation"]
    _expect_error(r, "entries must be plain use-case strings")


def test_normalize_coerces_spoken_enum_forms():
    """The transcript says "Seller" and "Under Construction"; the contract says
    "owner" and "under_construction". The model echoes the speech — and the strict
    enum refused it twice in the field (the Chikballapur bundle). _normalize now
    coerces label forms and everyday synonyms deterministically; garbage still fails."""
    from app.vocx.pipeline.structure import _normalize

    r = _valid_report()
    r["asset_monetisation"]["party_role"] = _cell("Seller")
    r["asset_monetisation"]["asset_status"] = _cell("Under Construction")
    r["common"]["meeting_type"] = _cell("In person")
    out = validate_report(_normalize(r))
    assert out["asset_monetisation"]["party_role"]["value"] == "owner"
    assert out["asset_monetisation"]["asset_status"]["value"] == "under_construction"
    assert out["common"]["meeting_type"]["value"] == "in_person"

    bad = _valid_report()
    bad["asset_monetisation"]["party_role"] = _cell("landlord")
    try:
        validate_report(_normalize(bad))
        raise AssertionError("a genuinely unknown enum value must still fail")
    except ContractError:
        pass


# ----------------------------------------------- the speech-echo class, closed
# Every coercion below is one deterministic spelling away from a validated field
# failure (the Chikballapur bundle) or its exact sibling. The rule throughout:
# a different spelling of the same fact folds; a different fact still fails.

from app.vocx.pipeline.structure import _normalize  # noqa: E402


def test_taxonomy_coercion_covers_every_sector_and_every_subsector():
    """The user's ask, verbatim: consider all sectors and subsectors too. The
    model echoing 'solar-epc', 'SOLAR EPC' or 'Solar EPC' for 'Solar-EPC' must
    fold to the registry's exact name — for all six sectors and all 32 subsectors."""
    reg = load_registry()
    for sector, subs in reg["taxonomy"].items():
        for spoken_sector in (sector.lower(), sector.upper(), sector.replace("&", "and")):
            r = _valid_report()
            r["common"]["sector"] = _cell(spoken_sector)
            r["common"]["subsector"] = _cell(None, "n/a")
            out = validate_report(_normalize(r))
            assert out["common"]["sector"]["value"] == sector, spoken_sector
        for sub in subs:
            for spoken in (sub.lower(), sub.upper(), sub.replace("-", " ")):
                r = _valid_report()
                r["common"]["sector"] = _cell(sector)
                r["common"]["subsector"] = _cell(spoken, "medium")
                r.pop("subsector_details", None)
                out = validate_report(_normalize(r))
                assert out["common"]["subsector"]["value"] == sub, (sector, spoken)


def test_sector_spoken_names_fold_to_the_locked_six():
    cases = {"Renewable Energy": "Renewables", "renewable": "Renewables",
             "Energy Storage": "BESS", "Battery Storage": "BESS",
             "EV": "EV Mobility", "Electric Mobility": "EV Mobility",
             "Agriculture": "Climate Resilience",
             "Industrial Decarbonization": "Industrial Decarbonisation",  # z-spelling
             "Water Treatment": "Water Treatment & Waste Management",
             "Solar": "Renewables"}  # names four subsectors — one shared roof
    for spoken, want in cases.items():
        r = _valid_report()
        r["common"]["sector"] = _cell(spoken)
        r["common"]["subsector"] = _cell(None, "n/a")
        out = validate_report(_normalize(r))
        assert out["common"]["sector"]["value"] == want, spoken


def test_a_subsector_names_its_parent_sector():
    r = _valid_report()
    r["common"]["sector"] = _cell(None, "n/a")
    r["common"]["subsector"] = _cell("Wind", "medium")
    out = validate_report(_normalize(r))
    assert out["common"]["sector"]["value"] == "Renewables"

    # And the more specific claim wins a contradiction — with a flag saying so.
    r = _valid_report()
    r["common"]["sector"] = _cell("BESS")
    r["common"]["subsector"] = _cell("Wind", "medium")
    out = validate_report(_normalize(r))
    assert out["common"]["sector"]["value"] == "Renewables"
    assert any("aligned" in f for f in out["common"]["data_quality_flags"]["value"])


def test_speech_outside_the_taxonomy_clears_with_a_flag_never_kills_the_take():
    r = _valid_report()
    r["common"]["sector"] = _cell("Textiles")
    r["common"]["subsector"] = _cell("Spinning mills", "medium")
    out = validate_report(_normalize(r))  # the take SURVIVES
    assert out["common"]["sector"]["value"] is None
    assert out["common"]["subsector"]["value"] is None
    flags = out["common"]["data_quality_flags"]["value"]
    assert any("Textiles" in f for f in flags) and any("Spinning" in f for f in flags)


def test_numbers_spoken_as_strings_fold_to_the_cr_denomination():
    r = _valid_report()
    r["lending"]["requirement_quantum_cr"] = _cell("25 Cr", "high")
    r["lending"]["company_turnover_cr"] = _cell("₹1,200", "medium")
    out = validate_report(_normalize(r))
    assert out["lending"]["requirement_quantum_cr"]["value"] == 25.0
    assert out["lending"]["company_turnover_cr"]["value"] == 1200.0

    r = _valid_report()
    r["lending"]["requirement_quantum_cr"] = _cell("50 lakhs", "high")
    out = validate_report(_normalize(r))
    assert out["lending"]["requirement_quantum_cr"]["value"] == 0.5  # exactly

    # deal_size is a STRING field — spoken prose stays prose.
    assert _normalize(_valid_report())["asset_monetisation"]["deal_size"]["value"] \
        == "~180 Cr EV (indicative)"


def test_a_unit_key_is_folded_by_arithmetic_never_dropped():
    """{"value": 25, "unit": "lakh"} naively stripped becomes 25 Cr — a 100x lie.
    Lakh divides, Cr spellings drop, and an alien unit (USD mn) stays put so the
    validator refuses the cell instead of us mis-reading it."""
    r = _valid_report()
    r["lending"]["requirement_quantum_cr"] = {"value": 25, "unit": "lakh", "confidence": "high"}
    out = validate_report(_normalize(r))
    assert out["lending"]["requirement_quantum_cr"]["value"] == 0.25

    r = _valid_report()
    r["lending"]["requirement_quantum_cr"] = {"value": 25, "unit": "Cr", "confidence": "high"}
    assert validate_report(_normalize(r))["lending"]["requirement_quantum_cr"]["value"] == 25

    bad = _valid_report()
    bad["lending"]["requirement_quantum_cr"] = {"value": 5, "unit": "USD mn", "confidence": "high"}
    with pytest.raises(ContractError):
        validate_report(_normalize(bad))


def test_score_arrives_as_json_float_or_string():
    for spoken in (4.0, "4"):
        r = _valid_report()
        r["common"]["opportunity_score"] = _cell(spoken, "medium")
        out = validate_report(_normalize(r))
        assert out["common"]["opportunity_score"]["value"] == 4
    bad = _valid_report()
    bad["common"]["opportunity_score"] = _cell("nine", "medium")
    with pytest.raises(ContractError):
        validate_report(_normalize(bad))


def test_dates_in_every_spoken_shape():
    for spoken in ("2026-09-15T10:00:00+05:30", "15/09/2026", "15-09-2026",
                   "15th September 2026", "September 15, 2026", "15 Sep 2026"):
        r = _valid_report()
        r["common"]["follow_up_date"] = _cell(spoken, "medium")
        out = validate_report(_normalize(r))
        assert out["common"]["follow_up_date"]["value"] == "2026-09-15", spoken
    # An impossible date is never guessed into a possible one.
    bad = _valid_report()
    bad["common"]["follow_up_date"] = _cell("31/02/2026", "medium")
    with pytest.raises(ContractError):
        validate_report(_normalize(bad))


def test_list_fields_spoken_as_sentences_wrap_never_split():
    r = _valid_report()
    r["common"]["attendees_counterparty"] = _cell("R. Sharma and the CFO", "medium")
    r["common"]["action_items"] = _cell(["Call SBI on the term sheet",
                                         {"task": "Share DPR", "owner": "RM"}], "medium")
    out = validate_report(_normalize(r))
    assert out["common"]["attendees_counterparty"]["value"] == ["R. Sharma and the CFO"]
    assert out["common"]["action_items"]["value"][0] == {"action": "Call SBI on the term sheet"}
    assert out["common"]["action_items"]["value"][1]["action"] == "Share DPR"


def test_confidence_spellings_fold_to_the_four_words():
    r = _valid_report()
    r["common"]["location"] = {"value": "Whitefield", "confidence": "High"}
    r["lending"]["existing_bankers"] = {"value": "SBI", "confidence": "med"}
    r["common"]["meeting_summary"] = {"value": None, "confidence": "N/A"}
    out = validate_report(_normalize(r))
    assert out["common"]["location"]["confidence"] == "high"
    assert out["lending"]["existing_bankers"]["confidence"] == "medium"
    assert out["common"]["meeting_summary"]["confidence"] == "n/a"


def test_omitted_fields_become_the_contracts_null():
    r = _valid_report()
    for k in ("location", "next_steps", "follow_up_date", "meeting_summary"):
        del r["common"][k]
    del r["lending"]["remarks"]
    out = validate_report(_normalize(r))
    assert out["common"]["location"] == {"value": None, "confidence": "n/a"}
    assert out["lending"]["remarks"]["value"] is None


def test_the_use_case_declaration_in_spoken_shapes():
    # A lone string, a spoken spelling, and a block filed under the spoken name.
    r = _valid_report()
    r["detected_use_cases"] = "lending"
    del r["asset_monetisation"]
    out = validate_report(_normalize(r))
    assert out["detected_use_cases"] == ["lending"]

    r = _valid_report()
    r["detected_use_cases"] = ["lending", "Asset Monetisation"]
    r["Asset Monetisation"] = r.pop("asset_monetisation")
    out = validate_report(_normalize(r))
    assert out["detected_use_cases"] == ["lending", "asset_monetisation"]
    assert out["asset_monetisation"]["party_role"]["value"] == "owner"

    # Detected with nothing heard: the block exists, every field null, flags nudge.
    r = _valid_report()
    r["detected_use_cases"] = ["lending", "asset_monetisation", "syndication"]
    out = validate_report(_normalize(r))
    assert out["syndication"]["deal_size_cr"]["value"] is None
    assert "Deal size" in " ".join(compute_data_quality_flags(out))


def test_entity_candidates_null_and_null_entries():
    r = _valid_report()
    r["entity_candidates"] = None
    assert validate_report(_normalize(r))["entity_candidates"] == []
    r = _valid_report()
    r["entity_candidates"] = ["Suryodaya EPC", None, "SBI"]
    assert validate_report(_normalize(r))["entity_candidates"] == ["Suryodaya EPC", "SBI"]


def test_decorative_cell_keys_drop_but_meaningful_ones_refuse():
    r = _valid_report()
    r["lending"]["existing_bankers"] = {"value": "SBI", "confidence": "medium",
                                        "note": "per the CFO"}
    out = validate_report(_normalize(r))
    assert set(out["lending"]["existing_bankers"]) == {"value", "confidence"}


def test_subsector_details_spoken_keys_bare_values_and_inventions():
    r = _valid_report()
    r["subsector_details"] = {
        "Operating / under-construction capacity (MW)": _cell("40 MW", "high"),  # label
        "portfolio_stage": "Under construction",       # bare value, no cell
        "promoter_pedigree": _cell("strong", "high"),  # invented — nowhere to render
    }
    out = validate_report(_normalize(r))
    d = out["subsector_details"]
    assert d["operating_uc_capacity_mw"]["value"] == "40 MW"
    assert d["portfolio_stage"] == {"value": "Under construction", "confidence": "medium"}
    assert "promoter_pedigree" not in d


def test_the_tool_schema_locks_the_taxonomy():
    from app.vocx.spec import build_tool_schema
    reg = load_registry()
    schema = build_tool_schema()
    common_props = schema["properties"]["common"]["properties"]
    assert set(common_props["sector"]["properties"]["value"]["enum"]) \
        == set(reg["taxonomy"]) | {None}
    subs = {s for lst in reg["taxonomy"].values() for s in lst}
    assert set(common_props["subsector"]["properties"]["value"]["enum"]) == subs | {None}


def test_the_synonym_tables_point_at_real_taxonomy_names():
    """Registry drift must break loudly here, not silently mis-file conversations."""
    from app.vocx.pipeline.structure import _SECTOR_SYN, _SUBSECTOR_SYN
    reg = load_registry()
    subs = {s for lst in reg["taxonomy"].values() for s in lst}
    assert set(_SECTOR_SYN.values()) <= set(reg["taxonomy"])
    assert set(_SUBSECTOR_SYN.values()) <= subs


# --------------------------------------------------- the salvage tier (last resort)

def test_a_report_that_defies_repair_salvages_instead_of_dying():
    """The user's rule: a field the machine could not read becomes an empty field
    the reviewer fills in — never a dead take. Two model rounds return the same
    stubborn cell; the report survives with that cell cleared, flagged, and
    everything else intact."""
    from app.vocx.pipeline.structure import structure_transcript

    stubborn = _valid_report()
    stubborn["asset_monetisation"]["party_role"] = _cell("landlord")  # no coercion fits
    stubborn["lending"]["requirement_quantum_cr"] = _cell("2-3 Cr", "low")  # a range, not a number
    payload = json.dumps(stubborn)
    calls = []

    def ask(model, system, user):
        calls.append(user)
        return payload  # the repair round echoes, exactly as seen in the field

    out = structure_transcript("we met the landlord", mode="note", ask_model=ask,
                               capture_ts="2026-08-27T10:00:00Z")
    assert len(calls) == 2  # initial + repair — salvage costs no third model round
    r = out["report"]
    assert r["asset_monetisation"]["party_role"]["value"] is None
    assert r["lending"]["requirement_quantum_cr"]["value"] is None
    # what stood, stands
    assert r["asset_monetisation"]["asset_status"]["value"] == "under_construction"
    assert r["common"]["sector"]["value"] == "Renewables"
    flags = r["common"]["data_quality_flags"]["value"]
    assert any("salvaged" in f for f in flags)
    assert any("landlord" in f for f in flags)
    assert any("2-3 Cr" in f for f in flags)


def test_salvage_forces_the_skeleton_right():
    from app.vocx.pipeline.structure import _salvage

    wreck = {"detected_use_cases": ["lending", "leasing"],  # one real, one invented
             "lending": {"requirement_nature": _cell("project_finance")},  # sparse
             "made_up_block": {"x": 1},
             "entity_candidates": ["Suryodaya EPC", 42]}
    out = _salvage(wreck)
    assert out is not None
    assert out["detected_use_cases"] == ["lending"]
    assert "made_up_block" not in out
    assert out["lending"]["requirement_nature"]["value"] == "project_finance"
    assert out["lending"]["requirement_quantum_cr"]["value"] is None
    assert out["entity_candidates"] == ["Suryodaya EPC"]
    assert out["common"]["meeting_type"]["value"] is None

    # Nothing detected, nothing heard: the note files under operations for re-filing.
    bare = _salvage({})
    assert bare is not None and bare["detected_use_cases"] == ["operations"]
    assert any("operations" in f for f in bare["common"]["data_quality_flags"]["value"])


def test_no_json_object_at_all_still_fails_into_retry():
    from app.vocx.pipeline.structure import StructuringError, structure_transcript

    def ask(model, system, user):
        return "I'm sorry, I can't structure that."

    with pytest.raises(StructuringError):
        structure_transcript("hello", mode="note", ask_model=ask)
