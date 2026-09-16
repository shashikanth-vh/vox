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


def test_normalize_folds_borrowed_action_items_shape_onto_lender_updates():
    """Seen live on the first field run: the model borrowed action_items'
    {owner, action, deadline} for lender_updates and omitted kind entirely —
    the review screen showed three empty rows while the substance sat in
    remarks. The normalizer folds owner→lender, action→note (deadline into the
    note), and reads the DIRECTION from the note's verbs."""
    from app.vocx.pipeline.structure import _normalize

    r = _valid_report()
    r["detected_use_cases"] = ["lending", "asset_monetisation", "syndication"]
    r["syndication"] = {"lender_updates": _cell([
        {"owner": "Axis Finance", "deadline": None,
         "action": "Axis Finance raised credit queries on security structure"},
        {"owner": "Godrej Capital", "deadline": "2026-09-15",
         "action": "Godrej Capital committed to respond"},
        {"lender": "Canara Bank", "kind": "chased",
         "note": "Followed up on the IM; no response yet."},
    ])}
    out = _normalize(r)
    got = out["syndication"]["lender_updates"]["value"]
    assert got[0] == {"lender": "Axis Finance", "kind": "reply",
                      "note": "Axis Finance raised credit queries on security structure"}
    assert got[1]["lender"] == "Godrej Capital" and got[1]["kind"] == "reply"
    assert got[1]["note"].endswith("(by 2026-09-15)")
    assert got[2] == {"lender": "Canara Bank", "kind": "chase",
                      "note": "Followed up on the IM; no response yet."}


def _hedged_syndication_report():
    """The exact live failure, twice over: remarks carries both events in full
    prose while lender_updates sits at null."""
    r = _valid_report()
    r["detected_use_cases"] = ["lending", "asset_monetisation", "syndication"]
    r["syndication"] = {
        "probable_lenders": _cell("Gurdwaj Capital, Access Finance", "medium"),
        "remarks": _cell("Gurdwaj Capital chased with no response yet. Access "
                         "Finance has reverted with security structure queries.",
                         "high"),
        "lender_updates": _cell(None, "n/a"),
    }
    return r


def test_lender_updates_hedged_empty_gets_a_focused_second_pass():
    """Proven live twice on the box: with the full contract in play, the note
    model wrote both events into remarks and left lender_updates null — even
    with the prompt section ordering it not to. The pipeline now asks once
    more, for that one field only; the focused answer lands through the same
    folding and the same validator as the main pass."""
    from app.vocx.pipeline.structure import structure_transcript

    focused = [{"lender": "Gurdwaj Capital", "kind": "chase",
                "note": "Chased for the IM response; nothing back yet."},
               {"lender": "Access Finance", "kind": "reply",
                "note": "Reverted with queries on the security structure."}]
    systems = []

    def ask(model, system, user):
        systems.append(system)
        return json.dumps(focused if len(systems) > 1
                          else _hedged_syndication_report())

    out = structure_transcript(
        "Chased Gurdwaj Capital, no response yet. Access Finance reverted "
        "with queries on the security structure.",
        mode="note", ask_model=ask, capture_ts="2026-09-11T03:00:00Z")
    assert len(systems) == 2 and "JSON array" in systems[1]
    got = out["report"]["syndication"]["lender_updates"]
    assert got["confidence"] == "medium"
    assert [e["kind"] for e in got["value"]] == ["chase", "reply"]
    assert got["value"][0]["lender"] == "Gurdwaj Capital"
    assert got["value"][1]["lender"] == "Access Finance"


def test_lender_second_pass_knows_when_not_to_ask():
    """No second call when the main pass delivered the field, and none when
    nothing chase-shaped was spoken — the extra round exists for the hedge,
    not for every take."""
    from app.vocx.pipeline.structure import structure_transcript

    delivered = _hedged_syndication_report()
    delivered["syndication"]["lender_updates"] = _cell(
        [{"lender": "SBI", "kind": "chase", "note": "Chased for sanction."}],
        "high")
    calls: list[str] = []

    def ask_delivered(model, system, user):
        calls.append(system)
        return json.dumps(delivered)

    out = structure_transcript("Chased SBI for the sanction letter today.",
                               mode="note", ask_model=ask_delivered,
                               capture_ts="2026-09-11T03:00:00Z")
    assert len(calls) == 1
    assert out["report"]["syndication"]["lender_updates"]["value"][0]["lender"] == "SBI"

    calls.clear()

    def ask_quiet(model, system, user):
        calls.append(system)
        return json.dumps(_hedged_syndication_report())

    structure_transcript("We discussed the solar project and the site visit.",
                         mode="note", ask_model=ask_quiet,
                         capture_ts="2026-09-11T03:00:00Z")
    assert len(calls) == 1


