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
  fetched server-side (the dialog's news is only a fallback when PULSE is down);
* the **Register's own records** beyond the panorama's summary: every interaction
  with its full notes (VOX captures, calls, meetings), the external intelligence the
  news monitor filed (SEBI, IBBI, NSE, adverse news — not dismissed), the financial
  statements on record and the monitoring records that feed the IRG.

The files are prepared the way the CAM prepares them (``cam_pipeline``): a
transaction listing of any bank is summed month by month by PRISM, a workbook's short
sheets go before its bulk listings, a file uploaded twice is read once — and the
model is told which KINDS of document were read, so it never calls them missing.

A grade is computed only on request — one company, when someone asks — and cached
per company AND per visible-section set (24 h, in memory; "Regrade" computes afresh).
"""

from __future__ import annotations

import asyncio
import json
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
from app.cam_pipeline import _uniq, claims_missing, focus_workbook, summarise_ledgers
from app.docx_out import markdown_to_docx
from app.panorama_bridge import unpack_zip
from app.risk_facts import coverage, facts_of, flags_from, key_figures, mask_ids

log = get_logger("panorama.risk_grade")

_TTL_S = 24 * 3600
_RETRY_S = 3.0          # a DocRAG worker restart takes a few seconds
# The AI host's edge caps this body (2 MB); the rubric reads the recent footprint.
_KEEP = {"interactions": 30, "documents": 80, "leads": 25, "deals": 25, "lending": 25,
         "syndication": 40, "asset_monetisation": 25, "contacts": 25}
# Register rows that carry no current file to read.
_SKIP_STATUS = {"Pending", "Rejected", "Superseded", "Expired", "Waived"}
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # Excel 97–2003 (.xls) container
# The AI host accepts at most this many files per grade (RiskGradeIn.documents). A real
# credit file runs to 70 (Pulze); with ledgers summarised the text budget holds them.
_MAX_SENT = 150
# The Register records read beyond the panorama, newest first: (path, cap, filters).
_RECORDS = {"interactions": ("/v1/interactions", 120, {}),
            "intelligence": ("/v1/external-intelligence", 120, {}),
            "financials": ("/v1/financials", 60, {"is_current": "true"}),
            "monitoring": ("/v1/monitoring", 60, {})}
_RECORD_FIELD_MAX = 1500            # one field of one record (an interaction's notes)
_RECORDS_TOTAL = 100_000            # all records together, characters of JSON (~25k tokens)
_DROP = {"id", "tenant_id", "entity_id", "deal_id", "created_by", "updated_by",
         "deleted_at", "is_deleted", "row_version", "payload", "embedding"}
# A file's kind from its name or Data Register slot — what the grade tells the model
# it HAS read (cam_pipeline's kinds, so "missing" is judged the same way).
_KIND_OF_NAME = (("bank statement analysis", r"\bbsa\b|statement analysis"),
                 ("bank statement", r"\bbank|account statement|statement of account|current "
                                    r"account|\baccount \d|\bcc\b|\bod\b|optransaction"),
                 ("GST return", r"\bgst(?:r|r-?\d\w?)?\b.*(?:return|report|summary|3b|r1)|gstr"),
                 ("income tax return", r"\bitr\b|income[- ]tax|\bit fy"),
                 ("CMA / projections", r"\bcma\b|projection"),
                 ("financial statements", r"audited|balance ?sheet|bsheet|p ?& ?l|pandl|profit"
                                          r"|financ"),
                 ("credit bureau report", r"cibil|\bcir\b|crif|experian|equifax|bureau|cmr"),
                 ("legal / litigation report", r"legal|litigation"),
                 ("debtor / creditor ageing", r"debtor|creditor|ageing|aging"),
                 ("order / PO", r"\bpo\b|purchase order|order"),
                 ("sanction letter", r"sanction|term loan|vehicle loan|\bloan\b"),
                 ("debt profile", r"debt profile|loan schedule"),
                 ("term sheet", r"term ?sheet"),
                 ("management profile", r"promoter|management|founder|\bteam\b|\bcvs?\b"
                                        r"|resume|bio[- ]?data|\bbios?\b|director'?s?\s+profile"),
                 ("company profile", r"company profile|pitch|deck|brochure"),
                 ("net worth statement", r"net ?worth"),
                 ("constitution / registration", r"deed|registration certificate|incorporation"
                                                 r"|msme|udyam|gst certificate"),
                 ("KYC", r"\bpan\b|aadha?a?r|kyc|passport|voter"))
# A desk zip's members, typed by suffix for the reader (unpack_zip keeps only these).
_MEMBER_CTYPE = {".pdf": "application/pdf", ".xls": "application/vnd.ms-excel",
                 ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff"}


def is_zip(name: str, ctype: str, blob: bytes) -> bool:
    """A zip ARCHIVE of documents — not an .xlsx/.docx/.pptx, which are zips too and
    carry [Content_Types].xml at their root. (Sniffing for "xl/" in the first bytes
    mistook an archive holding an uncompressed .xlsx for a workbook.)"""
    if not blob.startswith(b"PK"):
        return False
    import io
    import zipfile
    try:
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" not in names
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
        # Every name part, as a whole word: "Asha R Menon" needs Asha AND Menon — a
        # surname alone names thousands, and the CAM took their news.
        parts = [p for p in re.split(r"[\s.]+", name.lower()) if len(p) > 2]
        return bool(parts) and all(re.search(rf"\b{re.escape(p)}\b", h) for p in parts)
    core = name_core(name)
    return bool(core) and bool(re.search(rf"\b{re.escape(core)}\b", h))


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


def fingerprint(panorama: dict[str, Any], news: list[dict[str, Any]],
                rows: list[dict[str, Any]], day: str,
                records: dict[str, list[dict[str, Any]]] | None = None) -> str:
    """What a grade would be computed from: the records, the Data Register LISTING
    (each file's id and checksum — a replaced file changes it), the news, the day
    (days-in-stage moves daily). Equal fingerprints → the same grade.

    Built from the listing, not the files' text, so a regrade with nothing new is
    answered before any file is downloaded or read — seconds, not minutes."""
    import hashlib
    import json as _json

    p = {k: v for k, v in (panorama or {}).items() if k != "generated_at"}
    body = {"day": day, "panorama": p, "records": records or {},
            "news": sorted(str(n.get("headline")) for n in news or []),
            "documents": sorted((str(d.get("id")), str(d.get("checksum") or d.get("size_bytes")),
                                 str(d.get("updated_at") or ""), str(d.get("section")),
                                 str(d.get("title") or ""), str(d.get("status") or ""))
                                for d in rows or [])}
    return hashlib.sha256(_json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def share_budget(lengths: list[int], per_file: int, total: int) -> list[int]:
    """Each file's share of the grade's text budget — every file gets one.

    Handed out smallest first: a short file takes all it needs, and what it leaves
    is split among the larger ones (never above ``per_file``). First-come-first-
    served let two bank-statement dumps eat the whole budget and dropped every file
    after them — which ones depended on the file count, so a regrade could read a
    different set and move the band."""
    shares = [0] * len(lengths)
    left, n = total, len(lengths)
    for i in sorted(range(len(lengths)), key=lambda j: lengths[j]):
        shares[i] = min(lengths[i], per_file, left // n)
        left -= shares[i]
        n -= 1
    return shares


def clip_middle(text: str, room: int) -> str:
    """``text`` cut to ``room`` characters from the MIDDLE: the head names the
    document and its first figures, the tail holds a statement's latest months and
    closing balance — a head-only cut lost exactly those."""
    if len(text) <= room:
        return text
    mark = f"\n\n[… {len(text) - room:,} characters left out here …]\n\n"
    head = max(0, (room - len(mark)) * 2 // 3)
    tail = max(0, room - len(mark) - head)
    return text[:head] + mark + (text[-tail:] if tail else "")


# What a file's FIRST pages say it is, when its name does not ("REPORT (2)", "W-630215792").
_KIND_OF_TEXT = (("bank statement", r"prism ledger summary|statement of account|account statement"
                                    r"|opening balance.{0,200}(?:withdrawal|debit)"),
                 ("credit bureau report", r"credit information report|commercial credit report"
                                          r"|\bcibil\b|\bcmr\b|credit score"),
                 ("income tax return", r"income tax return|\bitr-\d|assessment year"),
                 ("financial statements", r"balance sheet as (?:at|on)|statement of profit"
                                          r"|profit (?:and|&) loss"),
                 ("sanction letter", r"sanction letter|pleased to sanction|sanctioned limit"),
                 ("net worth statement", r"net ?worth"),
                 ("constitution / registration", r"partnership deed|certificate of (?:incorporation"
                                                 r"|registration)|udyam|registration certificate"),
                 ("legal / litigation report", r"litigation|court cases?|ecourts"),
                 ("KYC", r"permanent account number|income tax department|aadhaa?r"))
# Only where a section holds ONE kind: "Banking & Debt" holds statements AND sanction
# letters, and calling a letter a statement would vouch for bank data that is not there.
_SECTION_KIND = {"kyc": "KYC", "fin": "financial statements"}


def kind_of(name: str, section: str = "", text: str = "") -> str | None:
    """A file's kind: from its name ("BOI - …_BSA_Report" → bank statement analysis),
    else from what its first pages say, else from its Data Register section. Names are
    read as words — "_Cons_CIR_" and "a7ba_BSA_" hide theirs behind underscores."""
    words = " " + re.sub(r"[^a-z0-9&]+", " ", (name or "").lower()) + " "
    if (k := next((k for k, rx in _KIND_OF_NAME if re.search(rx, words)), None)):
        return k
    head = (text or "")[:6000].lower()
    if head and (k := next((k for k, rx in _KIND_OF_TEXT if re.search(rx, head, re.S)), None)):
        return k
    sec = (section or "").lower()
    return next((v for code, v in _SECTION_KIND.items()
                 if sec == code or section_name(code).lower() == sec), None)


def kinds_read(fates: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{kind: the files of that kind the grade READ} — a statement and its analysis
    answer the same question, so each counts for the other."""
    out: dict[str, list[str]] = {}
    for f in fates:
        if f.get("used") and (k := f.get("kind") or kind_of(f["name"], f.get("section") or "")):
            out.setdefault(k, []).append(f["name"])
    bank = out.get("bank statement", []) + out.get("bank statement analysis", [])
    if bank:
        out["bank statement"] = out["bank statement analysis"] = bank
    return out


def clean_record(row: dict[str, Any]) -> dict[str, Any]:
    """A Register record as the model reads it: no ids or audit plumbing, long text
    clipped to its first ``_RECORD_FIELD_MAX`` characters (an interaction's notes are
    the desk's own words — 1,500 characters, not the panorama's 300)."""
    out = {}
    for k, v in row.items():
        if k in _DROP or v in (None, "", [], {}):
            continue
        out[k] = v[:_RECORD_FIELD_MAX] + "…" if isinstance(v, str) and len(v) > _RECORD_FIELD_MAX \
            else v
    return out


def fit_records(records: dict[str, list[dict[str, Any]]],
                total: int = _RECORDS_TOTAL) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """(the records within ``total`` characters, notes) — every kind keeps its newest
    rows; the oldest are dropped first, round-robin, and the count is named."""
    import json as _json

    sizes = {k: [len(_json.dumps(r, default=str)) for r in rows] for k, rows in records.items()}
    keep = {k: len(rows) for k, rows in records.items()}

    def used() -> int:
        return sum(sum(sizes[k][:keep[k]]) for k in keep)

    while used() > total and any(keep.values()):
        k = max(keep, key=lambda x: (sum(sizes[x][:keep[x]]), x))
        keep[k] -= 1
    notes = [f"{k}: {len(records[k]) - keep[k]} oldest of {len(records[k])} left out (budget)"
             for k in records if keep[k] < len(records[k])]
    return {k: rows[:keep[k]] for k, rows in records.items()}, notes


def grade_claims_missing(grade: dict[str, Any], kinds: dict[str, list[str]]) -> list[str]:
    """Where the grade's own words call a kind of document missing or empty while the
    grade read one — the CAM review's "bank statements not captured", checked the same
    way (cam_pipeline.claims_missing) on every reason and evidence line."""
    lines: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            lines.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk({k: v for k, v in grade.items()
          if k not in ("inputs", "evidence", "usage", "data_gaps")})
    # A data gap IS an absence claim, whatever its wording ("BOI sanction letter for the
    # ₹2.5 Cr facility" — listed as a gap while the letters were read).
    lines += [f"Missing {g}" for g in grade.get("data_gaps") or [] if isinstance(g, str)]
    text = "\n".join(lines)
    return _uniq(claims_missing(text, kinds)) if text and kinds else []


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


_SEV = {"red": "🔴", "amber": "🟠", "info": "🔵"}
_STATUS = {"read": "✓ read", "partly read": "◐ partly read", "not read": "✗ not read",
           "no file": "— no file", "none found": "— none found"}
_FIG = (("revenue", "Revenue (₹ lakh)", "{:,.2f}"), ("growth", "Revenue growth", "{:+.1f}%"),
        ("ebitda", "EBITDA (₹ lakh)", "{:,.2f}"), ("ebitda_pct", "EBITDA margin", "pct"),
        ("pat", "PAT (₹ lakh)", "{:,.2f}"), ("tnw", "Tangible net worth (₹ lakh)", "{:,.2f}"),
        ("current_ratio", "Current ratio", "{:.2f}×"), ("tol_tnw", "TOL / TNW", "{:.2f}×"),
        ("td_ebitda", "Total debt / EBITDA", "{:.2f}×"), ("dscr", "DSCR", "{:.2f}×"))


def _cell(v: Any) -> str:
    return str(v or "").replace("|", "/").replace("\n", " ").strip()


def _trigger_status(status: str) -> str:
    return {"Hit": "**HIT**", "Clear": "Clear", "Unknown": "Unverified"}.get(status, status)


def report_markdown(g: dict[str, Any]) -> str:
    """The grade as a credit reader needs it: the verdict, how much of the file it
    stands on, the red flags PRISM read itself, the key numbers, the triggers, what
    to do — and the rubric's working last, as the appendix."""
    ev = g.get("evidence") or {}
    c = g.get("confidence_detail") or {}
    lines = [
        f"# Risk report — {g.get('company', '')}",
        "",
        f"**{g.get('label')}** · **{g.get('score')}/100** · confidence "
        f"{g.get('confidence_pct', g.get('confidence', '—'))}"
        f"{'%' if g.get('confidence_pct') is not None else ''} · recommended: **{g.get('action')}**",
        "",
        g.get("verdict") or "",
        "",
    ]
    if g.get("borderline"):
        lines += ["> Near a band line (45 / 70): a small change in the evidence can change "
                  "the colour.", ""]
    flags = ev.get("flags") or []
    reds = sum(1 for f in flags if f.get("severity") == "red")
    ambers = sum(1 for f in flags if f.get("severity") == "amber")
    hits = [t for t in g.get("hard_triggers") or [] if t.get("status") == "Hit"]
    unverified = [t for t in g.get("hard_triggers") or [] if t.get("status") == "Unknown"]
    if ev:
        lines += [f"**At a glance:** {reds} red flag(s), {ambers} amber · "
                  f"{len(hits)} hard trigger(s) hit · {len(unverified)} trigger(s) unverified.", ""]

    cov = ev.get("coverage") or []
    if cov:
        lines += ["## 1. What the grade stands on", "",
                  "| Evidence | Status | Read | Not read (why) |", "|---|---|---|---|"]
        for r in cov:
            read = "; ".join(r.get("read") or []) or "—"
            unread = "; ".join(f"{u['name']} ({u.get('reason') or 'not read'})"
                               for u in r.get("unread") or []) or (r.get("note") or "—")
            mark = " · checked by PRISM" if r.get("checked_by_prism") else ""
            lines.append(f"| {r['area']} | {_STATUS.get(r['status'], r['status'])}{mark} | "
                         f"{_cell(read)} | {_cell(unread)} |")
        lines += ["", "*Checked by PRISM:* the file's key fields were read by PRISM itself, "
                  "not only by the model.", ""]
    inputs = g.get("inputs") or {}
    rec = inputs.get("records") or {}
    if rec:
        lines += ["**Also read from the Register:** "
                  + "; ".join(f"{n} {k}" for k, n in rec.items())
                  + (f" ({inputs['record_note']})" if inputs.get("record_note") else "") + ".", ""]
    if inputs.get("claims_missing"):
        lines += ["> **Check:** the grade calls these missing, but PRISM read them — verify "
                  "before relying on that part of the reasoning:", ""]
        lines += [f"> - {_cell(x)}" for x in inputs["claims_missing"]] + [""]

    if ev:
        lines += ["## 2. Red flags", ""]
        if flags:
            lines += [f"- {_SEV.get(f['severity'], '•')} **{f['title']}** — {f['detail']} "
                      f"*({f['source']})*" for f in flags]
        else:
            lines.append("- No red or amber flags in the files PRISM could check.")
        lines.append("")

    kf = ev.get("key_figures")
    if kf and kf.get("rows"):
        years = kf.get("years") or []
        lines += [f"## 3. Key figures — {kf.get('source')}", "",
                  "| | " + " | ".join(years) + " |", "|---|" + "---|" * len(years)]
        for key, label, fmt in _FIG:
            row = kf["rows"].get(key)
            if not row:
                continue
            vals = []
            for y in years:
                v = row.get(y)
                vals.append("—" if v is None else f"{v:.1%}" if fmt == "pct" else fmt.format(v))
            lines.append(f"| {label} | " + " | ".join(vals) + " |")
        lines.append("")

    n = 4 if ev else 1
    lines += [f"## {n}. Hard red triggers", "", "| Trigger (rubric STEP 1) | Status | Evidence |",
              "|---|---|---|"]
    lines += [f"| {_cell(t.get('trigger'))} | {_trigger_status(str(t.get('status')))} | "
              f"{_cell(t.get('evidence'))} |" for t in g.get("hard_triggers") or []]
    lines += ["", "*Unverified* = the evidence for it was not read; it is not a clear.", ""]

    def block(title: str, items: list[str]) -> list[str]:
        return [f"## {title}", "", *([f"- {i}" for i in items] or ["- None given."]), ""]

    lines += block(f"{n + 1}. Risks", g.get("risks") or [])
    lines += block(f"{n + 2}. Mitigants", g.get("mitigants") or [])
    lines += block(f"{n + 3}. Conditions precedent (if proceeding)", g.get("conditions") or [])
    gaps = list(g.get("data_gaps") or [])
    for r in cov:
        for u in r.get("unread") or []:
            gaps.append(f"{u['name']} — on file but not read ({u.get('reason') or 'unknown'}); "
                        "send it in a readable form")
    lines += block(f"{n + 4}. Documents to request", gaps)
    lines += [f"## {n + 5}. What would move the rating", "",
              f"- **Up a band:** {g.get('move_up') or '—'}",
              f"- **Down a band:** {g.get('move_down') or '—'}", ""]

    lines += ["## Appendix A — How the score was built", "",
              "| Pillar | Weight | Score /10 | Points | Evidence |", "|---|---|---|---|---|"]
    for p in g.get("pillars") or []:
        pts = p.get("weighted") if p.get("available") else "no data — left out"
        lines.append(f"| {p.get('key')}. {p.get('name')} | {p.get('weight')}% | "
                     f"{p.get('score') if p.get('available') else '—'} | {pts} | "
                     f"{_cell(p.get('evidence'))} |")
    lines += ["", "Bands: GREEN ≥ 70 · AMBER 45–69 · RED < 45, scored over the pillars "
              "that have data. Any hard trigger → RED. No financials or banking → never "
              "GREEN (PROVISIONAL).", ""]
    if c:
        lines += [f"Confidence {g.get('confidence_pct')}% = 60% × data coverage "
                  f"({c.get('data_coverage')}% of the rubric's weight had data) + 40% × "
                  f"agreement ({c.get('agreement')}% — the independent readings spread "
                  f"{c.get('spread')} points).", ""]
    if g.get("runs"):
        lines += [f"Independent readings: {', '.join(str(r.get('score')) for r in g['runs'])} "
                  "(each pillar takes the median).", ""]
    if g.get("adjustments"):
        lines += block("Appendix B — Where the rules corrected the model", g["adjustments"])
    inp = g.get("inputs") or {}
    docs = inp.get("documents") or []
    lines += ["## Appendix C — Files", ""]
    if docs:
        lines += ["| File | Section | Read by | Used |", "|---|---|---|---|"]
        for d in docs:
            used = ((f"yes — {d['note']}" if d.get("note") else "yes") if d.get("used")
                    else f"no — {d.get('reason', '')}")
            lines.append(f"| {_cell(d.get('name'))} | {d.get('section') or '—'} | "
                         f"{', '.join(d.get('engines') or []) or '—'} | {_cell(used)} |")
        lines.append("")
    else:
        lines += [f"- Documents: none ({inp.get('document_note') or 'no files in the graded sections'})", ""]
    lines += [f"- Register: {inp.get('lending_lines', 0)} lending line(s), "
              f"{inp.get('platform_deals', 0)} platform deal(s), "
              f"{inp.get('interactions', 0)} interaction(s)",
              f"- News (PULSE): {inp.get('news_items', 0)} headline(s)"
              + (f" — {inp.get('news_note')}" if inp.get("news_note") else "")]
    if inp.get("register_sections_hidden"):
        lines.append(f"- Hidden from the requesting role: {', '.join(inp['register_sections_hidden'])}")
    lines += ["", "---", "",
              f"{g.get('engine')} · rubric {g.get('prompt')} ({g.get('prompt_version')}) · "
              f"graded {g.get('generated_at')} by {g.get('generated_by')}. Flags and key figures "
              "are read by PRISM from the files; pillar scores, risks and conditions are "
              "AI-assisted. A desk aid, not a credit decision."]
    return "\n".join(lines)


def mount_risk_grade(app: Any, settings: Any, *, denied: Any, verified_email: Any,
                     caller_context: Any, problem: Any, reg_headers: Any) -> None:
    base = settings.register_base_url.rstrip("/")
    chitti_url = (getattr(settings, "chitti_url", "") or "").rstrip("/")
    docrag_url = (getattr(settings, "docrag_url", "") or "").rstrip("/")
    pulse_url = (getattr(settings, "pulse_url", "") or "").rstrip("/")
    wanted_sections = str(getattr(settings, "risk_grade_doc_sections", "all") or "all")
    # None = every section ("all", the default); else the named ones only.
    sections: set[str] | None = (None if wanted_sections.strip().lower() in ("all", "*") else
                                 {section_name(s) for s in wanted_sections.split(",") if s.strip()})
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
        # One retry for a transient failure (DocRAG restarting answers 502 for a few
        # seconds): without it a regrade silently reads fewer files and the band moves.
        for attempt in (1, 2):
            try:
                r = await http.post(
                    f"{docrag_url}/v1/extract", files={"file": (f"{doc_id}{suffix}", blob)},
                    headers={"X-API-Key": settings.docrag_api_key,
                             "X-Tenant": request.headers.get("X-Tenant", settings.register_tenant)},
                    timeout=float(getattr(settings, "docrag_timeout_s", 600.0)))
            except httpx.HTTPError as exc:
                if attempt == 1:
                    await asyncio.sleep(_RETRY_S)
                    continue
                return "", f"document AI unreachable ({exc.__class__.__name__})", [], False
            if r.status_code >= 500 and attempt == 1:
                await asyncio.sleep(_RETRY_S)
                continue
            break
        if r.status_code >= 300:
            return "", f"document AI refused it (HTTP {r.status_code})", [], False
        body = r.json() or {}
        md = str(body.get("markdown") or "")
        engines = list(body.get("extraction_engines") or [])
        cached = bool(body.get("cached") or (body.get("telemetry") or {}).get("cached"))
        return md, (None if md.strip() else "no text could be read"), engines, cached

    async def _register_list(request: Request, caller: Any, who: str, path: str,
                             params: dict[str, Any], cap: int) -> tuple[list[dict], str | None]:
        """Every row of a Register list (paged by cursor, the API's 100 a page) up to
        ``cap``, read AS the caller. A page that fails ends the read and says so — the
        rows before it are kept, never presented as the whole list."""
        rows: list[dict] = []
        cursor: str | None = None
        while len(rows) < cap:
            q = {**params, "limit": 100, **({"cursor": cursor} if cursor else {})}
            try:
                r = await request.app.state.http.get(
                    f"{base}{path}", params=q, headers=reg_headers(request, caller, who, "GET", path))
            except httpx.HTTPError as exc:
                return rows, f"unreachable ({exc.__class__.__name__})"
            if r.status_code >= 300:
                return rows, f"refused the list (HTTP {r.status_code})"
            body = r.json() or {}
            rows += body.get("items", []) if isinstance(body, dict) else body
            cursor = body.get("next_cursor") if isinstance(body, dict) else None
            if not cursor:
                break
        return rows[:cap], None

    async def _records(request: Request, caller: Any, who: str,
                       entity_id: str | None) -> tuple[dict[str, list[dict]], list[str]]:
        """The Register's records about the company that the panorama only summarises —
        each read as the caller (a role that cannot see one gets a named gap)."""
        if not entity_id:
            return {}, []
        notes: list[str] = []

        async def one(kind: str) -> tuple[str, list[dict]]:
            path, cap, extra = _RECORDS[kind]
            rows, err = await _register_list(request, caller, who, path,
                                             {"entity_id": entity_id, **extra}, cap)
            if err is not None:
                notes.append(f"{kind}: {err}")
            if kind == "intelligence":
                rows = [r for r in rows if not r.get("is_dismissed")]
            stamp = {"interactions": "occurred_at", "intelligence": "observed_at",
                     "financials": "period_end", "monitoring": "due_date"}[kind]
            rows.sort(key=lambda r: str(r.get(stamp) or r.get("created_at") or ""), reverse=True)
            return kind, [clean_record(r) for r in rows]

        got = dict(await asyncio.gather(*(one(k) for k in _RECORDS)))
        fitted, cut = fit_records(got)
        return fitted, notes + cut

    async def _doc_rows(request: Request, caller: Any, who: str,
                        entity_id: str | None) -> tuple[list[dict], str | None]:
        """(the Data Register files the grade reads, in read order; note). The listing
        only — cheap, so a regrade can tell "nothing new" before reading any file."""
        if not entity_id:
            return [], "no client master record — the Data Register is not linked"
        listed, err = await _register_list(request, caller, who, "/v1/documents",
                                           {"entity_id": entity_id}, 1000)
        if err is not None:
            return [], f"Data Register: {err}"
        rows = [d for d in listed
                if (sections is None or section_name(d.get("section")) in sections)
                and (d.get("status") or "") not in _SKIP_STATUS
                and (d.get("original_filename") or d.get("storage_uri") or d.get("size_bytes"))]
        if not rows:
            return [], ("no files on the Data Register" if sections is None else
                        f"no files on the Data Register in {', '.join(sorted(sections))}")
        # The same file uploaded into two slots is read once.
        unique: dict[tuple[str, Any], dict] = {}
        for d in sorted(rows, key=read_order):
            unique.setdefault((str(d.get("original_filename") or d.get("id")).lower(),
                               d.get("checksum") or d.get("size_bytes")), d)
        return list(unique.values()), None

    async def _documents(request: Request, caller: Any, who: str, rows: list[dict],
                         keep_full: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
        """(texts for the grader, every file's fate, the facts PRISM read itself).
        Nothing is copied anywhere — text travels, with ID numbers masked. A zip is
        opened and each file in it read and listed on its own ("Bank Statements.zip /
        BOI Apr–Jun.pdf"). Facts are read from each file's FULL text, before the
        budget clips it. A fate whose read failed for a passing reason carries
        ``transient`` so that grade is never remembered as "the" grade of these files."""
        gate = asyncio.Semaphore(4)

        async def read_one(fate: dict, doc_id: str, blob: bytes, ctype: str,
                           name: str) -> dict:
            text, reason, engines, cached = await _extract(request, doc_id, blob, ctype, name)
            transient = reason is not None and (reason.startswith("document AI unreachable")
                                                or "(HTTP 5" in reason)
            return {**fate, "engines": engines, "cached": cached, "text": text,
                    **({"reason": reason} if reason else {}),
                    **({"transient": True} if transient else {})}

        async def one(d: dict) -> list[dict]:
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
                    return [{**fate, "transient": True,
                             "reason": f"register read failed ({exc.__class__.__name__})"}]
                if got.status_code >= 300:
                    return [{**fate, "transient": got.status_code >= 500,
                             "reason": f"register refused the read (HTTP {got.status_code})"}]
                ctype = got.headers.get("content-type") or d.get("content_type") or ""
                if not is_zip(name, ctype, got.content):
                    return [await read_one(fate, str(d.get("id")), got.content, ctype, name)]
                members, skips = unpack_zip(got.content, name)
                out = [{**fate, "name": s["file"].replace(f"{name}/", f"{name} / ", 1),
                        "in_zip": name, "reason": s["reason"]} for s in skips]
                for i, (member, data, suffix) in enumerate(members):
                    out.append(await read_one(
                        {**fate, "name": f"{name} / {member}", "in_zip": name},
                        f"{d.get('id')}-{i}", data, _MEMBER_CTYPE.get(suffix, ""), member))
                return out or [{**fate, "reason": "the zip holds no files"}]

        fates = [f for group in await asyncio.gather(*(one(d) for d in rows)) for f in group]
        read = [(f, f.pop("text", "") or "") for f in fates]
        facts = [x for f, t in read if t.strip()
                 for x in facts_of(f["name"].rsplit(" / ", 1)[-1], t)]
        read = [(f, t) for f, t in read if t.strip()]
        # Prepared as the CAM prepares them: ledgers summed by PRISM, workbooks' short
        # sheets first, a file whose text another already has read once.
        prepared: list[tuple[dict, str]] = []
        seen_text: dict[str, str] = {}
        for f, t in read:
            key = re.sub(r"\s+", " ", t).strip()
            if key in seen_text:
                f["reason"] = f"identical to '{seen_text[key]}' — read once"
                continue
            seen_text[key] = f["name"]
            said = []
            t, note = summarise_ledgers(t)
            f["kind"] = kind_of(f["name"], f.get("section") or "", t)
            said += [note] if note else []
            if "## Sheet:" in t:
                t, note = focus_workbook(t)
                said += [note] if note else []
            if said:
                f["prepared"] = "; ".join(said)
            prepared.append((f, t))
        read = prepared
        # Most-telling kinds first (actuals before projections, newest year first):
        # the AI host takes at most _MAX_SENT files, and the rest are named, not dropped.
        read.sort(key=lambda ft: read_order({"title": ft[0].get("title"),
                                             "original_filename": ft[0]["name"]}))
        if keep_full:
            # The full report reads every file whole (into fact sheets, as the CAM does):
            # nothing is clipped or capped here.
            whole: list[dict] = []
            for f, full in read:
                said = [x for x in (f.pop("prepared", None),) if x]
                f.update(used=True, chars=len(full), **({"note": "; ".join(said)} if said else {}))
                # Unmasked: this text goes to the CAM engine (as the CAM's does, so the
                # fact sheets are shared); what reaches the AI host is masked there.
                whole.append({"name": f["name"], "section": f["section"], "title": f["title"],
                              "engines": f["engines"], "text": full})
            return whole, fates, facts
        for f, _t in read[_MAX_SENT:]:
            f["reason"] = (f"not sent: one grade reads at most {_MAX_SENT} files, and "
                           "higher-priority kinds came first")
        read = read[:_MAX_SENT]
        shares = share_budget([len(t) for _f, t in read], doc_max, docs_total)
        texts: list[dict] = []
        for (f, full), room in zip(read, shares, strict=True):
            text = clip_middle(mask_ids(full), room)
            notes = [x for x in (f.pop("prepared", None),
                                 f"clipped: {len(text):,} of {len(full):,} characters, "
                                 "beginning and end kept" if len(full) > room else None) if x]
            f.update(used=True, chars=len(text), **({"note": "; ".join(notes)} if notes else {}))
            texts.append({"name": f["name"], "section": f["section"], "title": f["title"],
                          "engines": f["engines"], "text": text})
        return texts, fates, facts

    # ------------------------------------------------------------------ PULSE
    async def _pulse(request: Request, p: dict, company: str) -> tuple[list[dict], str | None]:
        """The firm's and its key people's news, PULSE's verdicts, last 12 months —
        a default or a raid 100 days ago is as much the grade's business as last week's."""
        if not pulse_url:
            return [], "PULSE not configured"
        s = p.get("stats") or {}
        live = "live" if (s.get("deals_in_flight") or 0) + (s.get("deals_done") or 0) else ""
        since = (datetime.now(UTC).date() - timedelta(days=365)).isoformat()
        people = list(dict.fromkeys(c.get("name") for c in (p.get("contacts") or [])
                                    if c.get("name")))[:6]
        core = name_core(company) or company
        # Exact-phrase searches; every hit is then checked to actually name the firm/person.
        terms = [(f'"{core}"', company, 30)] + [(f'"{n}"', n, 8) for n in people]
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
        return out[:80], (f"{failures} PULSE search(es) failed" if failures else None)

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
        (rows, doc_note), (news, news_note), (records, record_notes) = await asyncio.gather(
            _doc_rows(request, caller, who, entity_id), _pulse(request, p, company),
            _records(request, caller, who, entity_id))
        if not news and news_note:
            # PULSE could not be asked from here: fall back to what the dialog showed.
            news = [n.model_dump() for n in payload.news]
            if news:
                news_note = f"{news_note}; the dialog's 30-day news used instead"
        elif not news:
            news_note = "no headlines naming the firm or its people in the last 12 months"
        trimmed = trim_panorama(p)
        fp = fingerprint(trimmed, news, rows, datetime.now(UTC).date().isoformat(), records)
        prior = last_read.get(key)
        if prior and prior[0] == fp:
            # Nothing the grade reads has changed since the last one: the same grade,
            # not a fresh model reading of the same facts (which would wobble) — and
            # answered before any file is downloaded or read again.
            log.info("risk_grade_unchanged", extra={"company": company, "by": who})
            return {**prior[1], "unchanged": True}, None
        docs, fates, facts = (await _documents(request, caller, who, rows) if rows
                              else ([], [], []))
        flags = flags_from(facts)
        figures = key_figures(facts)
        unread = sorted({f.get("in_zip") or f["name"] for f in fates if not f.get("used")}
                        - {f.get("in_zip") or f["name"] for f in fates if f.get("used")})
        # Tell the model which listed files it actually has: a file NAME is never
        # evidence ("GSTR3B Reports.zip uploaded" cleared a statutory trigger once).
        fate_of: dict[str, bool] = {}
        for f in fates:
            fname = (f.get("in_zip") or f["name"]).lower()
            fate_of[fname] = bool(fate_of.get(fname) or f.get("used"))
        for row in trimmed.get("documents") or []:
            if isinstance(row, dict) and (fn := str(row.get("filename") or "").lower()) in fate_of:
                row["read_by_grade"] = "yes" if fate_of[fn] else "NO — not evidence"
        kinds = kinds_read(fates)
        sent = {"company": company, "panorama": trimmed, "news": news, "documents": docs,
                "records": records,
                "facts": {"flags": flags, "key_figures": figures, "unread_files": unread,
                          "kinds_read": kinds,
                          "bureau_scores": [{"person": f["person"], "score": f["score"]}
                                            for f in facts if f["kind"] == "cir"],
                          "read_files": [f["name"] for f in fates if f.get("used")],
                          "triggers": [{"clause": f["trigger"],
                                        "evidence": f"{f['title']} — {f['detail']} "
                                                    f"({f['source']})"}
                                       for f in flags if f.get("trigger")]}}
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
                  "news_note": news_note,
                  "records": {k: len(v) for k, v in records.items()},
                  **({"record_note": "; ".join(record_notes)} if record_notes else {})}
        # The CAM review's check, on the grade: a kind of document it read but calls
        # missing. Named on the grade (and its report), never silently passed.
        said_missing = grade_claims_missing(grade, kinds)
        if said_missing:
            inputs["claims_missing"] = said_missing
        if doc_note:
            inputs["document_note"] = doc_note
        log.info("risk_grade", extra={"company": company, "rating": grade.get("label"),
                                      "score": grade.get("score"), "by": who,
                                      "documents_used": sum(1 for f in fates if f.get("used")),
                                      "news": len(news)})
        result = {**grade, "inputs": inputs, "generated_by": who, "entity_id": entity_id,
                  "unchanged": False,
                  "evidence": {"flags": flags, "key_figures": figures,
                               "coverage": coverage(fates, facts, len(news), news_note)}}
        if any(f.get("transient") for f in fates):
            # A file could not be read this time (DocRAG restarting, a Register blip):
            # the next regrade must read again, not repeat this short-handed grade.
            log.warning("risk_grade_partial_read", extra={
                "company": company, "unread": sum(1 for f in fates if f.get("transient"))})
        else:
            last_read[key] = (fp, result)
        return result, None

    # ------------------------------------------------------------------ the IRG report
    # The full report: the CAM's reading and dataset, the rubric's grade on the fact
    # sheets, and one section per pillar explaining it (app.irg). Minutes, so a job.
    full_jobs: dict[str, dict[str, Any]] = {}
    full_tasks: set[asyncio.Task] = set()

    async def _ask_chitti(request: Any, sent: dict[str, Any]) -> tuple[dict | None, str | None]:
        http = getattr(request.app.state, "docrag_http", None) or request.app.state.http
        try:
            r = await http.post(f"{chitti_url}/v1/risk-grade",
                                headers={"X-API-Key": settings.chitti_api_key,
                                         "X-Tenant": request.headers.get("X-Tenant",
                                                                         settings.register_tenant)},
                                json=sent, timeout=float(getattr(settings, "chitti_timeout_s", 300.0)))
        except httpx.HTTPError as exc:
            return None, f"the AI host did not answer ({exc.__class__.__name__})"
        if r.status_code >= 300:
            try:
                e = (r.json() or {}).get("error") or {}
            except ValueError:
                e = {}
            return None, f"{e.get('title') or 'grading failed'}: {e.get('detail') or r.status_code}"
        return r.json(), None

    async def _cam_master(request: Any, caller: Any, who: str) -> Any:
        """The tenant's CAM master prompt (its rules make the fact sheets the CAM's own,
        cached), or None — the IRG then uses its own fallback rules."""
        from app.cam_pipeline import parse_master_prompt

        try:
            path = "/v1/templates/cam_prompt"
            t = await request.app.state.http.get(f"{base}{path}",
                                                 headers=reg_headers(request, caller, who, "GET", path))
            tid = (t.json() or {}).get("id") if t.status_code < 300 else None
            if not tid:
                return None
            cpath = f"/v1/documents/{tid}/content"
            c = await request.app.state.http.get(
                f"{base}{cpath}", headers=reg_headers(request, caller, who, "GET", cpath),
                follow_redirects=True, timeout=60.0)
            if c.status_code >= 300:
                return None
            text, _why = extract_text(c.headers.get("content-type") or "", c.content)
            return parse_master_prompt(text)
        except (httpx.HTTPError, ValueError):
            return None

    def _report_store() -> Any:
        from pathlib import Path

        d = getattr(settings, "cam_digest_cache_dir", "") or ""
        return Path(d) / "irg" if d else None

    def _stored(fp: str) -> dict | None:
        store = _report_store()
        try:
            return json.loads((store / f"{fp}.json").read_text()) if store else None
        except (OSError, ValueError):
            return None

    def _store(fp: str, value: dict) -> None:
        store = _report_store()
        if store is None:
            return
        try:
            store.mkdir(parents=True, exist_ok=True)
            tmp = store / f".{fp}.tmp"
            tmp.write_text(json.dumps(value, default=str))
            tmp.replace(store / f"{fp}.json")
        except OSError as exc:
            log.warning("irg_report_store_failed", extra={"error": str(exc)})

    async def _run_full(job: dict[str, Any], ctx: Any, caller: Any, who: str,
                        p: dict, company: str, entity_id: str | None,
                        gathered: tuple, fp: str) -> None:
        from app import irg
        from app.cam import _SYSTEM, build_engine
        from app.cam_pipeline import DigestCache, SourceDoc, draft_staged, qa_markdown

        def progress(stage: str, done: int, total: int) -> None:
            job.update(stage=stage, done=done, total=total, updated=time.time())

        try:
            progress("Reading the file", 0, 1)
            (rows, doc_note), (news, news_note), (records, record_notes) = gathered
            texts, fates, facts = (await _documents(ctx, caller, who, rows, keep_full=True)
                                   if rows else ([], [], []))
            flags, figures = flags_from(facts), key_figures(facts)
            kinds = kinds_read(fates)
            docs = [SourceDoc(f"d{n}", t["name"], t["text"], t.get("section") or "", None)
                    for n, t in enumerate(texts) if t["text"].strip()]
            if not docs:
                raise RuntimeError("no file on the Data Register could be read"
                                   + (f" ({doc_note})" if doc_note else ""))
            engine = build_engine(settings)
            cam_prompt = await _cam_master(ctx, caller, who)
            prompt = irg.irg_prompt(cam_prompt)
            trimmed = trim_panorama(p)
            unread = sorted({f.get("in_zip") or f["name"] for f in fates if not f.get("used")})
            facts_sent = {"flags": flags, "key_figures": figures, "unread_files": unread,
                          "kinds_read": kinds,
                          "bureau_scores": [{"person": f["person"], "score": f["score"]}
                                            for f in facts if f["kind"] == "cir"],
                          "read_files": [f["name"] for f in fates if f.get("used")],
                          "triggers": [{"clause": f["trigger"],
                                        "evidence": f"{f['title']} — {f['detail']} ({f['source']})"}
                                       for f in flags if f.get("trigger")]}

            async def grade_step(typed: list[tuple[str, str]], dataset: str, res: Any) -> str:
                sent = {"company": company, "panorama": trimmed, "news": news,
                        "documents": irg.documents_for_grade(typed, dataset),
                        "records": records, "facts": facts_sent}
                g, why = await _ask_chitti(ctx, sent)
                if g is None:
                    raise RuntimeError(f"The rubric could not grade: {why}")
                res.extra["grade"] = g
                return irg.grade_context(g)

            public = "\n".join(
                [f"PULSE news (12 months): {len(news)} headline(s) naming the firm or its "
                 "people" + (f" — {news_note}" if news_note else "")]
                + [f"  - {n.get('when')} · {n.get('headline')} — {n.get('source')} · "
                   f"{n.get('severity') or 'untagged'}" for n in news[:40]]
                + ["Intelligence on the Register (not dismissed):"]
                + [f"  - {r.get('observed_at') or '?'} · {r.get('source') or '?'} · "
                   f"{r.get('signal') or '—'} · {r.get('title')}"
                   for r in records.get("intelligence") or []][:60])
            notes = json.dumps({"interactions": records.get("interactions") or [],
                                "monitoring": records.get("monitoring") or [],
                                "financials_on_register": records.get("financials") or []},
                               ensure_ascii=False, default=str)[:60_000]
            http = ctx.app.state.http
            effort = (getattr(settings, "cam_digest_reasoning_effort", "") or None
                      if "reasoning_effort" in getattr(engine, "extra_body", {}) else None)
            res = await draft_staged(
                generate=lambda sys, t, **kw: engine.generate(http, sys, t, **kw),
                system=_SYSTEM, prompt=prompt, docs=docs,
                analyst_notes=f"The Register's own records about the company:\n{notes}",
                cache=DigestCache(directory=getattr(settings, "cam_digest_cache_dir", "") or None),
                model_id=engine.name, progress=progress,
                concurrency=int(getattr(settings, "cam_pipeline_concurrency", 8)),
                call_budget=int(getattr(settings, "cam_call_budget_chars", 360_000)),
                digest_part_chars=int(getattr(settings, "cam_digest_part_chars", 90_000)),
                deal_context=irg.deal_text(p), public_context=public, retry_delay_s=10.0,
                digest_effort=effort, doc_title=irg.IRG_TITLE, interlude=grade_step,
                section_effort=((getattr(settings, "irg_section_reasoning_effort", "") or None)
                                if effort else None),
                light_dataset=True)
            g = res.extra.get("grade") or {}
            # A file the reading left out as redundant (covered by an analysis, an older
            # extract, a consent form) is on record but not read — and says why.
            left_out = {n["document"]: n["note"] for n in res.notes if n.get("skipped")}
            for f in fates:
                if f["name"] in left_out:
                    f.update(used=False, reason=left_out[f["name"]].removeprefix("not read: "))
            workpaper = res.workpaper_md.split("## Machine QA report")[0]
            md = irg.assemble(res.draft_md, g, flags, irg.appendix_md(
                g, fates, kinds, {k: len(v) for k, v in records.items()},
                qa_markdown(res.qa), workpaper))
            said_missing = grade_claims_missing(g, kinds)
            inputs = {**(g.get("inputs") or {}), "documents": fates, "news_note": news_note,
                      "records": {k: len(v) for k, v in records.items()},
                      **({"record_note": "; ".join(record_notes)} if record_notes else {}),
                      **({"claims_missing": said_missing} if said_missing else {})}
            grade = {**g, "inputs": inputs, "generated_by": who, "entity_id": entity_id,
                     "unchanged": False, "evidence": {
                         "flags": flags, "key_figures": figures,
                         "coverage": coverage(fates, facts, len(news), news_note)}}
            # The full report's grade is the company's grade too (Company 360 shows it).
            cache[_key(entity_id, company, p.get("restricted") or [])] = (time.monotonic(), grade)
            job.update(status="done", stage="Done", grade=grade, report_md=md, qa=res.qa,
                       calls=res.calls, notes=res.notes, finished=time.time())
            if not any(f.get("transient") for f in fates):
                # Nothing on file changes → the next request is answered from this.
                _store(fp, {"grade": grade, "report_md": md, "qa": res.qa,
                            "generated": time.time()})
            log.info("irg_report", extra={"company": company, "rating": g.get("label"),
                                          "score": g.get("score"), "calls": res.calls,
                                          "seconds": round(time.time() - job["started"])})
        except Exception as exc:  # noqa: BLE001 — the job reports it; nothing half-filed
            log.warning("irg_report_failed", extra={"company": company, "error": str(exc)})
            job.update(status="failed", error=str(exc), finished=time.time())

    def _full_view(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    @app.post("/v1/panorama/risk-grade/full", status_code=202, tags=["Company 360"],
              summary="The IRG report: the CAM's reading + the rubric's grade, explained (job)")
    async def risk_grade_full(payload: RiskGradeIn, request: Request) -> Any:
        resp, who, caller = await _identity(request)
        if resp is not None:
            return resp
        if not chitti_url:
            return problem(409, "No grading service configured",
                           "Set WORKFLOWS_CHITTI_URL and WORKFLOWS_CHITTI_API_KEY.")
        p, err = await _panorama(request, caller, who, payload.entity_id, payload.company)
        if err is not None:
            return err
        assert p is not None
        import uuid as _uuid
        from types import SimpleNamespace

        company = (p.get("anchor") or {}).get("name") or payload.company
        entity_id = payload.entity_id or (p.get("anchor") or {}).get("entity_id")
        job_id = _uuid.uuid4().hex
        job = {"job_id": job_id, "company": company, "entity_id": entity_id,
               "status": "running", "stage": "Starting", "done": 0, "total": 0,
               "started": time.time(), "_who": who.lower()}
        # What the report would be built from — the listing, the records, the news, the
        # day. A report built from exactly that is answered at once, not recomputed.
        gathered = await asyncio.gather(_doc_rows(request, caller, who, entity_id),
                                        _pulse(request, p, company),
                                        _records(request, caller, who, entity_id))
        (rows, _dn), (news, _nn), (records, _rn) = gathered
        fp = fingerprint(trim_panorama(p), news, rows, datetime.now(UTC).date().isoformat(),
                         records)
        if not payload.refresh and (hit := _stored(fp)) is not None:
            job.update(status="done", stage="Done", grade=hit["grade"],
                       report_md=hit["report_md"], qa=hit.get("qa"), reused=True,
                       finished=time.time())
            full_jobs[job_id] = job
            cache[_key(entity_id, company, p.get("restricted") or [])] = (time.monotonic(),
                                                                        hit["grade"])
            return {k: v for k, v in _full_view(job).items() if k != "report_md"}
        for old in sorted(full_jobs.values(), key=lambda j: j["started"])[:-50]:
            full_jobs.pop(old["job_id"], None)
        full_jobs[job_id] = job
        ctx = SimpleNamespace(headers=dict(request.headers), app=request.app)
        task = asyncio.create_task(_run_full(job, ctx, caller, who, p, company, entity_id,
                                             gathered, fp))
        full_tasks.add(task)
        task.add_done_callback(full_tasks.discard)
        return {k: v for k, v in _full_view(job).items() if k != "report_md"}

    def _own_job(job_id: str, who: str) -> dict[str, Any] | None:
        job = full_jobs.get(job_id)
        return job if job is not None and job.get("_who") == who.lower() else None

    @app.get("/v1/panorama/risk-grade/full/{job_id}", tags=["Company 360"],
             summary="Progress of an IRG report job; the grade and report when done")
    async def risk_grade_full_job(job_id: str, request: Request) -> Any:
        resp, who, _caller = await _identity(request)
        if resp is not None:
            return resp
        job = _own_job(job_id, who)
        if job is None:
            return problem(404, "No such job", "That IRG report job is not yours or has expired.")
        return _full_view(job)

    @app.get("/v1/panorama/risk-grade/full/{job_id}/report", tags=["Company 360"],
             summary="The IRG report as a Word document")
    async def risk_grade_full_docx(job_id: str, request: Request) -> Any:
        resp, who, _caller = await _identity(request)
        if resp is not None:
            return resp
        job = _own_job(job_id, who)
        if job is None or job.get("status") != "done":
            return problem(404, "No report", "The IRG report is not ready (or not yours).")
        blob = markdown_to_docx(job["report_md"], None, cam=True)
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in str(job["company"]))
        return Response(content=blob, media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            headers={"Content-Disposition": f'attachment; filename="IRG report - {safe.strip()}'
                                            f' - {date.today().isoformat()}.docx"'})
