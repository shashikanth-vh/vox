"""The CAM workbench — where a credit analyst drafts a CAM with an LLM and reworks it.

The workbench holds NO credit judgement of its own. The analyst selects the source
documents and a PROMPT DOC (both Data Register documents — the prompts are data the
credit team owns, not code), the engine drafts, the analyst refines turn by turn, and a
human finalises the result into the Data Register. Persistence is the register's
``cam_reports``/``cam_turns`` (maker-checker lifecycle, committee decision) — this
module is stateless between calls.

**Provider seam.** The workbench never talks to a vendor directly: ``build_engine``
resolves ``WORKFLOWS_CAM_ENGINE`` ("anthropic:<model>") to an implementation, and every
CAM version records which engine drafted it. Today: Anthropic (Haiku by default), plus a
deterministic stub when no key is configured — dev and CI run the whole lifecycle
without a vendor account. Adding a provider is one class + a config value.

**Documents.** Text-like documents (markdown, txt, csv, json, html) are read whole;
DOCX is unzipped and stripped of markup (stdlib); PDF goes through pypdf. All per-doc
bounded. A format that still cannot be read — or a scan with no text layer — is
SKIPPED AND SAID SO: the response names what went in and what did not, because a CAM
that silently omits a document it claims to cover is worse than one that refuses.
Extraction lives in ``extract_text``; adding a format is one branch there.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def extract_text(ctype: str, blob: bytes) -> tuple[str, str | None]:
    """(text, unreadable-reason). Text types decode; DOCX is unzipped and stripped of
    markup (stdlib only — a .docx is a zip holding word/document.xml); PDF goes through
    pypdf. Anything else — or an extraction that yields nothing — returns the REASON,
    because a CAM that silently omits a document it claims to cover is worse than one
    that refuses."""
    ctype = (ctype or "").lower()
    if any(ctype.startswith(t) for t in _TEXT_TYPES):
        return blob.decode("utf-8", "ignore"), None
    if ctype.startswith(_DOCX_TYPE) or (blob[:2] == b"PK" and b"word/" in blob[:4096]):
        import html
        import io
        import re
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            return "", f"could not read the .docx ({exc})"
        xml = xml.replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml)))
        if text.strip():
            return text, None
        return "", "the .docx contains no extractable text"
    if ctype.startswith("application/pdf") or blob[:5] == b"%PDF-":
        import io
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", "PDF extraction needs pypdf, which is not installed in this image"
        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)
        except Exception as exc:  # noqa: BLE001 — malformed PDFs raise all sorts
            return "", f"could not read the PDF ({exc})"
        if text.strip():
            return text, None
        return "", "the PDF has no extractable text (a scan needs OCR, which is not wired)"
    return "", f"binary content ({ctype or 'unknown type'}) — no extractor for this format"
_SYSTEM = (
    "You are drafting a Credit Assessment Memo (CAM) for a climate-finance lender. "
    "Work ONLY from the supplied documents and the analyst's prompt document; where a "
    "figure is not in the documents, say 'not on record' rather than inventing one. "
    "Answer in clean Markdown."
)


# --------------------------------------------------------------------------- #
# The engine seam
# --------------------------------------------------------------------------- #
class CamEngine:
    """One drafting engine. ``generate`` gets the full conversation each call —
    engines are stateless; the register's cam_turns is the memory."""

    name = "stub:none"

    async def generate(self, http: Any, system: str,
                       turns: list[dict[str, str]]) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class StubEngine(CamEngine):
    """No key configured: a deterministic draft so the LIFECYCLE works everywhere.
    The text says loudly that no model was involved."""

    name = "stub:offline"

    async def generate(self, http: Any, system: str, turns: list[dict[str, str]]) -> str:
        asked = sum(1 for t in turns if t["role"] == "user")
        return ("# CAM (offline stub)\n\n"
                "No LLM engine is configured (set WORKFLOWS_ANTHROPIC_API_KEY). This "
                f"placeholder proves the workbench lifecycle only. Turns so far: {asked}.")


class AnthropicEngine(CamEngine):
    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self.name = f"anthropic:{model}"

    async def generate(self, http: Any, system: str, turns: list[dict[str, str]]) -> str:
        r = await http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 8192, "system": system,
                  "messages": turns},
            timeout=120.0)
        if r.status_code >= 300:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message") or ""
            except ValueError:
                pass
            raise RuntimeError(f"The drafting engine refused (HTTP {r.status_code})"
                               + (f": {detail}" if detail else "."))
        blocks = (r.json() or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip():
            raise RuntimeError("The drafting engine returned no text.")
        return text


def build_engine(settings: Any) -> CamEngine:
    spec = (getattr(settings, "cam_engine", "") or "anthropic:claude-haiku-4-5").strip()
    provider, _, model = spec.partition(":")
    key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if provider == "anthropic" and key:
        return AnthropicEngine(model or "claude-haiku-4-5", key)
    return StubEngine()


# --------------------------------------------------------------------------- #
# Workbench routes
# --------------------------------------------------------------------------- #
class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_doc_ids: list[str] = Field(min_length=1, max_length=25)
    prompt_doc_id: str = Field(min_length=1, max_length=64)
    deal_id: str | None = Field(default=None, max_length=64)


class RefineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=20_000)


class FinaliseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)


def mount_cam(app: Any, settings: Any, *, denied: Any, verified_email: Any,
              caller_context: Any, problem: Any) -> None:
    """Register the workbench routes on the orchestrator app (closure style, like the
    rest of the API — the helpers come from create_app)."""

    engine = build_engine(settings)
    base = settings.register_base_url.rstrip("/")
    max_doc_chars = int(getattr(settings, "cam_max_doc_chars", 60_000))

    def _reg_headers(request: Request, caller: Any, who: str,
                     method: str, path: str) -> dict[str, str]:
        headers = {"X-Tenant": request.headers.get("X-Tenant", settings.register_tenant),
                   "X-API-Key": settings.register_api_key}
        if settings.internal_signing_secret and caller is not None and caller.email:
            from evam_backend_core.internal_token import mint_internal_context

            headers["X-Internal-Context"] = mint_internal_context(
                signing_key=settings.internal_signing_secret,
                algorithm=settings.internal_signing_algorithm,
                ttl_seconds=settings.internal_token_ttl_seconds,
                tenant=headers["X-Tenant"], email=caller.email,
                user_id=caller.user_id or caller.email, roles=list(caller.roles),
                report_ids=list(caller.report_ids),
                report_emails=list(caller.report_emails),
                effective_views=caller.effective_views,
                effective_operations=caller.effective_operations,
                decision=caller.decision or "FULL",
                method=method, path=path.split("?", 1)[0])
        else:
            headers["X-User-Email"] = who
            if caller is not None and caller.roles:
                headers["X-User-Roles"] = ",".join(caller.roles)
        return headers

    async def _doc_text(request: Request, caller: Any, who: str,
                        doc_id: str) -> tuple[str, str | None]:
        """(text, skip_reason). Text-like content comes back whole (bounded);
        binary formats are skipped WITH the reason."""
        path = f"/v1/documents/{doc_id}/content"
        r = await request.app.state.http.get(
            f"{base}{path}",
            headers=_reg_headers(request, caller, who, "GET", path),
            follow_redirects=True)
        if r.status_code == 404:
            return "", "not found on the register"
        if r.status_code >= 300:
            return "", f"register refused the read (HTTP {r.status_code})"
        text, reason = extract_text(r.headers.get("content-type") or "", r.content)
        if reason is not None:
            return "", reason
        if len(text) > max_doc_chars:
            return text[:max_doc_chars], f"truncated to {max_doc_chars} characters"
        return text, None

    async def _open_report(request: Request, caller: Any, who: str,
                           lending_id: str) -> dict[str, Any] | None:
        """The line's current Draft/Returned CAM, or None."""
        path = "/v1/internal/cam-reports"
        r = await request.app.state.http.get(
            f"{base}{path}", params={"lending_id": lending_id},
            headers=_reg_headers(request, caller, who, "GET", path))
        if r.status_code >= 300:
            return None
        rows = r.json() or []
        live = [x for x in rows if x.get("status") in ("Draft", "Returned")]
        return live[-1] if live else None

    async def _record_turn(request: Request, caller: Any, who: str, report_id: str,
                           role: str, content: str, draft: str | None = None) -> None:
        path = f"/v1/internal/cam-reports/{report_id}/turns"
        body: dict[str, Any] = {"role": role, "content": content}
        if draft is not None:
            body["draft_md"] = draft
        r = await request.app.state.http.post(
            f"{base}{path}", json=body,
            headers=_reg_headers(request, caller, who, "POST", path))
        if r.status_code >= 300:
            raise RuntimeError(f"the register refused the workbench turn "
                               f"(HTTP {r.status_code}): {r.text[:300]}")

    @app.post("/v1/cam/{lending_id}/generate", status_code=201, tags=["CAM"],
              summary="Draft a CAM from selected documents + the prompt doc")
    async def cam_generate(lending_id: str, payload: GenerateIn,
                           request: Request) -> Any:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)

        # The prompt doc IS the brief — read it first, and refuse without it.
        prompt_text, skip = await _doc_text(request, caller, who, payload.prompt_doc_id)
        if not prompt_text.strip():
            return problem(422, "Validation failed",
                           f"The prompt document could not be read"
                           f"{f' ({skip})' if skip else ''} — the workbench drafts only "
                           "from the credit team's own prompts.")

        included: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        parts: list[str] = []
        for doc_id in payload.source_doc_ids:
            text, reason = await _doc_text(request, caller, who, doc_id)
            if text.strip():
                included.append({"doc_id": doc_id, **({"note": reason} if reason else {})})
                parts.append(f"\n\n===== DOCUMENT {doc_id} =====\n{text}")
            else:
                skipped.append({"doc_id": doc_id, "reason": reason or "empty"})
        if not included:
            return problem(422, "Validation failed",
                           "None of the selected documents could be read as text — "
                           + "; ".join(f"{s['doc_id']}: {s['reason']}" for s in skipped))

        # Open the register's CAM version (one Draft at a time — the register enforces it).
        path = "/v1/internal/cam-reports"
        opened = await request.app.state.http.post(
            f"{base}{path}",
            json={"lending_id": lending_id, "deal_id": payload.deal_id,
                  "engine": engine.name, "source_doc_ids": payload.source_doc_ids,
                  "prompt_doc_id": payload.prompt_doc_id},
            headers=_reg_headers(request, caller, who, "POST", path))
        if opened.status_code >= 300:
            return problem(opened.status_code if opened.status_code in (403, 404, 409)
                           else 502, "CAM not opened", opened.text[:500])
        report = opened.json()

        first_ask = (f"{prompt_text.strip()}\n{''.join(parts)}")
        try:
            await _record_turn(request, caller, who, report["id"], "user",
                               f"[generate] prompt doc {payload.prompt_doc_id}; "
                               f"documents: {', '.join(d['doc_id'] for d in included)}")
            draft = await engine.generate(request.app.state.http, _SYSTEM,
                                          [{"role": "user", "content": first_ask}])
            await _record_turn(request, caller, who, report["id"], "assistant",
                               draft, draft=draft)
        except RuntimeError as exc:
            return problem(502, "Drafting failed", f"{exc} The CAM version stays open — "
                           "retry the generation; nothing was filed.")
        return {"report_id": report["id"], "report_version": report["report_version"],
                "engine": engine.name, "draft_md": draft,
                "included": included, "skipped": skipped}

    @app.post("/v1/cam/{lending_id}/refine", tags=["CAM"],
              summary="Rework the current CAM draft with a further instruction")
    async def cam_refine(lending_id: str, payload: RefineIn, request: Request) -> Any:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)
        report = await _open_report(request, caller, who, lending_id)
        if report is None:
            return problem(404, "Not found",
                           "This line has no CAM draft in progress — generate one first.")
        # Rebuild the conversation from the durable transcript (engines are stateless).
        path = f"/v1/internal/cam-reports/{report['id']}"
        full = await request.app.state.http.get(
            f"{base}{path}", headers=_reg_headers(request, caller, who, "GET", path))
        turns = [{"role": t["role"], "content": t["content"]}
                 for t in (full.json().get("turns") or [])] if full.status_code < 300 else []
        turns.append({"role": "user", "content": payload.instruction})
        try:
            await _record_turn(request, caller, who, report["id"], "user",
                               payload.instruction)
            draft = await engine.generate(request.app.state.http, _SYSTEM, turns)
            await _record_turn(request, caller, who, report["id"], "assistant",
                               draft, draft=draft)
        except RuntimeError as exc:
            return problem(502, "Drafting failed", str(exc))
        return {"report_id": report["id"], "report_version": report["report_version"],
                "engine": engine.name, "draft_md": draft}

    @app.post("/v1/cam/{lending_id}/finalise", tags=["CAM"],
              summary="File the draft to the Data Register and submit it to committee")
    async def cam_finalise(lending_id: str, payload: FinaliseIn, request: Request) -> Any:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)
        report = await _open_report(request, caller, who, lending_id)
        if report is None:
            return problem(404, "Not found",
                           "This line has no CAM draft in progress — generate one first.")
        draft = (report.get("draft_md") or "").strip()
        if not draft:
            return problem(422, "Validation failed",
                           "The current CAM version has no draft text yet.")
        title = payload.title or f"CAM v{report['report_version']}"
        # File the draft as a Data Register document under the Sanction shelf…
        up_path = f"/v1/lending/{lending_id}/documents/upload"
        up = await request.app.state.http.post(
            f"{base}{up_path}",
            files={"file": (f"{title}.md", draft.encode("utf-8"), "text/markdown")},
            data={"section": "Sanction", "title": title, "doc_type": "CAM",
                  "status": "On File"},
            headers=_reg_headers(request, caller, who, "POST", up_path))
        if up.status_code >= 300:
            return problem(502, "Filing failed",
                           f"The register refused the CAM document (HTTP "
                           f"{up.status_code}): {up.text[:300]}")
        doc = up.json()
        # …then submit that version to the committee, carrying the document id.
        sub_path = f"/v1/internal/cam-reports/{report['id']}/submit"
        sub = await request.app.state.http.post(
            f"{base}{sub_path}", json={"document_id": str(doc.get("id") or "")},
            headers=_reg_headers(request, caller, who, "POST", sub_path))
        if sub.status_code >= 300:
            return problem(502, "Submit failed", sub.text[:500])
        return {"report_id": report["id"], "report_version": report["report_version"],
                "document_id": str(doc.get("id") or ""),
                "checksum": doc.get("checksum"), "status": "Submitted"}