def test_the_structure_provider_switch_is_exclusive(monkeypatch):
    """One box, one model: default is Claude (Haiku notes / Sonnet live) exactly
    as production runs; VOCX_STRUCTURE_PROVIDER=sarvam routes EVERYTHING to
    Sarvam-M — never both at once — and the run is attributed to the model
    that actually structured it."""
    from app.vocx.pipeline.structure import _structure_model, structure_transcript

    monkeypatch.delenv("VOCX_STRUCTURE_PROVIDER", raising=False)
    monkeypatch.delenv("SARVAM_MODEL", raising=False)
    assert _structure_model("post_meeting") == "claude-haiku-4-5-20251001"
    assert _structure_model("live") == "claude-sonnet-5"

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    # default: the conversations tune — no always-on reasoning, direct answers
    assert _structure_model("post_meeting") == "sarvam-105b-conversations"
    assert _structure_model("live") == "sarvam-105b-conversations"
    monkeypatch.setenv("SARVAM_MODEL", "sarvam-105b")
    assert _structure_model("live") == "sarvam-105b"
    monkeypatch.delenv("SARVAM_MODEL", raising=False)

    seen: list[str] = []

    def ask(model, system, user):
        seen.append(model)
        return json.dumps(_valid_report())

    out = structure_transcript("we met suryodaya", mode="post_meeting",
                               ask_model=ask, capture_ts="2026-09-15T10:00:00Z")
    assert out["model"] == "sarvam-105b-conversations"
    assert seen[0] == "sarvam-105b-conversations"


