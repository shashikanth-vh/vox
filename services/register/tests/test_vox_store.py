"""The VOX conversation store — statuses hold their line, edits are atomic and
audited, consent is immutable in the database itself, and erasure is the one
sanctioned exception (Build Specification, Sections 12 and 16)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


def _as(email: str, roles: str) -> dict:
    return {"X-User-Email": email, "X-User-Roles": roles}


RECORDER = _as("ananda@evamfinance.com", "BDRM")
OTHER_RM = _as("chetan@evamfinance.com", "BDRM")
MGMT = _as("kannan@evamfinance.com", "Management")
ADMIN = _as("admin@evamfinance.com", "Admin")


def _report(quantum=25):
    cell = lambda v, c="high", **kw: {"value": v, "confidence": c, **kw}  # noqa: E731
    return {
        "detected_use_cases": ["lending"],
        "common": {
            "meeting_type": cell("in_person"),
            "meeting_date": cell("2026-08-20"),
            "location": cell("Whitefield", "medium"),
            "sector": cell("Renewables"),
            "subsector": cell("Solar-Developer", "medium"),
            "attendees_counterparty": cell(["R. Sharma"], "medium"),
            "key_discussion_points": cell(["40 MW under construction"]),
            "action_items": cell([], "n/a"),
            "next_steps": cell("Review DPR"),
            "follow_up_date": cell(None, "n/a"),
            "opportunity_assessment": cell("Strong sponsor.", "n/a"),
            "opportunity_score": cell(4, "medium", user_override=False),
            "opportunity_score_override_reason": cell(None, "n/a"),
            "competitive_intelligence": cell("", "n/a"),
            "data_quality_flags": cell([], "n/a"),
        },
        "lending": {
            "requirement_nature": cell("project_finance"),
            "requirement_quantum_cr": cell(quantum, "low"),
            "company_turnover_cr": cell(None, "n/a"),
            "existing_bankers": cell("SBI", "medium"),
            "project_location": cell("Karnataka", "medium"),
            "present_requirement": cell("~25 Cr project finance"),
            "remarks": cell(None, "n/a"),
        },
        "entity_candidates": ["Suryodaya EPC", "SBI"],
    }


async def _make(client: AsyncClient, **kw) -> dict:
    body = {"recording_mode": "post_meeting", **kw}
    r = await client.post("/v1/vox/conversations", json=body, headers=RECORDER)
    assert r.status_code == 201, r.text
    return r.json()


async def _to_ready(client: AsyncClient, cid: str, report=None) -> dict:
    for status in ("processing", "ready"):
        payload = {"status": status}
        if status == "ready":
            payload["structured_report"] = report or _report()
            payload["raw_transcript"] = "met suryodaya at whitefield, forty megawatt"
            payload["prompt_version"] = "v1"
            payload["registry_version"] = "v1"
        r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json=payload)
        assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ create / replay

async def test_a_note_is_created_queued_and_a_retry_replays(client: AsyncClient):
    cap = f"cap-{uuid.uuid4()}"
    first = await _make(client, capture_id=cap)
    assert first["status"] == "queued"
    again = await client.post("/v1/vox/conversations",
                              json={"recording_mode": "post_meeting", "capture_id": cap},
                              headers=RECORDER)
    assert again.status_code == 201
    assert again.json()["id"] == first["id"] and again.json()["replayed"] is True


async def test_live_mode_cannot_exist_without_consent(client: AsyncClient):
    r = await client.post("/v1/vox/conversations", json={"recording_mode": "live"},
                          headers=RECORDER)
    assert r.status_code in (400, 422), r.text
    consent = await client.post("/v1/vox/consents", json={
        "certification_text": "I certify that everyone in this meeting has been told "
                              "it is being recorded, and consented."}, headers=RECORDER)
    assert consent.status_code == 201, consent.text
    r = await client.post("/v1/vox/conversations",
                          json={"recording_mode": "live", "consent_id": consent.json()["id"]},
                          headers=RECORDER)
    assert r.status_code == 201, r.text


# ----------------------------------------------------------------- status machine

async def test_the_status_machine_refuses_illegal_moves(client: AsyncClient):
    row = await _make(client)
    cid = row["id"]
    # queued -> ready skips processing: refused
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={"status": "ready"})
    assert r.status_code == 409, r.text
    await _to_ready(client, cid)
    # ready -> queued goes backwards: refused
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={"status": "queued"})
    assert r.status_code == 409, r.text


async def test_failure_retry_and_permanent_failure_paths(client: AsyncClient):
    row = await _make(client)
    cid = row["id"]
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline",
                           json={"status": "processing"})
    assert r.status_code == 200
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline",
                           json={"status": "processing_failed",
                                 "processing_error": "whisper timeout after 120s",
                                 "retry_increment": True})
    assert r.status_code == 200 and r.json()["retry_count"] == 1
    # retry goes back to processing, then a clean ready clears the error
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={"status": "processing"})
    assert r.status_code == 200
    out = await _to_ready(client, cid)
    assert out["processing_error"] is None
    # the terminal state exists and is reachable only from a failure
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline",
                           json={"status": "failed_permanently"})
    assert r.status_code == 409


async def test_ready_denormalises_and_tags_use_cases(client: AsyncClient):
    row = await _make(client)
    out = await _to_ready(client, row["id"])
    assert out["sector"] == "Renewables" and out["subsector"] == "Solar-Developer"
    assert out["meeting_date"] == "2026-08-20"
    got = (await client.get(f"/v1/vox/conversations/{row['id']}", headers=OTHER_RM)).json()
    assert got["use_cases"] == ["lending"]


# ------------------------------------------------------------------- the edit path

async def test_the_recorder_edits_and_the_audit_remembers(client: AsyncClient):
    row = await _make(client)
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 30, "confidence": "high"}}],
    }, headers=RECORDER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 1
    assert body["structured_report"]["lending"]["requirement_quantum_cr"]["value"] == 30


async def test_an_unrelated_rm_cannot_edit_but_management_can(client: AsyncClient):
    row = await _make(client)
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 99, "confidence": "high"}}]}, headers=OTHER_RM)
    assert r.status_code == 403, r.text
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 40, "confidence": "high"}}]}, headers=MGMT)
    assert r.status_code == 200, r.text


async def test_score_override_and_use_case_retag_flow_through_the_same_path(client: AsyncClient):
    row = await _make(client)
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "common.opportunity_score",
                   "new_value": {"value": 5, "confidence": "n/a", "user_override": True}}],
        "use_cases": ["lending", "asset_monetisation"],
    }, headers=RECORDER)
    assert r.status_code == 200, r.text
    got = (await client.get(f"/v1/vox/conversations/{row['id']}")).json()
    assert sorted(got["use_cases"]) == ["asset_monetisation", "lending"]
    assert got["structured_report"]["common"]["opportunity_score"]["user_override"] is True


async def test_edits_keep_the_denormalised_columns_honest(client: AsyncClient):
    row = await _make(client)
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "common.subsector",
                   "new_value": {"value": "Wind", "confidence": "high"}}]}, headers=RECORDER)
    assert r.status_code == 200
    assert r.json()["subsector"] == "Wind"


# ------------------------------------------------------------------ approve / list

async def test_approve_needs_ready_and_is_idempotent(client: AsyncClient):
    row = await _make(client)
    r = await client.post(f"/v1/vox/conversations/{row['id']}/approve", headers=RECORDER)
    assert r.status_code == 409  # still queued
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/approve", headers=RECORDER)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    again = await client.post(f"/v1/vox/conversations/{row['id']}/approve", headers=RECORDER)
    assert again.status_code == 200 and again.json()["replayed"] is True


async def test_everyone_reads_everything_and_mine_narrows(client: AsyncClient):
    mine = await _make(client, capture_id=f"mine-{uuid.uuid4()}")
    other = await client.post("/v1/vox/conversations",
                              json={"recording_mode": "post_meeting"}, headers=OTHER_RM)
    assert other.status_code == 201
    everyone = (await client.get("/v1/vox/conversations", headers=OTHER_RM)).json()
    ids = {i["id"] for i in everyone["items"]}
    assert mine["id"] in ids and other.json()["id"] in ids  # no privacy tier (D2)
    only_mine = (await client.get("/v1/vox/conversations", params={"mine": "true"},
                                  headers=RECORDER)).json()
    assert all(i["recorder_email"] == "ananda@evamfinance.com" for i in only_mine["items"])
    assert mine["id"] in {i["id"] for i in only_mine["items"]}


async def test_search_and_filters_find_the_conversation(client: AsyncClient):
    row = await _make(client)
    await _to_ready(client, row["id"])
    by_q = (await client.get("/v1/vox/conversations",
                             params={"q": "suryodaya"})).json()
    assert row["id"] in {i["id"] for i in by_q["items"]}
    by_uc = (await client.get("/v1/vox/conversations",
                              params={"use_case": "lending", "status": "ready"})).json()
    assert row["id"] in {i["id"] for i in by_uc["items"]}
    none = (await client.get("/v1/vox/conversations",
                             params={"q": "zebra-quantum-nonsense"})).json()
    assert row["id"] not in {i["id"] for i in none["items"]}


# ---------------------------------------------------- immutability in the database

async def test_consent_records_refuse_update_and_delete(client: AsyncClient, db_session):
    r = await client.post("/v1/vox/consents", json={
        "certification_text": "I certify the attendees were told and consented."},
        headers=RECORDER)
    cid = r.json()["id"]
    for stmt in (f"UPDATE vox_consent_records SET certification_text='edited' WHERE id='{cid}'",
                 f"DELETE FROM vox_consent_records WHERE id='{cid}'"):
        with pytest.raises(Exception) as exc:
            await db_session.execute(text(stmt))
        assert "immutable" in str(exc.value) or "never changed" in str(exc.value)
        await db_session.rollback()


async def test_the_edit_trail_is_append_only(client: AsyncClient, db_session):
    row = await _make(client)
    await _to_ready(client, row["id"])
    await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 31, "confidence": "high"}}]}, headers=RECORDER)
    with pytest.raises(Exception):
        await db_session.execute(text(
            f"DELETE FROM vox_conversation_edits WHERE conversation_id='{row['id']}'"))
    await db_session.rollback()


async def test_erasure_removes_content_but_consent_and_log_survive(client: AsyncClient):
    consent = await client.post("/v1/vox/consents", json={
        "certification_text": "I certify the attendees were told and consented."},
        headers=RECORDER)
    consent_id = consent.json()["id"]
    row = await _make(client, capture_id=f"erase-{uuid.uuid4()}", consent_id=consent_id)
    await _to_ready(client, row["id"])
    await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 30, "confidence": "high"}}]}, headers=RECORDER)

    approved = await client.post(f"/v1/vox/conversations/{row['id']}/approve", headers=RECORDER)
    assert approved.status_code == 200

    denied = await client.post(f"/v1/vox/conversations/{row['id']}/erase", headers=RECORDER)
    assert denied.status_code == 403  # an approved record is the firm's, not the RM's

    erased = await client.post(f"/v1/vox/conversations/{row['id']}/erase", headers=ADMIN)
    assert erased.status_code == 200, erased.text
    got = (await client.get(f"/v1/vox/conversations/{row['id']}")).json()
    assert got["erased_at"] is not None
    assert got["raw_transcript"] is None and got["structured_report"] is None
    assert got["consent_id"] == consent_id  # the certification outlives the content
    # further edits and pipeline writes are refused
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "lending.remarks",
                   "new_value": {"value": "x", "confidence": "high"}}]}, headers=ADMIN)
    assert r.status_code == 409
    again = await client.post(f"/v1/vox/conversations/{row['id']}/erase", headers=ADMIN)
    assert again.status_code == 200 and again.json()["replayed"] is True


# --------------------------------------------------- proposed lead / draft delete

async def test_a_proposed_lead_materialises_only_on_approve(client: AsyncClient):
    """Field feedback: 'create new lead' used to write the Lead the moment the
    button was tapped. Now the intent rides on the conversation and the register
    gains the lead exactly at approval."""
    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    r = await client.post(f"/v1/vox/conversations/{cid}/edits", json={
        "proposed_lead_company": "Adani Power", "proposed_lead_rm": "Chetan Malik",
    }, headers=RECORDER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposed_lead_company"] == "Adani Power"
    assert body["lead_id"] is None
    # no lead exists yet
    leads = (await client.get("/v1/leads", params={"company": "Adani Power"},
                              headers=RECORDER)).json()
    assert not leads["items"]
    # approve births it, numbered, RM'd, sourced
    r = await client.post(f"/v1/vox/conversations/{cid}/approve", headers=RECORDER)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["lead_id"] is not None
    assert approved["proposed_lead_company"] is None
    lead = (await client.get(f"/v1/leads/{approved['lead_id']}", headers=RECORDER)).json()
    assert lead["company"] == "Adani Power"
    assert lead["rm"] == "Chetan Malik"
    assert lead["source_name"] == "VOX conversation"
    assert lead["lead_no"]  # auto-numbered like any lead


async def test_a_pinned_line_wins_over_a_stale_proposal(client: AsyncClient):
    """If the user proposed a lead but then pinned an existing line, approve must
    not create anything."""
    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    lead = await client.post("/v1/leads", json={"company": f"Existing {uuid.uuid4()}"},
                             headers=MGMT)
    assert lead.status_code == 201
    r = await client.post(f"/v1/vox/conversations/{cid}/edits", json={
        "proposed_lead_company": "Ghost Co", "lead_id": lead.json()["id"],
    }, headers=RECORDER)
    assert r.status_code == 200
    approved = (await client.post(f"/v1/vox/conversations/{cid}/approve",
                                  headers=RECORDER)).json()
    assert approved["lead_id"] == lead.json()["id"]
    ghosts = (await client.get("/v1/leads", params={"company": "Ghost Co"},
                               headers=RECORDER)).json()
    assert not ghosts["items"]


async def test_recorder_deletes_their_draft_but_not_an_approved_record(client: AsyncClient):
    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    # another RM cannot delete someone else's draft
    r = await client.post(f"/v1/vox/conversations/{cid}/erase", headers=OTHER_RM)
    assert r.status_code == 403
    # the recorder can — and it leaves the feeds
    r = await client.post(f"/v1/vox/conversations/{cid}/erase", headers=RECORDER)
    assert r.status_code == 200 and r.json()["erased_at"]
    listed = (await client.get("/v1/vox/conversations", headers=RECORDER)).json()
    assert cid not in {i["id"] for i in listed["items"]}
    listed_all = (await client.get("/v1/vox/conversations",
                                   params={"include_erased": "true"},
                                   headers=RECORDER)).json()
    assert cid in {i["id"] for i in listed_all["items"]}
    # an approved record refuses everyone but Admin
    row2 = await _make(client)
    await _to_ready(client, row2["id"])
    await client.post(f"/v1/vox/conversations/{row2['id']}/approve", headers=RECORDER)
    r = await client.post(f"/v1/vox/conversations/{row2['id']}/erase", headers=RECORDER)
    assert r.status_code == 403
    r = await client.post(f"/v1/vox/conversations/{row2['id']}/erase", headers=MGMT)
    assert r.status_code == 403
    r = await client.post(f"/v1/vox/conversations/{row2['id']}/erase", headers=ADMIN)
    assert r.status_code == 200 and r.json()["erased_at"]


# ------------------------------------------------ corrected transcript / regenerate

async def test_correct_transcript_and_regenerate_preserves_overrides(client: AsyncClient):
    """The Sarvodaya problem: one mis-heard name propagates everywhere. The fix —
    correct the transcript (original preserved, audited), regenerate the report,
    and the reviewer's own confirmed cells survive the rebuild."""
    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    # the reviewer overrides a cell, then corrects the transcript
    r = await client.post(f"/v1/vox/conversations/{cid}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 30, "confidence": "high", "user_override": True}}],
        "corrected_transcript": "met SUYODAYA epc in whitefield, forty megawatt",
    }, headers=RECORDER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["corrected_transcript"].startswith("met SUYODAYA")
    assert body["raw_transcript"] == "met suryodaya at whitefield, forty megawatt"  # evidence untouched

    # regenerate: report cleared, status back to processing, stage structuring
    r = await client.post(f"/v1/vox/conversations/{cid}/regenerate", headers=RECORDER)
    assert r.status_code == 200, r.text
    regen = r.json()
    assert regen["status"] == "processing"
    assert regen["structured_report"] is None
    assert regen["corrected_transcript"]          # the worker will structure THIS

    # the worker lands a fresh report — the override must ride back on top
    fresh = _report(quantum=25)                   # AI re-extracted 25 again
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={
        "status": "ready", "structured_report": fresh})
    assert r.status_code == 200, r.text
    after = r.json()
    cell = after["structured_report"]["lending"]["requirement_quantum_cr"]
    assert cell["value"] == 30 and cell["user_override"] is True

    # a second plain pipeline write must NOT re-apply anything (stash consumed)
    r = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={
        "structured_report": _report(quantum=40)})
    assert r.json()["structured_report"]["lending"]["requirement_quantum_cr"]["value"] == 40


