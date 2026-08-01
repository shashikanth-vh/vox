"""The shared stage/field policy engine — one server-side authority for the three lifecycle
rules every PRISM workflow enforces, so each workflow does not reinvent (and drift on)
validation:

1. **Transitions** — which status/stage moves are legal (the lifecycle graph). Sourced from the
   existing :data:`evam_backend_core.rbac.ALLOWED_TRANSITIONS` + :func:`transition_error`, and
   creation-time states from :func:`initial_status_error`, re-exported here so callers depend on
   ONE module.
2. **Mandatory fields to enter a stage** — a resource may not advance to a target status/stage
   until the fields that stage requires are present (in the update, or already on the row).
3. **Role/stage field locks** — at a given stage, a field may be edited only by the listed
   roles (e.g. a sanctioned amount is frozen except for Admin/Management).

The DATA below is the seed: it wires the Lead and Deal lifecycles that exist today and is the
extension point every subsequent workflow round adds its product line's rules to (document
collection, appraisal, committee, sanction, monitoring, syndication, …). The ENGINE
(the evaluators) is complete and product-line-agnostic.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from evam_backend_core.rbac import (
    ALLOWED_TRANSITIONS,
    INITIAL_STATUS,
    ROW_LOCKS,
    STAGE_VOCAB,
    initial_status_error,
    stage_vocab_error,
    transition_error,
)

__all__ = [
    "ALLOWED_TRANSITIONS", "INITIAL_STATUS", "STAGE_VOCAB", "ROW_LOCKS",
    "MANDATORY_FOR_STAGE", "FIELD_LOCKS", "EVIDENCE_FOR_STAGE", "PolicyViolation",
    "transition_error", "initial_status_error", "stage_vocab_error", "mandatory_field_error",
    "field_lock_error", "row_lock_error", "evidence_error", "stage_field_of", "check_write",
]

# The lifecycle field whose value IS the "stage" for a subject (what mandatory/lock rules key on).
# The KEYS are the Register's real ``subject_type`` strings (see services/register .../resources.py):
# Lead / Deal / Lending / Syndication / AssetMonetisation — NOT the ORM class names. A key that does
# not match the API subject silently disables every mandatory/lock rule for that resource, so these
# MUST stay in lock-step with the resource registry (guarded by the integration tests that PATCH the
# real /v1/lending and /v1/syndication routes).
_STAGE_FIELD: dict[str, str] = {
    "Lead": "status",
    "Deal": "stage",
    "Lending": "stage",
    "Syndication": "status",
    "AssetMonetisation": "status",
}


def stage_field_of(subject_type: str) -> str | None:
    """The name of the field that carries ``subject_type``'s lifecycle stage, if any."""
    return _STAGE_FIELD.get(subject_type)


# Fields REQUIRED to enter a given stage value. subject → stage-value → [required field names].
# A resource may not move to that value until each field is present (in the change or the row).
# Seeded conservatively for the flows that exist; workflow rounds extend their product lines.
MANDATORY_FOR_STAGE: dict[str, dict[str, list[str]]] = {
    # A Deal's stage is the COMMERCIAL funnel — no credit-governance data gates apply to it.
    # (The former Deal→Sanctioned mandate moved with the sanction itself to the Lending line;
    # the structuring workflow still records product_type/rm on the deal as plain data.)
    "Lending": {
        # A facility cannot be marked ready for disbursement without recording the PROPOSED drawdown
        # amount and date — these are fixed here and carried into the Advaya handover package. The
        # ACTUAL disbursed_amount/disbursement_date are reserved for a real disbursement confirmation
        # (a future Advaya integration), never asserted by PRISM on its own.
        "Ready for Disbursement": ["proposed_disbursement_amount", "proposed_disbursement_date"],
        # Still required at handover (already on the row by now).
        "Disbursed": ["proposed_disbursement_amount", "proposed_disbursement_date"],
    },
}

