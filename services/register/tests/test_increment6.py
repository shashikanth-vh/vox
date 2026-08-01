"""Increment 6 (Register side) — asset-monetisation governance.

The asset_monetisation decision kind is subject-bound and reserved to AM authority; the
am_closure_approval evidence is VERIFIED against it; and a mandate cannot reach 'Closed'
until that evidence is on file."""

from __future__ import annotations

import uuid

import pytest

from tests.test_decisions import _ctx, wf_client  # noqa: F401 - fixture import

pytestmark = pytest.mark.asyncio

AM_HEAD = {"X-User-Email": "ah@evamfinance.com", "X-User-Roles": "AM Head"}


async def test_am_decision_kind_is_authority_and_subject_bound(wf_client):  # noqa: F811
    mid = uuid.uuid4().hex
    wf = f"amon-{mid}"
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation"},
        headers={"X-Internal-Context": _ctx(roles=("AM Head",))})
    assert r.status_code == 422, r.text                      # subject binding required
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation",
              "subject_type": "AssetMonetisation", "subject_id": mid},
        headers={"X-Internal-Context": _ctx(roles=("Syn Head",))})
    assert r.status_code == 403, r.text                      # wrong desk's authority
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": wf, "decision": "Approved", "kind": "asset_monetisation",
              "subject_type": "AssetMonetisation", "subject_id": mid,
              "committee_reference": "am-closure/SPA-1"},
        headers={"X-Internal-Context": _ctx(roles=("AM Head",))})
    assert r.status_code == 201, r.text
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 0                             # no conversion-delivery row


async def test_closure_gate_requires_verified_am_evidence(client):
    code = "AMC" + uuid.uuid4().hex[:6].upper()
    eid = (await client.post("/v1/entities",
                             json={"code": code, "legal_name": "AM Co",
                                   "entity_type": "Company"})).json()["id"]
    mid = (await client.post("/v1/asset-monetisation",
                             json={"entity_id": eid,
                                   "status": "Teaser Prepared"})).json()["id"]
    for st in ("Teaser Shared", "In Discussion", "NBO Received", "BO Received",
               "SPA / Documentation"):
        r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": st})
        assert r.status_code == 200, f"{st}: {r.text}"

    # No evidence → 'Closed' is refused; an invented decision ref is refused too.
    r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": "Closed"})
    assert r.status_code == 422 and "evidence" in r.text.lower()
    bad = await client.post("/v1/evidence", json={
        "subject_type": "AssetMonetisation", "subject_id": mid,
        "evidence_kind": "am_closure_approval", "reference": "am/1",
        "sha256": "a" * 64, "decision_ref": "invented"}, headers=AM_HEAD)
    assert bad.status_code == 422, bad.text

    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    wf = f"amon-{uuid.uuid4().hex[:12]}"
    async with get_sessionmaker()() as s:
        await s.execute(text(
            "INSERT INTO workflow_decisions (workflow_id, decision, subject_type, "
            "subject_id, run_id, decided_by, decided_by_id, roles, tenant_id) "
            "SELECT :wf, 'Approved', 'AssetMonetisation', CAST(:mid AS varchar), 'run-1', "
            "'ah@evamfinance.com', 'u-7', CAST('[\"AM Head\"]' AS jsonb), tenant_id "  # noqa: S608
            "FROM asset_monetisation WHERE id = CAST(:mid AS uuid)"),
            {"wf": wf, "mid": mid})
        await s.commit()
    ev = await client.post("/v1/evidence", json={
        "subject_type": "AssetMonetisation", "subject_id": mid,
        "evidence_kind": "am_closure_approval", "reference": "am/1",
        "sha256": "a" * 64, "decision_ref": wf}, headers=AM_HEAD)
    assert ev.status_code == 201, ev.text
    r = await client.patch(f"/v1/asset-monetisation/{mid}", json={"status": "Closed"})
    assert r.status_code == 200, r.text
    # The teaser / NDA / offer artefacts file cleanly under AM authority.
    for kind, ref in (("teaser_document", "teaser/T-1"), ("am_nda", "nda/1"),
                      ("am_offer", "offer/1")):
        r = await client.post("/v1/evidence", json={
            "subject_type": "AssetMonetisation", "subject_id": mid,
            "evidence_kind": kind, "reference": ref,
            "workflow_id": wf, "run_id": "run-1"}, headers=AM_HEAD)
        assert r.status_code == 201, f"{kind}: {r.text}"
