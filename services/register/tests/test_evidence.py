"""Evidence-based lifecycle gates.

Ordered transitions prove sequence; mandatory fields prove shape; an evidence gate proves the
real-world governance WORK happened. A LENDING line may reach the sanction milestone only once
the Credit Committee approval AND the sanction letter are on file as IMMUTABLE evidence (the
deal-level credit stage is deprecated — every credit gate keys on the lending line) — enforced
by the shared policy engine for humans and services alike, bypassable only via an audited senior
break-glass. Runs against real Postgres + the migration."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
MGMT = {"X-User-Email": "cro@evamfinance.com", "X-User-Roles": "Management"}
CREDIT_HEAD = {"X-User-Email": "ch@evamfinance.com", "X-User-Roles": "Credit Head"}
RM = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM", "X-Authz-Decision": "FULL"}
ANALYST = {"X-User-Email": "an@evamfinance.com", "X-User-Roles": "Deal Analyst",
           "X-Authz-Decision": "FULL"}


async def _entity(client) -> str:  # noqa: ANN001
    code = "EV" + uuid.uuid4().hex[:6].upper()
    r = await client.post("/v1/entities",
                          json={"code": code, "legal_name": "Evidence Co", "entity_type": "Company"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _line_at_note_circulated(client) -> str:  # noqa: ANN001
    """A LENDING line walked to the committee stage — the subject every credit-evidence gate
    keys on (a deal's stage is the commercial funnel and carries no evidence gates)."""
    eid = await _entity(client)
    lid = (await client.post("/v1/lending",
                             json={"entity_id": eid, "stage": "Diligence"})).json()["id"]
    assert (await client.patch(f"/v1/lending/{lid}",
                               json={"stage": "Note Circulated"})).status_code == 200
    return lid


_TABLE_OF = {"Lending": "lending_tracker"}

_DECISION_INSERT_COLS = (
    "INSERT INTO workflow_decisions "
    "(workflow_id, decision, subject_type, subject_id, run_id, decided_by, "
    " decided_by_id, roles, tenant_id) "
    "SELECT :wf, :dec, :st, CAST(:sid AS varchar), 'run-1', 'ch@evamfinance.com', 'u-1', "
    "CAST(:roles AS jsonb), tenant_id FROM ")
_TAIL = " WHERE id = CAST(:sid AS uuid)"
# Literal per-table statements (the table name is never interpolated from input).
_DECISION_INSERT_SQL = {
    "Lending": _DECISION_INSERT_COLS + "lending_tracker" + _TAIL,   # noqa: S608
}


async def _seed_committee_decision(subject_type, subject_id, outcome="Approved",  # noqa: ANN001
                                   roles=("Credit Head",)) -> str:
    """Insert a durable, single-winner Credit Committee decision the evidence attach verifies
    against — bound to this subject, recorded by committee authority. Returns its workflow_id (the
    decision_ref governance evidence must cite)."""
    import json

    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    wf = f"committee-{uuid.uuid4().hex[:12]}"
    sm = get_sessionmaker()
    # Full statements per subject type (no interpolation → no dynamic-SQL lint), differing only in
    # the fixed source table the tenant_id is read from.
    sql = _DECISION_INSERT_SQL[subject_type]
    async with sm() as s:
        await s.execute(text(sql),
                        {"wf": wf, "dec": outcome, "st": subject_type, "sid": str(subject_id),
                         "roles": json.dumps(list(roles))})
        await s.commit()
    return wf


async def _attach(client, subject_type, subject_id, kind, headers=ADMIN,  # noqa: ANN001
                  decision_ref=None, **extra):
    """Attach evidence, seeding + citing a verified committee decision for decision-backed kinds and
    supplying the digest/run provenance other governance kinds need."""
    from evam_backend_core.evidence import spec_for_kind
    spec = spec_for_kind(kind)
    body = {"subject_type": subject_type, "subject_id": subject_id,
            "evidence_kind": kind, "reference": f"{kind}/DOC-1", **extra}
    if spec is not None and spec.decision_outcome is not None:
        # Only seed a decision when the subject can carry one (a mismatched subject_type is left to
        # the endpoint to reject).
        if (decision_ref is None and "decision_ref" not in extra
                and subject_type in _TABLE_OF):
            decision_ref = await _seed_committee_decision(subject_type, subject_id,
                                                          spec.decision_outcome)
        if decision_ref is not None:
            body["decision_ref"] = decision_ref
        body.setdefault("sha256", "a" * 64)
    elif spec is not None and spec.governance:
        body.setdefault("sha256", "a" * 64)
        body.setdefault("workflow_id", "wf-doc")
        body.setdefault("run_id", "run-doc")
    return await client.post("/v1/evidence", json=body, headers=headers)


_SANCTION_BODY = {"stage": "Sanctioned", "rm": "asha"}


async def test_sanction_is_blocked_without_governance_evidence(client):
    """A lending line that has satisfied the ordered pipeline STILL cannot reach
    Sanctioned until the committee-approval + sanction-letter evidence is on file."""
    did = await _line_at_note_circulated(client)
    blocked = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert blocked.status_code == 422, blocked.text
    assert "evidence" in blocked.text.lower()
    assert "credit_committee_approval" in blocked.text and "sanction_letter" in blocked.text


async def test_partial_evidence_still_blocks_sanction(client):
    """BOTH required kinds are needed — one on its own is not enough."""
    did = await _line_at_note_circulated(client)
    assert (await _attach(client, "Lending", did, "credit_committee_approval")).status_code == 201
    blocked = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert blocked.status_code == 422, blocked.text
    assert "sanction_letter" in blocked.text
    # The still-missing kind is named; the one already present is not demanded again.
    assert "credit_committee_approval" not in blocked.text


async def test_sanction_allowed_once_all_evidence_is_on_file(client):
    """With both immutable evidence records attached, the sanction transition is accepted."""
    did = await _line_at_note_circulated(client)
    assert (await _attach(client, "Lending", did, "credit_committee_approval",
                          sha256="a" * 64)).status_code == 201
    assert (await _attach(client, "Lending", did, "sanction_letter")).status_code == 201
    ok = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "Sanctioned"


async def test_evidence_gate_binds_machine_callers_too(client):
    """The gate is not a role check — a machine/service caller (no user context) is equally bound;
    the evidence must exist regardless of who advances the stage."""
    did = await _line_at_note_circulated(client)
    # No headers → machine caller. Still refused without evidence.
    blocked = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert blocked.status_code == 422 and "evidence" in blocked.text.lower()


async def test_break_glass_requires_senior_authority_and_is_audited(client):
    """The ONLY way past a missing-evidence gate is an audited break-glass reserved to a designated
    senior authority (Admin/Management). A non-senior may not; a service may not; a senior may, and
    the override is recorded in the audit log."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    did = await _line_at_note_circulated(client)
    bg = "X-Evidence-Break-Glass"

    # A machine caller supplying the header has no senior identity → refused (403).
    svc = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY,
                             headers={bg: "urgent, letter pending"})
    assert svc.status_code == 403, svc.text

    # A non-senior human (RM, even with FULL scope) → refused.
    rm = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "RM", "X-Authz-Decision": "FULL",
          bg: "urgent"}
    assert (await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY,
                               headers=rm)).status_code == 403

    # Management with a justification → allowed, and audited.
    ok = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY,
                            headers={**MGMT, bg: "board pre-cleared; letter issues tomorrow"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "Sanctioned"
    sm = get_sessionmaker()
    async with sm() as s:
        row = (await s.execute(text(
            "SELECT changes FROM audit_log WHERE action='evidence.break_glass' "
            "AND resource_id=:i"), {"i": did})).first()
    assert row is not None, "a break-glass override must be audited"
    assert row[0]["target_stage"] == "Sanctioned"
    assert "board pre-cleared" in row[0]["justification"]


async def test_break_glass_without_a_real_gap_does_not_forge_an_audit(client):
    """A break-glass header on a transition whose evidence IS on file is a harmless no-op — it
    must not fabricate an override audit entry (nothing was actually bypassed)."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    did = await _line_at_note_circulated(client)
    await _attach(client, "Lending", did, "credit_committee_approval")
    await _attach(client, "Lending", did, "sanction_letter")
    ok = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY,
                            headers={**ADMIN, "X-Evidence-Break-Glass": "unnecessary"})
    assert ok.status_code == 200, ok.text
    sm = get_sessionmaker()
    async with sm() as s:
        row = (await s.execute(text(
            "SELECT changes FROM audit_log WHERE action='evidence.break_glass' "
            "AND resource_id=:i"), {"i": did})).first()
    assert row is None, "no gap was bypassed, so no break-glass audit should be written"


