"""Database-level, two-tenant proof that row-level security is FAIL-CLOSED.

RLS is normally bypassed by the table owner, so this test FORCEs it on a single table
(``entities``) for the duration and connects as that owner to prove the policy binds even
then. It asserts three properties the audit required:

* **fail-closed** — with no ``app.current_tenant`` set, a SELECT returns ZERO rows
  (the old policy's NULL escape returned the whole table);
* **isolation** — with the GUC set to tenant A, only A's rows are visible;
* **WITH CHECK** — an INSERT for tenant B while the GUC says A is REJECTED.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine

pytestmark = pytest.mark.asyncio


async def test_app_crud_works_under_forced_rls(client: AsyncClient):
    """Deployability check: with FORCE RLS on (the production posture), the app's normal
    per-request pattern — set app.current_tenant transaction-locally, then read/write —
    still works. This is what proves RLS is *deployable*, not just fail-closed: the API
    keeps functioning when the owner no longer bypasses the policy."""
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(text("ALTER TABLE entities FORCE ROW LEVEL SECURITY"))
        await s.commit()
    try:
        created = await client.post("/v1/entities",
                                    json={"code": "RLSAPP", "legal_name": "RLS App Co"})
        assert created.status_code == 201, created.text
        got = await client.get(f"/v1/entities/{created.json()['id']}")
        assert got.status_code == 200 and got.json()["code"] == "RLSAPP"
        listed = await client.get("/v1/entities", params={"with_total": True})
        assert listed.json()["total"] >= 1
    finally:
        async with sm() as s:
            await s.execute(text("ALTER TABLE entities NO FORCE ROW LEVEL SECURITY"))
            await s.commit()


_RLS_ROLE = "rls_probe_role"


async def test_rls_is_fail_closed_and_tenant_isolating():
    clear_tenant_cache()
    init_engine(get_settings())
    sm = get_sessionmaker()

    # A SUPERUSER (and BYPASSRLS role) is exempt from RLS even with FORCE, so a direct
    # connection can't prove the boundary. Rather than SKIP under such a role (e.g. the CI
    # Postgres superuser), we create a dedicated NON-superuser, NOBYPASSRLS probe role and
    # run every boundary assertion under `SET LOCAL ROLE` as that role — so RLS is PROVEN
    # in exactly the environments (CI) where the old test used to skip.
    async with sm() as s:
        is_super = (await s.execute(text(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))).scalar_one()
        can_make_role = (await s.execute(text(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"))).scalar_one()

    # Use the probe role when the login role is exempt (superuser/bypass) AND we may create
    # roles. When the login role is already a non-owner non-superuser (the local sandbox),
    # RLS binds it directly and no probe role is needed.
    use_role = bool(is_super and can_make_role)
    if is_super and not can_make_role:
        pytest.skip("superuser login without CREATEROLE — cannot build a probe role to "
                    "prove RLS; run as a role that is non-superuser or can create roles.")

    async def enter_probe(s) -> None:  # noqa: ANN001
        if use_role:
            await s.execute(text(f"SET LOCAL ROLE {_RLS_ROLE}"))

    a_code, b_code = "RLSA", "RLSB"
    try:
        # Seed two tenants + one entity each (as the OWNER, before FORCE), and — when
        # needed — a NOBYPASSRLS probe role with the DML grants a runtime role would hold.
        async with sm() as s:
            if use_role:
                await s.execute(text(f"""
                    DO $$ BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RLS_ROLE}')
                        THEN CREATE ROLE {_RLS_ROLE} NOLOGIN NOBYPASSRLS; END IF;
                    END $$;"""))  # noqa: S608 - _RLS_ROLE is a fixed constant, not input
                await s.execute(text(  # noqa: S608 - _RLS_ROLE is a fixed constant
                    f"GRANT SELECT, INSERT ON entities, tenants TO {_RLS_ROLE}"))
            ids: dict[str, uuid.UUID] = {}
            for code, name in ((a_code, "A Co"), (b_code, "B Co")):
                await s.execute(text(
                    "INSERT INTO tenants (code, name, is_active) VALUES (:c, :n, true) "
                    "ON CONFLICT (code) DO NOTHING"), {"c": code, "n": name})
                ids[code] = (await s.execute(
                    text("SELECT id FROM tenants WHERE code = :c"), {"c": code})).scalar_one()
            for code in (a_code, b_code):
                await s.execute(text(
                    "INSERT INTO entities (tenant_id, code, legal_name, entity_type) "
                    "VALUES (:t, :code, :name, 'Company')"),
                    {"t": str(ids[code]), "code": f"{code}-CO", "name": f"{code} Co"})
            await s.execute(text("ALTER TABLE entities FORCE ROW LEVEL SECURITY"))
            await s.commit()

        # 1) fail-closed: no tenant context → nothing visible.
        async with sm() as s:
            await enter_probe(s)
            n = (await s.execute(
                text("SELECT count(*) FROM entities WHERE code IN "
                     "('RLSA-CO','RLSB-CO')"))).scalar_one()
            assert n == 0, "RLS is NOT fail-closed: rows leaked with no tenant set"

        # 2) isolation: tenant A sees only A.
        async with sm() as s:
            await enter_probe(s)
            await s.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                            {"t": str(ids[a_code])})
            codes = set((await s.execute(
                text("SELECT code FROM entities WHERE code LIKE 'RLS%'"))).scalars().all())
            assert codes == {"RLSA-CO"}, codes

        # 3) WITH CHECK: writing tenant B's row while acting as A is rejected.
        async with sm() as s:
            await enter_probe(s)
            await s.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                            {"t": str(ids[a_code])})
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                await s.execute(text(
                    "INSERT INTO entities (tenant_id, code, legal_name, entity_type) "
                    "VALUES (:t, 'RLSX-CO', 'X', 'Company')"), {"t": str(ids[b_code])})
                await s.flush()
    finally:
        async with sm() as s:
            await s.execute(text("ALTER TABLE entities NO FORCE ROW LEVEL SECURITY"))
            await s.execute(text("DELETE FROM entities WHERE code LIKE 'RLS%'"))
            await s.execute(text("DELETE FROM tenants WHERE code IN ('RLSA','RLSB')"))
            if use_role:
                await s.execute(text(f"REVOKE ALL ON entities, tenants FROM {_RLS_ROLE}"))
                await s.execute(text(f"DROP ROLE IF EXISTS {_RLS_ROLE}"))
            await s.commit()
        await dispose_engine()


_APP_PW = "register_app_login_pw:with'quote"  # noqa: S105 - also exercises the escaping path


async def test_apply_rls_provisions_login_and_is_tenant_isolated(monkeypatch):
    """The REAL deploy path end to end: run ``apply_rls.apply()`` itself (the same code the
    migrate Job runs) with REGISTER_APP_PASSWORD set — it creates the actual ``register_app``
    role, grants it, sets its LOGIN password (via the fixed inline-literal ALTER ROLE), and
    FORCEs RLS. Then LOG IN as register_app and prove tenant-isolated reads/writes. This
    exercises the bootstrap + the ALTER ROLE … PASSWORD path the reviewer flagged, not a
    hand-made probe role. Skips only where the connecting role cannot create roles."""
    from app.db import apply_rls

    clear_tenant_cache()
    settings = get_settings()
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as s:
        can_make_role = (await s.execute(text(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"))).scalar_one()
    if not can_make_role:
        pytest.skip("connecting role cannot CREATE ROLE — run where register_app can be "
                    "provisioned (CI superuser) to exercise the real bootstrap + login.")

    # Drive apply_rls exactly as the migrate Job does: password in the env, RLS enforced.
    monkeypatch.setenv("REGISTER_APP_PASSWORD", _APP_PW)
    monkeypatch.setattr(settings, "enforce_rls", True)
    async with sm() as s:
        await s.execute(text(
            "INSERT INTO tenants (code, name, is_active) VALUES ('RLSLGN','Login Co',true) "
            "ON CONFLICT (code) DO NOTHING"))
        tid = (await s.execute(
            text("SELECT id FROM tenants WHERE code = 'RLSLGN'"))).scalar_one()
        await s.commit()

    await apply_rls.apply()          # <-- the real bootstrap under test
    await dispose_engine()           # apply() left its own engine; start clean below

    dsn = (f"postgresql+asyncpg://register_app:{_APP_PW}@{settings.db_host}:"
           f"{settings.db_port}/{settings.db_name}")
    app_engine = create_async_engine(dsn)
    try:
        async with app_engine.begin() as c:
            # apply_rls must have converged the runtime role to a SAFE shape (both false),
            # else RLS would be silently bypassed.
            super_, bypass = (await c.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"))).one()
            assert super_ is False and bypass is False
            # Least privilege converged AND verified: after apply_rls (which grants DML on
            # ALL tables, then re-revokes on append-only ones), register_app must hold NEITHER
            # UPDATE nor DELETE on the append-only decision table.
            for priv in ("UPDATE", "DELETE"):
                held = (await c.execute(text(
                    "SELECT has_table_privilege(current_user, 'workflow_decisions', :p)"),
                    {"p": priv})).scalar()
                assert held is False, f"register_app must not hold {priv} on workflow_decisions"
            await c.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                            {"t": str(tid)})
            await c.execute(text(
                "INSERT INTO entities (tenant_id, code, legal_name, entity_type) "
                "VALUES (:t, 'RLSLGN-CO', 'Login Co', 'Company')"), {"t": str(tid)})
            assert (await c.execute(
                text("SELECT code FROM entities WHERE code = 'RLSLGN-CO'"))
            ).scalar_one() == "RLSLGN-CO"
        async with app_engine.begin() as c:
            n = (await c.execute(
                text("SELECT count(*) FROM entities WHERE code = 'RLSLGN-CO'"))).scalar_one()
            assert n == 0, "runtime role saw a row with no tenant context — RLS not binding"
    finally:
        await app_engine.dispose()
        # Restore: un-FORCE every table apply() touched (else later tests, connecting as the
        # OWNER, would find RLS forced on them), delete rows, drop the role.
        init_engine(settings)
        sm2 = get_sessionmaker()
        async with sm2() as s:
            for tbl in apply_rls.TENANT_TABLES:
                await s.execute(text(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY"))
            await s.execute(text("DELETE FROM entities WHERE code = 'RLSLGN-CO'"))
            await s.execute(text("DELETE FROM tenants WHERE code = 'RLSLGN'"))
            # DROP OWNED BY clears every grant + default privilege apply() gave the role
            # (table, sequence AND default privileges), so the role drops cleanly.
            await s.execute(text("DROP OWNED BY register_app"))
            await s.execute(text("DROP ROLE IF EXISTS register_app"))
            await s.commit()
        await dispose_engine()