# Field-edit locks BY STAGE: at ``stage``, ``field`` may be edited ONLY by one of these roles.
# subject → field → stage-value → {allowed roles}. Absent (field, stage) → unconstrained here
# (other RBAC gates still apply). Admin is always permitted (it is the break-glass role).
FIELD_LOCKS: dict[str, dict[str, dict[str, set[str]]]] = {
    "Lending": {
        # Once the facility is Sanctioned, reassigning its relationship manager is a senior
        # action (the sanction was granted against that RM's ownership) — Management or Admin
        # only. (This lock lived on the deal's credit stage before that stage was deprecated;
        # it moved to the lending line with the rest of the sanction governance.)
        "rm": {"Sanctioned": {"Management"}},
    },
    "Syndication": {
        # Once a syndication is Sanctioned, the syndicated amount is committed to the members —
        # revising it is a Syndication-head decision, not a line RM's, so lock it there.
        "amount_cr": {"Sanctioned": {"Syn Head", "Management"}},
    },
}

_ADMIN = "Admin"

# ---------------------------------------------------------------------------------------------
# Evidence-based lifecycle gates.
#
# Ordered transitions and mandatory FIELDS prove *sequence* and *shape*, but not that the
# real-world GOVERNANCE WORK actually happened before a sensitive stage. A deal must not reach
# 'Sanctioned' because someone typed the word — it reaches it because a Credit Committee approved
# it and a sanction letter was issued. Those facts live as IMMUTABLE EVIDENCE objects (an append-
# only ``governance_evidence`` record: kind + reference + integrity hash + who/when), and the map
# below states which evidence KINDS a given stage requires. The policy engine refuses the
# transition until every required kind is present — for humans AND services alike — so the record
# can advance to a governance milestone only once the milestone genuinely occurred.
#
# The ONLY way past a missing-evidence gate is an explicit, audited BREAK-GLASS (``break_glass``
# below), which the calling endpoint may grant solely to a designated senior authority and MUST
# record with a justification. Routine callers (and every service) cannot bypass the gate.
#
# subject_type → stage-value → [required evidence kinds]. Seeded for the sensitive milestones that
# exist today; each workflow round extends its product line (appraisal, CIPHER, sanction, Advaya…).
EVIDENCE_FOR_STAGE: dict[str, dict[str, list[str]]] = {
    # SANCTION is the platform's most sensitive credit milestone. A lending line may not
    # reach 'Sanctioned' until BOTH the Credit Committee's recorded approval AND the issued sanction
    # letter are on file as immutable evidence — closing the "the graph let you type the word
    # 'Sanctioned' without the committee ever meeting" gap. The map is keyed on the REAL pipeline
    # stage values (see rbac.STAGE_VOCAB); each later workflow round extends its product line
    # (CP/CS + executed agreement before 'CP/CS Completed', a verified Advaya acknowledgement
    # before 'Disbursement Pending', CIPHER, …). A DEAL carries no evidence gates: its stage is
    # the commercial funnel, and all credit governance lives on the lending line.
    "Lending": {
        "Sanctioned": ["credit_committee_approval", "sanction_letter"],
        # A facility reaches 'CP/CS Completed' only once the things PRISM controls are on file: the
        # conditions precedent / subsequent (proven by an APPROVED CP/CS checklist — the
        # cp_cs_completion evidence is minted from it, not caller-attached) and the executed facility
        # agreement. From there the terminal (current product scope) is 'Disbursed'.
        "CP/CS Completed": ["cp_cs_completion", "executed_agreement"],
        # 'Disbursed' itself carries no evidence gate here: interactively it is reached through
        # the senior-locked maker-checker handover approval, and the dormant
        # advaya_acknowledgement kind (future Advaya integration) would add downstream
        # verification when that mode is enabled.
    },
    # A syndication MANDATE may not reach 'Sanctioned' until the recorded syndication
    # decision's evidence is on file — same shape as the lending sanction gate.
    "Syndication": {
        "Sanctioned": ["syndication_sanction"],
    },
    # An asset-monetisation mandate may not CLOSE until the recorded closure approval's
    # evidence is on file — the sale of an asset is a senior decision, never a typed word.
    "AssetMonetisation": {
        "Closed": ["am_closure_approval"],
    },
}


def evidence_error(subject_type: str, target_stage: str | None,
                   evidence_present: Iterable[str] | None) -> str | None:
    """Reject advancing to ``target_stage`` until every evidence KIND that stage requires is
    present in the subject's immutable evidence store. ``evidence_present`` is the set of kinds
    recorded for the subject (``None`` means the caller supplied no evidence context — treated as
    empty, i.e. fail-closed for any gated stage). Returns an error string, or None when the stage
    needs no evidence / all of it is present."""
    if not target_stage:
        return None
    required = EVIDENCE_FOR_STAGE.get(subject_type, {}).get(target_stage)
    if not required:
        return None
    present = set(evidence_present or ())
    missing = [k for k in required if k not in present]
    if missing:
        return (f"{subject_type} cannot advance to {target_stage!r} without the governance "
                f"evidence that stage requires: {sorted(missing)}. Attach the evidence "
                f"(POST /v1/evidence) — or, for a designated senior authority, an audited "
                f"break-glass — before the transition.")
    return None


