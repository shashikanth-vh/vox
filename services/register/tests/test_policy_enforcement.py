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


_SUBJECT_OF_KIND = {"deals": "Deal", "lending": "Lending"}
_DECISION_TABLE = {"deals": "deals", "lending": "lending_tracker"}
_DECISION_SQL = {
    "deals": ("INSERT INTO workflow_decisions (workflow_id, decision, subject_type, subject_id, "
              "run_id, decided_by, decided_by_id, roles, tenant_id) "
              "SELECT :wf, 'Approved', :st, CAST(:sid AS varchar), 'run-1', "
              "'ch@evamfinance.com', 'u-1', "
              "CAST('[\"Credit Head\"]' AS jsonb), tenant_id FROM deals "  # noqa: S608
              "WHERE id = CAST(:sid AS uuid)"),
    "lending": ("INSERT INTO workflow_decisions (workflow_id, decision, subject_type, subject_id, "
                "run_id, decided_by, decided_by_id, roles, tenant_id) "
                "SELECT :wf, 'Approved', :st, CAST(:sid AS varchar), 'run-1', "
                "'ch@evamfinance.com', 'u-1', "
                "CAST('[\"Credit Head\"]' AS jsonb), tenant_id FROM lending_tracker "  # noqa: S608
                "WHERE id = CAST(:sid AS uuid)"),
}


async def _attach_sanction_evidence(client, kind, oid):  # noqa: ANN001
    """Sanctioning is evidence-gated: a Deal/Lending line may reach 'Sanctioned' only once the
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
    ev2 = await client.post(
        "/v1/evidence",
        json={"subject_type": subject, "subject_id": str(oid),
              "evidence_kind": "executed_agreement", "reference": "ea/1", "sha256": "a" * 64,
              "workflow_id": "wf", "run_id": "run"}, headers=ADMIN)
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
    # Diligence → Handed Over to Advaya skips Note Circulated / Sanctioned / CP/CS Completed /
    # Ready for Disbursement → refused.
    r = await client.patch(f"/v1/lending/{lid}",
                           json={"stage": "Handed Over to Advaya",
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


async def test_mandatory_fields_block_stage_advance(client):
    eid = await _entity(client)
    did = await _deal(client, eid)
    await _advance(client, "deals", did, ["Diligence", "Note Circulated"])
    # Advancing a deal to Sanctioned without product_type + rm is refused (422).
    r = await client.patch(f"/v1/deals/{did}", json={"stage": "Sanctioned"})
    assert r.status_code == 422, r.text
    assert "required" in r.text.lower() and "rm" in r.text.lower()
    # With the required fields but no sanction evidence on file, the evidence gate refuses it.
    no_ev = await client.patch(f"/v1/deals/{did}",
                               json={"stage": "Sanctioned", "product_type": "Term Loan",
                                     "rm": "asha"})
    assert no_ev.status_code == 422 and "evidence" in no_ev.text.lower()
    # Supplying the required fields AND the sanction evidence → allowed.
    await _attach_sanction_evidence(client, "deals", did)
    ok = await client.patch(f"/v1/deals/{did}",
                            json={"stage": "Sanctioned", "product_type": "Term Loan",
                                  "rm": "asha"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "Sanctioned"


async def test_field_lock_restricts_who_may_edit_at_a_stage(client):
    eid = await _entity(client)
    did = await _deal(client, eid)
    await _advance(client, "deals", did, ["Diligence", "Note Circulated"])
    await _attach_sanction_evidence(client, "deals", did)
    await client.patch(f"/v1/deals/{did}",
                       json={"stage": "Sanctioned", "product_type": "Term Loan", "rm": "asha"})
    # A user context that passes scope (FULL) but is only an RM may NOT reassign rm at Sanctioned.
    rm_headers = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "RM",
                  "X-Authz-Decision": "FULL"}
    denied = await client.patch(f"/v1/deals/{did}", json={"rm": "someone-else"},
                                headers=rm_headers)
    assert denied.status_code == 403, denied.text
    assert "locked at stage" in denied.text.lower()
    # Management may.
    mgmt_headers = {"X-User-Email": "md@evamfinance.com", "X-User-Roles": "Management",
                    "X-Authz-Decision": "FULL"}
    allowed = await client.patch(f"/v1/deals/{did}", json={"rm": "new-rm"}, headers=mgmt_headers)
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
    # A facility cannot be marked 'Ready for Disbursement' without the PROPOSED amount + date.
    blocked = await client.patch(f"/v1/lending/{lid}", json={"stage": "Ready for Disbursement"})
    assert blocked.status_code == 422, blocked.text
    assert "proposed_disbursement_amount" in blocked.text and "required" in blocked.text.lower()
    ready = await client.patch(
        f"/v1/lending/{lid}",
        json={"stage": "Ready for Disbursement", "proposed_disbursement_amount": 12.5,
              "proposed_disbursement_date": "2026-01-15"})
    assert ready.status_code == 200, ready.text
    # Handover to Advaya is PRISM's TERMINAL — reachable from 'Ready for Disbursement'.
    ok = await client.patch(f"/v1/lending/{lid}", json={"stage": "Handed Over to Advaya"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "Handed Over to Advaya"
    # There is no self-disbursement onward: 'Handed Over to Advaya' only off-ramps to 'On Hold'
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
                   ["IM Circulated", "Queries Received", "IP Received", "Sanctioned"],
                   amount_cr=100)
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
        ("/v1/deals", {"entity_id": eid, "stage": "Sanctioned"}),
        ("/v1/deals", {"entity_id": eid, "stage": "Note Circulated"}),
        ("/v1/lending", {"entity_id": eid, "stage": "Handed Over to Advaya"}),
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
        ("/v1/deals", {"entity_id": eid, "stage": "Diligence"}),
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
    r = await client.post("/v1/lending", json={"entity_id": eid, "stage": "Handed Over to Advaya",
                                               "proposed_disbursement_amount": 5,
                                               "proposed_disbursement_date": "2026-01-01"})
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# The change-request APPROVAL path runs the same policy as a direct PATCH
# --------------------------------------------------------------------------- #
async def test_approval_cannot_bypass_mandatory_fields_lending(client):
    """A change request approving Lending → Ready for Disbursement must be refused when the
    disbursement amount/date are not already on the row — the approval path uses the SAME policy
    authority a direct PATCH does."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    await _advance(client, "lending", lid, ["Note Circulated", "Sanctioned", "CP/CS Completed"])
    cr = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lid, "field": "stage",
        "to_value": "Ready for Disbursement"}, headers=BD_HEAD)
    assert cr.status_code == 201, cr.text
    decided = await client.post(f"/v1/requests/{cr.json()['id']}/approve", json={},
                                headers=CREDIT_HEAD)
    assert decided.status_code == 409, decided.text
    assert "proposed_disbursement_amount" in decided.text
    # The stage did NOT change — the bypass is closed.
    assert (await client.get(f"/v1/lending/{lid}")).json()["stage"] == "CP/CS Completed"


