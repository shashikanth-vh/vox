"""Company 360 → DocRAG bridge: index one company's Data Register files for Q&A.

The 360 dialog's ask-box answers ONLY from DocRAG's index, and the Data Register
is a different store — a company can carry seven files on the register and none
in the index. This bridge closes that seam on demand: given a company, it reads
the register's document list (as the verified caller, so RBAC and company scope
hold), streams each supported file's bytes and uploads them to DocRAG under a
name that carries the company ("<Company> — <filename>"), which is exactly the
name-match the ask-box scopes by.

Honesty rules:

* Only DocRAG-supported types travel (.pdf, .xlsx, images). A .zip is UNPACKED
  server-side and its readable members are indexed as "<zip>/<member>" — with
  bomb guards (member count, per-file and total size, no nested zips) — and
  anything unreadable is reported as skipped WITH ITS REASON, never silently
  dropped.
* Uploads are idempotent on DocRAG's side (same bytes → duplicate flag), so
  pressing the button twice indexes nothing twice.
* The caller needs the same document grant the files already require — the
  gateway maps this route to upload_remove_documents.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from evam_backend_core.logging import get_logger

log = get_logger("panorama.bridge")

# Mirrors services/docrag/app/store.SUPPORTED_SUFFIXES (kept small on purpose:
# a new suffix lands there first, then here).
_SUPPORTED = {".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif"}

# Zip guards: a desk archive holds a handful of scans, not thousands — anything
# past these bounds is a mistake or a bomb, and is refused with its reason.
_ZIP_MAX_MEMBERS = 50
_ZIP_MAX_MEMBER_BYTES = 30 * 1024 * 1024
_ZIP_MAX_TOTAL_BYTES = 120 * 1024 * 1024


def unpack_zip(blob: bytes, zip_name: str
               ) -> tuple[list[tuple[str, bytes, str]], list[dict[str, str]]]:
    """The indexable members of a desk zip: (name-inside-zip, bytes, suffix)
    plus skipped entries WITH reasons. Names are flattened to their basename —
    an archive's folder layout is packaging, and a crafted path must never
    travel anywhere. Nested zips stay closed (one level is a desk archive;
    two is a bomb's favourite shape)."""
    files: list[tuple[str, bytes, str]] = []
    skipped: list[dict[str, str]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return [], [{"file": zip_name, "reason": "not a readable zip archive"}]
    members = [m for m in zf.infolist() if not m.is_dir()]
    if len(members) > _ZIP_MAX_MEMBERS:
        return [], [{"file": zip_name,
                     "reason": f"holds {len(members)} files — more than the "
                               f"{_ZIP_MAX_MEMBERS} an archive of documents has"}]
    total = 0
    for m in members:
        name = Path(m.filename).name
        suffix = Path(name).suffix.lower()
        label = f"{zip_name}/{name}"
        if suffix not in _SUPPORTED:
            skipped.append({"file": label,
                            "reason": f"{suffix or 'no extension'} is not "
                                      f"readable by the document AI"})
            continue
        if m.file_size > _ZIP_MAX_MEMBER_BYTES:
            skipped.append({"file": label, "reason": "larger than 30 MB"})
            continue
        total += m.file_size
        if total > _ZIP_MAX_TOTAL_BYTES:
            skipped.append({"file": label,
                            "reason": "archive exceeds the 120 MB indexing budget"})
            continue
        try:
            data = zf.read(m)
        except Exception:  # noqa: BLE001 - a corrupt member must not kill the rest
            skipped.append({"file": label, "reason": "could not be read from the zip"})
            continue
        files.append((name, data, suffix))
    return files, skipped


class IndexDocumentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=8, max_length=64)
    company: str = Field(min_length=1, max_length=300)


def mount_panorama_bridge(app: Any, settings: Any, *, denied: Any, verified_email: Any,
                          caller_context: Any, problem: Any,
                          reg_headers: Any) -> None:
    """Closure-mounted like the CAM workbench; `reg_headers` is cam.py's
    signed-context builder so the register sees the human, not a service blob."""

    base = settings.register_base_url.rstrip("/")
    docrag_url = (getattr(settings, "docrag_url", "") or "").rstrip("/")

    @app.post("/v1/panorama/index-documents", tags=["Company 360"],
              summary="Index a company's Data Register files into DocRAG for the ask-box")
    async def index_documents(payload: IndexDocumentsIn, request: Request) -> Any:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)

        if not docrag_url:
            return problem(409, "DocRAG not configured",
                           "Set WORKFLOWS_DOCRAG_URL to enable document Q&A indexing.")

        http = request.app.state.http
        # The register's list routes REJECT unknown query params (a guessed
        # filter must fail loudly, not silently return everything) — so this
        # speaks their exact dialect: `limit`, not `page_size`.
        list_path = f"/v1/documents?entity_id={payload.entity_id}&limit=100"
        try:
            r = await http.get(f"{base}{list_path}",
                               headers=reg_headers(request, caller, who, "GET",
                                                   "/v1/documents"))
        except httpx.HTTPError as exc:
            log.warning("panorama_index_register_unreachable",
                        extra={"error": str(exc)})
            return problem(502, "Register unreachable",
                           f"Could not read the document list: {exc}")
        if r.status_code >= 300:
            return problem(502, "Register refused the document list",
                           f"HTTP {r.status_code} from the register.")
        items = (r.json() or {}).get("items", [])

        indexed: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        # Every per-file network hop is guarded: one unreachable host or one
        # corrupt file becomes a NAMED skip, never a 500 that hides the rest.
        async def _fetch(doc_id: str) -> tuple[bytes | None, str, str]:
            content_path = f"/v1/documents/{doc_id}/content"
            try:
                got = await http.get(f"{base}{content_path}",
                                     headers=reg_headers(request, caller, who,
                                                         "GET", content_path),
                                     follow_redirects=True,
                                     timeout=60.0)
            except httpx.HTTPError as exc:
                return None, "", f"register read failed ({exc.__class__.__name__})"
            if got.status_code >= 300:
                return None, "", f"register refused the read (HTTP {got.status_code})"
            return got.content, got.headers.get("content-type") or "", ""

        # DocRAG behind the AI host's private-CA edge needs the client that trusts
        # that CA (the CAM's); the default client fails every upload on TLS.
        docrag_http = getattr(request.app.state, "docrag_http", None) or http

        async def _upload(display_name: str, data: bytes, ctype: str) -> None:
            try:
                up = await docrag_http.post(
                    f"{docrag_url}/v1/documents",
                    headers={"X-API-Key": settings.docrag_api_key,
                             "X-Tenant": request.headers.get(
                                 "X-Tenant", settings.register_tenant)},
                    files={"file": (f"{payload.company} — {display_name}", data,
                                    ctype or "application/octet-stream")},
                    timeout=float(getattr(settings, "docrag_timeout_s", 600.0)))
            except httpx.HTTPError as exc:
                log.warning("panorama_index_docrag_unreachable",
                            extra={"error": str(exc), "docrag": docrag_url})
                skipped.append({"file": display_name,
                                "reason": f"document AI unreachable at {docrag_url} "
                                          f"({exc.__class__.__name__}) — is the "
                                          f"docrag container running?"})
                return
            if up.status_code >= 300:
                skipped.append({"file": display_name,
                                "reason": f"document AI refused it "
                                          f"(HTTP {up.status_code})"})
                return
            try:
                body = up.json()
            except ValueError:
                body = {}
            indexed.append({"file": display_name, "doc_id": body.get("id"),
                            "duplicate": bool(body.get("duplicate"))})

        try:
            await _walk(items, _fetch, _upload, skipped)
        except Exception as exc:  # noqa: BLE001 - the last resort must NAME itself
            log.exception("panorama_index_failed")
            return problem(500, "Indexing failed",
                           f"{exc.__class__.__name__}: {exc}")

        return {"company": payload.company, "total_on_register": len(items),
                "indexed": indexed, "skipped": skipped,
                "note": ("Files are processed in the background — the first "
                         "answers may take a minute while pages are read.")
                if indexed else None}

    async def _walk(items: list, _fetch: Any, _upload: Any,
                    skipped: list) -> None:
        for d in items:
            fname = d.get("original_filename") or f"{d.get('title', 'document')}"
            suffix = Path(fname).suffix.lower()
            if suffix == ".zip":
                blob, _, why = await _fetch(d.get("id"))
                if blob is None:
                    skipped.append({"file": fname, "reason": why})
                    continue
                inner, inner_skips = unpack_zip(blob, fname)
                skipped.extend(inner_skips)
                for name, data, _sfx in inner:
                    await _upload(f"{fname}/{name}", data, "")
                continue
            if suffix not in _SUPPORTED:
                skipped.append({"file": fname,
                                "reason": f"{suffix or 'no extension'} is not "
                                          f"readable by the document AI"})
                continue
            blob, ctype, why = await _fetch(d.get("id"))
            if blob is None:
                skipped.append({"file": fname, "reason": why})
                continue
            await _upload(fname, blob, ctype)