def mandatory_field_error(subject_type: str, target_stage: str | None,
                          merged: dict) -> str | None:
    """Reject advancing a resource to ``target_stage`` when a field that stage REQUIRES is still
    absent. ``merged`` is the row AFTER the change (existing values overlaid with the update), so
    a field already present on the row satisfies the requirement. Returns an error string, or
    None when nothing is missing / the stage has no requirements."""
    if not target_stage:
        return None
    required = MANDATORY_FOR_STAGE.get(subject_type, {}).get(target_stage)
    if not required:
        return None
    missing = [f for f in required
               if merged.get(f) in (None, "") or merged.get(f) == []]
    if missing:
        return (f"{subject_type} cannot advance to {target_stage!r} until these required "
                f"fields are set: {sorted(missing)}.")
    return None


def field_lock_error(subject_type: str, stage: str | None, roles: Iterable[str],
                     changed_fields: Iterable[str]) -> str | None:
    """Reject editing a field that is LOCKED at the resource's current ``stage`` for the
    caller's ``roles``. Admin always passes (break-glass). Returns an error string for the first
    locked field the caller may not edit, else None."""
    if stage is None:
        return None
    role_set = set(roles)
    if _ADMIN in role_set:
        return None
    locks = FIELD_LOCKS.get(subject_type, {})
    for field in changed_fields:
        allowed = locks.get(field, {}).get(stage)
        if allowed is not None and not (role_set & allowed):
            return (f"{subject_type}.{field} is locked at stage {stage!r}; only "
                    f"{sorted(allowed | {_ADMIN})} may change it.")
    return None


def row_lock_error(subject_type: str, roles: Iterable[str], changes: dict) -> str | None:
    """Reject MOVING a field INTO a locked value (a Converted lead, a handed-over lending line)
    when the caller's ``roles`` are not among those the lock names. Admin always passes. Returns
    an error string, else None. (The Field-Rules ROW_LOCKS layer, keyed on the TARGET value.)"""
    role_set = set(roles)
    if _ADMIN in role_set:
        return None
    lock = ROW_LOCKS.get(subject_type)
    if lock is None:
        return None
    field_name, locking_values, allowed_roles = lock
    if (field_name in changes and changes[field_name] in locking_values
            and not (role_set & set(allowed_roles))):
        return (f"Moving {subject_type}.{field_name} to {changes[field_name]!r} is a locked "
                f"transition only {sorted(allowed_roles)} may make.")
    return None


class PolicyViolation(NamedTuple):
    """A single policy failure. ``kind`` tells the caller which HTTP fault to raise:
    ``"validation"`` → 422 (a malformed/illegal lifecycle move or a missing required field),
    ``"forbidden"`` → 403 (the caller's role may not make this change)."""
    kind: str
    message: str