def test_sarvam_mode_structures_in_small_batched_calls(monkeypatch):
    """The contract-sized single call defeated sarvam live (it reasons past any
    budget); the desk's prototype proved small asks work. Under the sarvam
    provider the pipeline BATCHES: one detect+common call, one call per
    detected block — assembled, then normalized/validated exactly like the
    Claude path. Wrapped or bare block answers both land; unspoken fields
    arrive null; judgement confidences are forced to n/a."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    systems: list[str] = []

    def ask(model, system, user):
        systems.append(system)
        assert model == "sarvam-105b-conversations"
        if '"detected_use_cases"' in system:
            return json.dumps({
                "detected_use_cases": ["lending", "syndication"],
                "entity_candidates": ["Sangara Limited"],
                "common": {
                    "sector": {"value": "Renewables", "confidence": "high"},
                    "meeting_summary": {"value": "Sangara: 10 MW sale + 5 Cr WC.",
                                        "confidence": "high"},
                    # the model habitually stamps judgement fields — forced to n/a
                    "opportunity_assessment": {"value": "Strong ask.",
                                               "confidence": "high"},
                }})
        if "syndication" in system:
            return json.dumps({          # bare cells — no block wrapper
                "deal_size_cr": {"value": 5, "confidence": "high"},
                "lender_updates": {"value": [], "confidence": "n/a"},
                "not_a_field": {"value": 1, "confidence": "high"},  # dropped
            })
        return json.dumps({"lending": {
            "requirement_quantum_cr": {"value": 5, "confidence": "high"},
            "present_requirement": {"value": "5 Cr working capital",
                                    "confidence": "high"}}})

    out = structure_transcript(
        "met sangara, ten megawatt sale and five crore working capital",
        mode="post_meeting", ask_model=ask, capture_ts="2026-09-16T06:00:00Z")
    assert len(systems) == 3            # head + lending + syndication
    # a requirement taken to syndication is BOTH blocks (own book + arranged)
    assert "BOTH lending and syndication" in systems[0]
    # lane discipline: each block call extracts its own thread only — the
    # asset sale retold inside Lending remarks (seen live) is out of lane
    assert all("thread ONLY" in s for s in systems[1:])
    r = out["report"]
    assert out["model"] == "sarvam-105b-conversations"
    assert r["detected_use_cases"] == ["lending", "syndication"]
    assert r["entity_candidates"] == ["Sangara Limited"]
    assert r["common"]["sector"]["value"] == "Renewables"
    assert r["common"]["opportunity_assessment"]["confidence"] == "n/a"
    assert r["lending"]["requirement_quantum_cr"]["value"] == 5
    assert r["lending"]["remarks"]["value"] is None      # unspoken → null
    assert r["syndication"]["deal_size_cr"]["value"] == 5
    assert "not_a_field" not in r["syndication"]
    assert r["common"]["meeting_date"]["value"] == "2026-09-16"  # capture fill


def test_sarvam_briefs_carry_the_registrys_own_guidance():
    """The trial run left AM deal_size empty, returned raw phrases for offer
    chips and skipped the score — because the briefs told the model each
    field's TYPE but not what the cell wants. The registry's own notes,
    closed_set and min/max now ride in every brief, so a registry bump keeps
    flowing through with zero code changes."""
    from app.vocx.pipeline.structure import _sarvam_field_brief

    reg = load_registry()
    am = {f["key"]: f for f in reg["blocks"]["asset_monetisation"]["fields"]}
    offer = _sarvam_field_brief(am["offer_components"], reg)
    assert "entire_project" in offer and "free-text" in offer
    deal = _sarvam_field_brief(am["deal_size"], reg)
    assert "Free text" in deal                        # the registry note itself
    common = {f["key"]: f for f in (reg["common"] if isinstance(reg["common"], list)
                                    else reg["common"]["fields"])}
    score = _sarvam_field_brief(common["opportunity_score"], reg)
    assert "1-5" in score and "ull" in score
    assert "business substance" in score   # scored on the deal, not sentiment
    summary = _sarvam_field_brief(common["meeting_summary"], reg)
    assert "narrative" in summary                     # judgement guidance rides too
    # bake-off lessons: sector may be inferred from the business; "sell all
    # assets" means the entire_project chip; deal_size takes the offered
    # capacity when no rupee figure was spoken; remarks wants an analyst note
    sector = _sarvam_field_brief(common["sector"], reg)
    assert "infer" in sector
    # subsector by activity, never letterhead: "Chemenergy Biofuels" building
    # CBG plants filed under Biofuels live — compressed biogas is Biogas
    subsector = _sarvam_field_brief(common["subsector"], reg)
    assert "never the company's name" in subsector
    assert "OEM" in subsector and "owns/operates" in subsector  # role key
    lending = {f["key"]: f for f in reg["blocks"]["lending"]["fields"]}
    nature = _sarvam_field_brief(lending["requirement_nature"], reg)
    assert "project_finance" in nature and "BUILD" in nature
    # the own-book / syndicated split: a 25 Cr ask can be 2 Cr from the
    # desk's book and 23 Cr syndicated — quantum and deal size each know
    # which portion they carry
    quantum = _sarvam_field_brief(lending["requirement_quantum_cr"], reg)
    assert "own-book portion" in quantum
    synd = {f["key"]: f for f in reg["blocks"]["syndication"]["fields"]}
    dsize = _sarvam_field_brief(synd["deal_size_cr"], reg)
    assert "syndicated portion" in dsize
    loc = _sarvam_field_brief(am["asset_location"], reg)
    assert "shorthand" in loc
    # the asset-for-sale's site rode into Lending's project_location live
    ploc = _sarvam_field_brief(lending["project_location"], reg)
    assert "FINANCED" in ploc and "asset offered for sale" in ploc
    # location is the VENUE lane (matching the glossary rules that ride in
    # every call's context); project/asset places live in their own fields
    mloc = _sarvam_field_brief(common["location"], reg)
    assert "MEETING VENUE only" in mloc
    # a follow-up spoken in ANY form must land as concrete date+time — the
    # Add-to-Calendar / Google Meet flow consumes exactly that pair
    fud = _sarvam_field_brief(common["follow_up_date"], reg)
    assert "tomorrow" in fud and "Capture timestamp" in fud
    fut = _sarvam_field_brief(common["follow_up_time"], reg)
    assert "HH:MM" in fut
    assert "entire_project)" in offer or "entire_project'" in offer or \
        "means entire_project" in offer
    assert "offered capacity" in deal
    remarks = _sarvam_field_brief(
        {"key": "remarks", "label": "Remarks", "type": "string"}, reg)
    assert "analyst note" in remarks
    assert "data_quality_flags" in remarks     # artifacts never quoted in remarks
    assert "never mention use cases" in summary  # no machinery in the debrief
    from app.vocx.pipeline.structure import _SARVAM_RULES
    assert "transcription artifact" in _SARVAM_RULES  # garbles flagged, not copied
    # the narrator is named, never labelled: "Chetan had a call", not
    # "the admin (Recorded by) had a call" — seen verbatim on the 238 run
    assert "never echo the label" in _SARVAM_RULES


def test_sarvam_long_take_condenses_before_extraction(monkeypatch):
    """The 90-minute wall: above the char budget the transcript is condensed
    chunk by chunk into minutes, and the minutes — never the raw transcript —
    ride into the head call, the block calls AND the lender pass. Order is
    preserved across the parallel chunk calls; a 3-minute note (the sibling
    test below) rides whole."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    monkeypatch.setenv("SARVAM_TRANSCRIPT_CHAR_BUDGET", "1000")
    monkeypatch.setenv("SARVAM_DIGEST_CHUNK_CHARS", "500")

    sentence = ("We discussed the Kusum solar asset of ten megawatt with the "
                "owner and the working capital fund of five crore. ")
    transcript = "RAWMARKER " + sentence * 30          # ~3.3K chars, > budget
    digest_users: list[str] = []
    extraction_users: list[str] = []

    def ask(model, system, user):
        if "condense one segment" in system:
            import re as _tre
            digest_users.append(user)
            # number the minutes by the SEGMENT header, not call order — the
            # chunks digest in parallel, so call order is not chunk order
            k = int(_tre.search(r"SEGMENT (\d+) of", user).group(1))
            return json.dumps({"minutes": [
                f"fact {k}: 10 MW Kusum asset, 5 crore working capital",
                *(["chased HDFC Bank for the sanction, they will revert"]
                  if k == 1 else [])]})
        extraction_users.append(user)
        if '"detected_use_cases"' in system:
            return json.dumps({
                "detected_use_cases": ["syndication"],
                "entity_candidates": ["Sangara Limited"],
                "common": {"meeting_summary": {"value": "Long take.",
                                               "confidence": "n/a"}}})
        if "lender chase/reply events" in system:
            return json.dumps([{"lender": "HDFC Bank", "kind": "chase",
                                "note": "Chased for the sanction; will revert."}])
        return json.dumps({"syndication": {
            "deal_size_cr": {"value": 5, "confidence": "high"}}})

    out = structure_transcript(transcript, mode="live", ask_model=ask,
                               capture_ts="2026-09-16T06:00:00Z")
    assert len(digest_users) >= 2                     # it really chunked
    assert any("SEGMENT 1 of" in u for u in digest_users)
    # every extraction call read the minutes, none the raw transcript
    assert extraction_users and all("CONDENSED MINUTES" in u for u in extraction_users)
    assert all("RAWMARKER" not in u for u in extraction_users)
    # order preserved across parallel chunk digests
    head_user = extraction_users[0]
    assert head_user.index("fact 1") < head_user.index("fact 2")
    lu = out["report"]["syndication"]["lender_updates"]["value"]
    assert lu and lu[0]["lender"] == "HDFC Bank"      # lender pass saw the minutes


