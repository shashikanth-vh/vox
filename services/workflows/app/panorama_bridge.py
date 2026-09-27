"""Company 360 → DocRAG bridge: index one company's Data Register files for Q&A.

The 360 dialog's ask-box answers ONLY from DocRAG's index, and the Data Register
is a different store — a company can carry seven files on the register and none
in the index. This bridge closes that seam on demand: given a company, it reads
the register's document list (as the verified caller, so RBAC and company scope
hold), streams each supported file's bytes and uploads them to DocRAG under a
name that carries the company ("<Company> — <filename>"), which is exactly the
name-match the ask-box scopes by.

Honesty rules:

* Only DocRAG-supported types travel (.pdf, .xlsx, images). A .zip or .docx is
  reported as skipped WITH ITS REASON, never silently dropped.
* Uploads are idempotent on DocRAG's side (same bytes → duplicate flag), so
  pressing the button twice indexes nothing twice.
* The caller needs the same document grant the files already require — the
  gateway maps this route to upload_remove_documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

# Mirrors services/docrag/app/store.SUPPORTED_SUFFIXES (kept small on purpose:
# a new suffix lands there first, then here).
_SUPPORTED = {".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif"}


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
        list_path = f"/v1/documents?entity_id={payload.entity_id}&page_size=100"
        r = await http.get(f"{base}{list_path}",
                           headers=reg_headers(request, caller, who, "GET",
                                               "/v1/documents"))
        if r.status_code >= 300:
            return problem(502, "Register refused the document list",
                           f"HTTP {r.status_code} from the register.")
        items = (r.json() or {}).get("items", [])

        indexed: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for d in items:
            fname = d.get("original_filename") or f"{d.get('title', 'document')}"
            suffix = Path(fname).suffix.lower()
            if suffix not in _SUPPORTED:
                skipped.append({"file": fname,
                                "reason": f"{suffix or 'no extension'} is not "
                                          f"readable by the document AI"})
                continue
            doc_id = d.get("id")
            content_path = f"/v1/documents/{doc_id}/content"
            got = await http.get(f"{base}{content_path}",
                                 headers=reg_headers(request, caller, who, "GET",
                                                     content_path),
                                 follow_redirects=True)
            if got.status_code >= 300:
                skipped.append({"file": fname,
                                "reason": f"register refused the read "
                                          f"(HTTP {got.status_code})"})
                continue
            up_name = f"{payload.company} — {fname}"
            up = await http.post(
                f"{docrag_url}/v1/documents",
                headers={"X-API-Key": settings.docrag_api_key,
                         "X-Tenant": request.headers.get(
                             "X-Tenant", settings.register_tenant)},
                files={"file": (up_name, got.content,
                                got.headers.get("content-type")
                                or "application/octet-stream")},
                timeout=float(getattr(settings, "docrag_timeout_s", 600.0)))
            if up.status_code >= 300:
                skipped.append({"file": fname,
                                "reason": f"document AI refused it "
                                          f"(HTTP {up.status_code})"})
                continue
            body = up.json()
            indexed.append({"file": fname, "doc_id": body.get("id"),
                            "duplicate": bool(body.get("duplicate"))})

        return {"company": payload.company, "total_on_register": len(items),
                "indexed": indexed, "skipped": skipped,
                "note": ("Files are processed in the background — the first "
                         "answers may take a minute while pages are read.")
                if indexed else None}
