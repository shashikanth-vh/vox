"""reports.py — server-side persistence for pending captures (the RM's report list).

The PoC kept every pending report in browser localStorage: per device, gone with the
tab. This store makes the report list a BACKEND fact, so an RM can record on the phone,
edit on the laptop, and nothing is lost when a device dies:

    draft      auto-saved at preview time (every capture is recoverable immediately)
    ready      the RM saved edits / marked it ready to approve
    committed  the commit succeeded — the record carries the write results and turns
               read-only in spirit (kept for the audit trail until deleted)

Storage follows the platform rule — small JSON documents in MinIO/S3 under
``reports/<rm>/<capture_id>.json`` (same bucket as the audio captures), with the vocx
volume as the no-S3 fallback. Reports are single-writer by nature (an RM edits their own
capture), so last-write-wins is the intended concurrency model. Every failure path logs
and degrades — losing an EDIT is acceptable, wedging a capture flow is not — except
``save`` itself, which reports failure honestly so the UI can retry.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

log = logging.getLogger("vocx")

MAX_REPORT_BYTES = 512 * 1024          # an extraction is a few KB; half a MB is generous
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_STATUSES = ("draft", "ready", "committed")


def _safe_rm(rm: str) -> str:
    return "".join(c for c in (rm or "") if c.isalnum() or c in "._-")[:80] or "rm"


def valid_id(capture_id: str) -> bool:
    return bool(capture_id) and bool(_ID_RE.match(capture_id))


def _summary_of(doc: dict[str, Any]) -> dict[str, Any]:
    """The list-view projection — enough for the report list without the full payload."""
    ext = (doc.get("report") or {}).get("extraction") or {}
    em = ext.get("entity_match") or {}
    return {
        "capture_id": doc.get("capture_id"),
        "rm": doc.get("rm"),
        "status": doc.get("status"),
        "updated_at": doc.get("updated_at"),
        "company": ext.get("company_mentioned") or em.get("proposed_company"),
        "entity_code": em.get("code"),
        "needs_approval": ((doc.get("report") or {}).get("decision") or {}).get("needs_approval"),
        "summary": (doc.get("report") or {}).get("summary") or "",
    }


class LocalReportStore:
    """Volume-backed JSON documents — the no-S3 fallback tier."""

    kind = "local"

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def _path(self, rm: str, capture_id: str) -> str:
        return os.path.join(self.directory, _safe_rm(rm), f"{capture_id}.json")

    def save(self, rm: str, capture_id: str, doc: dict[str, Any]) -> bool:
        try:
            path = self._path(rm, capture_id)
            with self._lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False)
                os.replace(tmp, path)          # atomic on POSIX — no torn reads
            return True
        except OSError as e:
            log.error("vocx reports: local save failed for %s/%s: %s", rm, capture_id, e)
            return False

    def get(self, rm: str, capture_id: str) -> dict[str, Any] | None:
        try:
            with open(self._path(rm, capture_id), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            log.error("vocx reports: local read failed for %s/%s: %s", rm, capture_id, e)
            return None

    def list(self, rm: str) -> list[dict[str, Any]]:
        d = os.path.join(self.directory, _safe_rm(rm))
        out = []
        try:
            names = sorted(os.listdir(d))
        except FileNotFoundError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            doc = self.get(rm, name[:-5])
            if doc:
                out.append(_summary_of(doc))
        out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return out

    def delete(self, rm: str, capture_id: str) -> bool:
        try:
            os.remove(self._path(rm, capture_id))
            return True
        except FileNotFoundError:
            return True                        # deleting the absent is success
        except OSError as e:
            log.error("vocx reports: local delete failed for %s/%s: %s", rm, capture_id, e)
            return False


class S3ReportStore:
    """MinIO/S3 JSON documents under reports/<rm>/ — shares the captures bucket."""

    kind = "s3"

    def __init__(self, *, bucket: str, endpoint_url: str, access_key_id: str,
                 secret_access_key: str, auto_create_bucket: bool = True,
                 prefix: str = "reports",
                 fallback: LocalReportStore | None = None) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.auto_create_bucket = auto_create_bucket
        self.prefix = prefix.strip("/")
        self.fallback = fallback
        self._client: Any = None
        self._lock = threading.Lock()

    def _s3(self) -> Any:
        with self._lock:
            if self._client is None:
                import boto3
                from botocore.config import Config
                self._client = boto3.client(
                    "s3", endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    config=Config(s3={"addressing_style": "path"},
                                  retries={"max_attempts": 3, "mode": "standard"},
                                  connect_timeout=5, read_timeout=15))
                from botocore.exceptions import ClientError
                try:
                    self._client.head_bucket(Bucket=self.bucket)
                except ClientError:
                    if self.auto_create_bucket:
                        self._client.create_bucket(Bucket=self.bucket)
            return self._client

    def _key(self, rm: str, capture_id: str) -> str:
        return f"{self.prefix}/{_safe_rm(rm)}/{capture_id}.json"

    def save(self, rm: str, capture_id: str, doc: dict[str, Any]) -> bool:
        body = json.dumps(doc, ensure_ascii=False).encode()
        try:
            self._s3().put_object(Bucket=self.bucket, Key=self._key(rm, capture_id),
                                  Body=body, ContentType="application/json")
            return True
        except Exception as e:  # noqa: BLE001 — degrade to the volume, report honestly
            log.error("vocx reports: S3 save failed (%s) — falling back to local", e)
            return self.fallback.save(rm, capture_id, doc) if self.fallback else False

    def get(self, rm: str, capture_id: str) -> dict[str, Any] | None:
        try:
            r = self._s3().get_object(Bucket=self.bucket, Key=self._key(rm, capture_id))
            return json.loads(r["Body"].read())
        except Exception:  # noqa: BLE001 — missing or unreachable → try the fallback
            return self.fallback.get(rm, capture_id) if self.fallback else None

    def list(self, rm: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            s3 = self._s3()
            token: str | None = None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": f"{self.prefix}/{_safe_rm(rm)}/",
                      "MaxKeys": 200}
                if token:
                    kw["ContinuationToken"] = token
                page = s3.list_objects_v2(**kw)
                for obj in page.get("Contents", []):
                    cid = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
                    doc = self.get(rm, cid)
                    if doc:
                        out.append(_summary_of(doc))
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        except Exception as e:  # noqa: BLE001
            log.error("vocx reports: S3 list failed (%s) — falling back to local", e)
            return self.fallback.list(rm) if self.fallback else []
        out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return out

    def delete(self, rm: str, capture_id: str) -> bool:
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=self._key(rm, capture_id))
            return True
        except Exception as e:  # noqa: BLE001
            log.error("vocx reports: S3 delete failed (%s)", e)
            return self.fallback.delete(rm, capture_id) if self.fallback else False


def make_doc(rm: str, capture_id: str, status: str, report: dict[str, Any],
             writes: dict[str, Any] | None = None) -> dict[str, Any]:
    if status not in _STATUSES:
        status = "draft"
    doc = {"capture_id": capture_id, "rm": rm, "status": status,
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "report": report}
    if writes is not None:
        doc["writes"] = writes
    return doc


def build_report_store(settings: Any) -> S3ReportStore | LocalReportStore | None:
    tokens_dir = getattr(settings, "tokens_dir", "") or ""
    local = LocalReportStore(os.path.join(tokens_dir, "reports")) if tokens_dir else None
    endpoint = getattr(settings, "s3_endpoint_url", "") or ""
    bucket = getattr(settings, "s3_bucket", "") or ""
    if endpoint and bucket:
        return S3ReportStore(
            bucket=bucket, endpoint_url=endpoint,
            access_key_id=getattr(settings, "s3_access_key_id", "") or "",
            secret_access_key=getattr(settings, "s3_secret_access_key", "") or "",
            auto_create_bucket=bool(getattr(settings, "s3_auto_create_bucket", True)),
            fallback=local)
    return local


# --- print view ----------------------------------------------------------------
def _details_rows(ext: dict[str, Any], rep: dict[str, Any],
                  config: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """The DETAILS table both renderers share: client/follow-up + the deal facts +
    labeled template extras. One assembly, so a field added here shows up in the
    print view AND the downloaded PDF without anyone remembering to do it twice."""
    nm = ext.get("next_meeting") or {}
    rows: list[tuple[str, Any]] = [("Client", rep.get("title") or ext.get("company_mentioned"))]
    if nm.get("date"):
        follow = str(nm["date"]) + (f" {nm['time']}" if nm.get("time") else "") \
            + (f" ({nm['mode']})" if nm.get("mode") else "")
        rows.append(("Follow-up", follow))
    for label, key in (("Sector", "sector"), ("Project type", "project_type"),
                       ("Project size", "project_size"), ("Location", "location"),
                       ("Loan product", "loan_product"), ("Ticket size", "ticket_size"),
                       ("Collateral", "collateral"), ("Equity raised", "equity_raised"),
                       ("Turnover", "turnover"), ("Pipeline stage", "pipeline_stage"),
                       ("Opportunity score", "opportunity_score"), ("Temperature", "deal_temp")):
        if rep.get(key) not in (None, ""):
            rows.append((label, rep[key]))
    labels: dict[str, str] = {}
    for t in (config or {}).get("report_templates", []):
        for fdef in t.get("fields", []):
            if fdef.get("key"):
                labels[fdef["key"]] = fdef.get("label", fdef["key"])
    for fdef in rep.get("_custom") or []:
        if fdef.get("key"):
            labels[fdef["key"]] = fdef.get("label", fdef["key"])
    for k, v in (rep.get("extra") or {}).items():
        if v not in (None, "", []):
            rows.append((labels.get(k, k), v))
    return [(lbl, v) for lbl, v in rows if v not in (None, "")]


def render_print_html(doc: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    """The report as print-ready HTML — the PoC's "Download PDF" is the browser printing
    exactly this kind of view, so parity means serving it, not bundling a PDF engine."""
    import html as _html

    def esc(v: Any) -> str:
        return _html.escape(str(v)) if v not in (None, "") else ""

    ext = (doc.get("report") or {}).get("extraction") or {}
    rep = ext.get("report") or {}
    meta = ext.get("_meta") or {}
    title = rep.get("title") or (ext.get("entity_match") or {}).get("canonical_name") \
        or ext.get("company_mentioned") or "Field report"
    ts = (meta.get("capture_ts") or doc.get("updated_at") or "")[:16].replace("T", " ")
    rows = _details_rows(ext, rep, config)

    def section(name: str, body: str) -> str:
        return f'<h2>{esc(name)}</h2>\n{body}' if body.strip() else ""

    bullets = "".join(f"<li>{esc(k)}</li>" for k in rep.get("key_intel") or [] if k)
    nuances = "".join(f"<li>{esc(n)}</li>" for n in rep.get("nuances") or [] if n)
    steps = "".join(
        "<li>{}{}{}</li>".format(
            f"<b>{esc(s.get('owner'))}:</b> " if s.get("owner") else "",
            esc(s.get("action")), f" — due {esc(s.get('date'))}" if s.get("date") else "")
        for s in rep.get("next_steps") or [] if s.get("action"))
    attendees = " · ".join(
        " ".join(x for x in (esc(a.get("name")), f"({esc(a.get('role'))})" if a.get("role") else "",
                             f"— {esc(a.get('company'))}" if a.get("company") else "") if x)
        for a in rep.get("attendees") or [] if a.get("name"))
    detail_rows = "".join(f"<tr><td>{esc(lbl)}</td><td>{esc(v)}</td></tr>" for lbl, v in rows
                          if v not in (None, ""))
    transcript = rep.get("transcript_english") or ""
    status = esc((doc.get("status") or "draft").upper())

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
  body {{ font: 14px/1.55 system-ui, sans-serif; color: #111; max-width: 800px;
         margin: 24px auto; padding: 0 16px; }}
  .brand {{ color: #0e7a7a; font-weight: 700; letter-spacing: .06em; font-size: 12px; }}
  h1 {{ margin: 4px 0 2px; font-size: 26px; }}
  .ts {{ color: #666; margin-bottom: 18px; }}
  h2 {{ color: #0e7a7a; font-size: 13px; letter-spacing: .08em; text-transform: uppercase;
        border-bottom: 1px solid #ddd; padding-bottom: 4px; margin: 22px 0 8px; }}
  table {{ border-collapse: collapse; }}
  td {{ padding: 3px 24px 3px 0; vertical-align: top; }}
  td:first-child {{ color: #666; min-width: 140px; }}
  ul {{ margin: 6px 0; padding-left: 22px; }}
  .print-btn {{ position: fixed; top: 12px; right: 12px; padding: 8px 14px; }}
  @media print {{ .print-btn {{ display: none; }} }}
</style></head><body>
<button class="print-btn" onclick="window.print()">Print / Save as PDF</button>
<div class="brand">VOCX · EVAM FIELD INTEL · {status}</div>
<h1>{esc(title)}</h1>
<div class="ts">{esc(ts)}</div>
{section("Summary", f"<p>{esc(rep.get('summary'))}</p>" if rep.get("summary") else "")}
{section("Key intelligence", f"<ul>{bullets}</ul>" if bullets else "")}
{section("Details", f"<table>{detail_rows}</table>" if detail_rows else "")}
{section("Next steps", f"<ul>{steps}</ul>" if steps else "")}
{section("Nuances", f"<ul>{nuances}</ul>" if nuances else "")}
{section("Attendees", f"<p>{attendees}</p>" if attendees else "")}
{section("Full transcript (English)", f"<p>{esc(transcript)}</p>" if transcript else "")}
</body></html>"""


