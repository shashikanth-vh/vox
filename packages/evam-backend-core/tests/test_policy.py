"""The shared stage/field policy engine: mandatory-fields-on-advance and role/stage field
locks. Pure evaluators — no DB."""

from __future__ import annotations

from evam_backend_core import policy


def test_mandatory_field_error_blocks_advance_until_required_fields_present():
    # Lending → Disbursed requires the proposed amount + date; amount present → only the date
    # missing. ('Ready for Disbursement' carries NO field mandate any more — it flips on CP
    # approval, before a drawdown is proposed; the figures are fixed by the Disburse action and
    # re-verified at handover. The former Deal→Sanctioned mandate moved to the Lending line with
    # the rest of the sanction governance — a deal's stage is the commercial funnel.)
    err = policy.mandatory_field_error("Lending", "Disbursed",
                                       {"proposed_disbursement_amount": 5})
    assert err and "proposed_disbursement_date" in err and "amount" not in (err.split("required")[0])
    # Both present (merged from row + change) → passes.
    assert policy.mandatory_field_error(
        "Lending", "Disbursed",
        {"proposed_disbursement_amount": 5, "proposed_disbursement_date": "2026-01-01"}) is None
    # Empty string counts as missing.
    assert policy.mandatory_field_error(
        "Lending", "Disbursed",
        {"proposed_disbursement_amount": "", "proposed_disbursement_date": "2026-01-01"}) is not None
    # The CP-approval milestone itself needs no proposed drawdown to take the label.
    assert policy.mandatory_field_error("Lending", "Ready for Disbursement", {}) is None


def test_mandatory_field_error_is_noop_without_a_rule():
    assert policy.mandatory_field_error("Lending", "Data Awaited", {"anything": 1}) is None
    assert policy.mandatory_field_error("Lead", "Active", {}) is None
    # A Deal has NO mandatory-data gates at all — its stage is the funnel.
    assert policy.mandatory_field_error("Deal", "Closed Won", {}) is None
    assert policy.mandatory_field_error("Deal", None, {}) is None


def test_field_lock_error_blocks_locked_field_for_wrong_role():
    # rm is locked at the LENDING line's Sanctioned to Management (or Admin) — the lock moved
    # there from the deprecated deal-level credit stage.
    err = policy.field_lock_error("Lending", "Sanctioned", roles=["RM"], changed_fields=["rm"])
    assert err and "rm" in err
    # Management may edit it.
    assert policy.field_lock_error("Lending", "Sanctioned", ["Management"], ["rm"]) is None
    # Admin is always break-glass.
    assert policy.field_lock_error("Lending", "Sanctioned", ["Admin"], ["rm"]) is None


def test_field_lock_error_is_noop_when_field_or_stage_unlocked():
    # rm is only locked at Sanctioned, not earlier stages.
    assert policy.field_lock_error("Lending", "Data Awaited", ["RM"], ["rm"]) is None
    # A field with no lock rule is unconstrained here.
    assert policy.field_lock_error("Lending", "Sanctioned", ["RM"], ["remarks"]) is None
    # No stage → nothing to enforce.
    assert policy.field_lock_error("Lending", None, ["RM"], ["rm"]) is None
    # A Deal (funnel) has no field locks at all.
    assert policy.field_lock_error("Deal", "Closed Won", ["RM"], ["rm"]) is None


def test_stage_field_uses_the_registers_real_subject_names():
    # These MUST be the Register's real subject_type strings (Lending / Syndication, not the ORM
    # class names) or every rule for those resources silently no-ops. Guards the wiring mismatch.
    assert policy.stage_field_of("Lending") == "stage"
    assert policy.stage_field_of("Syndication") == "status"
    assert policy.stage_field_of("AssetMonetisation") == "status"
    assert policy.stage_field_of("Unknown") is None
    # And the seeded rules for those subjects are actually reachable under the real names.
    assert policy.mandatory_field_error("Lending", "Disbursed", {}) is not None
    assert policy.field_lock_error("Syndication", "Sanctioned", ["Syn RM"], ["amount_cr"]) is not None
    assert policy.field_lock_error("Syndication", "Sanctioned", ["Syn Head"], ["amount_cr"]) is None


