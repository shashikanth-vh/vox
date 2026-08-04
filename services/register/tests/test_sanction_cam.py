"""Lending increments 1–2: sanction terms seeding + the CAM report lifecycle."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

ANALYST = {"X-User-Email": "bhavana@evamfinance.com", "X-User-Roles": "Deal Analyst",
           "X-User-Id": "8c5a2c1e-0000-4000-8000-000000000001"}
HEAD = {"X-User-Email": "credithead@evamfinance.com", "X-User-Roles": "Credit Head",
        "X-User-Id": "8c5a2c1e-0000-4000-8000-000000000002"}


async def _lending(client: AsyncClient) -> tuple[str, str]:
    ent = (await client.post("/v1/entities", json={
        "code": f"SC-{uuid.uuid4().hex[:6]}", "legal_name": "Sanction Co"})).json()
    lend = (await client.post("/v1/lending", json={
        "entity_id": ent["id"], "stage": "Diligence"})).json()
    assert "id" in lend, lend
    return ent["id"], lend["id"]


async def test_sanction_terms_seed_the_checklist_and_the_covenants(client: AsyncClient):
    """One save, four registers: the terms row, the CP/CS checklist (CP and CS items
    distinguished), and covenant definitions — nothing re-typed downstream."""
    eid, lid = await _lending(client)
    r = await client.post("/v1/internal/sanction-terms", json={
        "lending_id": lid, "amount_cr": 45, "rate_kind": "Fixed", "rate_pct": 12.5,
        "tenor_months": 54, "emi_amount": 447608, "day_count": "365",
        "cp_items": [{"key": "board_resolution", "label": "Board resolution"},
                     {"key": "insurance", "label": "Insurance policy", "required": False}],
        "cs_items": [{"key": "end_use_cert", "label": "End-use certificate"}],
        "covenants": [{"name": "DSCR", "covenant_type": "Financial", "metric": "dscr",
                       "operator": ">=", "threshold": 1.2, "frequency": "Quarterly",
                       "first_due_on": "2026-12-31"}]}, headers=ANALYST)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seeded_checklist_id"], body
    assert len(body["seeded_covenant_ids"]) == 1

    # The checklist is REAL — CP and CS items present, Pending, with condition types.
    lst = (await client.get("/v1/internal/cpcs-checklists",
                            params={"lending_id": lid}, headers=ANALYST))
    assert lst.status_code == 200, lst.text
    rows = lst.json() if isinstance(lst.json(), list) else lst.json().get("items", [])
    items = rows[-1]["items"]
    kinds = {(i["key"], i["condition_type"]) for i in items}
    assert ("board_resolution", "CP") in kinds and ("end_use_cert", "CS") in kinds

    # The covenant is real too, attached to the company and this line.
    cov = (await client.get("/v1/covenants",
                            params={"entity_id": eid}, headers=ANALYST))
    assert cov.status_code == 200, cov.text
    assert any(c.get("name") == "DSCR" for c in
               (cov.json() if isinstance(cov.json(), list)
                else cov.json().get("items", []))), cov.text

    # Terms are entered ONCE — a second entry is refused, not merged.
    again = await client.post("/v1/internal/sanction-terms",
                              json={"lending_id": lid, "amount_cr": 50},
                              headers=ANALYST)
    assert again.status_code == 409, again.text

    got = await client.get("/v1/internal/sanction-terms",
                           params={"lending_id": lid}, headers=ANALYST)
    assert got.status_code == 200 and got.json()["amount_cr"] == 45.0


async def test_cam_lifecycle_is_maker_checker_with_an_amend_loop(client: AsyncClient):
    """Draft → Submitted → Returned → (amend = redraft) → Submitted → Approved,
    with the preparer barred from deciding and reasons mandatory on Return/Reject."""
    _, lid = await _lending(client)

    opened = await client.post("/v1/internal/cam-reports", json={
        "lending_id": lid, "engine": "anthropic:claude-haiku",
        "source_doc_ids": ["d1", "d2"], "prompt_doc_id": "p1"}, headers=ANALYST)
    assert opened.status_code == 201, opened.text
    rid = opened.json()["id"]
    assert opened.json()["report_version"] == 1

    # Only one open version at a time.
    dup = await client.post("/v1/internal/cam-reports", json={"lending_id": lid},
                            headers=ANALYST)
    assert dup.status_code == 409, dup.text

    # An empty CAM cannot go to committee.
    empty = await client.post(f"/v1/internal/cam-reports/{rid}/submit", json={},
                              headers=ANALYST)
    assert empty.status_code == 422, empty.text

    # The workbench writes turns; the assistant turn carries the new draft.
    for role, content, draft in (("user", "Generate the CAM.", None),
                                 ("assistant", "CAM v1 text", "# CAM\nDraft one.")):
        t = await client.post(f"/v1/internal/cam-reports/{rid}/turns",
                              json={"role": role, "content": content,
                                    **({"draft_md": draft} if draft else {})},
                              headers=ANALYST)
        assert t.status_code == 201, t.text

    assert (await client.post(f"/v1/internal/cam-reports/{rid}/submit", json={},
                              headers=ANALYST)).status_code == 200

    # An analyst holds no deciding authority at all (RBAC, before four-eyes).
    own = await client.post(f"/v1/internal/cam-reports/{rid}/decide",
                            json={"decision": "Approved"}, headers=ANALYST)
    assert own.status_code == 403, own.text

    # A return without reasons is refused; with reasons it lands and reopens the draft.
    bare = await client.post(f"/v1/internal/cam-reports/{rid}/decide",
                             json={"decision": "Returned"}, headers=HEAD)
    assert bare.status_code == 422, bare.text
    ret = await client.post(f"/v1/internal/cam-reports/{rid}/decide",
                            json={"decision": "Returned",
                                  "note": "Collateral section is thin."}, headers=HEAD)
    assert ret.status_code == 200 and ret.json()["status"] == "Returned"

    # Amending writes into the SAME version, which goes back to Draft…
    amend = await client.post(f"/v1/internal/cam-reports/{rid}/turns",
                              json={"role": "assistant", "content": "CAM v1 amended",
                                    "draft_md": "# CAM\nDraft two."}, headers=ANALYST)
    assert amend.status_code == 201 and amend.json()["status"] == "Draft"
    assert (await client.post(f"/v1/internal/cam-reports/{rid}/submit", json={},
                              headers=ANALYST)).status_code == 200

    ok = await client.post(f"/v1/internal/cam-reports/{rid}/decide",
                           json={"decision": "Approved", "note": "Good."}, headers=HEAD)
    assert ok.status_code == 200 and ok.json()["status"] == "Approved"

    # …and the full story is readable: transcript + decision on one record.
    full = (await client.get(f"/v1/internal/cam-reports/{rid}", headers=HEAD)).json()
    assert [t["role"] for t in full["turns"]] == ["user", "assistant", "assistant"]
    assert full["decided_by"] == "credithead@evamfinance.com"
    assert full["draft_md"].endswith("Draft two.")

    # After approval a NEW version can open (e.g. for a later enhancement round).
    v2 = await client.post("/v1/internal/cam-reports", json={"lending_id": lid},
                           headers=ANALYST)
    assert v2.status_code == 201 and v2.json()["report_version"] == 2


async def test_a_cam_preparer_with_authority_still_may_not_decide_their_own(
        client: AsyncClient):
    """Four-eyes at EQUAL authority: a Credit Head who drafted the CAM cannot also be
    its committee."""
    _, lid = await _lending(client)
    opened = await client.post("/v1/internal/cam-reports", json={"lending_id": lid},
                               headers=HEAD)
    rid = opened.json()["id"]
    assert (await client.post(f"/v1/internal/cam-reports/{rid}/turns",
                              json={"role": "assistant", "content": "CAM",
                                    "draft_md": "# CAM"},
                              headers=HEAD)).status_code == 201
    assert (await client.post(f"/v1/internal/cam-reports/{rid}/submit", json={},
                              headers=HEAD)).status_code == 200
    own = await client.post(f"/v1/internal/cam-reports/{rid}/decide",
                            json={"decision": "Approved"}, headers=HEAD)
    assert own.status_code == 422, own.text
    assert "DIFFERENT" in own.text


async def test_the_default_sanction_template_seeds_once_and_only_once(client: AsyncClient):
    """Bootstrap ships the deployment's default sanction letter (design §D): a
    tenant-level Template document, inline bytes, replaceable by upload. Idempotent by
    checksum — a re-run bootstrap never duplicates it. (The test drives the seeder
    directly: this conftest builds the schema without running bootstrap.)"""
    import hashlib
    from pathlib import Path

    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models.documents import Document
    from app.seed.loader import ensure_tenant
    from app.seed.sanction_template import seed_sanction_template

    tpl = Path(__file__).parents[1] / "app" / "seed" / "templates" / \
        "sanction_letter_default.docx"
    assert tpl.exists(), "the default template must ship with the register"
    want = hashlib.sha256(tpl.read_bytes()).hexdigest()

    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, "EVAM", "Evam Finance")
        assert await seed_sanction_template(session, tenant_id) == 1
        assert await seed_sanction_template(session, tenant_id) == 0   # idempotent
        await session.commit()
        row = (await session.execute(select(Document).where(
            Document.tenant_id == tenant_id,
            Document.subject_type == "Template",
            Document.doc_type == "sanction_template",
            Document.checksum == want))).scalars().first()
    assert row is not None
    assert row.inline_content and len(row.inline_content) == tpl.stat().st_size
    assert row.content_type.endswith("wordprocessingml.document")
