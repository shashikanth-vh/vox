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

    # The committee QUEUE reads the same rows by status; an unfiltered dump is refused.
    queue = await client.get("/v1/internal/cam-reports",
                             params={"status": "Submitted"}, headers=HEAD)
    assert queue.status_code == 200 and rid in {r["id"] for r in queue.json()}
    assert (await client.get("/v1/internal/cam-reports",
                             headers=HEAD)).status_code == 422

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
        # Three shipped defaults: the sanction letter, the CAM master prompt, and the
        # example CAM the workbench offers as a format reference.
        assert await seed_sanction_template(session, tenant_id) == 3
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


async def test_the_default_templates_resolve_by_doc_type(client: AsyncClient):
    """/v1/templates/{doc_type} answers what 'use the default' means — the newest
    tenant Template of that kind; an unknown kind is a 404 that says what to do."""
    from app.db.session import get_sessionmaker
    from app.seed.loader import ensure_tenant
    from app.seed.sanction_template import seed_sanction_template

    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, "EVAM", "Evam Finance")
        await seed_sanction_template(session, tenant_id)
        await session.commit()

    got = await client.get("/v1/templates/cam_prompt")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["id"] and body["doc_type"] == "cam_prompt"
    assert "CAM" in (body["title"] or "")
    # And the bytes are readable through the ordinary content route (the workbench
    # reads the prompt exactly this way).
    content = await client.get(f"/v1/documents/{body['id']}/content")
    assert content.status_code == 200
    assert content.content[:2] == b"PK"          # a .docx is a zip

    assert (await client.get("/v1/templates/never-shipped")).status_code == 404


async def test_the_roster_reconciles_from_access(client: AsyncClient, monkeypatch):
    """Access users become roster rows automatically — keyed by e-mail, roles copied,
    roster-only fields untouched, never deleted, collisions reported not guessed."""
    from app.api import people_sync

    ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin",
             "X-User-Id": "8c5a2c1e-0000-4000-8000-00000000000a"}

    # An existing roster row with roster-only data the sync must not touch —
    # and an OUTDATED role Access has since changed.
    assert (await client.post("/v1/people", json={
        "name": "Priya", "full_name": "E2E Priya Nair", "role": "RM",
        "email": "e2e.rm@evamfinance.com", "geography": "Karnataka"},
        headers=ADMIN)).status_code == 201
    # A leaver who IS on the roster: the sync marks them inactive (never deletes).
    assert (await client.post("/v1/people", json={
        "name": "gone", "full_name": "Left The Firm", "role": "BDRM",
        "email": "gone@evamfinance.com"}, headers=ADMIN)).status_code == 201

    access_users = [
        {"id": "u1", "email": "e2e.rm@evamfinance.com", "full_name": "E2E Priya Nair",
         "short_name": "Priya", "roles": ["BDRM", "Syn RM", "AM RM"], "is_active": True},
        {"id": "u2", "email": "e2e.checker@evamfinance.com", "full_name": "E2E Divya Rao",
         "roles": ["Management"], "is_active": True},
        {"id": "u3", "email": "gone@evamfinance.com", "full_name": "Left The Firm",
         "roles": ["BDRM"], "is_active": False},
        {"id": "u4", "email": "", "full_name": "No Mailbox", "roles": ["BDRM"],
         "is_active": True},
    ]

    async def fake_list(tenant_code):  # noqa: ANN001
        return access_users

    monkeypatch.setattr(people_sync, "_list_access_users", fake_list)

    r = await client.post("/v1/internal/people/sync-access", headers=ADMIN)
    assert r.status_code == 200, r.text
    out = r.json()
    # The inactive u3 UPDATES its existing row; an inactive user with NO row would be
    # skipped, not created — creating one is how deleted employees used to resurrect.
    assert set(out["created"]) == {"e2e.checker@evamfinance.com"}
    assert set(out["updated"]) == {"e2e.rm@evamfinance.com", "gone@evamfinance.com"}
    assert out["skipped"][0]["reason"].startswith("no e-mail")

    # Priya: role now matches Access; geography (roster-only) survived.
    got = (await client.get("/v1/people/resolve",
                            params={"name": "e2e.rm@evamfinance.com"},
                            headers=ADMIN)).json()
    assert got["resolved"]["role"] == "BDRM, Syn RM, AM RM"
    # Divya is now on the roster (no short_name in Access → handle = e-mail local
    # part, the same key VocX uses); the leaver is inactive, not deleted.
    divya = (await client.get("/v1/people/resolve", params={"name": "E2E Divya Rao"},
                              headers=ADMIN)).json()
    assert divya["resolved"] and divya["resolved"]["name"] == "e2e.checker"
    leaver = (await client.get("/v1/people/resolve", params={"name": "gone"},
                               headers=ADMIN)).json()
    assert leaver["resolved"]["inactive"] is True

    # Idempotent: a second run changes nothing.
    again = (await client.post("/v1/internal/people/sync-access", headers=ADMIN)).json()
    assert again["created"] == [] and again["updated"] == []