def test_sarvam_short_note_rides_whole(monkeypatch):
    """A 3-minute note is far under the default budget: no digest calls, the
    raw transcript rides into every call — byte-identical to the pre-digest
    behavior."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    users: list[str] = []

    def ask(model, system, user):
        assert "condense one segment" not in system
        users.append(user)
        if '"detected_use_cases"' in system:
            return json.dumps({"detected_use_cases": ["lending"],
                               "entity_candidates": [], "common": {}})
        return json.dumps({"lending": {
            "requirement_quantum_cr": {"value": 5, "confidence": "high"}}})

    structure_transcript("met sangara, five crore working capital",
                         mode="post_meeting", ask_model=ask,
                         capture_ts="2026-09-16T06:00:00Z")
    assert all("met sangara" in u and "CONDENSED MINUTES" not in u for u in users)


def test_sarvam_block_calls_run_concurrently(monkeypatch):
    """Two detected blocks must be in flight at the same time — the barrier
    only releases when both calls have arrived, so a sequential regression
    deadlocks it (and fails fast on its timeout) instead of passing slowly."""
    import threading

    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    barrier = threading.Barrier(2, timeout=10)

    def ask(model, system, user):
        if '"detected_use_cases"' in system:
            return json.dumps({"detected_use_cases": ["lending", "syndication"],
                               "entity_candidates": [], "common": {}})
        barrier.wait()
        if "fill the Syndication fields" in system:
            return json.dumps({"syndication": {
                "deal_size_cr": {"value": 5, "confidence": "high"}}})
        return json.dumps({"lending": {
            "requirement_quantum_cr": {"value": 5, "confidence": "high"}}})

    out = structure_transcript("sale and working capital", mode="post_meeting",
                               ask_model=ask, capture_ts="2026-09-16T06:00:00Z")
    r = out["report"]
    assert r["lending"]["requirement_quantum_cr"]["value"] == 5
    assert r["syndication"]["deal_size_cr"]["value"] == 5


def test_sarvam_parallelism_can_be_pinned_to_sequential(monkeypatch):
    """SARVAM_MAX_PARALLEL=1 restores strictly sequential calls — the escape
    hatch if their API ever rate-limits concurrent requests."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    monkeypatch.setenv("SARVAM_MAX_PARALLEL", "1")
    in_flight = {"now": 0, "max": 0}

    def ask(model, system, user):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        try:
            if '"detected_use_cases"' in system:
                return json.dumps({"detected_use_cases": ["lending", "syndication"],
                                   "entity_candidates": [], "common": {}})
            if "Syndication" in system:
                return json.dumps({"syndication": {
                    "deal_size_cr": {"value": 5, "confidence": "high"}}})
            return json.dumps({"lending": {
                "requirement_quantum_cr": {"value": 5, "confidence": "high"}}})
        finally:
            in_flight["now"] -= 1

    structure_transcript("sale and working capital", mode="post_meeting",
                         ask_model=ask, capture_ts="2026-09-16T06:00:00Z")
    assert in_flight["max"] == 1


