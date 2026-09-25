"""Prospect-list import engine — the desk's research xlsx, deterministically.

One engine behind two doors (the Tools dialog and the maintenance CLI), and one
promise: PREVIEW IS THE TRUTH. ``build_plan`` computes exactly what an apply
would do — creates, merges, in-file duplicates, skips, conflicts — and writes
nothing; ``apply_plan`` executes precisely that plan. The Tools flow calls
plan (preview) → plan+apply (confirm) on the same uploaded bytes, asserted by
the batch checksum.

What the real files taught this module (all five launch lists):

* every file names its columns slightly differently ("CIN" / "CIN/LLPIN",
  "(INR Cr)" / "(INR Crores)" / "(INR)") — a synonym table absorbs it;
* Google-Sheets remnants arrive as literal text ("=IFERROR(__xludf.…") — any
  cell carrying the marker is data lost upstream, scrubbed to NULL;
* the same workbook holds raw sheets, pivots and the curated cut — the sheet
  whose name says "eligible"/"clean" wins, else the best header match;
* the same company appears twice in one file (Tds-G) and across files
  (Neuron Energy in ESS and Solar) — CIN anchors identity, name-key+domain
  is the fallback, and a cross-file repeat becomes ONE prospect with both
  vertical tags;
* multi-valued cells ("a@x.com,\nb@x.com") split on comma/newline/semicolon.

MERGE POLICY (the safety rule): an import fills blanks and adds tags. It never
overwrites a non-empty value and never touches status or remarks — a person's
edits outrank a spreadsheet. Where the file disagrees with a non-empty field,
the conflict is REPORTED, not resolved.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from evam_backend_core.company_identity import canonical_name
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Column recognition
# ---------------------------------------------------------------------------

_SCRUB_MARKER = "__xludf"

_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("company name",),
    "domain": ("domain name", "domain"),
    "cin": ("cin", "cin/llpin", "llpin"),
    "overview": ("overview",),
    "description": ("description",),
    "sub_sector": ("evam sector", "evam ev sector", "evam sectors",
                   "sector (pratice area & feed)", "sector (practice area & feed)"),
    "emails": ("company emails", "company email"),
    "phones": ("company phone numbers", "company phone number", "company phones"),
    "founded_year": ("founded year",),
    "state": ("state",),
    "city": ("city",),
    "country": ("country",),
    "revenue_cr": ("annual revenue (inr cr)", "annual revenue (inr crore)",
                   "annual revenue (inr crores)", "annual revenue"),
    "net_profit_cr": ("annual net profit (inr cr)", "annual net profit (inr crore)",
                      "annual net profit (inr crores)", "annual net profit (inr)",
                      "annual net profit"),
    "ebitda_cr": ("annual ebitda (inr cr)", "annual ebitda (inr crore)",
                  "annual ebitda (inr)", "annual ebitda"),
    "total_funding_cr": ("total funding (inr cr)", "total funding (inr)",
                         "total funding"),
    "latest_funding_cr": ("latest funded amount (inr cr)",
                          "latest funded amount (inr)", "latest funded amount"),
    "latest_valuation_cr": ("latest valuation (inr cr)", "latest valuation (inr)",
                            "latest valuation"),
    "latest_funded_on": ("latest funded date",),
    # Written by our own export (round-trip): a per-row vertical list that outranks
    # the filename detection, so one exported universe re-imports losslessly.
    "row_verticals": ("vertical", "verticals"),
}

_SYNONYM_TO_FIELD = {syn: fld for fld, syns in _HEADER_SYNONYMS.items() for syn in syns}

# The launch verticals, detected from the filename; the caller may override.
_VERTICAL_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("solar", "Solar"),
    ("ess", "ESS"),
    ("ev", "EV"),
    ("bioenergy", "Bioenergy"),
    ("bio", "Bioenergy"),
    ("waste", "Waste & Water"),
    ("wastewater", "Waste & Water"),
    ("water", "Waste & Water"),
)


def detect_vertical(filename: str) -> str | None:
    low = (filename or "").lower()
    for key, vertical in _VERTICAL_KEYWORDS:
        if re.search(rf"\b{re.escape(key)}\b", low.replace("&", " ")):
            return vertical
    return None


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _scrubbed(value: Any) -> Any:
    """A formula remnant is data that was lost before the file reached us."""
    if isinstance(value, str) and _SCRUB_MARKER in value:
        return None
    return value


def _text(value: Any, cap: int = 300) -> str | None:
    value = _scrubbed(value)
    if value is None:
        return None
    out = re.sub(r"\s+", " ", str(value)).strip()
    return out[:cap] or None


def _long_text(value: Any) -> str | None:
    value = _scrubbed(value)
    if value is None:
        return None
    out = str(value).strip()
    return out or None


def _multi(value: Any, cap: int = 12) -> list[str]:
    value = _scrubbed(value)
    if value is None:
        return []
    parts = re.split(r"[,\n;]+", str(value))
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p.lower() not in ("nan", "none", "-") and p not in out:
            out.append(p[:200])
    return out[:cap]


def _number(value: Any) -> float | None:
    value = _scrubbed(value)
    if value is None or value == "":
        return None
    try:
        n = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    # The lists are crore-scale sheets whatever the header's unit spelling; an
    # absurd magnitude means a rupee figure slipped in — refuse rather than store
    # a lakh-crore "revenue".
    if abs(n) >= 10_000_000:
        return None
    return round(n, 4)


def _year(value: Any) -> int | None:
    n = _number(value)
    if n is None:
        return None
    y = int(n)
    return y if 1800 <= y <= 2100 else None


def _funded_date(value: Any) -> date | None:
    value = _scrubbed(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%b %d, %Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Workbook → normalized rows
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One company as a file states it, normalized."""

    name: str
    name_key: str
    domain: str | None = None
    cin: str | None = None
    overview: str | None = None
    verticals: list[str] = field(default_factory=list)
    sub_sectors: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    founded_year: int | None = None
    state: str | None = None
    city: str | None = None
    country: str | None = None
    revenue_cr: float | None = None
    net_profit_cr: float | None = None
    ebitda_cr: float | None = None
    total_funding_cr: float | None = None
    latest_funding_cr: float | None = None
    latest_valuation_cr: float | None = None
    latest_funded_on: date | None = None
    source: str | None = None

    def identity(self) -> str:
        if self.cin:
            return f"cin::{self.cin.lower()}"
        return f"nk::{self.name_key}::{(self.domain or '').lower()}"


