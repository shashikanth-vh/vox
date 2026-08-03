"""CLI: ``python -m app.seed.bootstrap`` — provision an EMPTY-but-usable Register.

This creates only the things the API needs to *function*, not business data:

* the default **tenant** (so tenant-scoped requests are permitted instead of 403), and
* the **reference vocabularies** (dropdown values for `/v1/ref`, sector/stage/lens/… ).

No leads, deals, entities or any other business rows are loaded. Run it once against a
fresh database so you can start creating real records through the API immediately:

    python -m app.seed.bootstrap
    python -m app.seed.bootstrap --tenant-code EVAM --tenant-name "Evam Finance"

Idempotent: safe to run repeatedly (tenant + ref values upsert, nothing is duplicated).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.seed.checklist import seed_document_checklist
from app.seed.loader import ensure_tenant, seed_ref_values

log = get_logger("bootstrap")


def _opt(name: str, default: str) -> str:
    """Read ``--name value`` from argv, else return the default."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


async def run(tenant_code: str, tenant_name: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, tenant_code, tenant_name)
        # Scope writes to this tenant for RLS.
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(tenant_id)}
        )
        ref_added = await seed_ref_values(session)
        checklist_added = await seed_document_checklist(session, tenant_id)
        await session.commit()
    log.info("bootstrap complete: tenant=%s (%s), ref_values~%d, checklist=+%d",
             tenant_code, tenant_id, ref_added, checklist_added)
    print(f"Bootstrap complete. Tenant '{tenant_code}' ready ({tenant_id}). "
          f"Reference values reconciled: {ref_added}, checklist items added: {checklist_added}. "
          f"No business data loaded. (Users live in the Access service.)")
    await dispose_engine()


def main() -> None:
    settings = get_settings()
    tenant_code = _opt("--tenant-code", settings.default_tenant_code)
    tenant_name = _opt("--tenant-name", "Evam Finance")
    asyncio.run(run(tenant_code, tenant_name))


if __name__ == "__main__":
    main()