async def test_a_deleted_person_frees_their_name(client: AsyncClient, monkeypatch):
    """Removing an employee releases their full name for a future hire — and while a
    live row still holds the name, the refusal says WHO, not 'constraint violated'.
    The sync must not undo the release by resurrecting the deactivated user."""
    from app.api import people_sync

    ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin",
             "X-User-Id": "8c5a2c1e-0000-4000-8000-00000000000a"}

    first = (await client.post("/v1/people", json={
        "name": "Arun", "full_name": "Arun Menon", "role": "Credit Head",
        "email": "e2e.maker@evamfinance.com"}, headers=ADMIN))
    assert first.status_code == 201, first.text

    # Same full name, different mailbox → refused, naming the holder.
    dup = await client.post("/v1/people", json={
        "name": "Arun", "full_name": "Arun Menon", "role": "Credit Head",
        "email": "arun.menon@evamfinance.com"}, headers=ADMIN)
    assert dup.status_code == 422, dup.text
    assert "already on the roster" in dup.text
    assert "e2e.maker@evamfinance.com" in dup.text

    # The employee is deleted (Access keeps the deactivated user; roster soft-deletes).
    assert (await client.delete(f"/v1/people/{first.json()['id']}",
                                headers=ADMIN)).status_code == 204

    # A sync that still sees the deactivated Access user must NOT resurrect the row.
    async def fake_list(tenant_code):  # noqa: ANN001
        return [{"id": "u9", "email": "e2e.maker@evamfinance.com",
                 "full_name": "Arun Menon", "roles": ["Credit Head"],
                 "is_active": False}]
    monkeypatch.setattr(people_sync, "_list_access_users", fake_list)
    synced = (await client.post("/v1/internal/people/sync-access",
                                headers=ADMIN)).json()
    assert synced["created"] == []

    # The name is free: the successor (or namesake) can be hired.
    again = await client.post("/v1/people", json={
        "name": "Arun", "full_name": "Arun Menon", "role": "Credit Head",
        "email": "arun.menon@evamfinance.com"}, headers=ADMIN)
    assert again.status_code == 201, again.text


