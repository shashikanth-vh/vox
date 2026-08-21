"""Access service — governance, matrix-as-data, guardrails, resolve."""

from __future__ import annotations

import pytest
from evam_backend_core.rbac_catalog import POLICY_VERSION
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

ADMIN = {"X-User-Email": "admin@evamfinance.com"}


async def test_seeded_matrix_and_admin(client: AsyncClient):
    body = (await client.get("/v1/access")).json()
    assert body["version"] >= 1
    # Spec cells present verbatim.
    assert body["operations"]["delete_row"]["Admin"] == "FULL"
    assert body["operations"]["delete_row"]["Management"] == "NONE"
    assert body["views"]["lending"]["Credit Head"] == "FULL"
    assert body["views"]["audit"]["Management"] == "NONE"
    users = (await client.get("/v1/users")).json()
    assert any(u["email"] == "admin@evamfinance.com" for u in users)
    # The deployment's own operator (ACCESS_EXTRA_ADMIN_EMAILS default) is a default
    # Admin too — provisioned by the same seed, with the Admin role.
    tech = next(u for u in users if u["email"] == "tech@evamfinance.com")
    assert tech["full_name"] == "TechAdmin"
    detail = (await client.get(f"/v1/users/{tech['id']}")).json()
    assert "Admin" in (detail.get("roles") or [])


async def test_user_governance_admin_only(client: AsyncClient):
    # Admin creates a BDRM.
    r = await client.post("/v1/users", headers=ADMIN, json={
        "email": "bdrm@evamfinance.com", "full_name": "BDRM", "roles": ["BDRM"]})
    assert r.status_code == 201, r.text
    # Non-admin (the BDRM) may not create users.
    r = await client.post("/v1/users", headers={"X-User-Email": "bdrm@evamfinance.com"},
                          json={"email": "x@evamfinance.com", "full_name": "X"})
    assert r.status_code == 403
    # Domain enforced.
    r = await client.post("/v1/users", headers=ADMIN,
                          json={"email": "eve@gmail.com", "full_name": "Eve"})
    assert r.status_code == 422


async def test_matrix_edit_bumps_version_and_guardrails(client: AsyncClient):
    v0 = (await client.get("/v1/access/version")).json()["version"]
    # Admin grants BDRM the reassign_lead operation (spec default: NONE).
    r = await client.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "reassign_lead", "role": "BDRM", "access": "FULL"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == v0 + 1
    body = (await client.get("/v1/access")).json()
    assert body["operations"]["reassign_lead"]["BDRM"] == "FULL"
    # Guardrail cell refuses even Admin.
    r = await client.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "delete_row", "role": "Management", "access": "FULL"})
    assert r.status_code == 403
    assert "guardrail" in r.text.lower()
    # Non-admin cannot edit the matrix at all.
    await client.post("/v1/users", headers=ADMIN, json={
        "email": "mgmt@evamfinance.com", "full_name": "M", "roles": ["Management"]})
    r = await client.patch("/v1/access", headers={"X-User-Email": "mgmt@evamfinance.com"},
                          json={"kind": "view", "item": "leads", "role": "BDRM",
                                "access": "FULL"})
    assert r.status_code == 403


async def test_resolve_stacking_and_inactive(client: AsyncClient):
    await client.post("/v1/users", headers=ADMIN, json={
        "email": "lead@evamfinance.com", "full_name": "Leader",
        "roles": ["BDRM", "Management"]})
    res = (await client.get("/v1/resolve", params={"email": "lead@evamfinance.com"})).json()
    assert sorted(res["roles"]) == ["BDRM", "Management"]
    assert res["views"]["lending"] == "FULL"       # stacked up from SCOPED
    assert res["views"]["audit"] == "NONE"         # Management ≠ Admin
    assert res["operations"]["approve_stage_change"] == "APPROVE"
    assert res["version"] >= 1

    # Deactivate → resolve 404s (gateway drops the user).
    uid = next(u["id"] for u in (await client.get("/v1/users")).json()
               if u["email"] == "lead@evamfinance.com")
    r = await client.patch(f"/v1/users/{uid}", headers=ADMIN, json={"is_active": False})
    assert r.status_code == 200
    r = await client.get("/v1/resolve", params={"email": "lead@evamfinance.com"})
    assert r.status_code == 404