def test_sarvam_covers_the_interaction_shapes_of_the_field(monkeypatch):
    """The desk's real conversation shapes, end to end under the sarvam
    provider: a lending-only chat; an asset-monetisation chat whose offer
    components mix closed-set tokens with a spoken free-text extra; and a
    vague check-in where nothing product-shaped was said (falls to
    operations, common only). Each must produce a contract-valid report."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")

    def run(head, blocks):
        def ask(model, system, user):
            if '"detected_use_cases"' in system:
                return json.dumps(head)
            for uc, cells in blocks.items():
                label = {"lending": "Lending", "syndication": "Syndication",
                         "asset_monetisation": "Asset monetisation"}[uc]
                if label in system:
                    return json.dumps({uc: cells})
            raise AssertionError(f"unexpected call: {system[:80]}")
        return structure_transcript("spoken words", mode="post_meeting",
                                    ask_model=ask,
                                    capture_ts="2026-09-16T06:00:00Z")["report"]

    # 1. lending-only
    r = run({"detected_use_cases": ["lending"], "entity_candidates": ["Acme"],
             "common": {}},
            {"lending": {"requirement_nature": {"value": "working_capital",
                                                "confidence": "high"},
                         "requirement_quantum_cr": {"value": 5,
                                                    "confidence": "high"}}})
    assert r["detected_use_cases"] == ["lending"]
    assert "syndication" not in r

    # 2. asset monetisation: closed-set tokens + a spoken free-text component
    r = run({"detected_use_cases": ["asset_monetisation"],
             "entity_candidates": [], "common": {}},
            {"asset_monetisation": {
                "party_role": {"value": "owner", "confidence": "high"},
                "offer_components": {"value": ["entire_project",
                                               "10 MW Kusum capacity"],
                                     "confidence": "medium"},
                "asset_status": {"value": "operational", "confidence": "medium"}}})
    assert r["asset_monetisation"]["offer_components"]["value"] == \
        ["entire_project", "10 MW Kusum capacity"]

    # 3. nothing product-shaped spoken: operations fallback, common only
    r = run({"detected_use_cases": [], "entity_candidates": [], "common": {}}, {})
    assert r["detected_use_cases"] == ["operations"]


def test_sarvam_bare_cells_are_wrapped_not_crashed(monkeypatch):
    """The first live sarvam-105b-conversations run, verbatim: the dialogue
    tune answered bare values — "sector": "Renewables", "deal_size_cr": 5 —
    instead of {value, confidence} cells. The bare sector crashed the
    validator with AttributeError (escaping the ContractError salvage wall)
    and the bare number silently nulled. Both are now wrapped at assembly:
    the spoken fact survives at medium confidence, judgement fields still
    force n/a, and nothing crashes."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")

    def ask(model, system, user):
        if '"detected_use_cases"' in system:
            return json.dumps({"detected_use_cases": ["syndication"],
                               "entity_candidates": ["Sangara Limited"],
                               "common": {"sector": "Renewables",
                                          "subsector": "Solar-Developer",
                                          "meeting_summary": "Met Sangara.",
                                          "opportunity_score": 3}})
        return json.dumps({"syndication": {
            "facility_nature": "term_loan_syndication",
            "deal_size_cr": 5,
            "remarks": None}})

    r = structure_transcript("met sangara five crore", mode="post_meeting",
                             ask_model=ask,
                             capture_ts="2026-09-16T06:00:00Z")["report"]
    assert r["common"]["sector"] == {"value": "Renewables", "confidence": "medium"}
    assert r["common"]["subsector"]["value"] == "Solar-Developer"
    assert r["common"]["opportunity_score"]["value"] == 3
    assert r["common"]["meeting_summary"]["confidence"] == "n/a"  # judgement
    assert r["syndication"]["deal_size_cr"] == {"value": 5, "confidence": "medium"}
    assert r["syndication"]["remarks"] == {"value": None, "confidence": "n/a"}