def test_transition_and_initial_status_are_reexported():
    # The engine is the single import surface for all lifecycle rules.
    assert policy.transition_error("Lead", "status", "Active", "Converted") is not None
    assert policy.initial_status_error("Lead", {"status": "Converted"}) is not None
    assert policy.initial_status_error("Lead", {"status": "Active"}) is None


def test_creation_is_restricted_to_genuine_entry_stages():
    # Only a genuine ENTRY stage may be set at birth; every later/working/governance state is
    # reached by stepping the ordered graph, never asserted at creation.
    assert policy.initial_status_error("Deal", {"stage": "Closed Won"}) is not None   # an outcome
    assert policy.initial_status_error("Deal", {"stage": "Screened Out"}) is not None  # an outcome
    assert policy.initial_status_error("Deal", {"stage": "Sanctioned"}) is not None    # not funnel vocab
    assert policy.initial_status_error("Lending", {"stage": "Disbursed"}) is not None
    assert policy.initial_status_error("Lending", {"stage": "CP/CS Completed"}) is not None  # too late
    assert policy.initial_status_error("Syndication", {"status": "IM Circulated"}) is not None  # late
    assert policy.initial_status_error("Syndication", {"status": "IP Received"}) is not None
    assert policy.initial_status_error("AssetMonetisation", {"status": "BO Received"}) is not None
    assert policy.initial_status_error("AssetMonetisation", {"status": "Closed"}) is not None
    # …but a genuine entry stage is fine, and omitting the field is fine.
    assert policy.initial_status_error("Lending", {"stage": "Data Awaited"}) is None
    assert policy.initial_status_error("Lending", {"stage": "Diligence"}) is None
    assert policy.initial_status_error("Syndication", {"status": "IM in Prep"}) is None
    assert policy.initial_status_error("Deal", {"stage": "In Pipeline"}) is None  # funnel entry
    assert policy.initial_status_error("Deal", {"rm": "asha"}) is None


def test_unknown_free_text_lifecycle_values_are_rejected_everywhere():
    # An arbitrary/free-text value is rejected at creation…
    assert policy.initial_status_error("Lending", {"stage": "Invalid Stage"}) is not None
    assert policy.stage_vocab_error("Deal", {"stage": "Whatever"}) is not None
    assert policy.stage_vocab_error("Syndication", {"status": "Nonsense"}) is not None
    # …and on update, INCLUDING when it would be the first stage of a NULL-stage row
    # (the transition graph exempts a NULL source, so the vocabulary check must catch it).
    v = policy.check_write("Deal", current={"stage": None}, changes={"stage": "Garbage"},
                           roles=["Admin"])
    assert v is not None and v.kind == "validation" and "unknown value" in v.message
    # A real vocabulary value passes the vocab gate.
    assert policy.stage_vocab_error("Lending", {"stage": "Note Circulated"}) is None