async def test_me(client: AsyncClient):
    me = (await client.get("/v1/me", headers=ADMIN)).json()
    assert me["email"] == "admin@evamfinance.com"
    assert me["operations"]["delete_row"] == "FULL"
    assert (await client.get("/v1/me")).status_code == 403  # requires user context


async def test_management_governs_users_and_resolve_reports(client: AsyncClient):
    """RBAC 3.1: edit_employee / add_employee_assign_role grant FULL to Management too
    (the matrix stays Admin-only — covered above). And /v1/resolve carries the
    transitive reporting tree for Register team scope."""
    r = await client.post("/v1/users", headers=ADMIN, json={
        "email": "mgr2@evamfinance.com", "full_name": "Manager Two",
        "roles": ["Management"]})
    assert r.status_code == 201, r.text
    mgmt = {"X-User-Email": "mgr2@evamfinance.com"}
    # Management may create users and grant roles…
    r = await client.post("/v1/users", headers=mgmt, json={
        "email": "head2@evamfinance.com", "full_name": "Head Two", "roles": ["BD Head"]})
    assert r.status_code == 201, r.text
    head_id = r.json()["id"]
    r = await client.post("/v1/users", headers=mgmt, json={
        "email": "junior2@evamfinance.com", "full_name": "Junior Two", "roles": ["BDRM"],
        "reports_to": head_id})
    assert r.status_code == 201, r.text
    junior_id = r.json()["id"]
    # …and the head's resolve now includes the junior as a report.
    res = (await client.get("/v1/resolve",
                            params={"email": "head2@evamfinance.com"})).json()
    assert {"id": junior_id, "email": "junior2@evamfinance.com"} in [
        {"id": x["id"], "email": x["email"]} for x in res["reports"]]
    # An IC role still may not govern users.
    r = await client.post("/v1/users", headers={"X-User-Email": "junior2@evamfinance.com"},
                          json={"email": "nope@evamfinance.com", "full_name": "Nope"})
    assert r.status_code == 403


async def test_revocation_epoch_bumps_on_role_and_activation_changes(client: AsyncClient):
    """The revocation epoch advances on every role grant/revoke and (de)activation — the
    signal sensitive-operation revalidation compares against a signed context's claim."""
    r = await client.post("/v1/users", headers=ADMIN, json={
        "email": "epoch@evamfinance.com", "full_name": "Epoch Test", "roles": ["BDRM"]})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    e0 = (await client.get("/v1/resolve", params={"email": "epoch@evamfinance.com"},
                           headers=ADMIN)).json()["epoch"]
    # Grant → epoch advances.
    assert (await client.post(f"/v1/users/{uid}/roles", headers=ADMIN,
                              json={"role": "Syn RM"})).status_code == 201
    e1 = (await client.get("/v1/resolve", params={"email": "epoch@evamfinance.com"},
                           headers=ADMIN)).json()["epoch"]
    assert e1 > e0
    # Revoke → advances again.
    assert (await client.delete(f"/v1/users/{uid}/roles/Syn RM",
                                headers=ADMIN)).status_code == 200
    e2 = (await client.get("/v1/resolve", params={"email": "epoch@evamfinance.com"},
                           headers=ADMIN)).json()["epoch"]
    assert e2 > e1
    # Deactivation bumps too (resolve then 404s — the strongest revocation).
    assert (await client.patch(f"/v1/users/{uid}", headers=ADMIN,
                               json={"is_active": False})).status_code == 200
    r = await client.get("/v1/resolve", params={"email": "epoch@evamfinance.com"},
                         headers=ADMIN)
    assert r.status_code == 404


