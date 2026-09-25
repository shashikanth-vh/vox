"""Bulk prospect-list import — the CLI door onto the SAME engine as the Tools dialog.

The interactive door (Tools → Prospects → Import) is self-service and needs
nobody from tech; this CLI exists for bulk first loads and scripted refreshes.
Same engine (app.seed.prospects_xlsx), same merge policy, same report — the two
doors cannot drift.

DRY-RUN by default — prints the plan. ``--apply`` executes and audits.

Run inside the register container:
    docker exec compose-register-1 python -m app.maintenance.import_prospects /data/lists
    docker exec compose-register-1 python -m app.maintenance.import_prospects \
        /data/lists --apply --tenant EVAM
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib

from sqlalchemy import select

ACTOR = "maintenance.import_prospects"


async def run(paths: list[str], apply: bool, tenant_code: str) -> None:
    from app.db.base import AuditLog
    from app.db.session import get_sessionmaker, init_engine
    from app.models import Tenant
    from app.seed.prospects_xlsx import apply_plan, build_plan, summarize_plan

    files: list[tuple[str, bytes, str | None]] = []
    for raw in paths:
        p = pathlib.Path(raw)
        candidates = sorted(p.glob("*.xlsx")) if p.is_dir() else [p]
        for f in candidates:
            if f.name.startswith("~$"):
                continue
            files.append((f.name, f.read_bytes(), None))
    if not files:
        print("no .xlsx files found")
        return

    init_engine()
    sm = get_sessionmaker()
    async with sm() as session:
        tenant = (await session.execute(
            select(Tenant).where(Tenant.code == tenant_code))).scalar_one_or_none()
        if tenant is None:
            print(f"no tenant with code {tenant_code!r}")
            return
        plan = await build_plan(session, tenant.id, files)
        summary = summarize_plan(plan)
        for fr in summary["files"]:
            print(f"file: {fr['file']:55s} vertical={fr['vertical'] or '?':14s} "
                  f"sheet={fr['sheet']!r} rows={fr['rows']}")
        c = summary["counts"]
        print(f"\nnew: {c['new']} · merged: {c['merged']} · "
              f"in-file duplicates: {c['in_file_duplicates']} · "
              f"skipped: {c['skipped']} · conflicts: {c['conflicts']}")
        for s in summary["skipped"]:
            print(f"  SKIPPED  {s['file']} row {s['row']}: {s['reason']}")
        for cf in summary["conflicts"]:
            print(f"  CONFLICT {cf['name']} [{cf['field']}]: "
                  f"register={cf['existing']!r} file={cf['incoming']!r} (kept register)")
        if not apply:
            await session.rollback()
            print("\n(dry run — nothing written; --apply to execute)")
            return
        result = await apply_plan(session, tenant.id, ACTOR, plan)
        session.add(AuditLog(
            tenant_id=tenant.id, actor=ACTOR, action="prospects.import",
            resource_type="prospects", resource_id=plan["batch"],
            changes={"files": summary["files"], "counts": c,
                     "created": result["created"][:200],
                     "merged": result["merged"][:200]}))
        await session.commit()
        print(f"\napplied: created {len(result['created'])} · "
              f"updated {len(result['merged'])} · batch {plan['batch'][:12]}…")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help=".xlsx files or directories of them")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tenant", default="EVAM")
    args = ap.parse_args()
    asyncio.run(run(args.paths, args.apply, args.tenant))