def test_transition_graph_enforces_ordered_sequencing():
    # A first set from NULL/unset is an initial set, not a transition → exempt at the graph layer.
    assert policy.transition_error("Lending", "stage", None, "Diligence") is None
    # A single forward step passes, and a one-step refer-back passes…
    assert policy.transition_error("Lending", "stage", "Diligence", "Note Circulated") is None
    assert policy.transition_error("Lending", "stage", "Note Circulated", "Diligence") is None
    # …but SKIPPING gates is rejected — no jumping Diligence → Sanctioned or Data Awaited →
    # Disbursed.
    assert policy.transition_error("Lending", "stage", "Diligence", "Sanctioned") is not None
    assert policy.transition_error("Lending", "stage", "Data Awaited", "Disbursed") is not None
    assert policy.transition_error("Syndication", "status", "Deal Sourced", "Disbursed") is not None
    assert policy.transition_error("AssetMonetisation", "status", "Teaser Prepared",
                                   "SPA / Documentation") is not None
    # A TERMINAL/committed state still cannot be silently reversed.
    assert policy.transition_error("Lending", "stage", "Disbursed", "Diligence") is not None
    assert policy.transition_error("Syndication", "status", "Withdrawn", "Deal Sourced") is not None
    # The DEAL funnel is ordered too: one step forward / back, no jumping to a win, re-openable
    # Screened Out, and final Closed terminals.
    assert policy.transition_error("Deal", "stage", "New Inquiry", "In Screening") is None
    assert policy.transition_error("Deal", "stage", "In Pipeline", "Closed Won") is None
    assert policy.transition_error("Deal", "stage", "New Inquiry", "Closed Won") is not None
    assert policy.transition_error("Deal", "stage", "Screened Out", "In Screening") is None
    assert policy.transition_error("Deal", "stage", "Closed Won", "In Pipeline") is not None
    assert policy.transition_error("Deal", "stage", "Closed Lost", "In Pipeline") is not None


def test_null_source_first_set_obeys_the_entry_allowlist():
    # A stage-less row PATCHed to its FIRST stage must still be an ENTRY stage — it cannot jump
    # straight to an advanced stage, skipping the pipeline (the NULL-source loophole is closed).
    v = policy.check_write("Lending", current={"stage": None}, changes={"stage": "Sanctioned"},
                           roles=["Credit Head"])
    assert v is not None and v.kind == "validation"
    # Entering at a genuine entry stage from NULL is fine.
    assert policy.check_write("Lending", current={"stage": None}, changes={"stage": "Diligence"},
                              roles=["Credit Head"]) is None


def test_check_write_is_the_single_authority_across_paths():
    # Creation: a reserved state is refused (validation).
    v = policy.check_write("Lending", current={}, changes={"stage": "Disbursed"},
                           roles=["Credit Head"], is_creation=True)
    assert v is not None and v.kind == "validation"
    # Update into a mandatory stage without the required fields → validation error. 'Disbursed'
    # requires the proposed amount/date ('Ready for Disbursement' no longer does — it is the
    # CP-approval milestone; but it still carries the cp_cs_completion evidence gate, checked
    # separately below and by the register).
    v = policy.check_write("Lending", current={"stage": "Ready for Disbursement"},
                           changes={"stage": "Disbursed"}, roles=["Credit Head"])
    assert v is not None and v.kind == "validation" and "proposed_disbursement_amount" in v.message
    # The evidence gate: reaching 'CP/CS Completed' needs CP/CS + executed-agreement evidence — from
    # 'Sanctioned' without it → refused…
    v = policy.check_write("Lending", current={"stage": "Sanctioned"},
                           changes={"stage": "CP/CS Completed"}, roles=["Credit Head"])
    assert v is not None and v.kind == "validation" and "evidence" in v.message.lower()
    # …and WITH the CP/CS + executed-agreement evidence → passes.
    assert policy.check_write(
        "Lending", current={"stage": "Sanctioned"},
        changes={"stage": "CP/CS Completed"},
        roles=["Credit Head"],
        evidence={"cp_cs_completion", "executed_agreement"}) is None
    # A field lock is a "forbidden" violation; Admin is break-glass.
    v = policy.check_write("Syndication", current={"status": "Sanctioned"},
                           changes={"amount_cr": 9}, roles=["Syn RM"])
    assert v is not None and v.kind == "forbidden"
    assert policy.check_write("Syndication", current={"status": "Sanctioned"},
                              changes={"amount_cr": 9}, roles=["Admin"]) is None
    # A machine caller (roles=None) skips role-based locks but still obeys lifecycle rules.
    assert policy.check_write("Syndication", current={"status": "Sanctioned"},
                              changes={"amount_cr": 9}, roles=None) is None
    v = policy.check_write("Lending", current={}, changes={"stage": "Disbursed"},
                           roles=None, is_creation=True)
    assert v is not None and v.kind == "validation"