def test_sarvam_fills_the_subsector_key_data(monkeypatch):
    """The panel's SOLAR-DEVELOPER KEY DATA stayed empty on every sarvam run:
    Claude's one full-contract call carries subsector_details, the batched
    path never asked. Once the head picks a subsector, a focused call now
    fills its canonical data points — wrapped or bare cells land, unspoken
    ones are dropped, and a failure skips (bonus data, never a dead take)."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")
    reg = load_registry()
    canon_keys = [f["key"] for f in reg["subsector_canonicals"]["Solar-Developer"]]
    systems: list[str] = []

    def ask(model, system, user):
        systems.append(system)
        # an all-lowercase recorder username is sentence-cased for the model
        assert "Recorded by: Admin" in user
        if '"detected_use_cases"' in system:
            return json.dumps({
                "detected_use_cases": ["asset_monetisation"],
                "entity_candidates": [],
                "common": {"sector": {"value": "Renewables", "confidence": "high"},
                           "subsector": {"value": "Solar-Developer",
                                         "confidence": "medium"}}})
        if "canonical data points" in system:
            # a dropdown canonical (portfolio_stage) lists its options
            assert "Mixed portfolio" in system
            return json.dumps({"subsector_details": {"Solar-Developer": {
                canon_keys[0]: "26.3 MW operational; 12 MW under construction",
                canon_keys[1]: None}}})           # unspoken → dropped, bare → wrapped
        return json.dumps({"asset_monetisation": {
            "party_role": {"value": "owner", "confidence": "high"}}})

    out = structure_transcript("remanindia sells 26.3 MW", mode="post_meeting",
                               ask_model=ask, capture_ts="2026-09-16T06:00:00Z",
                               recorder="admin")
    assert len(systems) == 3               # head + block + details
    details = out["report"]["subsector_details"]
    got = details.get("Solar-Developer") or details   # wrapped or flattened
    assert got[canon_keys[0]]["value"].startswith("26.3 MW")
    assert canon_keys[1] not in got

    # a details failure skips, never fails the take
    def ask_broken(model, system, user):
        if "canonical data points" in system:
            return "not json at all"
        return ask(model, system, user)

    out2 = structure_transcript("remanindia sells 26.3 MW", mode="post_meeting",
                                ask_model=ask_broken,
                                capture_ts="2026-09-16T06:00:00Z",
                                recorder="admin")
    assert out2["report"]["asset_monetisation"]["party_role"]["value"] == "owner"


def test_every_subsectors_key_data_brief_renders_completely():
    """The whole taxonomy, not just the two subsectors the trial exercised:
    for all 32 subsectors, every canonical KEY DATA field is offered to the
    model, every dropdown lists its options (portfolio_stage stayed empty
    live until it was shown its choices), and every text cell carries the
    plans-without-numbers rule ('Three CBG plants' was nulled live for
    lacking a capacity figure)."""
    from app.vocx.pipeline.structure import _details_briefs

    reg = load_registry()
    taxonomy_subs = {s for subs in reg["taxonomy"].values() for s in subs}
    assert set(reg["subsector_canonicals"]) == taxonomy_subs
    for sub, canon in reg["subsector_canonicals"].items():
        briefs = _details_briefs(canon)
        for f in canon:
            assert f["key"] in briefs, (sub, f["key"])
            if f.get("options"):
                for opt in f["options"]:
                    assert str(opt) in briefs, (sub, f["key"], opt)
            else:
                assert "WITHOUT numbers still fills" in briefs


def test_machinery_talk_is_scrubbed_from_prose_deterministically(monkeypatch):
    """Told twice not to, the dialogue tune still wrote 'The transcript
    references SHIT and DA, which appears to be a speech-to-text artifact'
    into Remarks. Prompting lost — the scrubber drops machinery sentences
    and flagged artifact terms from every prose field and list bullet,
    keeps the analyst substance beside them, and never touches
    data_quality_flags (the artifacts' one home)."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")

    def ask(model, system, user):
        if '"detected_use_cases"' in system:
            return json.dumps({
                "detected_use_cases": ["asset_monetisation"],
                "entity_candidates": [],
                "common": {
                    "meeting_summary": {
                        "value": ("Admin held a call with Mr. Bhumik. "
                                  "The use cases discussed include asset "
                                  "monetisation and lending."),
                        "confidence": "n/a"},
                    "key_discussion_points": {
                        "value": ["26.3 MW project discussed",
                                  "SHIT and DA data required for developers"],
                        "confidence": "medium"},
                    "data_quality_flags": {
                        "value": ["transcription artifact: SHIT and DA"],
                        "confidence": "n/a"}}})
        return json.dumps({"asset_monetisation": {
            "party_role": {"value": "owner", "confidence": "high"},
            "remarks": {
                "value": ("The transcript references SHIT and DA, which "
                          "appears to be a speech-to-text artifact for "
                          "unidentified terms. Clarification is needed on "
                          "the 53 acres of additional land."),
                "confidence": "medium"}}})

    r = structure_transcript("remanindia call", mode="post_meeting",
                             ask_model=ask,
                             capture_ts="2026-09-16T06:00:00Z")["report"]
    remark = r["asset_monetisation"]["remarks"]["value"]
    assert "artifact" not in remark                   # meta-sentence dropped
    assert "53 acres" in remark                       # substance survives
    summary = r["common"]["meeting_summary"]["value"]
    assert "use cases" not in summary and "Mr. Bhumik" in summary
    # USING a flagged term is legitimate prose and survives (the term match
    # once gutted a whole summary over flagged 'Kosum'/'AMPY'); only QUOTED
    # mentions — discussing the term — die with the meta sentences
    ki = r["common"]["key_discussion_points"]["value"]
    assert ki == ["26.3 MW project discussed",
                  "SHIT and DA data required for developers"]
    flags = r["common"]["data_quality_flags"]["value"]
    assert "transcription artifact: SHIT and DA" in flags  # home untouched


def test_flagged_terms_in_use_survive_the_scrub(monkeypatch):
    """The 247 live run: the model flagged 'Kosum' and 'AMPY' as artifacts
    and the scrubber then deleted every summary sentence containing them —
    the report kept only the one sentence without proper nouns. Terms IN USE
    stay; a QUOTED term ('Kosum') still reads as discussing-the-term and
    dies with its sentence."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")

    def ask(model, system, user):
        if '"detected_use_cases"' in system:
            return json.dumps({
                "detected_use_cases": ["asset_monetisation"],
                "entity_candidates": [],
                "common": {
                    "meeting_summary": {
                        "value": ("Sangara offered the 10 MW Kosum asset "
                                  "across 2.5 into 4 sites in AMPY. The term "
                                  "'Kosum' could not be resolved."),
                        "confidence": "n/a"},
                    "data_quality_flags": {
                        "value": ["transcription artifact: Kosum",
                                  "transcription artifact: AMPY"],
                        "confidence": "n/a"}}})
        return json.dumps({"asset_monetisation": {
            "party_role": {"value": "owner", "confidence": "high"}}})

    r = structure_transcript("sangara call", mode="post_meeting", ask_model=ask,
                             capture_ts="2026-09-16T06:00:00Z")["report"]
    summary = r["common"]["meeting_summary"]["value"]
    assert "10 MW Kosum asset" in summary and "AMPY" in summary   # usage kept
    assert "could not be resolved" not in summary                 # meta died


def test_a_spoken_requirement_taken_to_syndication_rides_as_lending_too(monkeypatch):
    """The desk rule, deterministic: '5 Cr working capital, taking it to
    syndication' files BOTH blocks even when the model detects syndication
    alone — part can fund from the desk's own book. A chase-only follow-up
    (no requirement language) stays syndication-alone."""
    from app.vocx.pipeline.structure import structure_transcript

    monkeypatch.setenv("VOCX_STRUCTURE_PROVIDER", "sarvam")

    def make_ask(seen):
        def ask(model, system, user):
            seen.append(system)
            if '"detected_use_cases"' in system:
                return json.dumps({"detected_use_cases": ["syndication"],
                                   "entity_candidates": [], "common": {}})
            if "lender chase/reply" in system:
                return "[]"
            if "fill the Lending fields" in system:
                return json.dumps({"lending": {
                    "requirement_nature": {"value": "working_capital",
                                           "confidence": "high"},
                    "requirement_quantum_cr": {"value": 5, "confidence": "high"}}})
            return json.dumps({"syndication": {
                "deal_size_cr": {"value": 5, "confidence": "high"}}})
        return ask

    seen: list[str] = []
    r = structure_transcript(
        "they need five crore working capital, we are taking it to syndication",
        mode="post_meeting", ask_model=make_ask(seen),
        capture_ts="2026-09-16T06:00:00Z")["report"]
    assert set(r["detected_use_cases"]) == {"syndication", "lending"}
    assert r["lending"]["requirement_quantum_cr"]["value"] == 5
    assert r["syndication"]["deal_size_cr"]["value"] == 5

    seen2: list[str] = []
    r2 = structure_transcript(
        "chased HDFC on the mandate, they will revert on sanction",
        mode="post_meeting", ask_model=make_ask(seen2),
        capture_ts="2026-09-16T06:00:00Z")["report"]
    assert r2["detected_use_cases"] == ["syndication"]
    assert "lending" not in r2


def test_a_bare_sector_cell_is_a_named_violation_never_a_crash():
    """Defense in depth behind the assembly wrap: a non-object cell reaching
    the validator raises ContractError (which salvage handles), never
    AttributeError (which killed the take live)."""
    r = _valid_report()
    r["common"]["sector"] = "Renewables"            # bare string, no cell
    with pytest.raises(ContractError):
        validate_report(r)


def test_lender_second_pass_failure_never_breaks_the_take():
    """The focused round answering prose instead of JSON — or anything else
    going wrong in it — leaves the report exactly as the main pass made it."""
    from app.vocx.pipeline.structure import structure_transcript

    systems: list[str] = []

    def ask(model, system, user):
        systems.append(system)
        if len(systems) == 1:
            return json.dumps(_hedged_syndication_report())
        return "No lender events to report."

    out = structure_transcript(
        "Chased Gurdwaj Capital, no response yet.",
        mode="note", ask_model=ask, capture_ts="2026-09-11T03:00:00Z")
    assert len(systems) == 2
    assert out["report"]["syndication"]["lender_updates"]["value"] is None


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


def test_the_tool_schema_describes_each_item_shape_from_the_registry():
    """The forced tool call is the outer wall the model's answer must pass —
    and for weeks it described EVERY item_shape list as action_items
    ({action, owner, deadline}, action required). For lender_updates that
    contradicted the prompt's {lender, kind, note}: the model either borrowed
    the action shape or, told firmly not to, returned null. The wall must be
    built from the registry's own declared shape, per field."""
    from app.vocx.spec import build_tool_schema

    schema = build_tool_schema()
    lu = schema["properties"]["syndication"]["properties"]["lender_updates"] \
        ["properties"]["value"]["items"]
    assert set(lu["required"]) == {"lender", "kind", "note"}
    assert lu["properties"]["kind"] == {"enum": ["chase", "reply"]}
    assert lu["properties"]["lender"] == {"type": "string"}
    ai = schema["properties"]["common"]["properties"]["action_items"] \
        ["properties"]["value"]["items"]
    assert ai["required"] == ["action"]
    assert ai["properties"]["owner"] == {"type": ["string", "null"]}

    # The validator enforces the same declared shape.
    good = _valid_report()
    good["detected_use_cases"] = ["lending", "asset_monetisation", "syndication"]
    good["syndication"] = {"lender_updates": _cell(
        [{"lender": "SBI", "kind": "chase", "note": "Chased for sanction."}],
        "high")}
    from app.vocx.pipeline.structure import _normalize
    assert validate_report(_normalize(good)) is not None
    bad = copy.deepcopy(good)
    bad["syndication"]["lender_updates"]["value"][0]["kind"] = "shouted"
    with pytest.raises(ContractError):
        validate_report(bad)


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
