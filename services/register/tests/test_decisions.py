"""The dedicated workflow-decision resource: single-winner (DB-enforced), server-controlled
provenance, and service-only restricted access. Runs against the real Postgres + migration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from evam_backend_core.internal_token import mint_internal_context
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.seed.loader import ensure_tenant

pytestmark = pytest.mark.asyncio

SIGN = "decisions-signing-secret"
WF = "leadconv-EVAMdeadbeef00-lead-xyz"


def _ctx(path: str = "/v1/internal/decisions", tenant: str = "EVAM",
         email: str = "head@evamfinance.com", roles=("BD Head",)) -> str:  # noqa: ANN001
    return mint_internal_context(
        signing_key=SIGN, tenant=tenant, email=email, user_id=str(uuid.uuid4()),
        roles=list(roles), effective_operations={"push_lead_to_deals": "FULL"},
        method="POST", path=path, ttl_seconds=300)


@pytest_asyncio.fixture
async def wf_client(monkeypatch) -> AsyncIterator[AsyncClient]:
    clear_tenant_cache()
    s = get_settings()
    monkeypatch.setattr(s, "service_api_keys", {"wf-key": "svc_workflows"})
    monkeypatch.setattr(s, "internal_signing_secret", SIGN)
    init_engine(s)
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": "wf-key", "X-Tenant": "EVAM"}) as c:
            yield c
    finally:
        from sqlalchemy import text
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(text(
                "TRUNCATE workflow_decisions, workflow_decision_outbox RESTART IDENTITY"))
            await session.commit()
        await dispose_engine()


async def test_single_winner_replay_and_opposite_conflict(wf_client):
    # First decision wins → 201, provenance from the VERIFIED token (not a body field).
    r = await wf_client.post("/v1/internal/decisions",
                             json={"workflow_id": WF, "decision": "Approved",
                                   "lead_id": "lead-xyz", "note": "ok"},
                             headers={"X-Internal-Context": _ctx()})
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["decision"] == "Approved"
    assert first["decided_by"] == "head@evamfinance.com"
    assert first["operations"].get("push_lead_to_deals") == "FULL"

    # Replaying the SAME decision returns the original record (idempotent), no duplicate.
    r2 = await wf_client.post("/v1/internal/decisions",
                              json={"workflow_id": WF, "decision": "Approved"},
                              headers={"X-Internal-Context": _ctx()})
    assert r2.status_code in (200, 201), r2.text
    assert r2.json()["id"] == first["id"]

    # The OPPOSITE decision is refused — single winner, even concurrently.
    r3 = await wf_client.post("/v1/internal/decisions",
                              json={"workflow_id": WF, "decision": "Rejected"},
                              headers={"X-Internal-Context": _ctx()})
    assert r3.status_code == 409, r3.text


async def test_get_returns_decision_and_404_when_absent(wf_client):
    assert (await wf_client.get(f"/v1/internal/decisions/{WF}")).status_code == 404
    await wf_client.post("/v1/internal/decisions",
                         json={"workflow_id": WF, "decision": "Rejected", "note": "no"},
                         headers={"X-Internal-Context": _ctx()})
    got = await wf_client.get(f"/v1/internal/decisions/{WF}")
    assert got.status_code == 200
    assert got.json()["decision"] == "Rejected"


async def test_write_requires_delegated_approver(wf_client):
    # svc_workflows key but NO signed approver context → refused (provenance is server-set).
    r = await wf_client.post("/v1/internal/decisions",
                             json={"workflow_id": WF, "decision": "Approved"})
    assert r.status_code == 403, r.text


async def test_non_workflow_service_is_denied(wf_client):
    # The generic (non-workflows) key may not touch decisions at all.
    r = await wf_client.get(f"/v1/internal/decisions/{WF}",
                            headers={"X-API-Key": "test-key"})
    assert r.status_code == 403, r.text


async def test_concurrent_approve_and_reject_yield_one_winner(wf_client):
    """Two TRULY concurrent, opposite decisions: exactly one persists (201), the other 409.
    The database UNIQUE constraint — not timing — decides the single winner."""
    async def submit(dec: str):
        return await wf_client.post("/v1/internal/decisions",
                                    json={"workflow_id": WF, "decision": dec},
                                    headers={"X-Internal-Context": _ctx()})

    a, b = await asyncio.gather(submit("Approved"), submit("Rejected"))
    codes = sorted([a.status_code, b.status_code])
    assert codes == [201, 409], (a.status_code, a.text, b.status_code, b.text)
    # Whichever won, exactly one decision is now stored, and GET agrees with it.
    got = (await wf_client.get(f"/v1/internal/decisions/{WF}")).json()
    assert got["decision"] in {"Approved", "Rejected"}


async def test_same_outcome_from_different_approvers_returns_the_first(wf_client):
    """Two approvers submit the SAME outcome: the second is an idempotent replay that returns
    the FIRST record — so attribution never changes to the later caller."""
    r1 = await wf_client.post("/v1/internal/decisions",
                              json={"workflow_id": WF, "decision": "Approved",
                                    "note": "first"},
                              headers={"X-Internal-Context": _ctx(email="first@evamfinance.com")})
    assert r1.status_code == 201
    r2 = await wf_client.post("/v1/internal/decisions",
                              json={"workflow_id": WF, "decision": "Approved",
                                    "note": "second"},
                              headers={"X-Internal-Context": _ctx(email="second@evamfinance.com")})
    assert r2.status_code in (200, 201)
    # The stored record is the FIRST approver's, unchanged.
    assert r2.json()["decided_by"] == "first@evamfinance.com"
    assert r2.json()["note"] == "first"


async def test_decision_is_tenant_isolated(wf_client):
    """A decision recorded under one tenant is invisible to another tenant's read."""
    from app.db.session import get_sessionmaker
    from app.seed.loader import ensure_tenant
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "OTHER", "Other Co")
        await session.commit()
    await wf_client.post("/v1/internal/decisions",
                         json={"workflow_id": WF, "decision": "Approved"},
                         headers={"X-Internal-Context": _ctx()})
    # Same workflow id, different tenant → not found (tenant-scoped query + RLS).
    other = await wf_client.get(f"/v1/internal/decisions/{WF}",
                                headers={"X-Tenant": "OTHER"})
    assert other.status_code == 404, other.text