async def test_approval_cannot_bypass_mandatory_fields_deal(client):
    """A change request approving Deal → Sanctioned must be refused when product_type/rm are not
    already on the row — mandatory fields bind the approver (even Admin)."""
    eid = await _entity(client)
    did = await _deal(client, eid)
    await _advance(client, "deals", did, ["Diligence", "Note Circulated"])
    cr = await client.post("/v1/requests", json={
        "subject_type": "Deal", "subject_id": did, "field": "stage",
        "to_value": "Sanctioned"}, headers=BD_HEAD)
    assert cr.status_code == 201, cr.text
    decided = await client.post(f"/v1/requests/{cr.json()['id']}/approve", json={},
                                headers=BD_HEAD)
    assert decided.status_code == 409, decided.text
    assert "product_type" in decided.text or "rm" in decided.text
    assert (await client.get(f"/v1/deals/{did}")).json()["stage"] != "Sanctioned"


# --------------------------------------------------------------------------- #
# Authoritative lifecycle vocabulary — reject unknown / free-text values
# --------------------------------------------------------------------------- #
async def test_stage_vocab_matches_the_authoritative_reference_dropdowns():
    """The shared STAGE_VOCAB must equal the ATLAS reference dropdowns the UI uses, so the two
    cannot drift (a value valid in one but not the other would be a silent policy hole)."""
    from evam_backend_core.rbac import STAGE_VOCAB

    from app.seed.refdata import REF_VALUES
    assert STAGE_VOCAB["Lending"][1] == frozenset(REF_VALUES["Lending Stage"])
    assert STAGE_VOCAB["Deal"][1] == frozenset(REF_VALUES["Lending Stage"])
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