async def test_a_leavers_book_hands_over_whole(client: AsyncClient):
    """Every row the leaver owns — by EITHER of their names — moves to the successor in
    one call, with counts; their active assignments end and mirror to the successor."""
    ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin",
             "X-User-Id": "8c5a2c1e-0000-4000-8000-00000000000a"}
    for body in ({"name": "Priya", "full_name": "E2E Priya Nair", "role": "BDRM",
                  "email": "e2e.rm@evamfinance.com"},
                 {"name": "Kiran", "full_name": "E2E Kiran Shah", "role": "BDRM",
                  "email": "kiran@evamfinance.com"}):
        assert (await client.post("/v1/people", json=body,
                                  headers=ADMIN)).status_code == 201

    ent = (await client.post("/v1/entities", json={
        "code": f"HB-{uuid.uuid4().hex[:6]}", "legal_name": "Handover Book Co"})).json()
    # One row under the HANDLE, one under the FULL NAME — both must move.
    lead1 = (await client.post("/v1/leads", json={
        "company": "Handover Book Co", "entity_id": ent["id"], "rm": "Priya"})).json()
    lead2 = (await client.post("/v1/leads", json={
        "company": "Handover Book Co", "entity_id": ent["id"],
        "rm": "E2E Priya Nair"})).json()
    lend = (await client.post("/v1/lending", json={
        "entity_id": ent["id"], "rm": "Priya", "analyst": "Priya"})).json()

    r = await client.post("/v1/internal/people/handover", json={
        "from_person": "e2e.rm@evamfinance.com", "to_person": "Kiran"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["moved"]["leads"] == 2
    assert out["moved"]["lending"] == 2          # rm + analyst on the one line
    assert out["to"] == "E2E Kiran Shah"

    for lid in (lead1["id"], lead2["id"]):
        got = (await client.get(f"/v1/leads/{lid}", headers=ADMIN)).json()
        assert got["rm"] == "Kiran", got
    assert (await client.get(f"/v1/lending/{lend['id']}",
                             headers=ADMIN)).json()["rm"] == "Kiran"

    # Guard rails: no self-handover, no inactive successor, ambiguity refused upstream.
    self_h = await client.post("/v1/internal/people/handover", json={
        "from_person": "Kiran", "to_person": "kiran@evamfinance.com"}, headers=ADMIN)
    assert self_h.status_code == 422


async def test_a_row_naming_you_is_your_book_everywhere(client: AsyncClient):
    """The field report: a BDRM opened a lead whose RM column literally named her and
    was told "not in your scope" — because scope counted only assignments, created_by
    and vertical-head defaults, never the NAME on the row. Now that rm/analyst resolve
    against the roster, the name is an ownership fact, and it must hold on every
    surface at once: the list, the direct GET, the write, and the company reads."""
    ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin",
             "X-User-Id": "8c5a2c1e-0000-4000-8000-00000000000a"}
    PRIYA = {"X-User-Email": "e2e.rm@evamfinance.com", "X-User-Roles": "BDRM",
             "X-User-Id": "8c5a2c1e-0000-4000-8000-0000000000b1"}
    assert (await client.post("/v1/people", json={
        "name": "Priya", "full_name": "E2E Priya Nair", "role": "BDRM",
        "email": "e2e.rm@evamfinance.com"}, headers=ADMIN)).status_code == 201

    ent = (await client.post("/v1/entities", json={
        "code": f"NB-{uuid.uuid4().hex[:6]}", "legal_name": "NameBook Co"},
        headers=ADMIN)).json()
    # Admin creates the lead (so created_by is NOT Priya, and no assignment exists) —
    # but the rm column names her, in three different spellings across three rows.
    lead_ids = []
    for rm in ("Priya", "E2E Priya Nair", "e2e.rm"):
        lead = (await client.post("/v1/leads", json={
            "company": "NameBook Co", "entity_id": ent["id"], "rm": rm},
            headers=ADMIN)).json()
        lead_ids.append(lead["id"])

    # LIST: all three are her book.
    lst = (await client.get("/v1/leads", params={"limit": 100}, headers=PRIYA)).json()
    ids = {r["id"] for r in (lst.get("items") or lst)}
    assert set(lead_ids) <= ids, "a lead naming the user must appear in their list"

    # DIRECT GET + WRITE on the row that names her by the raw local part.
    got = await client.get(f"/v1/leads/{lead_ids[2]}", headers=PRIYA)
    assert got.status_code == 200, got.text
    patched = await client.patch(f"/v1/leads/{lead_ids[2]}",
                                 json={"temperature": "Hot"}, headers=PRIYA)
    assert patched.status_code == 200, patched.text

    # COMPANY surfaces follow: the dossier of a company whose line names her opens.
    dossier = await client.get(f"/v1/entities/{ent['id']}/dossier", headers=PRIYA)
    assert dossier.status_code == 200, dossier.text

    # And a row naming SOMEBODY ELSE is still not hers.
    other = (await client.post("/v1/leads", json={
        "company": "NameBook Co", "entity_id": ent["id"], "rm": "Somebody Else"},
        headers=ADMIN)).json()
    # The company is in her scope (its other lines name her), so the row is readable —
    # but remove the company link and the name alone decides:
    ent2 = (await client.post("/v1/entities", json={
        "code": f"NB-{uuid.uuid4().hex[:6]}", "legal_name": "NotHers Co"},
        headers=ADMIN)).json()
    foreign = (await client.post("/v1/leads", json={
        "company": "NotHers Co", "entity_id": ent2["id"], "rm": "Somebody Else"},
        headers=ADMIN)).json()
    assert other["id"]  # (readable via company connection — intended)
    denied = await client.get(f"/v1/leads/{foreign['id']}", headers=PRIYA)
    assert denied.status_code == 403, denied.text