async def test_reanalysis_of_an_approved_record_returns_it_approved(client: AsyncClient):
    """An approved record whose transcript the desk corrects rebuilds its report
    and COMES BACK approved — with the filed timeline entry re-synced and the
    reviewer's overridden cells surviving the rebuild."""
    ent = await client.post("/v1/entities", json={
        "code": "VOXRSM1", "legal_name": "Resume Co", "entity_type": "Company"},
        headers=MGMT)
    eid = ent.json()["id"]
    itx = await client.post("/v1/interactions", json={
        "subject_type": "Entity", "subject_id": eid,
        "interaction_type": "VOX conversation", "summary": "old summary",
        "transcript": "old transcript", "performed_by": "Ananda H"}, headers=MGMT)
    iid = itx.json()["id"]

    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    await client.post(f"/v1/vox/conversations/{cid}/edits",
                      json={"entity_id": eid, "interaction_id": iid}, headers=RECORDER)
    await client.post(f"/v1/vox/conversations/{cid}/approve", headers=RECORDER)

    # the reviewer confirms a cell post-approval — it must survive the rebuild
    r = await client.post(f"/v1/vox/conversations/{cid}/edits", json={
        "edits": [{"field_path": "lending.requirement_quantum_cr",
                   "new_value": {"value": 50, "confidence": "high", "user_override": True}}],
        "corrected_transcript": "met SURYODAYA EPC: 50 crore, 90 megawatt"},
        headers=RECORDER)
    assert r.status_code == 200, r.text

    r = await client.post(f"/v1/vox/conversations/{cid}/regenerate", headers=RECORDER)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "processing"

    # the pipeline lands the fresh report — the row RETURNS approved
    fresh = _report(quantum=99)
    fresh["common"]["meeting_summary"] = {
        "value": "REBUILT: Suryodaya 50 Cr / 90 MW", "confidence": "n/a"}
    done = await client.patch(f"/v1/vox/conversations/{cid}/pipeline", json={
        "status": "ready", "structured_report": fresh})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "submitted"
    # the override outranked the re-extraction
    assert body["structured_report"]["lending"]["requirement_quantum_cr"]["value"] == 50
    # and the timeline entry tells the new story
    synced = (await client.get(f"/v1/interactions/{iid}", headers=MGMT)).json()
    assert synced["summary"] == "REBUILT: Suryodaya 50 Cr / 90 MW"
    assert synced["transcript"] == "met SURYODAYA EPC: 50 crore, 90 megawatt"


