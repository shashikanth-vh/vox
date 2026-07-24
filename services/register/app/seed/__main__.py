"""CLI: ``python -m app.seed`` — load reference data + the ATLAS mock into the Register."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.seed.loader import ensure_tenant, load_data_file, seed_all, seed_ref_values

log = get_logger("seed")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    data = load_data_file()

    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, settings.default_tenant_code, "Evam Finance")
        # Scope writes to this tenant for RLS.
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(tenant_id)}
        )
        ref_added = await seed_ref_values(session)
        counts = await seed_all(session, data, tenant_id)
        await session.commit()

    log.info("seed complete: ref_values=+%d, %s", ref_added, counts)
    print(f"Seed complete. ref_values added: {ref_added}")
    for k, v in counts.items():
        print(f"  {k:22s}: {v}")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