# --- pdf download ---------------------------------------------------------------
_DEJAVU_DIR = "/usr/share/fonts/truetype/dejavu"


def render_pdf(doc: dict[str, Any], config: dict[str, Any] | None = None) -> bytes:
    """The report as an actual PDF FILE. The print view answers "let me print this";
    this answers what the button says — Download PDF saves a file to the device, no
    tab and no print dialog. Same content as the print view, drawn with fpdf2.

    DejaVu ships in the image (fonts-dejavu-core) because the built-in Helvetica is
    latin-1 only and a ₹ in "Ticket size" would crash the render; on a bare checkout
    without the font, fall back to Helvetica and substitute what latin-1 cannot say.
    """
    from fpdf import FPDF

    ext = (doc.get("report") or {}).get("extraction") or {}
    rep = ext.get("report") or {}
    meta = ext.get("_meta") or {}
    title = rep.get("title") or (ext.get("entity_match") or {}).get("canonical_name") \
        or ext.get("company_mentioned") or "Field report"
    ts = (meta.get("capture_ts") or doc.get("updated_at") or "")[:16].replace("T", " ")
    status = (doc.get("status") or "draft").upper()

    pdf = FPDF(format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    uni = os.path.isfile(os.path.join(_DEJAVU_DIR, "DejaVuSans.ttf"))
    if uni:
        pdf.add_font("Deja", "", os.path.join(_DEJAVU_DIR, "DejaVuSans.ttf"))
        pdf.add_font("Deja", "B", os.path.join(_DEJAVU_DIR, "DejaVuSans-Bold.ttf"))
    face = "Deja" if uni else "helvetica"

    def txt(v: Any) -> str:
        s = str(v) if v not in (None, "") else ""
        return s if uni else s.encode("latin-1", "replace").decode("latin-1")

    teal, grey, ink = (14, 122, 122), (102, 102, 102), (17, 17, 17)

    def h2(name: str) -> None:
        pdf.ln(4)
        pdf.set_font(face, "B", 9.5)
        pdf.set_text_color(*teal)
        pdf.cell(0, 5, txt(name.upper()), new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y()
        pdf.set_draw_color(221, 221, 221)
        pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.ln(2)
        pdf.set_text_color(*ink)

    def para(s: Any) -> None:
        pdf.set_font(face, "", 10)
        pdf.multi_cell(0, 5, txt(s), new_x="LMARGIN", new_y="NEXT")

    def bullets(items: list[str]) -> None:
        pdf.set_font(face, "", 10)
        for it in items:
            pdf.multi_cell(0, 5, txt(("• " if uni else "- ") + str(it)),
                           new_x="LMARGIN", new_y="NEXT")

    pdf.add_page()
    pdf.set_font(face, "B", 8)
    pdf.set_text_color(*teal)
    pdf.cell(0, 4, txt(f"VOCX · EVAM FIELD INTEL · {status}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(face, "B", 19)
    pdf.set_text_color(*ink)
    pdf.multi_cell(0, 9, txt(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(face, "", 9.5)
    pdf.set_text_color(*grey)
    pdf.cell(0, 5, txt(ts), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*ink)

    if rep.get("summary"):
        h2("Summary")
        para(rep["summary"])
    intel = [k for k in rep.get("key_intel") or [] if k]
    if intel:
        h2("Key intelligence")
        bullets(intel)
    rows = _details_rows(ext, rep, config)
    if rows:
        h2("Details")
        for lbl, v in rows:
            pdf.set_font(face, "", 10)
            pdf.set_text_color(*grey)
            pdf.cell(48, 5, txt(lbl))
            pdf.set_text_color(*ink)
            pdf.multi_cell(0, 5, txt(v), new_x="LMARGIN", new_y="NEXT")
    steps = [s for s in rep.get("next_steps") or [] if s.get("action")]
    if steps:
        h2("Next steps")
        bullets([(f"{s['owner']}: " if s.get("owner") else "") + str(s.get("action") or "")
                 + (f" — due {s['date']}" if s.get("date") else "") for s in steps])
    nuances = [n for n in rep.get("nuances") or [] if n]
    if nuances:
        h2("Nuances")
        bullets(nuances)
    attendees = " · ".join(
        " ".join(x for x in (str(a.get("name") or ""),
                             f"({a['role']})" if a.get("role") else "",
                             f"— {a['company']}" if a.get("company") else "") if x)
        for a in rep.get("attendees") or [] if a.get("name"))
    if attendees:
        h2("Attendees")
        para(attendees)
    if rep.get("transcript_english"):
        h2("Full transcript (English)")
        para(rep["transcript_english"])
    return bytes(pdf.output())
