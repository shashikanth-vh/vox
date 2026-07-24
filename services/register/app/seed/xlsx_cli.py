"""CLI: load the ATLAS MIS xlsx into the Register.

    python -m app.seed.xlsx_cli data/Evam_ATLAS_MIS_Consolidated_v4.xlsx           # replace
    python -m app.seed.xlsx_cli <path> --no-truncate                               # merge
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.models import Entity
from app.seed.from_xlsx import import_workbook
from app.seed.loader import ensure_tenant

log = get_logger("xlsx-import")


async def run(path: str, truncate: bool, if_empty: bool) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    init_engine(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_id = await ensure_tenant(session, settings.default_tenant_code, "Evam Finance")
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, false)"), {"t": str(tenant_id)}
        )
        # --if-empty: skip when the tenant already has data, so container restarts never
        # wipe live edits. First boot (empty DB) loads; later boots preserve.
        if if_empty:
            n = (await session.execute(
                select(func.count()).select_from(Entity).where(Entity.tenant_id == tenant_id)
            )).scalar_one()
            if n > 0:
                print(f"Data already present ({n} entities) — skipping MIS import "
                      f"(--if-empty). Data preserved.")
                await dispose_engine()
                return
        counts = await import_workbook(session, tenant_id, path, truncate=truncate)
        await session.commit()
    print(f"Imported {path} ({'replace' if truncate else 'merge'} mode):")
    for k, v in counts.items():
        print(f"  {k:22s}: {v}")
    await dispose_engine()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python -m app.seed.xlsx_cli <path.xlsx> [--no-truncate] [--if-empty]")
        raise SystemExit(2)
    truncate = "--no-truncate" not in sys.argv
    if_empty = "--if-empty" in sys.argv
    asyncio.run(run(args[0], truncate, if_empty))


if __name__ == "__main__":
    main()