async def test_decision_row_is_immutable_at_the_database(wf_client):
    """The recorded decision cannot be UPDATEd or DELETEd — a DB trigger blocks it even for the
    owner connection (independent of grants)."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    await wf_client.post("/v1/internal/decisions",
                         json={"workflow_id": WF, "decision": "Approved"},
                         headers={"X-Internal-Context": _ctx()})
    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(Exception):  # noqa: B017 - immutability trigger raises
            await session.execute(
                text("UPDATE workflow_decisions SET decision='Rejected' "
                     "WHERE workflow_id=:w"), {"w": WF})
            await session.commit()
    async with sm() as session:
        with pytest.raises(Exception):  # noqa: B017 - immutability trigger raises
            await session.execute(
                text("DELETE FROM workflow_decisions WHERE workflow_id=:w"), {"w": WF})
            await session.commit()


async def _record(wf_client, decision="Approved", wf=WF):  # noqa: ANN001
    return await wf_client.post("/v1/internal/decisions",
                                json={"workflow_id": wf, "decision": decision},
                                headers={"X-Internal-Context": _ctx()})


async def test_recording_a_decision_creates_a_pending_outbox_row(wf_client):
    await _record(wf_client)
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 1
    assert stats["applied"] == 0 and stats["dead"] == 0


async def test_claim_leases_atomically_and_is_not_double_claimed(wf_client):
    await _record(wf_client)
    first = (await wf_client.post("/v1/internal/decisions/deliveries/claim",
                                  json={"limit": 10, "lease_seconds": 60})).json()
    assert len(first["claimed"]) == 1
    assert first["claimed"][0]["workflow_id"] == WF
    assert first["claimed"][0]["attempts"] == 1
    # A second immediate claim gets nothing — the row is leased.
    second = (await wf_client.post("/v1/internal/decisions/deliveries/claim",
                                   json={"limit": 10, "lease_seconds": 60})).json()
    assert second["claimed"] == []


async def _claim_token(wf_client, wf=WF):  # noqa: ANN001
    claimed = (await wf_client.post("/v1/internal/decisions/deliveries/claim",
                                    json={"limit": 10, "lease_seconds": 60})).json()["claimed"]
    return next(c["claim_token"] for c in claimed if c["workflow_id"] == wf)


async def test_mark_applied_dead_and_retry(wf_client):
    await _record(wf_client)
    token = await _claim_token(wf_client)
    # applied → counts move to applied, and it is no longer claimable.
    r = await wf_client.post(f"/v1/internal/decisions/{WF}/delivery",
                             json={"status": "applied", "claim_token": token})
    assert r.status_code == 200 and r.json()["status"] == "applied"
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["applied"] == 1 and stats["pending"] == 0
    claimed = (await wf_client.post("/v1/internal/decisions/deliveries/claim",
                                    json={"limit": 10, "lease_seconds": 60})).json()
    assert claimed["claimed"] == []

    # retry with a backoff keeps it pending but NOT immediately due.
    wf2 = "leadconv-EVAMdeadbeef00-lead2"
    await _record(wf_client, decision="Approved", wf=wf2)
    token2 = await _claim_token(wf_client, wf2)
    await wf_client.post(f"/v1/internal/decisions/{wf2}/delivery",
                         json={"status": "retry", "claim_token": token2,
                               "backoff_seconds": 3600, "error": "still running"})
    due = (await wf_client.post("/v1/internal/decisions/deliveries/claim",
                                json={"limit": 10, "lease_seconds": 60})).json()
    assert due["claimed"] == []   # backed off an hour → not due

    # dead is terminal.
    wf3 = "leadconv-EVAMdeadbeef00-lead3"
    await _record(wf_client, decision="Rejected", wf=wf3)
    token3 = await _claim_token(wf_client, wf3)
    await wf_client.post(f"/v1/internal/decisions/{wf3}/delivery",
                         json={"status": "dead", "claim_token": token3, "error": "closed"})
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["dead"] == 1


async def test_stale_claimant_cannot_regress_a_terminal_row(wf_client):
    await _record(wf_client)
    token = await _claim_token(wf_client)
    # The current claimant applies it (terminal).
    assert (await wf_client.post(f"/v1/internal/decisions/{WF}/delivery",
                                 json={"status": "applied", "claim_token": token})
            ).json()["status"] == "applied"
    # A stale claimant (wrong token) trying to mark dead is a NO-OP — the row stays applied.
    r = await wf_client.post(f"/v1/internal/decisions/{WF}/delivery",
                             json={"status": "dead", "claim_token": str(uuid.uuid4()),
                                   "error": "stale"})
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert r.json()["current"] == "applied"
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["applied"] == 1 and stats["dead"] == 0


async def test_replay_reensures_the_outbox_row(wf_client):
    """A decision recorded before the outbox existed (simulated by deleting its outbox row)
    becomes deliverable again on an idempotent same-outcome replay."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    await _record(wf_client)
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text("DELETE FROM workflow_decision_outbox WHERE workflow_id=:w"),
                        {"w": WF})
        await s.commit()
    assert (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()["pending"] == 0
    # Replay the SAME decision → the outbox row is re-created.
    r = await _record(wf_client)
    assert r.status_code in (200, 201)
    assert (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()["pending"] == 1


async def test_redrive_requires_an_admin_and_is_audited(wf_client):
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    await _record(wf_client)
    token = await _claim_token(wf_client)
    await wf_client.post(f"/v1/internal/decisions/{WF}/delivery",
                         json={"status": "dead", "claim_token": token, "error": "closed"})
    redrive_path = f"/v1/internal/decisions/{WF}/redrive"
    admin_hdr = {"X-Internal-Context": _ctx(path=redrive_path, email="admin@evamfinance.com",
                                            roles=("Admin",))}
    reason_body = {"reason": "worker outage recovered", "ticket": "INC-42"}
    # A non-Admin (BD Head) delegated identity is REFUSED.
    non_admin = await wf_client.post(
        redrive_path, json=reason_body, headers={"X-Internal-Context": _ctx(path=redrive_path)})
    assert non_admin.status_code == 403
    # No delegated identity at all (service key only) is REFUSED.
    assert (await wf_client.post(redrive_path, json=reason_body)).status_code == 403
    # A verified Admin WITHOUT a reason is refused (422) — redrive must be explainable.
    assert (await wf_client.post(redrive_path, json={}, headers=admin_hdr)).status_code == 422
    # A whitespace-only reason is also refused.
    assert (await wf_client.post(redrive_path, json={"reason": "   "},
                                 headers=admin_hdr)).status_code == 422
    # A verified Admin WITH a reason succeeds...
    r = await wf_client.post(redrive_path, json=reason_body, headers=admin_hdr)
    assert r.status_code == 200 and r.json()["by"] == "admin@evamfinance.com"
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 1 and stats["dead"] == 0
    assert await _claim_token(wf_client)   # claimable again
    # ...and an immutable audit event names the admin, the reason, the ticket, and the PREVIOUS
    # dead-letter cause (captured before it was cleared).
    sm = get_sessionmaker()
    async with sm() as s:
        row = (await s.execute(text(
            "SELECT actor, action, resource_id, changes FROM audit_log "
            "WHERE action='decision.redrive' AND resource_id=:w"), {"w": WF})).first()
    assert row is not None and row[0] == "admin@evamfinance.com"
    changes = row[3]
    assert changes["reason"] == "worker outage recovered"
    assert changes["ticket"] == "INC-42"
    assert changes["previous_error"] == "closed"


async def test_backfill_statement_recovers_pre_outbox_decisions(wf_client):
    """The migration 0009 backfill INSERT…SELECT creates a pending delivery for a decision that
    has no outbox row (the round-15..15f orphan condition). Proven by deleting the outbox row and
    re-running the exact backfill statement."""
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    await _record(wf_client)
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text("DELETE FROM workflow_decision_outbox WHERE workflow_id=:w"),
                        {"w": WF})
        await s.execute(text(
            "INSERT INTO workflow_decision_outbox "
            "(tenant_id, workflow_id, decision, status, attempts, next_attempt_at) "
            "SELECT d.tenant_id, d.workflow_id, d.decision, 'pending', 0, now() "
            "FROM workflow_decisions d "
            "ON CONFLICT ON CONSTRAINT workflow_decision_outbox_tenant_wf DO NOTHING"))
        await s.commit()
    assert (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()["pending"] == 1


async def test_delivery_endpoints_require_the_workflow_service(wf_client):
    r = await wf_client.post("/v1/internal/decisions/deliveries/claim",
                             json={"limit": 5, "lease_seconds": 60},
                             headers={"X-API-Key": "test-key"})
    assert r.status_code == 403


async def test_internal_tenants_lists_active_codes(wf_client):
    r = await wf_client.get("/v1/internal/tenants")
    assert r.status_code == 200
    assert "EVAM" in r.json()["tenants"]


async def test_rls_isolates_workflow_decisions_at_the_database(wf_client):
    """DIRECT proof of RLS — NOT just the app's WHERE clause. Runs the queries as a role that is
    provably NOSUPERUSER + NOBYPASSRLS (a superuser or BYPASSRLS role would silently defeat RLS
    even under FORCE — which is exactly the trap when the deploy connects as a superuser). Under
    FORCE, that role sees the decision row only under the matching tenant GUC; a wrong tenant or
    an UNSET GUC (fail-closed) returns zero. Run in a transaction that rolls back the FORCE."""
    import uuid as _uuid

    from sqlalchemy import text

    from app.core.security import _resolve_tenant_id
    from app.db.session import get_engine, get_sessionmaker

    await wf_client.post("/v1/internal/decisions",
                         json={"workflow_id": WF, "decision": "Approved"},
                         headers={"X-Internal-Context": _ctx()})
    sm = get_sessionmaker()
    async with sm() as s:
        evam_id = await _resolve_tenant_id(s, "EVAM")

    engine = get_engine()
    conn = await engine.connect()
    trans = await conn.begin()
    try:
        # Choose a role that CANNOT bypass RLS. If the current role is itself
        # NOSUPERUSER+NOBYPASSRLS (a non-owner CI role, or a plain owner), use it directly;
        # otherwise (a superuser owner, as the postgres image creates) hop to register_app,
        # which the RLS bootstrap converges to NOSUPERUSER+NOBYPASSRLS.
        cur = (await conn.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).first()
        if cur is not None and not cur[0] and not cur[1]:
            role = None
        else:
            ra = (await conn.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='register_app'"))
                  ).first()
            if ra is None or ra[0] or ra[1]:
                pytest.skip("no NOSUPERUSER NOBYPASSRLS role available to prove RLS directly")
            role = "register_app"

        await conn.execute(text("ALTER TABLE workflow_decisions FORCE ROW LEVEL SECURITY"))
        if role:
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
        # PROVE the effective role can't bypass RLS (else the assertions below are meaningless).
        eff = (await conn.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).first()
        assert eff is not None and not eff[0] and not eff[1], eff

        q = text("SELECT count(*) FROM workflow_decisions WHERE workflow_id=:w")
        # Right tenant → visible.
        await conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                           {"t": str(evam_id)})
        assert (await conn.execute(q, {"w": WF})).scalar() == 1
        # A DIFFERENT tenant GUC → the same row is invisible (RLS binds the non-bypass role,
        # not just the app's WHERE clause). Fail-closed on a wholly-unset GUC is proven for the
        # shared policy in test_rls.py.
        await conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                           {"t": str(_uuid.uuid4())})
        assert (await conn.execute(q, {"w": WF})).scalar() == 0
    finally:
        await trans.rollback()   # undoes FORCE + SET LOCAL ROLE
        await conn.close()


