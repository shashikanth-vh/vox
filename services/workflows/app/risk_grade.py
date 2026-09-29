"""Company 360 risk grade — PRISM's half: gather the company as the caller, grade on
the AI host, remember the answer, and hand out the report.

The grading itself (the desk's ATLAS client rubric on GLM, with the rubric's rules
re-applied to the model's answer) lives in Chitti on the AI host:
``POST {chitti}/v1/risk-grade``. What stays here is everything that must be read AS
the verified caller or from this host, so RBAC holds — a role never gets a grade
built from data it cannot see:

* the company's panorama from the Register;
* its **Data Register files** in the configured sections (Financials, Banking &
  Debt by default) — read the way the CAM reads documents: the bytes come from the
  Register and go through DocRAG ``/v1/extract`` (OpenDataLoader, Sarvam OCR for
  scans, cached per file), and only the extracted text travels to the AI host;
* **PULSE**: the firm's and its key people's news, with PULSE's own verdicts,
  fetched server-side (the dialog's news is only a fallback when PULSE is down).

A grade is computed only on request — one company, when someone asks — and cached
per company AND per visible-section set (24 h, in memory; "Regrade" computes afresh).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from evam_backend_core.logging import get_logger
from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.cam import extract_text, image_suffix, is_pdf
from app.docx_out import markdown_to_docx

log = get_logger("panorama.risk_grade")

_TTL_S = 24 * 3600
# The AI host's edge caps this body (2 MB); the rubric reads the recent footprint.
_KEEP = {"interactions": 30, "documents": 80, "leads": 25, "deals": 25, "lending": 25,
         "syndication": 40, "asset_monetisation": 25, "contacts": 25}
# Register rows that carry no current file to read.
_SKIP_STATUS = {"Pending", "Rejected", "Superseded", "Expired", "Waived"}
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # Excel 97–2003 (.xls) container
# The Data Register stores a section as the UI's short code (documentsService.ts);
# settings and reports use the names people see. Both are accepted.
SECTION_NAMES = {"kyc": "KYC & Constitutional", "fin": "Financials", "bank": "Banking & Debt",
                 "comp": "Compliance & Bureau", "proj": "Project & Technical",
                 "deal": "Deal Documents"}


# PULSE says GOOD / BAD / UGLY / POLICY; the rubric's news lines use the radar's colours.
_VERDICT = {"GOOD": "GREEN", "BAD": "AMBER", "UGLY": "RED", "POLICY": "BLUE"}
_SUFFIXES = re.compile(r"\b(private|pvt|limited|ltd|llp|inc|company|co|the)\b\.?", re.I)


def name_core(name: str) -> str:
    """The part of a firm's name a headline would actually carry."""
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", name or "")).strip(" .,&").lower()


def mentions(headline: str, name: str, *, person: bool = False) -> bool:
    """Whether a headline is about this firm (its core name) or person (surname) —
    a news search for "Fractal Energy" also returns "a fractal universe"."""
    h = (headline or "").lower()
    if person:
        parts = [p for p in re.split(r"[\s.]+", name.lower()) if len(p) > 2]
        return bool(parts) and parts[-1] in h
    core = name_core(name)
    return bool(core) and core in h


# The order files take the per-grade budget in: hard actuals first, projections last —
# so a budget that runs out cuts a CMA, never the audited balance sheet.
_PRIORITY = (("audited", 0), ("audit report", 0), ("balance sheet", 0),
             ("financial statement", 0),
             ("provisional", 1), ("itr", 2), ("bank statement", 3), ("current account", 3),
             ("cibil", 4), ("sanction", 5), ("debt", 5), ("outstanding", 5), ("soa", 5),
             ("cma", 8), ("projection", 8))