async def test_list_filters_by_lead_for_the_lead_only_dossier(client: AsyncClient):
    """A company that is still lead-only has no entity — its dossier is keyed by
    the lead, so the list must filter by lead_id."""
    lead = await client.post("/v1/leads", json={"company": f"LeadOnly {uuid.uuid4()}"},
                             headers=MGMT)
    assert lead.status_code == 201
    lid = lead.json()["id"]
    row = await _make(client)
    await _to_ready(client, row["id"])
    await client.post(f"/v1/vox/conversations/{row['id']}/edits",
                      json={"lead_id": lid}, headers=RECORDER)
    await client.post(f"/v1/vox/conversations/{row['id']}/approve", headers=RECORDER)
    got = (await client.get("/v1/vox/conversations",
                            params={"lead_id": lid, "status": "submitted"},
                            headers=RECORDER)).json()
    assert [i["id"] for i in got["items"]] == [row["id"]]


# ------------------------------------------------------------- concurrency races

async def test_parallel_approves_materialise_exactly_one_lead(client: AsyncClient):
    """The 150-user finding: two simultaneous Approve taps both passed the
    is-it-ready check and both created the proposed lead. The row lock makes
    the second serialize behind the first and replay idempotently."""
    import asyncio
    row = await _make(client)
    cid = row["id"]
    await _to_ready(client, cid)
    company = f"Race Co {uuid.uuid4()}"
    r = await client.post(f"/v1/vox/conversations/{cid}/edits", json={
        "proposed_lead_company": company, "proposed_lead_rm": "Chetan Malik",
    }, headers=RECORDER)
    assert r.status_code == 200

    results = await asyncio.gather(
        client.post(f"/v1/vox/conversations/{cid}/approve", headers=RECORDER),
        client.post(f"/v1/vox/conversations/{cid}/approve", headers=MGMT),
    )
    assert {r.status_code for r in results} == {200}
    assert sum(1 for r in results if r.json().get("replayed")) == 1
    leads = (await client.get("/v1/leads", params={"company": company},
                              headers=RECORDER)).json()
    assert len(leads["items"]) == 1        # one lead, not two


