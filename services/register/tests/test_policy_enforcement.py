"""The shared stage/field policy engine, enforced by the Register on a generic update:
ORDERED lifecycle sequencing, mandatory-fields-before-advancing a stage, role/stage field locks,
and reject-unknown-values. Runs against real Postgres + the migration."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

# Human role contexts (the Register builds ctx.user from these when no signing secret is set).
# BD Head holds FULL request_stage_change and approves Lead/Deal; Credit Head approves Lending.
ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
BD_HEAD = {"X-User-Email": "bdhead@evamfinance.com", "X-User-Roles": "BD Head"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}


async def _entity(client) -> str:  # noqa: ANN001
    code = "POL" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities",
                          json={"code": code, "legal_name": "Policy Co", "entity_type": "Company"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _deal(client, eid) -> str:  # noqa: ANN001
    r = await client.post("/v1/deals", json={"entity_id": eid})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# Sanction evidence applies to LENDING only — a deal's stage is the commercial funnel and
# carries no credit governance (the deal-level credit stage is deprecated).
_SUBJECT_OF_KIND = {"lending": "Lending"}
_DECISION_TABLE = {"lending": "lending_tracker"}
_DECISION_SQL = {
    "lending": ("INSERT INTO workflow_decisions (workflow_id, decision, subject_type, subject_id, "
                "run_id, decided_by, decided_by_id, roles, tenant_id) "
                "SELECT :wf, 'Approved', :st, CAST(:sid AS varchar), 'run-1', "
                "'ch@evamfinance.com', 'u-1', "
                "CAST('[\"Credit Head\"]' AS jsonb), tenant_id FROM lending_tracker "  # noqa: S608
                "WHERE id = CAST(:sid AS uuid)"),
}


async def _attach_sanction_evidence(client, kind, oid):  # noqa: ANN001
    """Sanctioning is evidence-gated: a Lending line may reach 'Sanctioned' only once the
    Credit Committee approval AND the sanction letter are on file — each VERIFIED against a durable,
    committee-authority decision. Seed that decision, then attach both records citing it."""
    import uuid as _uuid

    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    subject = _SUBJECT_OF_KIND.get(kind)
    if subject is None:
        return
    wf = f"committee-{_uuid.uuid4().hex[:12]}"
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text(_DECISION_SQL[kind]), {"wf": wf, "st": subject, "sid": str(oid)})
        await s.commit()
    for kind_ in ("credit_committee_approval", "sanction_letter"):
        r = await client.post("/v1/evidence",
                              json={"subject_type": subject, "subject_id": oid,
                                    "evidence_kind": kind_, "reference": f"{kind_}/DOC-1",
                                    "sha256": "a" * 64, "decision_ref": wf},
                              headers=ADMIN)
        assert r.status_code == 201, r.text


async def _attach_cpcs_evidence(client, kind, oid):  # noqa: ANN001
    """Reaching 'CP/CS Completed' is evidence-gated. cp_cs_completion is minted from an APPROVED
    CP/CS checklist (maker ADMIN completes it, a DIFFERENT checker CREDIT_HEAD approves it), never
    caller-attached; executed_agreement is a governance kind (digest + run provenance)."""
    subject = _SUBJECT_OF_KIND.get(kind)
    if subject is None:
        return
    # Maker prepares a completed checklist; a different checker approves it.
    chk = await client.post(
        "/v1/internal/cpcs-checklists",
        json={"lending_id": str(oid), "status": "Completed",
              "items": [{"key": "cp1", "condition_type": "CP", "status": "Completed"}]}, headers=ADMIN)
    assert chk.status_code == 201, chk.text
    cid = chk.json()["id"]
    appr = await client.post(f"/v1/internal/cpcs-checklists/{cid}/approve", headers=CREDIT_HEAD)
    assert appr.status_code == 200, appr.text
    # cp_cs_completion VERIFIED against the approved checklist (decision_ref = checklist id).
    ev = await client.post(
        "/v1/evidence",
        json={"subject_type": subject, "subject_id": str(oid), "evidence_kind": "cp_cs_completion",
              "reference": "cpcs/1", "sha256": "a" * 64, "decision_ref": cid}, headers=ADMIN)
    assert ev.status_code == 201, ev.text
    # executed_agreement must cite a workflow that RESOLVES to a decision recorded for this
    # subject (an invented id is refused) — seed one, exactly as the orchestrator would have.
    import uuid as _uuid

    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    wf = f"docs-{_uuid.uuid4().hex[:12]}"
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text(_DECISION_SQL[kind]), {"wf": wf, "st": subject, "sid": str(oid)})
        await s.commit()
    ev2 = await client.post(
        "/v1/evidence",
        json={"subject_type": subject, "subject_id": str(oid),
              "evidence_kind": "executed_agreement", "reference": "ea/1", "sha256": "a" * 64,
              "workflow_id": wf, "run_id": "run-1"}, headers=ADMIN)
    assert ev2.status_code == 201, ev2.text


async def _advance(client, kind, oid, path, **final):  # noqa: ANN001
    """Step a line through the ORDERED pipeline ``path`` (the stages AFTER its current one), one
    valid hop at a time, applying ``final`` on the last hop. A test can no longer jump straight
    to a governance stage — it must walk the real sequence, which is the point."""
    field = "status" if kind in ("syndication", "asset-monetisation") else "stage"
    for i, stage in enumerate(path):
        if stage == "Sanctioned":
            await _attach_sanction_evidence(client, kind, oid)
        if stage == "CP/CS Completed":
            await _attach_cpcs_evidence(client, kind, oid)
        body = {field: stage}
        if i == len(path) - 1:
            body.update(final)
        r = await client.patch(f"/v1/{kind}/{oid}", json=body)
        assert r.status_code == 200, f"advance to {stage!r}: {r.text}"


# --------------------------------------------------------------------------- #
# Ordered sequencing — no skipping gates; explicit refer-back allowed
# --------------------------------------------------------------------------- #
async def test_direct_patch_cannot_skip_pipeline_stages(client):
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    # Diligence → Disbursed skips Note Circulated / Sanctioned / CP/CS Completed /
    # Ready for Disbursement → refused.
    r = await client.patch(f"/v1/lending/{lid}",
                           json={"stage": "Disbursed",
                                 "proposed_disbursement_amount": 5,
                                 "proposed_disbursement_date": "2026-01-01"})
    assert r.status_code == 422, r.text
    assert "may not move" in r.text.lower()
    # A single forward step is allowed…
    assert (await client.patch(f"/v1/lending/{lid}",
                               json={"stage": "Note Circulated"})).status_code == 200
    # …and a refer-back one step (Note Circulated → Diligence) is allowed.
    back = await client.patch(f"/v1/lending/{lid}", json={"stage": "Diligence"})
    assert back.status_code == 200, back.text


async def test_deal_stage_is_the_commercial_funnel(client):
    """A deal's stage speaks ONLY the origination funnel: a credit-lifecycle word is an unknown
    value, the first set obeys the entry allowlist, movement is ordered, terminals are final."""
    eid = await _entity(client)
    did = await _deal(client, eid)
    # A credit word is no longer deal vocabulary (credit lives on the lending line) — the funnel
    # Literal rejects it at the schema (422).
    r = await client.patch(f"/v1/deals/{did}", json={"stage": "Sanctioned"})
    assert r.status_code == 422, r.text
    # NULL → a terminal is not a birth state (the entry allowlist governs the first set too).
    r = await client.patch(f"/v1/deals/{did}", json={"stage": "Closed Won"})
    assert r.status_code == 422, r.text
    # The funnel walks in order to the decision point…
    for fs in ("New Inquiry", "In Screening", "In Pipeline"):
        r = await client.patch(f"/v1/deals/{did}", json={"stage": fs})
        assert r.status_code == 200, f"{fs}: {r.text}"
    # …the CLOSED terminals are never a bare stage edit (increment 8: closure is
    # open-item validated via the dedicated endpoint)…
    r = await client.patch(f"/v1/deals/{did}", json={"stage": "Closed Won"})
    assert r.status_code == 422 and "close" in r.text.lower()
    r = await client.post(f"/v1/deals/{did}/close",
                          json={"outcome": "won", "note": "mandate signed"})
    assert r.status_code == 200 and r.json()["stage"] == "Closed Won"
    # …and a CLOSED terminal is final (a revived opportunity is a new deal).
    r = await client.patch(f"/v1/deals/{did}", json={"stage": "In Pipeline"})
    assert r.status_code == 422, r.text
    assert "may not move" in r.text.lower()


async def test_field_lock_restricts_who_may_edit_at_a_stage(client):
    """The sanctioned-RM lock lives on the LENDING line (it moved there with the rest of the
    sanction governance when the deal-level credit stage was deprecated)."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    await _advance(client, "lending", lid, ["Note Circulated", "Sanctioned"], rm="asha")
    # A user context that passes scope (FULL) but is only an RM may NOT reassign rm at Sanctioned.
    rm_headers = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "RM",
                  "X-Authz-Decision": "FULL"}
    denied = await client.patch(f"/v1/lending/{lid}", json={"rm": "someone-else"},
                                headers=rm_headers)
    assert denied.status_code == 403, denied.text
    assert "locked at stage" in denied.text.lower()
    # Management may.
    mgmt_headers = {"X-User-Email": "md@evamfinance.com", "X-User-Roles": "Management",
                    "X-Authz-Decision": "FULL"}
    allowed = await client.patch(f"/v1/lending/{lid}", json={"rm": "new-rm"}, headers=mgmt_headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["rm"] == "new-rm"


async def test_mandatory_fields_enforced_on_the_real_lending_route(client):
    """Guards the subject-name wiring: the policy engine must key on the Register's REAL subject
    type ("Lending"), so mandatory-field rules fire on the actual /v1/lending PATCH route."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    # Reaching 'CP/CS Completed' first requires the committee + sanction evidence (at 'Sanctioned')
    # and the CP/CS + executed-agreement evidence (at 'CP/CS Completed'); _advance files both.
    # Before CP/CS evidence is on file, the hop is refused.
    await _advance(client, "lending", lid, ["Note Circulated", "Sanctioned"])
    no_ev = await client.patch(f"/v1/lending/{lid}", json={"stage": "CP/CS Completed"})
    assert no_ev.status_code == 422 and "evidence" in no_ev.text.lower()
    await _advance(client, "lending", lid, ["CP/CS Completed"])
    # 'Ready for Disbursement' is the CP-approval milestone — it takes the label with NO
    # proposed drawdown on the row (the figures are fixed by the Disburse action later).
    ready = await client.patch(f"/v1/lending/{lid}", json={"stage": "Ready for Disbursement"})
    assert ready.status_code == 200, ready.text
    # 'Disbursed' still cannot be reached without the PROPOSED amount + date.
    blocked = await client.patch(f"/v1/lending/{lid}", json={"stage": "Disbursed"})
    assert blocked.status_code == 422, blocked.text
    assert "proposed_disbursement_amount" in blocked.text and "required" in blocked.text.lower()
    ok = await client.patch(
        f"/v1/lending/{lid}",
        json={"stage": "Disbursed", "proposed_disbursement_amount": 12.5,
              "proposed_disbursement_date": "2026-01-15"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "Disbursed"
    # There is no self-disbursement onward: 'Disbursed' only off-ramps to 'On Hold'
    # (the onward disbursement states exist only under a future Advaya integration).
    onward = await client.patch(f"/v1/lending/{lid}", json={"stage": "Disbursement Pending"})
    assert onward.status_code == 422 and "unknown value" in onward.text.lower()


async def test_field_lock_enforced_on_the_real_syndication_route(client):
    """Guards the subject-name wiring for Syndication (stage field is `status`): once a
    syndication is Sanctioned, only Syn Head/Management/Admin may revise the syndicated amount."""
    eid = await _entity(client)
    sid = (await client.post("/v1/syndication",
                             json={"entity_id": eid, "status": "IM in Prep"})).json()["id"]
    await _advance(client, "syndication", sid,
                   ["IM Circulated", "Queries Received", "IP Received"])
    # 'Sanctioned' is now evidence-gated (increment 5): seed the recorded syndication
    # decision and file the verified sanction evidence, exactly like the real path.
    import uuid as _uuid

    from sqlalchemy import text as _text

    from app.db.session import get_sessionmaker as _gsm
    wf = f"synd-{_uuid.uuid4().hex[:12]}"
    async with _gsm()() as _s:
        await _s.execute(_text(
            "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, "
            "subject_id, run_id, decided_by, decided_by_id, roles, tenant_id) "
            "SELECT :wf, 'Approved', 'Syndication', CAST(:sid AS varchar), 'run-1', "
            "'sh@evamfinance.com', 'u-9', CAST('[\"Syn Head\"]' AS jsonb), tenant_id "  # noqa: S608
            "FROM syndication_tracker WHERE id = CAST(:sid AS uuid)"),
            {"wf": wf, "sid": sid})
        await _s.commit()
    assert (await client.post("/v1/evidence", json={
        "subject_type": "Syndication", "subject_id": sid,
        "evidence_kind": "syndication_sanction", "reference": "syn/1",
        "sha256": "a" * 64, "decision_ref": wf},
        headers={"X-User-Email": "sh@evamfinance.com",
                 "X-User-Roles": "Syn Head"})).status_code == 201
    assert (await client.patch(f"/v1/syndication/{sid}",
                               json={"status": "Sanctioned",
                                     "amount_cr": 100})).status_code == 200
    # A line RM (passes scope via FULL) may NOT revise the committed amount at Sanctioned.
    rm_headers = {"X-User-Email": "synrm@evamfinance.com", "X-User-Roles": "Syn RM",
                  "X-Authz-Decision": "FULL"}
    denied = await client.patch(f"/v1/syndication/{sid}", json={"amount_cr": 150},
                                headers=rm_headers)
    assert denied.status_code == 403, denied.text
    assert "locked at stage" in denied.text.lower()
    # A Syndication head may.
    head_headers = {"X-User-Email": "synhead@evamfinance.com", "X-User-Roles": "Syn Head",
                    "X-Authz-Decision": "FULL"}
    allowed = await client.patch(f"/v1/syndication/{sid}", json={"amount_cr": 150},
                                 headers=head_headers)
    assert allowed.status_code == 200, allowed.text
    assert float(allowed.json()["amount_cr"]) == 150.0


# --------------------------------------------------------------------------- #
# Creation is restricted to genuine ENTRY stages
# --------------------------------------------------------------------------- #
async def test_creation_is_restricted_to_genuine_entry_stages(client):
    """A resource may be born only at a genuine entry stage — never directly at a later working
    stage (Note Circulated / IM Circulated) or a governance/terminal outcome."""
    eid = await _entity(client)
    refused = [
        ("/v1/deals", {"entity_id": eid, "stage": "Closed Won"}),
        ("/v1/deals", {"entity_id": eid, "stage": "Screened Out"}),
        ("/v1/lending", {"entity_id": eid, "stage": "Disbursed"}),
        ("/v1/lending", {"entity_id": eid, "stage": "Ready for Disbursement"}),
        ("/v1/syndication", {"entity_id": eid, "status": "Sanctioned"}),
        ("/v1/syndication", {"entity_id": eid, "status": "IM Circulated"}),
        ("/v1/asset-monetisation", {"entity_id": eid, "status": "BO Received"}),
        ("/v1/asset-monetisation", {"entity_id": eid, "status": "Dropped"}),
    ]
    for path, body in refused:
        r = await client.post(path, json=body)
        assert r.status_code == 422, f"{path} should refuse a non-entry initial state: {r.text}"
    # …but a genuine entry stage is accepted for each line.
    for path, body in [
        ("/v1/deals", {"entity_id": eid, "stage": "In Screening"}),
        ("/v1/lending", {"entity_id": eid, "stage": "Data Awaited"}),
        ("/v1/syndication", {"entity_id": eid, "status": "Deal Sourced"}),
        ("/v1/asset-monetisation", {"entity_id": eid, "status": "Teaser Prepared"}),
    ]:
        r = await client.post(path, json=body)
        assert r.status_code == 201, f"{path} should accept an entry state: {r.text}"


async def test_creation_applies_mandatory_fields_when_a_stage_is_supplied(client):
    """Creating a line directly in a non-entry/governance stage is refused (it must be reached
    through the pipeline) — proving creation runs the SAME check_write authority as a PATCH."""
    eid = await _entity(client)
    r = await client.post("/v1/lending", json={"entity_id": eid, "stage": "Disbursed",
                                               "proposed_disbursement_amount": 5,
                                               "proposed_disbursement_date": "2026-01-01"})
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# The change-request APPROVAL path runs the same policy as a direct PATCH
# --------------------------------------------------------------------------- #
async def test_approval_cannot_bypass_mandatory_fields_lending(client):
    """A change request approving Lending → Disbursed must be refused when the proposed
    drawdown amount/date are not already on the row — the approval path uses the SAME policy
    authority a direct PATCH does. ('Ready for Disbursement' carries no field mandate any
    more — it is the CP-approval milestone.)"""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    await _advance(client, "lending", lid,
                   ["Note Circulated", "Sanctioned", "CP/CS Completed",
                    "Ready for Disbursement"])
    cr = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lid, "field": "stage",
        "to_value": "Disbursed"}, headers=BD_HEAD)
    assert cr.status_code == 201, cr.text
    decided = await client.post(f"/v1/requests/{cr.json()['id']}/approve", json={},
                                headers=CREDIT_HEAD)
    assert decided.status_code == 409, decided.text
    assert "proposed_disbursement_amount" in decided.text
    # The stage did NOT change — the bypass is closed.
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "Ready for Disbursement"


async def test_approval_cannot_bypass_funnel_order_deal(client):
    """A change request approving a funnel JUMP (New Inquiry → Closed Won) must be refused —
    the approval path runs the SAME transition graph a direct PATCH does."""
    eid = await _entity(client)
    did = await _deal(client, eid)
    await client.patch(f"/v1/deals/{did}", json={"stage": "New Inquiry"})
    cr = await client.post("/v1/requests", json={
        "subject_type": "Deal", "subject_id": did, "field": "stage",
        "to_value": "Closed Won"}, headers=BD_HEAD)
    assert cr.status_code == 201, cr.text
    # a DIFFERENT authority decides (maker-checker refuses the requester first)
    decided = await client.post(f"/v1/requests/{cr.json()['id']}/approve", json={},
                                headers={"X-User-Email": "kannan@evamfinance.com",
                                         "X-User-Roles": "Management"})
    assert decided.status_code == 409, decided.text
    assert "may not move" in decided.text.lower()
    assert (await client.get(f"/v1/deals/{did}")).json()["stage"] == "New Inquiry"


# --------------------------------------------------------------------------- #
# Authoritative lifecycle vocabulary — reject unknown / free-text values
# --------------------------------------------------------------------------- #
async def test_stage_vocab_matches_the_authoritative_reference_dropdowns():
    """The shared STAGE_VOCAB must equal the ATLAS reference dropdowns the UI uses, so the two
    cannot drift (a value valid in one but not the other would be a silent policy hole)."""
    from evam_backend_core.rbac import STAGE_VOCAB

    from app.seed.refdata import REF_VALUES
    assert STAGE_VOCAB["Lending"][1] == frozenset(REF_VALUES["Lending Stage"])
    from evam_backend_core.rbac import DEAL_FUNNEL_STAGES
    assert STAGE_VOCAB["Deal"][1] == frozenset(DEAL_FUNNEL_STAGES)
    assert frozenset(DEAL_FUNNEL_STAGES) == frozenset(REF_VALUES["Deal Funnel Stage"])
    assert STAGE_VOCAB["Syndication"][1] == frozenset(REF_VALUES["Status of Proposal"])
    assert STAGE_VOCAB["AssetMonetisation"][1] == frozenset(REF_VALUES["Asset Mon Status"])


async def test_unknown_lifecycle_value_is_rejected_on_create_and_patch(client):
    """A free-text/unknown stage is refused both at creation and via a later PATCH — including
    when it would be the FIRST stage of a row created without one (the NULL-source case)."""
    eid = await _entity(client)
    # Creation with an unknown stage → refused.
    bad_create = await client.post("/v1/lending", json={"entity_id": eid, "stage": "Made Up"})
    assert bad_create.status_code == 422, bad_create.text
    # Create clean (no stage), then PATCH to an unknown value → refused (NULL-source case).
    lid = (await client.post("/v1/lending", json={"entity_id": eid})).json()["id"]
    bad_patch = await client.patch(f"/v1/lending/{lid}", json={"stage": "Totally Invalid"})
    assert bad_patch.status_code == 422, bad_patch.text
    assert "unknown value" in bad_patch.text.lower()
    # A real ENTRY vocabulary value is accepted as the first stage.
    ok = await client.patch(f"/v1/lending/{lid}", json={"stage": "Diligence"})
    assert ok.status_code == 200, ok.text


async def test_cp_approval_unblocks_disbursement_straight_from_sanctioned(client):
    """The parallel-CS design: with the CP checklist approved (cp_cs_completion on
    file) a Sanctioned line finalises for disbursement DIRECTLY — no pass through
    'CP/CS Completed', which stays the both-halves-done milestone. Without the CP
    evidence the same move is refused."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    await _advance(client, "lending", lid, ["Note Circulated", "Sanctioned"])
    # no CP evidence yet: the direct edge is gated shut
    r = await client.patch(f"/v1/lending/{lid}",
                           json={"stage": "Ready for Disbursement",
                                 "proposed_disbursement_amount": 5,
                                 "proposed_disbursement_date": "2026-09-01"},
                           headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "cp_cs_completion" in r.text
    # CP approved (evidence minted) → the same move lands; CS may still be open
    await _attach_cpcs_evidence(client, "lending", lid)
    r = await client.patch(f"/v1/lending/{lid}",
                           json={"stage": "Ready for Disbursement",
                                 "proposed_disbursement_amount": 5,
                                 "proposed_disbursement_date": "2026-09-01"},
                           headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "Ready for Disbursement"
    # and the money can move
    r = await client.patch(f"/v1/lending/{lid}", json={"stage": "Disbursed"}, headers=ADMIN)
    assert r.status_code == 200, r.text


async def test_money_on_the_book_shuts_the_on_hold_rewind(client):
    """Disbursed → On Hold is a pause; On Hold → Diligence on a line with booked money
    would rewind the book past the tranche — the money guard refuses it on the real
    route, while resuming to 'Disbursed' stays open."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    await _advance(client, "lending", lid,
                   ["Note Circulated", "Sanctioned", "CP/CS Completed",
                    "Ready for Disbursement"])
    ok = await client.patch(
        f"/v1/lending/{lid}",
        json={"stage": "Disbursed", "proposed_disbursement_amount": 1.6,
              "proposed_disbursement_date": "2026-08-26",
              "disbursed_amount": 1.6})
    assert ok.status_code == 200, ok.text
    assert (await client.patch(f"/v1/lending/{lid}",
                               json={"stage": "On Hold"})).status_code == 200
    rewind = await client.patch(f"/v1/lending/{lid}", json={"stage": "Diligence"})
    assert rewind.status_code == 422, rewind.text
    assert "already disbursed" in rewind.text
    resume = await client.patch(f"/v1/lending/{lid}", json={"stage": "Disbursed"})
    assert resume.status_code == 200, resume.text