def read_order(d: dict[str, Any]) -> tuple[int, int, str]:
    """Kind of file first, then the NEWEST year first (FY 2024-25 before FY 2022-23)."""
    text = f"{d.get('title') or ''} {d.get('original_filename') or ''}".lower()
    rank = next((r for key, r in _PRIORITY if key in text), 6)
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", text)]
    short = [2000 + int(y) for y in re.findall(r"(?:'|-|fy)(\d\d)(?!\d)", text)]
    return rank, -max(years + short, default=0), text


def fingerprint(payload: dict[str, Any], day: str) -> str:
    """What a grade was computed from: the records, the files' text, the news, the
    day (days-in-stage moves daily). Equal fingerprints → the same grade."""
    import hashlib
    import json as _json

    p = {k: v for k, v in (payload.get("panorama") or {}).items() if k != "generated_at"}
    body = {"day": day, "panorama": p,
            "news": sorted(str(n.get("headline")) for n in payload.get("news") or []),
            "documents": [(d.get("name"), hashlib.sha256(str(d.get("text")).encode()).hexdigest())
                          for d in payload.get("documents") or []]}
    return hashlib.sha256(_json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def section_name(value: Any) -> str:
    v = str(value or "").strip()
    return SECTION_NAMES.get(v.lower(), v)


class NewsIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headline: str = Field(max_length=500)
    source: str | None = Field(default=None, max_length=200)
    when: str | None = Field(default=None, max_length=60)
    severity: str | None = Field(default=None, max_length=10)


class RiskGradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str | None = Field(default=None, max_length=64)
    company: str = Field(min_length=1, max_length=300)
    # The dialog's news — used only when PULSE cannot be reached from here.
    news: list[NewsIn] = Field(default_factory=list, max_length=60)
    refresh: bool = False


class ReportIn(BaseModel):
    """A grade as the caller's screen shows it — rendered as-is, so the report is
    exactly what was on screen and does not depend on this process's memory."""
    model_config = ConfigDict(extra="forbid")
    grade: dict[str, Any]


def trim_panorama(p: dict[str, Any]) -> dict[str, Any]:
    """The panorama as sent: the most recent rows of each list, long notes clipped."""
    out = dict(p)
    for key, n in _KEEP.items():
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [{k: (v[:800] + "…" if isinstance(v, str) and len(v) > 800 else v)
                         for k, v in r.items()} if isinstance(r, dict) else r
                        for r in rows[:n]]
    return out


def docrag_suffix(ctype: str, blob: bytes, filename: str) -> str | None:
    """The DocRAG-readable kind of a file, or None (read in-process instead)."""
    ctype, name = (ctype or "").lower(), (filename or "").lower()
    if is_pdf(ctype, blob):
        return ".pdf"
    if "spreadsheetml" in ctype or (blob[:2] == b"PK" and b"xl/" in blob[:4096]):
        return ".xlsx"
    # Only a real OLE2 workbook: a CSV uploaded as "application/vnd.ms-excel" (as
    # Windows browsers do) is text, and is read as text below.
    if blob.startswith(_OLE2) and ("ms-excel" in ctype or name.endswith(".xls")):
        return ".xls"
    return image_suffix(ctype, blob)


def _csv_like(ctype: str, blob: bytes, filename: str) -> bool:
    return (("ms-excel" in (ctype or "").lower() or (filename or "").lower().endswith(".csv"))
            and not blob.startswith(_OLE2) and b"\x00" not in blob[:2048])


def report_markdown(g: dict[str, Any]) -> str:
    """The grade as a report: everything the (i) shows, in reading order."""
    c = g.get("confidence_detail") or {}
    lines = [
        f"# Risk grade — {g.get('company', '')}",
        "",
        f"**Rating:** {g.get('label')} · **Score:** {g.get('score')}/100 · "
        f"**Confidence:** {g.get('confidence_pct', '—')}% · **Action:** {g.get('action')}",
        "",
        g.get("verdict") or "",
        "",
    ]
    if g.get("borderline"):
        lines += ["> Near a band line (45 / 70): a small change in the evidence can change "
                  "the colour.", ""]
    lines += ["## How the score was built", "",
              "| Pillar | Weight | Score | Points | Evidence |", "|---|---|---|---|---|"]
    for p in g.get("pillars") or []:
        pts = p.get("weighted") if p.get("available") else "not scored (no data)"
        ev = str(p.get("evidence", "")).replace("|", "/").replace("\n", " ")
        lines.append(f"| {p.get('key')}. {p.get('name')} | {p.get('weight')}% | "
                     f"{p.get('score')}/10 | {pts} | {ev} |")
    lines += ["", "Bands: GREEN ≥ 70 · AMBER 45–69 · RED < 45, scored over the pillars "
              "that have data. Any hard trigger → RED. No financials or banking → never "
              "GREEN (PROVISIONAL).", ""]
    if c:
        lines += ["## Confidence", "",
                  f"{g.get('confidence_pct')}% = 60% × data coverage ({c.get('data_coverage')}% "
                  f"of the rubric's weight had data) + 40% × agreement ({c.get('agreement')}% — "
                  f"the independent readings spread {c.get('spread')} points).", ""]
    if g.get("runs"):
        lines += [f"Independent readings: {', '.join(str(r.get('score')) for r in g['runs'])} "
                  "(per-pillar median shown).", ""]
    lines += ["## Hard red triggers", ""]
    lines += [f"- **{t.get('status')}** — {t.get('trigger')}"
              + (f": {t.get('evidence')}" if t.get("evidence") else "")
              for t in g.get("hard_triggers") or []] or ["- Not reported."]

    def block(title: str, items: list[str]) -> list[str]:
        return ["", f"## {title}", "", *([f"- {i}" for i in items] or ["- None given."])]

    lines += block("Top risks", g.get("risks") or [])
    lines += block("Top mitigants", g.get("mitigants") or [])
    lines += block("Data gaps — documents to request", g.get("data_gaps") or [])
    lines += ["", "## What would move it", "", f"- Up: {g.get('move_up') or '—'}",
              f"- Down: {g.get('move_down') or '—'}"]
    if g.get("conditions"):
        lines += block("Conditions (if proceeding)", g["conditions"])
    if g.get("adjustments"):
        lines += block("Rubric overrides", g["adjustments"])
    inp = g.get("inputs") or {}
    lines += ["", "## Built from", ""]
    docs = inp.get("documents") or []
    if docs:
        lines += ["| Document | Section | Read by | Used |", "|---|---|---|---|"]
        for d in docs:
            used = "yes" if d.get("used") else f"no — {d.get('reason', '')}"
            lines.append(f"| {d.get('name')} | {d.get('section') or '—'} | "
                         f"{', '.join(d.get('engines') or []) or '—'} | {used} |")
        lines.append("")
    else:
        lines += [f"- Documents: none ({inp.get('document_note') or 'no files in the graded sections'})"]
    lines += [f"- Register: {inp.get('lending_lines', 0)} lending line(s), "
              f"{inp.get('platform_deals', 0)} platform deal(s), "
              f"{inp.get('interactions', 0)} interaction(s)",
              f"- News (PULSE): {inp.get('news_items', 0)} headline(s)"
              + (f" — {inp.get('news_note')}" if inp.get("news_note") else "")]
    if inp.get("register_sections_hidden"):
        lines.append(f"- Hidden from the requesting role: {', '.join(inp['register_sections_hidden'])}")
    lines += ["", "---", "",
              f"{g.get('engine')} · rubric {g.get('prompt')} ({g.get('prompt_version')}) · "
              f"graded {g.get('generated_at')} by {g.get('generated_by')}. AI-assisted: a desk "
              "aid, not a credit decision."]
    return "\n".join(lines)


def mount_risk_grade(app: Any, settings: Any, *, denied: Any, verified_email: Any,
                     caller_context: Any, problem: Any, reg_headers: Any) -> None:
    base = settings.register_base_url.rstrip("/")
    chitti_url = (getattr(settings, "chitti_url", "") or "").rstrip("/")
    docrag_url = (getattr(settings, "docrag_url", "") or "").rstrip("/")
    pulse_url = (getattr(settings, "pulse_url", "") or "").rstrip("/")
    sections = {section_name(s) for s in str(getattr(settings, "risk_grade_doc_sections",
                                                     "Financials,Banking & Debt")).split(",")
                if s.strip()}
    doc_max = int(getattr(settings, "risk_grade_doc_max_chars", 40_000))
    docs_total = int(getattr(settings, "risk_grade_docs_total_chars", 160_000))
    cache: dict[str, tuple[float, dict]] = {}
    inflight: dict[str, asyncio.Future] = {}
    # company key → (fingerprint of what the last grade read, that grade)
    last_read: dict[str, tuple[str, dict]] = {}

    async def _identity(request: Request) -> tuple[Any, str, Any]:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp, "", None
        # Production: the verified token's e-mail. Dev only: the forwarded header.
        who, err = await verified_email(request, request.headers.get("X-User-Email", ""))
        if err is not None:
            return err, "", None
        caller, _ = caller_context(request, who)
        return None, who, caller

    async def _panorama(request: Request, caller: Any, who: str,
                        entity_id: str | None, company: str) -> tuple[dict | None, Any]:
        params = {"entity_id": entity_id} if entity_id else {"company": company}
        try:
            r = await request.app.state.http.get(
                f"{base}/v1/panorama", params=params,
                headers=reg_headers(request, caller, who, "GET", "/v1/panorama"))
        except httpx.HTTPError as exc:
            return None, problem(502, "Register unreachable", f"Could not read the company: {exc}")
        if r.status_code >= 300:
            return None, problem(502, "Register refused the company read",
                                 f"HTTP {r.status_code} from the register.")
        return r.json(), None

    # ------------------------------------------------------------------ documents
    async def _extract(request: Request, doc_id: str, blob: bytes, ctype: str,
                       name: str) -> tuple[str, str | None, list[str], bool]:
        """(text, skip reason, engines, cached) — DocRAG for PDFs, spreadsheets and
        scans (the CAM's reader, cached per file hash); in-process for the rest."""
        suffix = docrag_suffix(ctype, blob, name) if docrag_url else None
        if suffix is None:
            if _csv_like(ctype, blob, name):
                return blob.decode("utf-8", "ignore"), None, ["text"], False
            text, reason = extract_text(ctype, blob)
            return text, reason, (["basic"] if text else []), False
        http = getattr(request.app.state, "docrag_http", None) or request.app.state.http
        try:
            r = await http.post(
                f"{docrag_url}/v1/extract", files={"file": (f"{doc_id}{suffix}", blob)},
                headers={"X-API-Key": settings.docrag_api_key,
                         "X-Tenant": request.headers.get("X-Tenant", settings.register_tenant)},
                timeout=float(getattr(settings, "docrag_timeout_s", 600.0)))
        except httpx.HTTPError as exc:
            return "", f"document AI unreachable ({exc.__class__.__name__})", [], False
        if r.status_code >= 300:
            return "", f"document AI refused it (HTTP {r.status_code})", [], False
        body = r.json() or {}
        md = str(body.get("markdown") or "")
        engines = list(body.get("extraction_engines") or [])
        cached = bool(body.get("cached") or (body.get("telemetry") or {}).get("cached"))
        return md, (None if md.strip() else "no text could be read"), engines, cached

    async def _documents(request: Request, caller: Any, who: str,
                         entity_id: str | None) -> tuple[list[dict], list[dict], str | None]:
        """(texts for the grader, every file's fate, note). Files in the configured
        Data Register sections only; nothing is copied anywhere — text travels."""
        if not entity_id:
            return [], [], "no client master record — the Data Register is not linked"
        path = "/v1/documents"
        try:
            r = await request.app.state.http.get(
                f"{base}{path}?entity_id={entity_id}&limit=100",
                headers=reg_headers(request, caller, who, "GET", path))
        except httpx.HTTPError as exc:
            return [], [], f"Data Register unreachable ({exc.__class__.__name__})"
        if r.status_code >= 300:
            return [], [], f"Data Register refused the list (HTTP {r.status_code})"
        rows = [d for d in (r.json() or {}).get("items", [])
                if section_name(d.get("section")) in sections
                and (d.get("status") or "") not in _SKIP_STATUS
                and (d.get("original_filename") or d.get("storage_uri") or d.get("size_bytes"))]
        if not rows:
            return [], [], f"no files on the Data Register in {', '.join(sorted(sections))}"
        # The same file uploaded into two slots is read once.
        unique: dict[tuple[str, Any], dict] = {}
        for d in sorted(rows, key=read_order):
            unique.setdefault((str(d.get("original_filename") or d.get("id")).lower(),
                               d.get("checksum") or d.get("size_bytes")), d)
        rows = list(unique.values())
        gate = asyncio.Semaphore(4)

        async def one(d: dict) -> dict:
            name = d.get("original_filename") or d.get("title") or "document"
            fate: dict[str, Any] = {"name": name, "section": section_name(d.get("section")),
                                    "title": d.get("title"), "used": False,
                                    "engines": [], "chars": 0}
            async with gate:
                cpath = f"/v1/documents/{d.get('id')}/content"
                try:
                    got = await request.app.state.http.get(
                        f"{base}{cpath}", headers=reg_headers(request, caller, who, "GET", cpath),
                        follow_redirects=True, timeout=60.0)
                except httpx.HTTPError as exc:
                    return {**fate, "reason": f"register read failed ({exc.__class__.__name__})"}
                if got.status_code >= 300:
                    return {**fate, "reason": f"register refused the read (HTTP {got.status_code})"}
                text, reason, engines, cached = await _extract(
                    request, str(d.get("id")), got.content,
                    got.headers.get("content-type") or d.get("content_type") or "", name)
                return {**fate, "engines": engines, "cached": cached, "text": text,
                        **({"reason": reason} if reason else {})}

        fates = list(await asyncio.gather(*(one(d) for d in rows)))
        texts: list[dict] = []
        used_total = 0
        for f in fates:
            text = f.pop("text", "") or ""
            if not text.strip():
                continue
            room = min(doc_max, docs_total - used_total)
            if room <= 1000:
                f["reason"] = "left out: the per-grade document budget was used up"
                continue
            clipped = len(text) > room
            text = text[:room]
            used_total += len(text)
            f.update(used=True, chars=len(text), **({"note": "clipped"} if clipped else {}))
            texts.append({"name": f["name"], "section": f["section"], "title": f["title"],
                          "engines": f["engines"], "text": text})
        return texts, fates, None

    # ------------------------------------------------------------------ PULSE
    async def _pulse(request: Request, p: dict, company: str) -> tuple[list[dict], str | None]:
        """The firm's and its key people's news, PULSE's verdicts, last 90 days."""
        if not pulse_url:
            return [], "PULSE not configured"
        s = p.get("stats") or {}
        live = "live" if (s.get("deals_in_flight") or 0) + (s.get("deals_done") or 0) else ""
        since = (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
        people = [c.get("name") for c in (p.get("contacts") or []) if c.get("name")][:3]
        core = name_core(company) or company
        # Exact-phrase searches; every hit is then checked to actually name the firm/person.
        terms = [(f'"{core}"', company, 20)] + [(f'"{n}"', n, 6) for n in people]
        out: list[dict] = []
        seen: set[str] = set()
        failures = 0
        for term, about, limit in terms:
            try:
                r = await request.app.state.http.get(
                    f"{pulse_url}/v1/news/search",
                    params={"q": term, "from": since, "limit": limit, "exposure": live},
                    headers={"X-API-Key": getattr(settings, "pulse_api_key", "")}, timeout=60.0)
                arts = (r.json() or {}).get("articles", []) if r.status_code < 300 else None
            except (httpx.HTTPError, ValueError):
                arts = None
            if arts is None:
                failures += 1
                continue
            person = about != company
            for a in arts:
                title = str(a.get("headline") or a.get("title") or "").strip()
                key = title.lower()
                if not key or key in seen or not mentions(title, about, person=person):
                    continue
                seen.add(key)
                sev = str(a.get("severity") or "").upper()
                out.append({"headline": title, "source": a.get("source"),
                            "when": a.get("when"), "severity": _VERDICT.get(sev, sev or None),
                            "about": about if person else None})
        if failures == len(terms):
            return [], "PULSE unreachable"
        return out[:40], (f"{failures} PULSE search(es) failed" if failures else None)

    def _key(entity_id: str | None, company: str, restricted: list[str]) -> str:
        return f"{entity_id or company.strip().lower()}|{','.join(sorted(restricted))}"

    async def _cached(request: Request, company: str,
                      entity_id: str | None) -> tuple[dict | None, Any]:
        resp, who, caller = await _identity(request)
        if resp is not None:
            return None, resp
        # The key carries the caller's visible sections — read them, so a grade built
        # from data this role cannot see is never handed to it.
        p, err = await _panorama(request, caller, who, entity_id, company)
        if err is not None:
            return None, err
        assert p is not None                  # no error → the panorama was read
        hit = cache.get(_key(entity_id, company, p.get("restricted") or []))
        if not hit or time.monotonic() - hit[0] > _TTL_S:
            return None, problem(404, "No grade yet",
                                 "No risk grade has been computed for this company.")
        return hit[1], None

    @app.get("/v1/panorama/risk-grade", tags=["Company 360"],
             summary="The cached risk grade for a company, if one was computed")
    async def get_risk_grade(request: Request, company: str, entity_id: str | None = None) -> Any:
        grade, err = await _cached(request, company, entity_id)
        return err if err is not None else {**(grade or {}), "cached": True}

    def _report(g: dict[str, Any], company: str) -> Response:
        blob = markdown_to_docx(report_markdown(g), None)
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_"
                       for ch in str(g.get("company") or company))
        stamp = str(g.get("generated_at", ""))[:10] or date.today().isoformat()
        return Response(content=blob, media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            headers={"Content-Disposition":
                     f'attachment; filename="Risk grade - {safe.strip()} - {stamp}.docx"'})

    @app.get("/v1/panorama/risk-grade/report", tags=["Company 360"],
             summary="The saved risk grade as a Word report (404 once it has expired)")
    async def risk_grade_report(request: Request, company: str,
                                entity_id: str | None = None) -> Any:
        grade, err = await _cached(request, company, entity_id)
        return err if err is not None else _report(grade or {}, company)

    @app.post("/v1/panorama/risk-grade/report", tags=["Company 360"],
              summary="Render the grade on screen as a Word report")
    async def risk_grade_report_of(payload: ReportIn, request: Request) -> Any:
        resp, _who, _caller = await _identity(request)
        if resp is not None:
            return resp
        if not isinstance(payload.grade.get("pillars"), list) or "score" not in payload.grade:
            return problem(422, "Not a risk grade", "Send the grade object the screen shows.")
        return _report(payload.grade, str(payload.grade.get("company") or "company"))

    @app.post("/v1/panorama/risk-grade", tags=["Company 360"],
              summary="Grade ONE company RED / AMBER / GREEN (on request; ATLAS client rubric)")
    async def risk_grade(payload: RiskGradeIn, request: Request) -> Any:
        resp, who, caller = await _identity(request)
        if resp is not None:
            return resp
        if not chitti_url:
            return problem(409, "No grading service configured",
                           "Set WORKFLOWS_CHITTI_URL and WORKFLOWS_CHITTI_API_KEY "
                           "(the AI host) to enable risk grades.")
        p, err = await _panorama(request, caller, who, payload.entity_id, payload.company)
        if err is not None:
            return err
        assert p is not None                  # no error → the panorama was read
        key = _key(payload.entity_id, payload.company, p.get("restricted") or [])
        hit = cache.get(key)
        if hit and not payload.refresh and time.monotonic() - hit[0] < _TTL_S:
            return {**hit[1], "cached": True}
        if key in inflight:
            try:
                return {**(await asyncio.shield(inflight[key])), "cached": True}
            except Exception:  # noqa: BLE001 - the owner reports its own failure
                return problem(502, "Grading failed", "A concurrent grading of this company failed.")

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        inflight[key] = fut
        try:
            result, failure = await _grade(request, p, payload, who, caller, key)
            if failure is not None:
                fut.set_exception(RuntimeError("grading failed"))
                fut.exception()                # consumed: no "never retrieved" warning
                return failure
        finally:
            inflight.pop(key, None)
        fut.set_result(result)
        cache[key] = (time.monotonic(), result)
        return {**result, "cached": False}

    async def _grade(request: Request, p: dict, payload: RiskGradeIn,
                     who: str, caller: Any, key: str) -> tuple[dict, Any]:
        company = (p.get("anchor") or {}).get("name") or payload.company
        entity_id = payload.entity_id or (p.get("anchor") or {}).get("entity_id")
        (docs, fates, doc_note), (news, news_note) = await asyncio.gather(
            _documents(request, caller, who, entity_id), _pulse(request, p, company))
        if not news and news_note:
            # PULSE could not be asked from here: fall back to what the dialog showed.
            news = [n.model_dump() for n in payload.news]
            if news:
                news_note = f"{news_note}; the dialog's 30-day news used instead"
        elif not news:
            news_note = "no headlines naming the firm or its people in the last 90 days"
        sent = {"company": company, "panorama": trim_panorama(p), "news": news,
                "documents": docs}
        fp = fingerprint(sent, datetime.now(UTC).date().isoformat())
        prior = last_read.get(key)
        if prior and prior[0] == fp:
            # Nothing the grade reads has changed since the last one: the same grade,
            # not a fresh model reading of the same facts (which would wobble).
            log.info("risk_grade_unchanged", extra={"company": company, "by": who})
            return {**prior[1], "unchanged": True}, None
        # The AI host sits behind the same private-CA edge as its DocRAG: the client
        # built with that CA (WORKFLOWS_DOCRAG_CA_FILE) is the one that trusts it.
        http = getattr(request.app.state, "docrag_http", None) or request.app.state.http
        try:
            r = await http.post(
                f"{chitti_url}/v1/risk-grade",
                headers={"X-API-Key": settings.chitti_api_key,
                         "X-Tenant": request.headers.get("X-Tenant", settings.register_tenant)},
                json=sent,
                timeout=float(getattr(settings, "chitti_timeout_s", 300.0)))
        except httpx.HTTPError as exc:
            log.warning("risk_grade_ai_host_unreachable", extra={"error": str(exc)})
            return {}, problem(502, "Grading service unreachable",
                               f"The AI host did not answer ({exc.__class__.__name__}).")
        if r.status_code >= 300:
            try:
                e = (r.json() or {}).get("error") or {}
            except ValueError:
                e = {}
            status = r.status_code if r.status_code in (409, 422, 502, 503) else 502
            return {}, problem(status, e.get("title") or "Grading failed",
                               e.get("detail") or f"HTTP {r.status_code} from the AI host.")
        grade = r.json()
        inputs = {**(grade.get("inputs") or {}), "documents": fates,
                  "news_note": news_note}
        if doc_note:
            inputs["document_note"] = doc_note
        log.info("risk_grade", extra={"company": company, "rating": grade.get("label"),
                                      "score": grade.get("score"), "by": who,
                                      "documents_used": sum(1 for f in fates if f.get("used")),
                                      "news": len(news)})
        result = {**grade, "inputs": inputs, "generated_by": who, "entity_id": entity_id,
                  "unchanged": False}
        last_read[key] = (fp, result)
        return result, None