_SCALARS = ("domain", "cin", "overview", "founded_year", "state", "city", "country",
            "revenue_cr", "net_profit_cr", "ebitda_cr", "total_funding_cr",
            "latest_funding_cr", "latest_valuation_cr", "latest_funded_on")
_LISTS = ("verticals", "sub_sectors", "emails", "phones")


def _pick_sheet(wb) -> tuple[Any, dict[int, str]] | None:
    """The curated cut: 'eligible'/'clean' by name among well-mapped sheets, else
    the best header match that actually has data."""
    scored: list[tuple[int, bool, Any, dict[int, str]]] = []
    for ws in wb.worksheets:
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            continue
        mapping: dict[int, str] = {}
        for idx, cell in enumerate(header_row):
            fld = _SYNONYM_TO_FIELD.get(_norm_header(cell))
            if fld and fld not in mapping.values():
                mapping[idx] = fld
        if "name" not in mapping.values() or len(mapping) < 3:
            continue
        preferred = bool(re.search(r"eligible|clean", ws.title, re.IGNORECASE))
        scored.append((len(mapping), preferred, ws, mapping))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[1], t[0]), reverse=True)
    _, _, ws, mapping = scored[0]
    return ws, mapping


def parse_workbook(filename: str, content: bytes, vertical: str | None,
                   skipped: list[dict]) -> tuple[list[Candidate], str | None]:
    """Normalized candidates from ONE workbook. Returns (candidates, sheet name)."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        picked = _pick_sheet(wb)
        if picked is None:
            skipped.append({"file": filename, "row": None,
                            "reason": "no sheet with recognisable columns"})
            return [], None
        ws, mapping = picked
        out: list[Candidate] = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            values = {fld: (row[idx] if idx < len(row) else None)
                      for idx, fld in mapping.items()}
            name = _text(values.get("name"))
            if not name:
                if any(v not in (None, "") for v in row):
                    skipped.append({"file": filename, "row": row_idx,
                                    "reason": "no company name"})
                continue
            key = canonical_name(name)
            if not key:
                skipped.append({"file": filename, "row": row_idx,
                                "reason": "name normalises to nothing"})
                continue
            overview = _long_text(values.get("overview")) or _long_text(values.get("description"))
            row_verticals = _multi(values.get("row_verticals"), cap=8)
            cand = Candidate(
                name=name, name_key=key,
                domain=_text(values.get("domain"), 200),
                cin=_text(values.get("cin"), 40),
                overview=overview,
                verticals=row_verticals or ([vertical] if vertical else []),
                sub_sectors=_multi(values.get("sub_sector"), cap=4),
                emails=_multi(values.get("emails")),
                phones=_multi(values.get("phones")),
                founded_year=_year(values.get("founded_year")),
                state=_text(values.get("state"), 60),
                city=_text(values.get("city"), 120),
                country=_text(values.get("country"), 60),
                revenue_cr=_number(values.get("revenue_cr")),
                net_profit_cr=_number(values.get("net_profit_cr")),
                ebitda_cr=_number(values.get("ebitda_cr")),
                total_funding_cr=_number(values.get("total_funding_cr")),
                latest_funding_cr=_number(values.get("latest_funding_cr")),
                latest_valuation_cr=_number(values.get("latest_valuation_cr")),
                latest_funded_on=_funded_date(values.get("latest_funded_on")),
                source=filename,
            )
            out.append(cand)
        return out, ws.title
    finally:
        wb.close()


def _merge_candidates(base: Candidate, extra: Candidate) -> None:
    """Fold a same-identity row into ``base`` — union tags, fill blanks."""
    for fld in _LISTS:
        seen = getattr(base, fld)
        for v in getattr(extra, fld):
            if v not in seen:
                seen.append(v)
    for fld in _SCALARS:
        if getattr(base, fld) in (None, "") and getattr(extra, fld) not in (None, ""):
            setattr(base, fld, getattr(extra, fld))


# ---------------------------------------------------------------------------
# Plan (preview == apply)
# ---------------------------------------------------------------------------

def _clean_list(values: Any) -> list[str]:
    return [v for v in (values or []) if isinstance(v, str) and v]


def _existing_index(rows) -> tuple[dict[str, Any], dict[str, Any]]:
    by_cin: dict[str, Any] = {}
    by_key: dict[str, Any] = {}
    for p in rows:
        if p.cin:
            by_cin[p.cin.lower()] = p
        if p.name_key:
            by_key[f"{p.name_key}::{(p.domain or '').lower()}"] = p
    return by_cin, by_key


def _same_value(current: Any, incoming: Any) -> bool:
    if isinstance(incoming, (int, float)):
        try:
            return abs(float(current) - float(incoming)) < 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(incoming, date):
        return current == incoming
    return str(current).strip() == str(incoming).strip()


def _additions_for(existing, cand: Candidate) -> tuple[dict, list[dict]]:
    """(fields to set, conflicts kept for a human) under the merge policy."""
    additions: dict[str, Any] = {}
    conflicts: list[dict] = []
    for fld in _LISTS:
        current = _clean_list(getattr(existing, fld))
        merged = list(current)
        for v in getattr(cand, fld):
            if v not in merged:
                merged.append(v)
        if merged != current:
            additions[fld] = merged
    for fld in _SCALARS:
        incoming = getattr(cand, fld)
        if incoming in (None, ""):
            continue
        current = getattr(existing, fld)
        if current in (None, ""):
            additions[fld] = incoming
        elif not _same_value(current, incoming):
            conflicts.append({"field": fld, "existing": str(current),
                              "incoming": str(incoming)})
    return additions, conflicts


async def build_plan(session, tenant_id, files: list[tuple[str, bytes, str | None]]) -> dict:
    """The whole import as data, nothing written.

    ``files``: (filename, content, vertical override or None → filename detection).
    """
    from app.models import Prospect

    skipped: list[dict] = []
    file_reports: list[dict] = []
    batch_hash = hashlib.sha256()

    merged: dict[str, Candidate] = {}
    in_file_dupes = 0
    for filename, content, vertical_override in files:
        batch_hash.update(hashlib.sha256(content).digest())
        vertical = vertical_override or detect_vertical(filename)
        cands, sheet = parse_workbook(filename, content, vertical, skipped)
        file_reports.append({"file": filename, "vertical": vertical,
                             "sheet": sheet, "rows": len(cands)})
        for cand in cands:
            key = cand.identity()
            if key in merged:
                _merge_candidates(merged[key], cand)
                in_file_dupes += 1
            else:
                merged[key] = cand

    rows = (await session.execute(
        select(Prospect).where(Prospect.tenant_id == tenant_id,
                               Prospect.deleted_at.is_(None)))).scalars().all()
    by_cin, by_key = _existing_index(rows)

    new: list[Candidate] = []
    merges: list[dict] = []
    for cand in merged.values():
        existing = None
        if cand.cin:
            existing = by_cin.get(cand.cin.lower())
        if existing is None:
            existing = by_key.get(f"{cand.name_key}::{(cand.domain or '').lower()}")
        if existing is None:
            new.append(cand)
            continue
        additions, conflicts = _additions_for(existing, cand)
        if additions or conflicts:
            merges.append({"id": existing.id, "prospect_no": existing.prospect_no,
                           "name": existing.name, "candidate": cand,
                           "additions": additions, "conflicts": conflicts})

    return {
        "batch": batch_hash.hexdigest(),
        "files": file_reports,
        "new": new,
        "merges": merges,
        "in_file_duplicates": in_file_dupes,
        "skipped": skipped,
    }


def summarize_plan(plan: dict, sample: int = 25) -> dict:
    """The JSON the Tools preview renders — counts first, samples capped."""

    def cand_row(c: Candidate) -> dict:
        return {"name": c.name, "cin": c.cin, "domain": c.domain,
                "verticals": c.verticals, "sub_sectors": c.sub_sectors}

    conflicts = [
        {"prospect_no": m["prospect_no"], "name": m["name"], **c}
        for m in plan["merges"] for c in m["conflicts"]
    ]
    return {
        "batch": plan["batch"],
        "files": plan["files"],
        "counts": {
            "new": len(plan["new"]),
            "merged": len(plan["merges"]),
            "in_file_duplicates": plan["in_file_duplicates"],
            "skipped": len(plan["skipped"]),
            "conflicts": len(conflicts),
        },
        "new_sample": [cand_row(c) for c in plan["new"][:sample]],
        "merge_sample": [
            {"prospect_no": m["prospect_no"], "name": m["name"],
             "added_fields": sorted(m["additions"].keys()),
             "conflicts": m["conflicts"]}
            for m in plan["merges"][:sample]
        ],
        "skipped": plan["skipped"][:sample * 4],
        "conflicts": conflicts[:sample * 4],
    }


async def apply_plan(session, tenant_id, actor: str, plan: dict) -> dict:
    """Execute EXACTLY the plan: create the news, apply the merge additions.
    Never deletes, never touches status/remarks, never resolves a conflict."""
    from evam_backend_core.crud import allocate_number

    from app.models import Prospect

    created_ids: list[str] = []
    for cand in plan["new"]:
        prospect = Prospect(
            tenant_id=tenant_id,
            prospect_no=await allocate_number(session, Prospect, tenant_id,
                                              "prospect_no", "P-", 4),
            name=cand.name, name_key=cand.name_key, domain=cand.domain,
            cin=cand.cin, overview=cand.overview,
            verticals=cand.verticals or None, sub_sectors=cand.sub_sectors or None,
            emails=cand.emails or None, phones=cand.phones or None,
            founded_year=cand.founded_year, state=cand.state, city=cand.city,
            country=cand.country, revenue_cr=cand.revenue_cr,
            net_profit_cr=cand.net_profit_cr, ebitda_cr=cand.ebitda_cr,
            total_funding_cr=cand.total_funding_cr,
            latest_funding_cr=cand.latest_funding_cr,
            latest_valuation_cr=cand.latest_valuation_cr,
            latest_funded_on=cand.latest_funded_on,
            source=cand.source, import_batch=plan["batch"],
            created_by=actor, updated_by=actor,
        )
        session.add(prospect)
        await session.flush()
        created_ids.append(str(prospect.id))

    merged_ids: list[str] = []
    for m in plan["merges"]:
        if not m["additions"]:
            continue
        obj = await session.get(Prospect, m["id"])
        if obj is None or obj.deleted_at is not None:
            continue
        for fld, value in m["additions"].items():
            setattr(obj, fld, value)
        obj.updated_by = actor
        merged_ids.append(str(obj.id))
    await session.flush()
    return {"created": created_ids, "merged": merged_ids}