async def test_parallel_duplicate_captures_replay_not_500(client: AsyncClient):
    """Two simultaneous retries of the same upload race the existence check;
    the unique constraint keeps the data single and the loser replays."""
    import asyncio
    cap = f"race-{uuid.uuid4()}"
    body = {"recording_mode": "post_meeting", "capture_id": cap}
    results = await asyncio.gather(
        client.post("/v1/vox/conversations", json=body, headers=RECORDER),
        client.post("/v1/vox/conversations", json=body, headers=RECORDER),
    )
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1                   # same conversation for both


async def test_post_approve_edits_resync_the_filed_interaction(client: AsyncClient):
    """The timeline is append-only at its public door, but a post-approval edit of an
    approved conversation must not leave the FILED interaction telling yesterday's
    story — the register owns both tables and re-derives summary/key_intel in the
    same transaction as the audited edit."""
    ent = await client.post("/v1/entities", json={
        "code": "VOXSYNC1", "legal_name": "Sync Co", "entity_type": "Company"},
        headers=MGMT)
    assert ent.status_code == 201, ent.text
    eid = ent.json()["id"]
    itx = await client.post("/v1/interactions", json={
        "subject_type": "Entity", "subject_id": eid,
        "interaction_type": "VOX conversation",
        "summary": "old summary", "key_intel": {"points": ["old point"]},
        "performed_by": "Ananda H"}, headers=MGMT)
    assert itx.status_code == 201, itx.text
    iid = itx.json()["id"]

    row = await _make(client, capture_id=f"cap-{uuid.uuid4()}")
    await _to_ready(client, row["id"])
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits",
                          json={"entity_id": eid, "interaction_id": iid}, headers=RECORDER)
    assert r.status_code == 200, r.text
    assert (await client.post(f"/v1/vox/conversations/{row['id']}/approve",
                              headers=RECORDER)).status_code == 200

    # a content edit AFTER approval — the summary cell changes
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits", json={
        "edits": [{"field_path": "common.key_discussion_points",
                   "new_value": {"value": ["REVISED: 45 MW, quotes compared"],
                                 "confidence": "high", "user_override": True}}]},
        headers=MGMT)
    assert r.status_code == 200, r.text

    got = await client.get(f"/v1/interactions/{iid}", headers=MGMT)
    assert got.status_code == 200, got.text
    synced = got.json()
    assert synced["summary"] == "REVISED: 45 MW, quotes compared"
    assert synced["key_intel"]["points"] == ["REVISED: 45 MW, quotes compared"]
    assert synced["key_intel"]["use_cases"] == ["lending"]
    # the per-lane geography map rides along, and the location column fills
    # from the meeting venue (falling back to the lane locations)
    assert synced["key_intel"]["locations"] == {"lending": "Karnataka"}
    assert synced["location"] == "Whitefield"

    # a LINK-only edit leaves the interaction content untouched
    lead = await client.post("/v1/leads", json={"entity_id": eid, "company": "Sync Co", "rm": "Ananda H"},
                             headers=MGMT)
    assert lead.status_code == 201, lead.text
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits",
                          json={"lead_id": lead.json()["id"]}, headers=MGMT)
    assert r.status_code == 200, r.text
    unchanged = (await client.get(f"/v1/interactions/{iid}", headers=MGMT)).json()
    assert unchanged["summary"] == "REVISED: 45 MW, quotes compared"