async def test_matrix_edit_records_override_provenance_and_audit(client: AsyncClient):
    """An Admin cell edit flips the cell's provenance to 'override', the drift report names
    it (still listing the approved baseline value), and an immutable audit event exists."""
    r = await client.patch("/v1/access", headers=ADMIN, json={
        "kind": "operation", "item": "add_lead", "role": "Syn RM", "access": "SCOPED"})
    assert r.status_code == 200, r.text
    drift = (await client.get("/v1/access/drift", headers=ADMIN)).json()
    assert drift["policy_version"] == POLICY_VERSION and drift["fingerprint"]
    assert drift["in_sync"] is False
    cell = next(c for c in drift["differing_cells"]
                if c["item"] == "add_lead" and c["role"] == "Syn RM")
    assert cell["origin"] == "override" and cell["live"] == "SCOPED"
    # The governance change is on the immutable audit trail, stamped with the policy version.
    from evam_backend_core.db.session import get_sessionmaker
    from sqlalchemy import text as _text
    sm = get_sessionmaker()
    async with sm() as s:
        row = (await s.execute(_text(
            "SELECT actor, policy_version, detail->>'to' FROM access_audit "
            "WHERE action='matrix.edit' AND item='operation:add_lead:Syn RM' "
            "ORDER BY created_at DESC LIMIT 1"))).first()
        assert row is not None and row[1] == POLICY_VERSION and row[2] == "SCOPED"
        # Append-only: the DB trigger refuses tampering.
        import pytest as _pytest
        from sqlalchemy.exc import DBAPIError
        with _pytest.raises(DBAPIError):
            await s.execute(_text("DELETE FROM access_audit"))
        await s.rollback()


async def test_drift_endpoint_admin_only(client: AsyncClient):
    await client.post("/v1/users", headers=ADMIN, json={
        "email": "plainrm@evamfinance.com", "full_name": "Plain RM", "roles": ["BDRM"]})
    r = await client.get("/v1/access/drift",
                         headers={"X-User-Email": "plainrm@evamfinance.com"})
    assert r.status_code == 403


async def test_first_boot_bootstrap_seeds_only_an_empty_database():
    """`--if-empty` (the prod-posture container start): a TRULY EMPTY authority DB is
    bootstrapped — tenant + baseline matrix + admin — because an empty Access service
    bricks the whole platform; any NON-empty DB is never written on start (an operator
    edit survives; the start degrades to the drift report)."""
    from evam_backend_core.db.session import dispose_engine, get_sessionmaker, init_engine
    from sqlalchemy import text

    from app import seed as access_seed
    from app.config import get_settings
    from app.security import clear_tenant_cache

    init_engine(get_settings())
    sm = get_sessionmaker()
    async with sm() as session:                       # start from a virgin DB
        await session.execute(text(
            "TRUNCATE tenants, users, user_roles, access_grants, matrix_versions, "
            "access_audit RESTART IDENTITY CASCADE"))
        await session.commit()
    await dispose_engine()
    clear_tenant_cache()

    assert await access_seed.bootstrap_if_empty() == 0     # first boot: seeds
    init_engine(get_settings())
    sm = get_sessionmaker()
    async with sm() as session:
        tenants = (await session.execute(text("SELECT count(*) FROM tenants"))).scalar()
        admins = (await session.execute(text(
            "SELECT count(*) FROM users WHERE email = 'admin@evamfinance.com'"))).scalar()
        cells = (await session.execute(text("SELECT count(*) FROM access_grants"))).scalar()
        assert (tenants, admins) == (1, 1) and cells > 0
        await session.execute(text("UPDATE tenants SET name = 'Renamed by operator'"))
        # Simulate a default operator account added to config AFTER first boot: drop it
        # so the next start has to provision it into the non-empty database.
        await session.execute(text(
            "DELETE FROM user_roles WHERE user_id IN "
            "(SELECT id FROM users WHERE email = 'tech@evamfinance.com')"))
        await session.execute(text(
            "DELETE FROM users WHERE email = 'tech@evamfinance.com'"))
        # Simulate a LONG-RUNNING database from before the visibility layer shipped:
        # one cell back to the spec's SCOPED (origin baseline), one narrowed by an
        # Admin (origin override). The next start must refresh the first and leave
        # the second exactly as the Admin set it.
        await session.execute(text(
            "UPDATE access_grants SET access='SCOPED', origin='baseline' "
            "WHERE kind='view' AND item='leads' AND role='AM RM'"))
        await session.execute(text(
            "UPDATE access_grants SET access='SCOPED', origin='override' "
            "WHERE kind='view' AND item='deals' AND role='Syn RM'"))
        await session.commit()
    await dispose_engine()

    assert await access_seed.bootstrap_if_empty() in (0, 3)  # non-empty: report only
    init_engine(get_settings())
    sm = get_sessionmaker()
    async with sm() as session:                       # the operator's edit SURVIVED
        name = (await session.execute(text("SELECT name FROM tenants"))).scalar()
        assert name == "Renamed by operator"
        # ...while the missing DEFAULT admin user was provisioned additively.
        tech = (await session.execute(text(
            "SELECT count(*) FROM users WHERE email = 'tech@evamfinance.com'"))).scalar()
        assert tech == 1
        tech_roles = (await session.execute(text(
            "SELECT role FROM user_roles WHERE user_id IN "
            "(SELECT id FROM users WHERE email = 'tech@evamfinance.com')"))).scalars().all()
        assert list(tech_roles) == ["Admin"]
        # ...and the visibility layer re-applied to the stale baseline cell, while the
        # Admin's own override stayed exactly as the Admin left it.
        leads_amrm = (await session.execute(text(
            "SELECT access FROM access_grants "
            "WHERE kind='view' AND item='leads' AND role='AM RM'"))).scalar()
        deals_synrm = (await session.execute(text(
            "SELECT access, origin FROM access_grants "
            "WHERE kind='view' AND item='deals' AND role='Syn RM'"))).first()
        assert leads_amrm == "READ", "the shipped layer must apply on a non-empty start"
        assert tuple(deals_synrm) == ("SCOPED", "override"), "an Admin override survives"
        await session.execute(text("UPDATE tenants SET name = 'Evam Finance'"))
        await session.commit()
    await dispose_engine()
    clear_tenant_cache()