def check_write(subject_type: str | None, *, current: dict, changes: dict,
                roles: Iterable[str] | None = None,
                is_creation: bool = False,
                evidence: Iterable[str] | None = None,
                break_glass: bool = False) -> PolicyViolation | None:
    """THE single policy authority every write path calls — direct PATCH, change-request
    approval, resource creation and workflow activities — so no path can enforce a different
    (or no) policy. Runs, in order and short-circuiting on the first failure:

    1. **Lifecycle position** — at creation, the initial state must be legal (allowlist +
       reserved-state denylist); on update, each changed lifecycle field must be a legal
       transition (fail-closed on an unrecognised current state; a first set from NULL is an
       initial set, not a transition).
    2. **Mandatory fields to ENTER a stage** — when the change moves the row to a target stage,
       every field that stage requires must be present (merged from the row + the change).
    3. **Role/stage field locks** — a field locked at the row's CURRENT stage may be changed
       only by the named roles (Admin break-glass). Skipped at creation (no prior stage).
    4. **Row locks** — moving a field INTO a locked TARGET value is restricted to the named
       roles.
    5. **Evidence gates** — a sensitive stage may be entered only once every immutable evidence
       kind it requires is on file (``evidence`` = the kinds recorded for the subject). Binds
       everyone (humans AND services); the sole bypass is an explicit audited ``break_glass``.

    ``current`` is the row BEFORE the change (``{}`` at creation); ``changes`` is the proposed
    update. ``roles=None`` marks a MACHINE/service caller: the role-based locks (3, 4) are
    skipped (services are bound by their service allowlist elsewhere) while the lifecycle,
    mandatory-field and evidence rules (1, 2, 5) still apply to everyone. ``evidence`` is the set
    of evidence kinds present for the subject (``None`` → none, fail-closed on any gated stage);
    ``break_glass`` is an audited senior override the caller has already authorised, which alone
    can pass a missing-evidence gate. Returns the first violation, or None when the write
    satisfies every rule."""
    if subject_type is None:
        return None
    stage_field = stage_field_of(subject_type)
    role_set = set(roles) if roles is not None else None

    # ORDER: role-based AUTHORIZATION (locks → 403) is checked before DATA validation
    # (transition/mandatory → 422), so a caller who may not make a change at all is refused
    # without being handed hints about what data the target stage needs. On creation the
    # universal reserved-state gate comes first (it binds every role, even a privileged one).

    # (A) Field locks at the CURRENT stage — updates only (creation has no prior stage). Human
    #     callers only; services are bound by their service allowlist elsewhere.
    if role_set is not None and not is_creation:
        current_stage = current.get(stage_field) if stage_field else None
        lerr = field_lock_error(subject_type, current_stage, role_set, list(changes))
        if lerr is not None:
            return PolicyViolation("forbidden", lerr)

    # (B) Lifecycle position.
    if is_creation:
        # Unknown-value + creation ALLOWLIST — universal, binds every caller including privileged
        # roles (a Sanctioned deal is reached only through the flow, never asserted at birth).
        serr = initial_status_error(subject_type, changes)
        if serr is not None:
            return PolicyViolation("validation", serr)
    else:
        # Reject an unknown/free-text lifecycle value FIRST — even when the transition graph
        # would exempt it (e.g. setting the first stage on a row whose stage was still NULL).
        verr = stage_vocab_error(subject_type, changes)
        if verr is not None:
            return PolicyViolation("validation", verr)
        # Setting the FIRST stage on a stage-less row (NULL → x) is an initial set, not a
        # transition — so it obeys the ENTRY-stage allowlist, exactly like creation. This closes
        # the hole where a row created without a stage could be PATCHed straight to an advanced
        # (e.g. Sanctioned/Disbursed) stage, skipping the whole pipeline.
        if stage_field and changes.get(stage_field) and current.get(stage_field) in (None, ""):
            ierr = initial_status_error(subject_type, {stage_field: changes[stage_field]})
            if ierr is not None:
                return PolicyViolation("validation", ierr)
        for field, to_value in changes.items():
            if (subject_type, field) in ALLOWED_TRANSITIONS and to_value is not None:
                terr = transition_error(subject_type, field, current.get(field), to_value)
                if terr is not None:
                    return PolicyViolation("validation", terr)

    # (C) Row locks — moving a field INTO a locked TARGET value (human callers only). After the
    #     lifecycle gate so a reserved-at-creation state is a 422, but before mandatory so a
    #     non-lock role advancing into a locked state is a 403, not a data hint.
    if role_set is not None:
        rerr = row_lock_error(subject_type, role_set, changes)
        if rerr is not None:
            return PolicyViolation("forbidden", rerr)

    # (D) Mandatory fields to ENTER the target stage (everyone).
    if stage_field and changes.get(stage_field):
        merged = {**current, **changes}
        merr = mandatory_field_error(subject_type, changes[stage_field], merged)
        if merr is not None:
            return PolicyViolation("validation", merr)

    # (E) Evidence gate to ENTER the target stage (everyone — humans and services). Only fires on a
    #     genuine ADVANCE (target != current stage), never a no-op re-assertion of the stage the
    #     row already holds — the gate governs the act of ENTERING a stage, which already happened
    #     for a stage the row is in. Only an explicit, audited break-glass the caller has already
    #     authorised may pass a missing gate.
    if (stage_field and changes.get(stage_field) and not break_glass
            and changes[stage_field] != current.get(stage_field)):
        eerr = evidence_error(subject_type, changes[stage_field], evidence)
        if eerr is not None:
            return PolicyViolation("validation", eerr)
    return None
