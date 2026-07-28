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
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine

pytestmark = pytest.mark.asyncio


async def test_rls_is_fail_closed_and_tenant_isolating():
    clear_tenant_cache()
    init_engine(get_settings())
    sm = get_sessionmaker()

    a_code, b_code = "RLSA", "RLSB"
    try:
        # Seed two tenants + one entity each (as owner, before FORCE).
        async with sm() as s:
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
            n = (await s.execute(
                text("SELECT count(*) FROM entities WHERE code IN "
                     "('RLSA-CO','RLSB-CO')"))).scalar_one()
            assert n == 0, "RLS is NOT fail-closed: rows leaked with no tenant set"

        # 2) isolation: tenant A sees only A.
        async with sm() as s:
            await s.execute(text("SELECT set_config('app.current_tenant', :t, true)"),
                            {"t": str(ids[a_code])})
            codes = set((await s.execute(
                text("SELECT code FROM entities WHERE code LIKE 'RLS%'"))).scalars().all())
            assert codes == {"RLSA-CO"}, codes

        # 3) WITH CHECK: writing tenant B's row while acting as A is rejected.
        async with sm() as s:
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
            await s.commit()
        await dispose_engine()