@pytest.mark.skip(reason="Dormant: the /v1/internal/advaya-handoffs router is only registered under "
                         "REGISTER_ADVAYA_INTEGRATION_ENABLED (default off). The disabled-by-default "
                         "behaviour is covered in test_handover.py; this single-winner test applies "
                         "when a real Advaya integration is enabled.")
async def test_advaya_handoff_is_single_winner_and_reads_back(wf_client):
    """The Advaya-handoff record (workflow-service only) is single-winner on (tenant, handoff_key):
    a replay of the same outcome is idempotent, a contradictory outcome is refused, and it reads
    back."""
    hk = f"advaya-handoff:{uuid.uuid4().hex}"
    body = {"handoff_key": hk, "lending_id": uuid.uuid4().hex, "payload_sha256": "a" * 64,
            "status": "Accepted", "acknowledgement_id": "ACK-1"}
    first = await wf_client.post("/v1/internal/advaya-handoffs", json=body)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "Accepted"
    # Replaying the SAME outcome is idempotent.
    again = await wf_client.post("/v1/internal/advaya-handoffs", json=body)
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    # A contradictory outcome is refused.
    bad = await wf_client.post("/v1/internal/advaya-handoffs",
                               json={**body, "status": "Rejected"})
    assert bad.status_code == 409, bad.text
    got = await wf_client.get(f"/v1/internal/advaya-handoffs/{hk}")
    assert got.status_code == 200 and got.json()["acknowledgement_id"] == "ACK-1"