async def test_malformed_ids_are_the_callers_error_not_a_500(client: AsyncClient):
    """Found by the 90-minute live E2E: a non-UUID consent_id from a corrupted
    manifest crashed conversation creation with an unhandled 500."""
    r = await client.post("/v1/vox/conversations", json={
        "recording_mode": "live", "consent_id": "not-a-uuid"}, headers=RECORDER)
    assert r.status_code == 422, r.text
    row = await _make(client)
    r = await client.post(f"/v1/vox/conversations/{row['id']}/edits",
                          json={"lead_id": "garbage-id"}, headers=RECORDER)
    assert r.status_code == 422, r.text


async def test_maker_never_checks_their_own_stage_request(client: AsyncClient):
    """Maker-checker on the request flow: the requester — approver role or not —
    is refused on their own request; a different authority decides it."""
    ent = await client.post("/v1/entities", json={
        "code": "MKRCHK1", "legal_name": "Maker Checker Co", "entity_type": "Company"},
        headers=MGMT)
    assert ent.status_code == 201, ent.text
    lend = await client.post("/v1/lending", json={
        "entity_id": ent.json()["id"], "stage": "Diligence"}, headers=MGMT)
    assert lend.status_code == 201, lend.text
    # Management MAY raise a request (their choice to route through a second pair
    # of eyes even though they hold direct edit rights)
    req = await client.post("/v1/requests", json={
        "subject_type": "Lending", "subject_id": lend.json()["id"],
        "field": "stage", "to_value": "Note Circulated"}, headers=MGMT)
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    # ...but the SAME person never decides their own request
    r = await client.post(f"/v1/requests/{rid}/approve", json={}, headers=MGMT)
    assert r.status_code == 403, r.text
    assert "different approver" in r.text
    # a DIFFERENT authority approves it fine
    r = await client.post(f"/v1/requests/{rid}/approve", json={},
                          headers=_as("admin@evamfinance.com", "Admin"))
    assert r.status_code == 200, r.text
