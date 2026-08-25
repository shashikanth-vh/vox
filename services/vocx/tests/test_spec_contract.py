"""Phase 0 acceptance: the registry is the single source of truth, the contract
validates, and a broken object never best-efforts its way past the gate."""

import copy

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