async def test_a_revoked_role_can_be_granted_back(client: AsyncClient):
    """The dead end the desk hit restoring a deactivated admin. Revocation soft-deletes
    the user_roles row, but user_roles_unique covers deleted rows too — so a blind
    INSERT on re-grant answered 409 for any role the user EVER held. The grant must
    restore the buried row instead, and the audit shows revoke and re-grant both."""
    r = await client.post("/v1/users", headers=ADMIN, json={
        "email": "regrant@evamfinance.com", "full_name": "Regrant Test",
        "roles": ["Admin"]})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert (await client.delete(f"/v1/users/{uid}/roles/Admin",
                                headers=ADMIN)).status_code == 200
    # The whole point: granting it back is a restore, not a constraint violation.
    r = await client.post(f"/v1/users/{uid}/roles", headers=ADMIN, json={"role": "Admin"})
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == ["Admin"]
    # A LIVE duplicate is still refused — the restore path must not eat the 409.
    assert (await client.post(f"/v1/users/{uid}/roles", headers=ADMIN,
                              json={"role": "Admin"})).status_code == 409


async def test_the_visibility_layer_is_seeded_and_survives_reseeding(client):
    """Firm-wide visibility ships with the build: the seed holds the VISIBILITY_READ
    view cells at READ on every start — which is how an `upgrade` applies the policy
    to a long-running database — while an Admin's own override always wins."""
    from sqlalchemy import select

    from app.matrix import VISIBILITY_READ

    r = await client.get("/v1/access")
    assert r.status_code == 200, r.text
    views = r.json()["views"]
    for item, role in VISIBILITY_READ:
        assert views[item][role] == "READ", f"{item}×{role} should seed to READ"

    # An Admin decision beats the shipped layer: override one cell back to SCOPED,
    # re-run the seed (exactly what the next service start does), and the override
    # stands — origin='override' rows are never refreshed.
    edit = await client.patch("/v1/access", json={
        "kind": "view", "item": "deals", "role": "BDRM", "access": "SCOPED"})
    assert edit.status_code == 200, edit.text

    from evam_backend_core.db.session import get_sessionmaker

    from app.matrix import seed_matrix
    from app.models import Tenant

    sm = get_sessionmaker()
    async with sm() as session:
        tid = (await session.execute(select(Tenant.id).where(Tenant.code == "EVAM"))
               ).scalar_one()
        await seed_matrix(session, tid)
        await session.commit()

    views = (await client.get("/v1/access")).json()["views"]
    assert views["deals"]["BDRM"] == "SCOPED", "the Admin override must survive a re-seed"
    for item, role in VISIBILITY_READ:
        if (item, role) == ("deals", "BDRM"):
            continue
        assert views[item][role] == "READ"
