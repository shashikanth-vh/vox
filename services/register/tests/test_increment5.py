"""Increment 5 (Register side) — syndication governance.

The syndication decision kind is subject-bound and reserved to syndication authority; the
syndication_sanction evidence is VERIFIED against it; and a mandate cannot reach
'Sanctioned' until that evidence is on file (the same gate shape as the lending sanction)."""

from __future__ import annotations

import uuid

import pytest

from tests.test_decisions import _ctx, wf_client  # noqa: F401 - fixture import

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
SYN_HEAD = {"X-User-Email": "sh@evamfinance.com", "X-User-Roles": "Syn Head"}


async def _mandate(client) -> str:  # noqa: ANN001
    code = "SYN" + uuid.uuid4().hex[:6].upper()
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": "Syn Co",
                                   "entity_type": "Company"})).json()["id"]
    r = await client.post("/v1/syndication",
                          json={"entity_id": eid, "status": "IM in Prep",
                                "amount_cr": 100})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _walk(client, sid, *statuses):  # noqa: ANN001
    for st in statuses:
        r = await client.patch(f"/v1/syndication/{sid}", json={"status": st})
        assert r.status_code == 200, f"{st}: {r.text}"


async def test_syndication_decision_kind_is_authority_and_subject_bound(wf_client):  # noqa: F811
    sid = uuid.uuid4().hex
    wf = f"synd-{sid}"
    # No subject binding → refused.
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "syndication"},
        headers={"X-Internal-Context": _ctx(roles=("Syn Head",))})
    assert r.status_code == 422, r.text
    # Non-syndication authority → refused.
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "syndication",
              "subject_type": "Syndication", "subject_id": sid},
        headers={"X-Internal-Context": _ctx(roles=("BD Head",))})
    assert r.status_code == 403, r.text
    # Syn Head records it — durable, subject-bound, no conversion-delivery row.
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "syndication",
              "subject_type": "Syndication", "subject_id": sid,
              "committee_reference": "syn-sanction/SL-9"},
        headers={"X-Internal-Context": _ctx(roles=("Syn Head",))})
    assert r.status_code == 201, r.text
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 0


async def test_sanction_gate_requires_verified_syndication_evidence(client):
    sid = await _mandate(client)
    await _walk(client, sid, "IM Circulated", "Queries Received", "IP Received")

    # No evidence on file → the gate refuses 'Sanctioned'.
    r = await client.patch(f"/v1/syndication/{sid}", json={"status": "Sanctioned"})
    assert r.status_code == 422 and "evidence" in r.text.lower()

    # The evidence itself must VERIFY against a recorded syndication decision — an
    # invented ref is refused.
    bad = await client.post("/v1/evidence", json={
        "subject_type": "Syndication", "subject_id": sid,
        "evidence_kind": "syndication_sanction", "reference": "syn/1",
        "sha256": "a" * 64, "decision_ref": "invented"}, headers=SYN_HEAD)
    assert bad.status_code == 422, bad.text

    # Record the REAL decision (Syn Head authority, bound to this mandate)…
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    wf = f"synd-{uuid.uuid4().hex[:12]}"
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text(
            "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, "
            "subject_id, run_id, decided_by, decided_by_id, roles, tenant_id) "
            "SELECT :wf, 'Approved', 'Syndication', CAST(:sid AS varchar), 'run-1', "
            "'sh@evamfinance.com', 'u-9', CAST('[\"Syn Head\"]' AS jsonb), tenant_id "  # noqa: S608
            "FROM syndication_tracker WHERE id = CAST(:sid AS uuid)"),
            {"wf": wf, "sid": sid})
        await s.commit()
    ev = await client.post("/v1/evidence", json={
        "subject_type": "Syndication", "subject_id": sid,
        "evidence_kind": "syndication_sanction", "reference": "syn/1",
        "sha256": "a" * 64, "decision_ref": wf}, headers=SYN_HEAD)
    assert ev.status_code == 201, ev.text

    # …and the mandate may now be sanctioned; the IM artefact also files cleanly.
    r = await client.patch(f"/v1/syndication/{sid}", json={"status": "Sanctioned"})
    assert r.status_code == 200, r.text
    im = await client.post("/v1/evidence", json={
        "subject_type": "Syndication", "subject_id": sid, "evidence_kind": "im_document",
        "reference": "im/IM-1", "workflow_id": wf, "run_id": "run-1"}, headers=SYN_HEAD)
    assert im.status_code == 201, im.text