async def test_control_records_are_kind_bound_and_create_no_delivery(wf_client):
    """Run-control records (cancel / return / resubmit) share the decision store's
    durability and immutability but are AUDIT anchors, not appliable outcomes: the value
    space is disjoint by kind (no control write can mint an approval and vice versa) and no
    conversion-delivery outbox row is created for them."""
    ref = f"{WF}:control:abc123"
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": ref, "decision": "Cancelled", "kind": "control",
              "note": "client withdrew"},
        headers={"X-Internal-Context": _ctx()})
    assert r.status_code == 201, r.text
    assert r.json()["decision"] == "Cancelled"
    # Readable back for the workflow's fail-closed verification…
    got = await wf_client.get(f"/v1/internal/decisions/{ref}")
    assert got.status_code == 200 and got.json()["decision"] == "Cancelled"
    # …but nothing to deliver: the outbox is untouched.
    stats = (await wf_client.get("/v1/internal/decisions/deliveries/stats")).json()
    assert stats["pending"] == 0

    # The kind/value spaces are DISJOINT, both ways.
    bad = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": f"{WF}:control:x", "decision": "Approved", "kind": "control"},
        headers={"X-Internal-Context": _ctx()})
    assert bad.status_code == 422, bad.text
    bad = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": f"{WF}:c2", "decision": "Cancelled"},
        headers={"X-Internal-Context": _ctx()})
    assert bad.status_code == 422, bad.text


async def test_conditional_approval_fields_roundtrip(wf_client):
    """A committee decision can carry CONDITIONS and a validity window — recorded once,
    immutable, and read back by the workflow's per-facility verification."""
    ref = f"{WF}:lending:l1"
    r = await wf_client.post(
        "/v1/internal/decisions",
        json={"workflow_id": ref, "decision": "Approved", "kind": "committee",
              "subject_type": "Lending", "subject_id": "l1",
              "conditions": "quarterly covenant reporting; insurance assignment",
              "valid_days": 90},
        headers={"X-Internal-Context": _ctx(roles=("Credit Head",))})
    assert r.status_code == 201, r.text
    got = (await wf_client.get(f"/v1/internal/decisions/{ref}")).json()
    assert got["conditions"] == "quarterly covenant reporting; insurance assignment"
    assert got["valid_days"] == 90
