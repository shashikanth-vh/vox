"""Company-360 market feed — Tracxn, CIN-anchored, cached, served normalised.

``GET /v1/panorama/financials?entity_id=…`` (or ``?cin=…``) lights up the 360's
FINANCIALS card with what Tracxn has filed for the company: the revenue /
EBITDA / net-profit / valuation time series, employee counts, balance-sheet and
cash-flow series, the board, and the cap table.

The rules that keep it honest and affordable:

* **CIN is the anchor** — the same identity the register keeps on masters,
  prospects and (now) leads. No CIN on record → a 409 that says where to add
  it, never a fuzzy name-match against a paid API.
* **Every answer is cached** per (tenant, CIN, endpoint) and served until it
  ages out (``REGISTER_TRACXN_CACHE_DAYS``) or ``refresh=true`` forces the
  spend. Payloads are stored RAW; normalisation happens on read, so a mapping
  fix never refetches what was already paid for.
* **Series values ride "as reported"** — Tracxn's own units and currency are
  surfaced beside each series rather than silently converted. A wrong crore
  is worse than a labelled unknown.
* **Each external fetch batch is audited** (action ``tracxn.fetch``).
* Not configured (no token) → 409, and the UI keeps its "soon" card.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import Depends, Query
from sqlalchemy import select

from app.core.errors import ValidationAppError
from app.core.logging import request_id_ctx
from app.core.router import api_router
from app.core.security import RequestContext, get_context
from app.db.base import AuditLog

router = api_router(tags=["Company 360"])

# endpoint-key → (path, kind). Kinds drive normalisation.
_ENDPOINTS: dict[str, tuple[str, str]] = {
    "legalentity": ("/legalentities", "resolve"),
    "board": ("/boardmembers", "board"),
    "captable": ("/captables", "captable"),
    "revenue": ("/companies/timeseries/revenue", "series"),
    "ebitda": ("/companies/timeseries/ebitda", "series"),
    "net_profit": ("/companies/timeseries/netprofit", "series"),
    "valuation": ("/companies/timeseries/valuation", "series"),
    "employees": ("/companies/timeseries/employeecountannualreport", "series"),
    "employees_labour": ("/companies/timeseries/employeecountlabourfilings", "series"),
    "bs_equity": ("/companies/timeseries/balancesheetequity", "series"),
    "bs_liability": ("/companies/timeseries/balancesheetliability", "series"),
    "bs_assets": ("/companies/timeseries/balancesheetassets", "series"),
    "cf_operating": ("/companies/timeseries/cashflowoperatingactivity", "series"),
    "cf_investing": ("/companies/timeseries/cashflowinvestingactivity", "series"),
    "cf_financing": ("/companies/timeseries/cashflowfinancingactivity", "series"),
}

_YEAR_KEYS = ("year", "financialYear", "fiscalYear", "fy", "calendarYear",
              "asOnYear", "date", "asOnDate")
_VALUE_KEYS = ("value", "amount", "count", "revenue", "ebitda", "netProfit",
               "valuation", "employeeCount", "total")
_UNIT_KEYS = ("currency", "unit", "currencyCode", "denomination")
_NAME_KEYS = ("name", "fullName", "shareholderName", "investorName", "title")
_PCT_KEYS = ("percentage", "pct", "holdingPercentage", "stake",
             "shareholdingPercentage", "percentageHolding")


async def _tracxn_post(settings: Any, path: str, body: dict) -> dict:
    """One Tracxn call. Isolated so tests stub the wire, not the logic. Never
    raises: an unreachable host or a refused call is an answer with ``_error``,
    so the card can say what happened instead of the register answering 500."""
    timeout = httpx.Timeout(float(settings.tracxn_timeout_s), connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{settings.tracxn_base_url.rstrip('/')}{path}",
                headers={"accessToken": settings.tracxn_access_token,
                         "Content-Type": "application/json"},
                json=body)
    except httpx.HTTPError as exc:
        return {"_error": f"unreachable ({exc.__class__.__name__})"}
    if r.status_code >= 300:
        return {"_error": f"HTTP {r.status_code}"}
    try:
        return r.json()
    except ValueError:
        return {"_error": "non-JSON answer"}


def _period_of(row: dict) -> tuple[int | None, str | None]:
    """(year, label) of a Tracxn timeseries row. Filed financials come as a
    range — Indian FY Apr 2025–Mar 2026 → year 2026, label "FY 2025-26"; a
    point-in-time row carries ``timestamp.date``. Flat ``year``-style keys
    still count, for any endpoint that answers that way."""
    ts = row.get("timestamp")
    if isinstance(ts, dict):
        rng = ts.get("dateRange") if isinstance(ts.get("dateRange"), dict) else None
        if rng:
            end = rng.get("end") if isinstance(rng.get("end"), dict) else {}
            start = rng.get("start") if isinstance(rng.get("start"), dict) else {}
            y_end, y_start = end.get("year"), start.get("year")
            if isinstance(y_end, int):
                label = (f"FY {y_start}-{str(y_end)[-2:]}"
                         if isinstance(y_start, int) and y_start != y_end else str(y_end))
                return y_end, label
            if isinstance(y_start, int):
                return y_start, str(y_start)
        for k in ("date", "asOn", "asOnDate"):
            d = ts.get(k)
            if isinstance(d, dict) and isinstance(d.get("year"), int):
                return d["year"], str(d["year"])
    y = _year_of(row)
    return y, (str(y) if y is not None else None)


def _year_of(row: dict) -> int | None:
    for k in _YEAR_KEYS:
        v = row.get(k)
        if v is None:
            continue
        s = str(v)
        for token in (s[:4], s[-4:]):
            if token.isdigit() and 1900 < int(token) < 2100:
                return int(token)
    return None


_INR_PER_CRORE = 10_000_000.0


def _money_of(row: dict) -> tuple[float | None, str | None]:
    """(value, unit) of a row. Tracxn files money as ``data.amount.value`` in
    whole currency units with ``data.currency``; INR is shown in crore because
    that is the unit every other number in PRISM wears. Counts (employees)
    come as ``data.value``/``data.count`` and stay as they are."""
    data = row.get("data")
    if isinstance(data, dict):
        amt = data.get("amount")
        if isinstance(amt, dict) and isinstance(amt.get("value"), (int, float)):
            cur = data.get("currency") or amt.get("currency")
            v = float(amt["value"])
            if cur == "INR":
                return v / _INR_PER_CRORE, "₹ Cr"
            return v, str(cur) if cur else None
        for k in ("value", "count", "employeeCount", "total"):
            if isinstance(data.get(k), (int, float)):
                return float(data[k]), data.get("unit")
    return _value_of(row), None


def _value_of(row: dict) -> float | None:
    for k in _VALUE_KEYS:
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for kk in _VALUE_KEYS:
                vv = v.get(kk)
                if isinstance(vv, (int, float)):
                    return float(vv)
    return None


def _unit_of(rows: list[dict]) -> str | None:
    for row in rows:
        for k in _UNIT_KEYS:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize(kind: str, payload: dict | None) -> dict:
    """Raw Tracxn payload → the shape the card renders. Defensive by design:
    unknown layouts degrade to empty series with the raw row count noted,
    never to a 500."""
    if not payload:
        return {"points": [], "note": "no answer"}
    if payload.get("_error"):
        return {"points": [], "note": f"unavailable ({payload['_error']})"}
    rows = payload.get("result")
    if not isinstance(rows, list):
        return {"points": [], "note": "unrecognised answer shape"}

    if kind == "series":
        pts: dict[int, tuple[float, str | None]] = {}
        unit: str | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            y, label = _period_of(row)
            v, u = _money_of(row)
            if y is None or v is None:
                continue
            unit = unit or u
            # Tracxn may file the same year twice (restated); the LAST row wins.
            pts[y] = (v, label)
        return {"points": [{"year": y, "value": pts[y][0], "fy": pts[y][1]}
                           for y in sorted(pts)],
                "unit": unit or _unit_of([r for r in rows if isinstance(r, dict)]),
                "rows": len(rows)}

    if kind == "board":
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            person = row.get("people") if isinstance(row.get("people"), dict) else row
            name = _pick(person, _NAME_KEYS) or _pick(row, _NAME_KEYS)
            if not name:
                continue
            out.append({"name": str(name),
                        "designation": _pick(row, ("designation", "role", "position")),
                        "since": _pick(row, ("from", "since", "appointedOn",
                                             "startDate"))})
        return {"members": out[:12], "rows": len(rows)}

    if kind == "captable":
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            holders = row.get("shareholders") if isinstance(
                row.get("shareholders"), list) else [row]
            for h in holders:
                if not isinstance(h, dict):
                    continue
                name = _pick(h, _NAME_KEYS)
                pct = _pick(h, _PCT_KEYS)
                if name is not None and isinstance(pct, (int, float)):
                    out.append({"name": str(name), "pct": float(pct)})
        out.sort(key=lambda x: -x["pct"])
        return {"holders": out[:10], "rows": len(rows)}

    # resolve
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            cur = row.get("currentName") if isinstance(row.get("currentName"), dict) else {}
            inc = row.get("incorporationDate") if isinstance(
                row.get("incorporationDate"), dict) else {}
            loc = row.get("location") if isinstance(row.get("location"), dict) else {}
            country = (loc.get("country") or {}).get("name") if isinstance(
                loc.get("country"), dict) else None
            incorporated = None
            if isinstance(inc.get("year"), int):
                incorporated = (f"{inc['year']:04d}-{int(inc.get('month') or 1):02d}-"
                                f"{int(inc.get('day') or 1):02d}")
            return {"legal_entity_id": str(row["id"]),
                    "name": cur.get("name") or _pick(row, ("name", "legalName",
                                                           "companyName")),
                    "incorporated": incorporated,
                    "status": row.get("status"),
                    "country": country,
                    "company_ids": [str(x) for x in (row.get("companyIds") or [])
                                    if x] or None,
                    "rows": len(rows)}
    return {"legal_entity_id": None, "rows": len(rows)}


_PLACEHOLDER_CIN_STEM = "U40106KA2015PTC"


def real_cin(entity: Any) -> str | None:
    """The master's CIN, unless it is the placeholder older ATLAS builds stamped
    on every UI-created client: the fixed stem plus the SAME six digits that
    end the entity code (``NAME-123456`` ↔ ``U40106KA2015PTC123456``). That
    pairing is exact, so a genuine CIN is never mistaken for one — and a
    placeholder is never sent to a paid API."""
    cin = (getattr(entity, "cin", None) or "").strip()
    if not cin:
        return None
    code = getattr(entity, "code", None) or ""
    tail = code.rsplit("-", 1)[-1] if "-" in code else ""
    if tail.isdigit() and len(tail) == 6 and cin == f"{_PLACEHOLDER_CIN_STEM}{tail}":
        return None
    return cin


async def _resolve_cin(ctx: RequestContext, entity_id: str | None,
                       cin: str | None) -> str:
    from app.models import Entity, Lead, Prospect

    if cin and cin.strip():
        return cin.strip()
    if not entity_id:
        raise ValidationAppError("Pass entity_id or cin.")
    ent = (await ctx.session.execute(select(Entity).where(
        Entity.tenant_id == ctx.tenant_id, Entity.id == entity_id,
        Entity.deleted_at.is_(None)))).scalar_one_or_none()
    if ent is not None and real_cin(ent):
        return real_cin(ent)
    # A CIN the desk typed on a lead AFTER the master existed never reaches the
    # master (birth-only copy) — but it is still this company's CIN.
    lc = (await ctx.session.execute(select(Lead.cin).where(
        Lead.tenant_id == ctx.tenant_id, Lead.deleted_at.is_(None),
        Lead.entity_id == entity_id, Lead.cin.is_not(None), Lead.cin != "")
        .order_by(Lead.updated_at.desc()).limit(1))).scalar_one_or_none()
    if lc:
        return lc.strip()
    # The prospect universe may know the CIN even when the master does not.
    p = (await ctx.session.execute(select(Prospect.cin).where(
        Prospect.tenant_id == ctx.tenant_id, Prospect.deleted_at.is_(None),
        Prospect.entity_id == entity_id,
        Prospect.cin.is_not(None)).limit(1))).scalar_one_or_none()
    if p:
        return p
    from app.core.errors import ConflictError

    raise ConflictError(
        "No CIN on record for this company — add it on the client master, the "
        "lead, or the prospect row, then try again.")


@router.get("/v1/panorama/financials",
            summary="Company 360 market feed (Tracxn) — cached, CIN-anchored")
async def panorama_financials(
    ctx: RequestContext = Depends(get_context),
    entity_id: str | None = Query(default=None),
    cin: str | None = Query(default=None, max_length=40),
    refresh: bool = Query(default=False,
                          description="Force a refetch past the cache (spends "
                                      "Tracxn calls)."),
    cached_only: bool = Query(default=False,
                              description="Never call Tracxn: answer from the "
                                          "cache whatever its age, or say nothing "
                                          "is cached. The 360 opens this way — a "
                                          "fetch is always someone's decision."),
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.core.errors import ConflictError
    from app.models import TracxnCache

    if ctx.user is None:
        raise ValidationAppError("The market feed needs a signed-in identity.")
    settings = get_settings()
    if not settings.tracxn_access_token:
        raise ConflictError(
            "The market feed is not configured — set TRACXN_ACCESS_TOKEN in "
            "deploy/compose/.env and restart the register.")

    the_cin = await _resolve_cin(ctx, entity_id, cin)
    ttl = timedelta(days=max(int(settings.tracxn_cache_days), 1))
    now = datetime.now(UTC)

    cached = {c.endpoint: c for c in (await ctx.session.execute(
        select(TracxnCache).where(TracxnCache.tenant_id == ctx.tenant_id,
                                  TracxnCache.cin == the_cin))).scalars()}
    if cached_only:
        if "legalentity" not in cached:
            return {"cin": the_cin, "configured": True, "resolved": False,
                    "cached": False, "fetched_now": 0,
                    "note": "Not fetched from Tracxn yet.",
                    "endpoints": len(_ENDPOINTS)}
        # Serve the larder whatever its age: age is shown, spending is explicit.
        ttl = timedelta(days=36500)

    async def get_payload(key: str, body: dict) -> tuple[dict | None, bool]:
        row = cached.get(key)
        if not refresh and row is not None and row.fetched_at is not None \
                and (now - row.fetched_at) < ttl:
            return row.payload, False
        payload = await _tracxn_post(settings, _ENDPOINTS[key][0], body)
        # An error is not a filing: it is never kept, so the next open asks
        # again instead of serving "unreachable" for a week.
        if isinstance(payload, dict) and payload.get("_error"):
            return payload, True
        if row is None:
            row = TracxnCache(tenant_id=ctx.tenant_id, cin=the_cin, endpoint=key)
            ctx.session.add(row)
            cached[key] = row
        row.payload = payload
        row.fetched_at = now
        return payload, True

    fetched = 0

    # 1) resolve the Tracxn legal entity from the CIN.
    payload, was_fetch = await get_payload(
        "legalentity", {"filter": {"entityId": [the_cin]}})
    fetched += int(was_fetch)
    resolved = normalize("resolve", payload)
    lei = resolved.get("legal_entity_id")
    if not lei:
        err = (payload or {}).get("_error") if isinstance(payload, dict) else None
        if err and err.startswith("unreachable"):
            note = (f"Tracxn could not be reached from the register "
                    f"({err.split('(')[-1].rstrip(')')}) — check outbound HTTPS "
                    f"from the register container.")
        elif err:
            note = f"Tracxn refused the lookup ({err}) — check TRACXN_ACCESS_TOKEN."
        else:
            note = "Tracxn has no legal entity for this CIN."
        return {"cin": the_cin, "configured": True, "resolved": False,
                "note": note, "fetched_at": now.isoformat()}

    # 2) everything else hangs off the legal entity id — fetched TOGETHER. One
    #    slow endpoint must not turn fourteen of them into a minute of waiting.
    body = {"filter": {"legalEntityId": [lei]}}
    keys = [k for k in _ENDPOINTS if k != "legalentity"]
    gate = asyncio.Semaphore(5)

    async def one(key: str) -> tuple[str, dict | None, bool]:
        async with gate:
            got, was = await get_payload(key, body)
            return key, got, was

    results = await asyncio.gather(*(one(k) for k in keys))
    series: dict[str, Any] = {}
    board: dict[str, Any] = {}
    captable: dict[str, Any] = {}
    for key, payload, was_fetch in results:
        fetched += int(was_fetch)
        kind = _ENDPOINTS[key][1]
        norm = normalize(kind, payload)
        if kind == "series":
            series[key] = norm
        elif kind == "board":
            board = norm
        else:
            captable = norm

    if fetched:
        actor = ctx.user.email if ctx.user is not None else ctx.actor
        ctx.session.add(AuditLog(
            tenant_id=ctx.tenant_id, actor=actor, action="tracxn.fetch",
            resource_type="tracxn", resource_id=the_cin,
            request_id=request_id_ctx.get(),
            changes={"cin": the_cin, "endpoints_fetched": fetched,
                     "refresh": refresh}))

    oldest = min((c.fetched_at for c in cached.values()
                  if c.fetched_at is not None), default=now)
    return {
        "cin": the_cin, "configured": True, "resolved": True,
        "legal_entity": {"id": lei, "name": resolved.get("name"),
                         "incorporated": resolved.get("incorporated"),
                         "status": resolved.get("status"),
                         "country": resolved.get("country")},
        "series": series,
        "board": board.get("members", []),
        "shareholders": captable.get("holders", []),
        "fetched_now": fetched,
        "fetched_at": oldest.isoformat(),
    }