async def test_evidence_is_immutable_at_the_database(client):
    """Evidence is WRITE-ONCE: the database trigger rejects UPDATE and DELETE, so an attached record
    cannot be silently altered or removed to retro-(un)justify a transition."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db.session import get_sessionmaker
    did = await _line_at_note_circulated(client)
    r = await _attach(client, "Lending", did, "credit_committee_approval")
    ev_id = r.json()["id"]
    sm = get_sessionmaker()
    async with sm() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE governance_evidence SET reference='tampered' "
                                 "WHERE id=:i"), {"i": ev_id})
        await s.rollback()
    async with sm() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM governance_evidence WHERE id=:i"), {"i": ev_id})
        await s.rollback()


async def test_attaching_evidence_requires_an_identified_principal(client):
    """Attaching evidence asserts a governance fact — an anonymous (unnamed-key, no-user) caller may
    not; an Admin may, and it is listed back."""
    did = await _line_at_note_circulated(client)
    anon = await _attach(client, "Lending", did, "credit_committee_approval", headers={})
    assert anon.status_code == 403, anon.text
    ok = await _attach(client, "Lending", did, "credit_committee_approval")
    assert ok.status_code == 201, ok.text
    listed = await client.get("/v1/evidence",
                              params={"subject_type": "Lending", "subject_id": did}, headers=ADMIN)
    assert listed.status_code == 200
    kinds = [e["evidence_kind"] for e in listed.json()["items"]]
    assert "credit_committee_approval" in kinds


# --------------------------------------------------------------------------- #
# Evidence must be AUTHORISED — not manufacturable by any identified caller.
# --------------------------------------------------------------------------- #
async def test_committee_and_sanction_evidence_need_the_credit_authority(client):
    """The core trust fix: committee-approval / sanction-letter evidence is reserved to the credit
    authority (Credit Head / Management / Admin) and the workflow service. An ordinary identified
    principal — an RM or an Analyst — CANNOT manufacture it, even though they can otherwise operate
    on the line. Without this, anyone able to advance the line could satisfy the sanction gate."""
    did = await _line_at_note_circulated(client)
    for kind in ("credit_committee_approval", "sanction_letter"):
        assert (await _attach(client, "Lending", did, kind, headers=RM)).status_code == 403
        assert (await _attach(client, "Lending", did, kind, headers=ANALYST)).status_code == 403
    # The credit authority may.
    ok = await _attach(client, "Lending", did, "credit_committee_approval", headers=CREDIT_HEAD)
    assert ok.status_code == 201, ok.text


async def test_unknown_evidence_kind_is_rejected(client):
    """Evidence kinds are a controlled vocabulary — an arbitrary string is refused (no self-minted
    'kinds' that a future gate might trust)."""
    did = await _line_at_note_circulated(client)
    r = await client.post("/v1/evidence",
                          json={"subject_type": "Lending", "subject_id": did,
                                "evidence_kind": "totally_made_up", "reference": "x/1"},
                          headers=ADMIN)
    assert r.status_code == 422 and "controlled vocabulary" in r.text.lower()


async def test_evidence_kind_must_match_subject_type(client):
    """A committee approval belongs on a Deal/Lending, never a Lead — the kind's allowed subject
    types are enforced."""
    eid = await _entity(client)
    lead = (await client.post("/v1/leads",
                              json={"entity_id": eid, "company": "L"})).json()["id"]
    r = await _attach(client, "Lead", lead, "credit_committee_approval")
    assert r.status_code == 422 and "may not be attached" in r.text.lower()


async def test_committee_evidence_needs_a_digest_and_a_decision_ref(client):
    """A committee/sanction record must carry an integrity digest AND cite a committee decision — a
    free-typed record is not enough."""
    did = await _line_at_note_circulated(client)
    wf = await _seed_committee_decision("Lending", did)
    base = {"subject_type": "Lending", "subject_id": did,
            "evidence_kind": "credit_committee_approval", "reference": "c/1"}
    # Missing sha256 (decision cited).
    r1 = await client.post("/v1/evidence", json={**base, "decision_ref": wf}, headers=ADMIN)
    assert r1.status_code == 422 and "sha256" in r1.text.lower()
    # Missing decision_ref.
    r2 = await client.post("/v1/evidence", json={**base, "sha256": "a" * 64}, headers=ADMIN)
    assert r2.status_code == 422 and "decision_ref" in r2.text.lower()


async def test_committee_provenance_is_verified_not_asserted(client):
    """The core Round-L fix: committee/sanction provenance is VERIFIED against the durable decision,
    not merely recorded. Invented, mismatched, rejected and cross-subject decisions are all
    refused; only a genuine Approved committee decision for THIS subject works."""
    did = await _line_at_note_circulated(client)
    body = {"subject_type": "Lending", "subject_id": did,
            "evidence_kind": "credit_committee_approval", "reference": "c/1", "sha256": "a" * 64}
    # (1) Invented decision_ref → refused.
    r = await client.post("/v1/evidence", json={**body, "decision_ref": "does-not-exist"},
                          headers=ADMIN)
    assert r.status_code == 422 and "does not resolve" in r.text.lower()
    # (2) A REJECTED committee decision cannot back an APPROVAL record.
    wf_rej = await _seed_committee_decision("Lending", did, outcome="Rejected")
    r = await client.post("/v1/evidence", json={**body, "decision_ref": wf_rej}, headers=ADMIN)
    assert r.status_code == 422 and "not 'approved'" in r.text.lower()
    # (3) A decision for a DIFFERENT subject cannot be reused here.
    other = await _line_at_note_circulated(client)
    wf_other = await _seed_committee_decision("Lending", other)
    r = await client.post("/v1/evidence", json={**body, "decision_ref": wf_other}, headers=ADMIN)
    assert r.status_code == 422 and "different subject" in r.text.lower()
    # (4) A decision NOT recorded by committee authority is refused.
    wf_bd = await _seed_committee_decision("Lending", did, roles=("BD Head",))
    r = await client.post("/v1/evidence", json={**body, "decision_ref": wf_bd}, headers=ADMIN)
    assert r.status_code == 422 and "committee authority" in r.text.lower()
    # (5) A genuine Approved committee decision for this subject → accepted; and it is one-to-one:
    #     the same decision cannot back a second approval record (409).
    wf_ok = await _seed_committee_decision("Lending", did)
    ok = await client.post("/v1/evidence", json={**body, "decision_ref": wf_ok}, headers=ADMIN)
    assert ok.status_code == 201, ok.text
    # The evidence's provenance is COPIED from the decision, not the caller.
    assert ok.json()["workflow_id"] == wf_ok and ok.json()["run_id"] == "run-1"
    dup = await client.post("/v1/evidence",
                            json={**body, "reference": "c/2", "decision_ref": wf_ok}, headers=ADMIN)
    assert dup.status_code == 409, dup.text


async def test_evidence_requires_an_existing_subject(client):
    """No evidence for a phantom record — the subject must actually exist."""
    r = await client.post("/v1/evidence",
                          json={"subject_type": "Lending", "subject_id": str(uuid.uuid4()),
                                "evidence_kind": "document:kyc", "reference": "d/1"},
                          headers=ADMIN)
    assert r.status_code == 404, r.text


async def test_scoped_authority_cannot_attach_out_of_scope(client):
    """A SCOPED authority (a BDRM filing document evidence) may only attach to a subject in their
    scope — an unrelated lending line is refused."""
    did = await _line_at_note_circulated(client)          # created by the default (service) client
    r = await _attach(client, "Lending", did, "document:kyc", headers=RM)
    assert r.status_code == 403 and "scope" in r.text.lower()


async def test_revoked_evidence_no_longer_satisfies_the_gate(client):
    """Immutable but not immutably-TRUSTED: a mistaken committee approval can be REVOKED (an
    append-only status), after which the sanction gate stops accepting it — and re-supplying valid
    evidence restores it. History is preserved throughout."""
    did = await _line_at_note_circulated(client)
    ca = await _attach(client, "Lending", did, "credit_committee_approval")
    await _attach(client, "Lending", did, "sanction_letter")
    ca_id = ca.json()["id"]
    # Revoke the committee approval → the gate should now refuse the sanction again.
    rev = await client.post(f"/v1/evidence/{ca_id}/revoke",
                            json={"status": "Invalidated", "reason": "attached in error"},
                            headers=ADMIN)
    assert rev.status_code == 200, rev.text
    blocked = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert blocked.status_code == 422 and "credit_committee_approval" in blocked.text
    # The revoked row is still on file (history preserved) but marked invalid.
    listed = (await client.get("/v1/evidence",
                               params={"subject_type": "Lending", "subject_id": did},
                               headers=ADMIN)).json()["items"]
    ca_row = next(e for e in listed if e["id"] == ca_id)
    assert ca_row["valid"] is False
    # Re-attach a fresh, valid committee approval → the gate is satisfied again.
    await _attach(client, "Lending", did, "credit_committee_approval", reference="committee/RE-1")
    ok = await client.patch(f"/v1/lending/{did}", json=_SANCTION_BODY)
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------------------------- #
# Revocation / supersession integrity (P1-C)
# --------------------------------------------------------------------------- #
async def test_supersession_must_match_subject_and_kind(client):
    """A superseding row must have the SAME subject and evidence_kind as the row it replaces — so a
    scoped document authority cannot 'supersede' (and thereby invalidate) committee evidence it has
    no authority over."""
    did = await _line_at_note_circulated(client)
    ca = await _attach(client, "Lending", did, "credit_committee_approval")
    ca_id = ca.json()["id"]
    # A document-evidence attach that names the committee approval as supersedes_id → refused.
    bad = await _attach(client, "Lending", did, "document:kyc", supersedes_id=ca_id)
    assert bad.status_code == 422 and "same subject and evidence_kind" in bad.text.lower()
    # The committee evidence is untouched (still valid).
    listed = (await client.get("/v1/evidence",
                               params={"subject_type": "Lending", "subject_id": did},
                               headers=ADMIN)).json()["items"]
    assert next(e for e in listed if e["id"] == ca_id)["valid"] is True


async def test_revocation_enforces_subject_scope(client):
    """Revocation repeats the subject-scope check — a scoped document authority cannot revoke
    evidence on a lending line outside their scope just by knowing its id."""
    did = await _line_at_note_circulated(client)
    ev = await _attach(client, "Lending", did, "document:kyc")     # attached by Admin (in scope)
    ev_id = ev.json()["id"]
    denied = await client.post(f"/v1/evidence/{ev_id}/revoke",
                               json={"status": "Revoked", "reason": "x"}, headers=RM)
    assert denied.status_code == 403 and "scope" in denied.text.lower()


async def test_listing_enforces_subject_scope(client):
    """Listing a subject's evidence requires subject-level read authority — an out-of-scope scoped
    principal is refused (not merely any identified same-tenant caller)."""
    did = await _line_at_note_circulated(client)
    await _attach(client, "Lending", did, "document:kyc")
    denied = await client.get("/v1/evidence",
                              params={"subject_type": "Lending", "subject_id": did}, headers=RM)
    assert denied.status_code == 403 and "scope" in denied.text.lower()


async def _lending(client, stage="Diligence"):  # noqa: ANN001
    eid = await _entity(client)
    return (await client.post("/v1/lending",
                              json={"entity_id": eid, "stage": stage})).json()["id"]


async def _seed_advaya(lending_id, status="Accepted", digest="a" * 64):  # noqa: ANN001
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text(
            "INSERT INTO advaya_handoffs (handoff_key, lending_id, payload_sha256, status, "
            "workflow_id, run_id, tenant_id) "
            "SELECT :hk, CAST(:sid AS varchar), :dig, :st, 'wf', 'run', tenant_id "  # noqa: S608
            "FROM lending_tracker WHERE id = CAST(:sid AS uuid)"),
            {"hk": f"advaya-handoff:{lending_id}", "sid": str(lending_id), "dig": digest,
             "st": status})
        await s.commit()


@pytest.fixture
def _advaya_on():
    """Enable the (default-off) Advaya integration so the dormant acknowledgement VERIFY path can be
    exercised. The disabled-by-default behaviour is covered in test_handover.py."""
    from app.core.config import get_settings
    s = get_settings()
    prev = s.advaya_integration_enabled
    object.__setattr__(s, "advaya_integration_enabled", True)
    try:
        yield
    finally:
        object.__setattr__(s, "advaya_integration_enabled", prev)


async def test_advaya_acknowledgement_is_verified_against_an_accepted_handoff(client, _advaya_on):
    """When an Advaya integration IS enabled, the acknowledgement cannot be manually manufactured: it
    is VERIFIED against an Accepted Advaya-handoff record (matching payload digest) — invented,
    rejected, cross-subject or wrong-digest handoffs are all refused; only a genuine one works."""
    lid = await _lending(client)
    body = {"subject_type": "Lending", "subject_id": lid,
            "evidence_kind": "advaya_acknowledgement", "reference": "advaya/1", "sha256": "a" * 64}
    # (1) No handoff → refused.
    r = await client.post("/v1/evidence",
                          json={**body, "decision_ref": f"advaya-handoff:{lid}"}, headers=ADMIN)
    assert r.status_code == 422 and "does not resolve" in r.text.lower()
    # (2) A REJECTED handoff cannot back an acknowledgement.
    await _seed_advaya(lid, status="Rejected")
    r = await client.post("/v1/evidence",
                          json={**body, "decision_ref": f"advaya-handoff:{lid}"}, headers=ADMIN)
    assert r.status_code == 422 and "not 'accepted'" in r.text.lower()


async def test_advaya_acknowledgement_digest_must_match_the_handoff(client, _advaya_on):
    lid = await _lending(client)
    await _seed_advaya(lid, status="Accepted", digest="b" * 64)
    # A different digest than the accepted handoff's payload hash → refused.
    r = await client.post("/v1/evidence",
                          json={"subject_type": "Lending", "subject_id": lid,
                                "evidence_kind": "advaya_acknowledgement", "reference": "advaya/1",
                                "sha256": "a" * 64, "decision_ref": f"advaya-handoff:{lid}"},
                          headers=ADMIN)
    assert r.status_code == 422 and "does not match" in r.text.lower()
    # The matching digest is accepted, and provenance is copied from the handoff.
    ok = await client.post("/v1/evidence",
                           json={"subject_type": "Lending", "subject_id": lid,
                                 "evidence_kind": "advaya_acknowledgement", "reference": "advaya/1",
                                 "sha256": "b" * 64, "decision_ref": f"advaya-handoff:{lid}"},
                           headers=ADMIN)
    assert ok.status_code == 201, ok.text
    assert ok.json()["workflow_id"] == "wf"


async def test_evidence_cannot_be_revoked_twice(client):
    """The status ledger rejects a repeated/contradictory terminal status — one terminal status per
    evidence row."""
    did = await _line_at_note_circulated(client)
    ev = await _attach(client, "Lending", did, "document:kyc")
    ev_id = ev.json()["id"]
    first = await client.post(f"/v1/evidence/{ev_id}/revoke",
                              json={"status": "Revoked", "reason": "one"}, headers=ADMIN)
    assert first.status_code == 200, first.text
    second = await client.post(f"/v1/evidence/{ev_id}/revoke",
                               json={"status": "Invalidated", "reason": "two"}, headers=ADMIN)
    assert second.status_code == 409, second.text
