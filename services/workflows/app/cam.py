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

import re
import time as _time
from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.cam_pipeline import DigestCache, SourceDoc, draft_staged, parse_master_prompt
from app.cam_telemetry import UsageMeter, metered, record_document, record_llm_call, stage
from app.cam_telemetry import log as _tlog
from app.docx_out import markdown_into_template, markdown_to_docx

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
        return "", _SCANNED_PDF
    if image_suffix(ctype, blob):
        return "", ("an image — reading it needs OCR: configure DocRAG with Sarvam "
                    "(WORKFLOWS_DOCRAG_URL + SARVAM_API_KEY)")
    return "", f"binary content ({ctype or 'unknown type'}) — no extractor for this format"


# Images DocRAG reads by OCR, by content type and by their leading bytes.
_IMAGE_KINDS = ((("image/jpeg", "image/jpg", "image/pjpeg"), (b"\xff\xd8\xff",), ".jpg"),
                (("image/png",), (b"\x89PNG",), ".png"),
                (("image/tiff",), (b"II*\x00", b"MM\x00*"), ".tif"),
                (("image/bmp", "image/x-ms-bmp"), (b"BM",), ".bmp"))


def image_suffix(ctype: str, blob: bytes) -> str | None:
    ctype = (ctype or "").lower()
    if any(ctype.startswith(t) for t in _TEXT_TYPES):
        return None
    for mimes, magics, suffix in _IMAGE_KINDS:
        if ctype.startswith(mimes) or any(blob.startswith(m) for m in magics):
            return suffix
    return None


def doc_format(ctype: str, blob: bytes) -> str:
    """A short, stable label for telemetry: pdf, xlsx, docx, text, jpg, png, ..."""
    ctype = (ctype or "").lower()
    if is_pdf(ctype, blob):
        return "pdf"
    if "spreadsheetml" in ctype or (blob[:2] == b"PK" and b"xl/" in blob[:4096]):
        return "xlsx"
    if ctype.startswith(_DOCX_TYPE) or (blob[:2] == b"PK" and b"word/" in blob[:4096]):
        return "docx"
    if (suffix := image_suffix(ctype, blob)) is not None:
        return suffix.lstrip(".")
    if any(ctype.startswith(t) for t in _TEXT_TYPES):
        return "text"
    return ctype.split(";")[0] or "unknown"


# The marker reason for a PDF with no text layer — a SCAN. Not a dead end: an engine
# that reads documents visually (Anthropic's PDF support) is handed the file itself.
_SCANNED_PDF = "the PDF has no text layer (a scan)"
_SCANNED_IMAGE = "the image produced no text"
_PDF_ATTACH_MAX_BYTES = 10 * 1024 * 1024   # Anthropic's request cap is 32MB total
_PDF_ATTACH_MAX_DOCS = 4                   # …and ~100 pages across attached PDFs


def is_pdf(ctype: str, blob: bytes) -> bool:
    return (ctype or "").lower().startswith("application/pdf") or blob[:5] == b"%PDF-"
# The credit team's master prompt was written for an agent WITH tools (it asks for
# workpapers on disk, public web checks, a generated .docx and a programmatic QA pass).
# The workbench runs it as ONE text response, so this note tells the engine how to honour
# each instruction here — without editing the credit team's prompt, and without ever
# letting a check that did not happen read as done.
_ENVIRONMENT = (
    "OPERATING ENVIRONMENT: you are answering as a single text response inside PRISM's "
    "CAM workbench. You have NO file system, NO web or public-registry access and NO code "
    "execution. Where the prompt document asks for any of these: (1) do the extraction, "
    "ratio, triangulation and stress-test work yourself and show the results where the "
    "CAM needs them, with formulas and [source: document, page] tags — do not output "
    "separate workpaper files; (2) NEVER state or imply that a public check, registry "
    "search or web lookup was performed — list each required check in a table marked "
    "'NOT PERFORMED — to be completed by the analyst'; (3) ignore .docx generation, "
    "formatting (fonts, colours, page setup) and machine-QA instructions — PRISM renders "
    "your Markdown into the EVAM CAM template with that styling; (4) output ONLY the CAM "
    "itself in Markdown: '#' for the title, '##' for each Section and Annexure, '###' for "
    "sub-sections, and Markdown tables with a header row."
)

_SYSTEM = (
    "You are drafting a Credit Assessment Memo (CAM) for a climate-finance lender. "
    "Work ONLY from the supplied documents and the analyst's prompt document; where a "
    "figure is not in the documents, say 'not on record' rather than inventing one. "
    "Answer in clean Markdown.\n\n" + _ENVIRONMENT
)

# The ASK lane is the analyst's open conversation — questions, pasted text, requests of
# any shape while they fill the CAM in Word. It must answer like a capable assistant,
# not refuse everything outside the strict drafting frame; only the no-invented-figures
# rule carries over.
_ASK_SYSTEM = (
    "You are the credit desk's assistant at a climate-finance lender, working alongside "
    "an analyst who is preparing a Credit Assessment Memo. Answer whatever they ask, "
    "directly and helpfully — questions, summaries, rewrites, calculations, pasted "
    "text. Use any supplied documents as the factual record: never invent figures "
    "about the borrower; where a borrower figure is not in the documents, say 'not on "
    "record'. If a supplied document is itself a prompt or instruction sheet (the "
    "credit team's CAM prompt, a drafting guide), FOLLOW it as instructions for this "
    "answer rather than treating it as facts. General knowledge questions are fine to "
    "answer from your own knowledge. You have no file system, web or code access: never "
    "claim to have run a public check or written a file. Answer in clean Markdown."
)


# --------------------------------------------------------------------------- #
# The engine seam
# --------------------------------------------------------------------------- #
class CamEngine:
    """One drafting engine. ``generate`` gets the full conversation each call —
    engines are stateless; the register's cam_turns is the memory.

    ``supports_documents``: whether a turn's content may be a BLOCK LIST carrying
    base64 PDF documents (how scanned files reach an engine that reads them
    visually). Engines that cannot get plain strings only.
    """

    name = "stub:none"
    supports_documents = False

    async def generate(self, http: Any, system: str,
                       turns: list[dict[str, Any]]) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class StubEngine(CamEngine):
    """No key configured: a deterministic draft so the LIFECYCLE works everywhere.
    The text says loudly that no model was involved."""

    name = "stub:offline"

    async def generate(self, http: Any, system: str, turns: list[dict[str, str]]) -> str:
        record_llm_call(engine=self.name, ok=True, started=_time.monotonic(),
                        usage={"input_tokens": 0, "output_tokens": 0}, finish="stub")
        asked = sum(1 for t in turns if t["role"] == "user")
        return ("# CAM (offline stub)\n\n"
                "No LLM engine is configured (set WORKFLOWS_CAM_LLM_API_KEY for "
                "bedrock:<model>, or WORKFLOWS_ANTHROPIC_API_KEY). This "
                f"placeholder proves the workbench lifecycle only. Turns so far: {asked}.")


class AnthropicEngine(CamEngine):
    supports_documents = True   # the Messages API reads PDFs, scanned pages included

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key
        self.name = f"anthropic:{model}"

    async def generate(self, http: Any, system: str, turns: list[dict[str, Any]]) -> str:
        # STREAMED: an "update this CAM" answer can be the whole memo — tens of
        # thousands of tokens. Non-streaming requests that long hit idle-connection
        # timeouts; with SSE the read timeout is per-chunk, and 64000 is the model
        # family's output ceiling, so a full CAM never truncates mid-sentence.
        started = _time.monotonic()
        usage: dict[str, Any] = {}
        try:
            text = await self._request(http, system, turns, usage)
        except Exception as exc:
            record_llm_call(engine=self.name, ok=False, started=started, usage=usage,
                            error=str(exc))
            raise
        record_llm_call(engine=self.name, ok=True, started=started, usage=usage,
                        finish=usage.pop("_stop", ""))
        return text

    async def _request(self, http: Any, system: str, turns: list[dict[str, Any]],
                       usage: dict[str, Any]) -> str:
        import json as _json

        import httpx
        text_parts: list[str] = []
        async with http.stream(
            "POST", "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 64000, "system": system,
                  "messages": turns, "stream": True},
            timeout=httpx.Timeout(240.0, connect=15.0),
        ) as r:
            if r.status_code >= 300:
                body = (await r.aread()).decode("utf-8", "ignore")
                detail = ""
                try:
                    detail = ((_json.loads(body) or {}).get("error") or {}).get("message") or ""
                except ValueError:
                    pass
                raise RuntimeError(f"The drafting engine refused (HTTP {r.status_code})"
                                   + (f": {detail}" if detail else "."))
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = _json.loads(line[6:])
                except ValueError:
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta.get("text", ""))
                elif event.get("type") == "message_start":
                    # Input tokens arrive up front; output tokens with message_delta.
                    usage.update(((event.get("message") or {}).get("usage")) or {})
                elif event.get("type") == "message_delta":
                    usage.update(event.get("usage") or {})
                    usage["_stop"] = (event.get("delta") or {}).get("stop_reason") or ""
                elif event.get("type") == "error":
                    msg = (event.get("error") or {}).get("message") or "stream error"
                    raise RuntimeError(f"The drafting engine failed mid-answer: {msg}")
        text = "".join(text_parts)
        if not text.strip():
            raise RuntimeError("The drafting engine returned no text.")
        return text


class OpenAICompatEngine(CamEngine):
    """Any OpenAI-compatible chat endpoint — AWS Bedrock's by default (the GLM models
    Chitti also uses). Text only: scans reach it as OCR text from DocRAG, never as
    images. Requests a stream (a full CAM can take minutes, and SSE keeps the
    connection alive chunk by chunk) but also accepts a plain JSON reply. A reasoning
    model's separate ``reasoning_content`` is ignored — only the answer is kept."""

    supports_documents = False

    def __init__(self, model: str, base_url: str, api_key: str, *,
                 max_tokens: int = 32768, timeout_s: float = 540.0,
                 provider: str = "bedrock") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.name = f"{provider}:{model}"

    # A full CAM can run past one response's output limit. When the engine stops for
    # length, it is asked to continue from where it stopped (bounded), and the pieces
    # are joined — a CAM missing its last sections is worse than one that took longer.
    max_continuations = 3
    _CONTINUE = ("Continue exactly where you stopped, mid-sentence if needed. Do not repeat "
                 "anything already written and do not add a preamble.")

    async def generate(self, http: Any, system: str, turns: list[dict[str, Any]]) -> str:
        messages = [{"role": "system", "content": system},
                    *({"role": t["role"], "content": _as_text(t["content"])} for t in turns)]
        pieces: list[str] = []
        for _ in range(self.max_continuations + 1):
            piece, finish = await self._complete(http, messages)
            pieces.append(piece)
            if finish != "length":
                break
            messages = [*messages, {"role": "assistant", "content": piece},
                        {"role": "user", "content": self._CONTINUE}]
        text = "".join(pieces)
        if not text.strip():
            raise RuntimeError("The drafting engine returned no text.")
        return text

    async def _complete(self, http: Any, messages: list[dict[str, Any]]) -> tuple[str, str]:
        """One request: (text, finish_reason). Every request is recorded with the token
        usage the provider reports — failures too, since they cost time if not tokens."""
        started = _time.monotonic()
        usage: dict[str, Any] | None = None
        finish = ""
        try:
            text, finish, usage = await self._request(http, messages)
        except Exception as exc:
            record_llm_call(engine=self.name, ok=False, started=started, usage=usage,
                            error=str(exc))
            raise
        record_llm_call(engine=self.name, ok=True, started=started, usage=usage,
                        finish=finish)
        return text, finish

    async def _request(self, http: Any, messages: list[dict[str, Any]],
                       ) -> tuple[str, str, dict[str, Any] | None]:
        import json as _json

        import httpx
        text_parts: list[str] = []
        finish = ""
        usage: dict[str, Any] | None = None
        async with http.stream(
            "POST", f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "stream": True,
                  # The final stream event then carries the token counts.
                  "stream_options": {"include_usage": True},
                  "max_completion_tokens": self.max_tokens},
            timeout=httpx.Timeout(self.timeout_s, connect=15.0),
        ) as r:
            if r.status_code >= 300:
                body = (await r.aread()).decode("utf-8", "ignore")
                detail = ""
                try:
                    err = (_json.loads(body) or {}).get("error") or {}
                    detail = str(err.get("message") or "") if isinstance(err, dict) else str(err)
                except ValueError:
                    detail = body[:300]
                raise RuntimeError(f"The drafting engine refused (HTTP {r.status_code})"
                                   + (f": {detail}" if detail else "."))
            if "text/event-stream" not in (r.headers.get("content-type") or ""):
                data = _json.loads((await r.aread()).decode("utf-8", "ignore") or "{}")
                choice = (data.get("choices") or [{}])[0]
                text_parts.append(((choice.get("message") or {}).get("content")) or "")
                finish = choice.get("finish_reason") or ""
                usage = data.get("usage") or None
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = _json.loads(payload)
                    except ValueError:
                        continue
                    if event.get("error"):
                        raise RuntimeError("The drafting engine failed mid-answer: "
                                           f"{event['error']}")
                    if event.get("usage"):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text_parts.append(delta["content"])
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        return "".join(text_parts), finish, usage


def _as_text(content: Any) -> str:
    """A turn's content as plain text (block lists only arise for Anthropic scans)."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")


def build_engine(settings: Any) -> CamEngine:
    spec = (getattr(settings, "cam_engine", "") or "anthropic:claude-haiku-4-5").strip()
    provider, _, model = spec.partition(":")
    anthropic_key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if provider in ("bedrock", "openai"):
        key = (getattr(settings, "cam_llm_api_key", "") or "").strip()
        if key and model:
            return OpenAICompatEngine(
                model, settings.cam_llm_base_url, key,
                max_tokens=int(getattr(settings, "cam_max_completion_tokens", 32768)),
                timeout_s=float(getattr(settings, "cam_llm_timeout_s", 540.0)),
                provider=provider)
        # A deployment configured before GLM (only the Anthropic key set) keeps a real
        # engine instead of silently dropping to the stub when the default changed.
        if anthropic_key:
            return AnthropicEngine("claude-haiku-4-5", anthropic_key)
        return StubEngine()
    key = anthropic_key
    if provider == "anthropic" and key:
        return AnthropicEngine(model or "claude-haiku-4-5", key)
    return StubEngine()


# --------------------------------------------------------------------------- #
# Workbench routes
# --------------------------------------------------------------------------- #
class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_doc_ids: list[str] = Field(min_length=1, max_length=25)
    # The drafting brief: EITHER a Data Register document (the credit team's master
    # prompt, or a case-specific upload) OR text typed in the workbench. One required.
    prompt_doc_id: str | None = Field(default=None, max_length=64)
    prompt_text: str | None = Field(default=None, max_length=100_000)
    deal_id: str | None = Field(default=None, max_length=64)


class RefineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=100_000)
    # True (default): the engine's reply REPLACES the working draft — a rework.
    # False: an ASK — the reply comes back (and joins the transcript) but the analyst's
    # working draft stays untouched; they copy what is useful into it themselves.
    update_draft: bool = True
    # Documents to SEND WITH this turn — the conversation is not frozen at generate
    # time; a summary of two fresh files mid-drafting is an ordinary move.
    source_doc_ids: list[str] = Field(default_factory=list, max_length=25)


class ExtractTermsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str = Field(min_length=1, max_length=64)
    # The committee's credit note — extra context so the numeric terms (amount, rate,
    # tenor, EMI, …) come out of what was actually approved, not only the letter.
    credit_note: str | None = Field(default=None, max_length=20_000)


class ExportDocxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The box content — the engine's answer (Markdown) after the analyst's edits.
    markdown: str = Field(min_length=1, max_length=400_000)
    title: str | None = Field(default=None, max_length=200)
    # The CAM template (a Data Register .docx). When given, the draft is rendered INSIDE
    # that package — the credit team's styles, fonts, letterhead and page setup.
    template_doc_id: str | None = Field(default=None, max_length=64)


class DraftLetterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The letterhead template whose STRUCTURE the draft must follow.
    template_doc_id: str = Field(min_length=1, max_length=64)
    # The completed CAM the figures come from (optional — a letter can draft from the
    # credit note and typed terms alone, with placeholders for the rest).
    cam_doc_id: str | None = Field(default=None, max_length=64)
    credit_note: str | None = Field(default=None, max_length=20_000)
    # Whatever the analyst has ALREADY typed into the terms form — these override the
    # documents (the human's figures win).
    terms: dict[str, Any] | None = None


class StagedDraftIn(BaseModel):
    """Generate CAM, staged: the analyst's documents + the master prompt → a full CAM."""
    model_config = ConfigDict(extra="forbid")
    # Documents ticked for this generation. Documents sent earlier in the conversation
    # are included automatically (the transcript records them).
    source_doc_ids: list[str] = Field(default_factory=list, max_length=60)
    prompt_doc_id: str | None = Field(default=None, max_length=64)
    prompt_text: str | None = Field(default=None, max_length=200_000)
    # The CAM template (rendering only) — never sent to the engine as a source.
    template_doc_id: str | None = Field(default=None, max_length=64)
    # Anything typed but not yet sent — rides as an extra instruction.
    instruction: str | None = Field(default=None, max_length=20_000)
    # Human names for the documents (the UI has them): used in the manifest and in
    # [source: …] tags instead of raw ids.
    doc_titles: dict[str, str] = Field(default_factory=dict, max_length=200)


class FinaliseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    # The analyst may finish the CAM OUTSIDE the workbench (download the Word template,
    # fill it, upload it to the line). Passing that document's id submits IT to the
    # committee — the in-app draft, if any, is superseded as the committee copy.
    document_id: str | None = Field(default=None, max_length=64)


def mount_cam(app: Any, settings: Any, *, denied: Any, verified_email: Any,
              caller_context: Any, problem: Any) -> None:
    """Register the workbench routes on the orchestrator app (closure style, like the
    rest of the API — the helpers come from create_app)."""

    engine = build_engine(settings)
    base = settings.register_base_url.rstrip("/")
    max_doc_chars = int(getattr(settings, "cam_max_doc_chars", 60_000))
    total_budget = int(getattr(settings, "cam_max_total_doc_chars", 450_000))
    docrag_url = (getattr(settings, "docrag_url", "") or "").rstrip("/")

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

    async def _doc_fetch(request: Request, caller: Any, who: str,
                         doc_id: str) -> tuple[bytes | None, str, str | None]:
        """(blob, content_type, fetch_error) — one read, shared by text extraction
        and the scanned-PDF attachment path."""
        path = f"/v1/documents/{doc_id}/content"
        r = await request.app.state.http.get(
            f"{base}{path}",
            headers=_reg_headers(request, caller, who, "GET", path),
            follow_redirects=True)
        if r.status_code == 404:
            return None, "", "not found on the register"
        if r.status_code >= 300:
            return None, "", f"register refused the read (HTTP {r.status_code})"
        return r.content, r.headers.get("content-type") or "", None

    def _docrag_suffix(ctype: str, blob: bytes) -> str | None:
        """The DocRAG-readable kind of this file, or None (read locally)."""
        if not docrag_url:
            return None
        if is_pdf(ctype, blob):
            return ".pdf"
        if "spreadsheetml" in (ctype or "") or (blob[:2] == b"PK" and b"xl/" in blob[:4096]):
            return ".xlsx"
        return image_suffix(ctype, blob)

    async def _extract(request: Request, doc_id: str, blob: bytes,
                       ctype: str) -> tuple[str, str | None, dict[str, Any]]:
        """(text, skip_reason, info). PDFs and spreadsheets go to DocRAG when it is
        configured — OpenDataLoader structure, tables as Markdown, Sarvam OCR for
        scanned pages — everything else (and everything, when DocRAG is down) takes
        the basic in-process pass. ``info`` says which ran and carries DocRAG's
        warnings, so the analyst sees e.g. that a scan had no OCR."""
        suffix = _docrag_suffix(ctype, blob)
        if suffix is None:
            text, reason = extract_text(ctype, blob)
            return text, reason, {"via": "basic"}
        import httpx
        http = getattr(request.app.state, "docrag_http", None) or request.app.state.http
        try:
            r = await http.post(
                f"{docrag_url}/v1/extract",
                files={"file": (f"{doc_id}{suffix}", blob)},
                headers={"X-API-Key": settings.docrag_api_key,
                         "X-Tenant": request.headers.get("X-Tenant", settings.register_tenant)},
                timeout=float(getattr(settings, "docrag_timeout_s", 600.0)))
            failure = None if r.status_code < 300 else f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            r, failure = None, type(exc).__name__
        if r is not None and failure is None:
            body = r.json() or {}
            md = body.get("markdown") or ""
            info = {"via": "docrag", "engines": body.get("extraction_engines") or [],
                    "warnings": body.get("warnings") or [],
                    "doc_type": body.get("doc_type") or "", "pages": body.get("page_count"),
                    "telemetry": body.get("telemetry") or {"cached": body.get("cached")}}
            if md.strip():
                return md, None, info
            if suffix == ".pdf":
                return "", _SCANNED_PDF, info
            if suffix not in (".xlsx",):
                return "", _SCANNED_IMAGE, info
            return "", "no text extracted", info
        text, reason = extract_text(ctype, blob)
        return text, reason, {"via": "basic",
                              "note": f"DocRAG unavailable ({failure}); basic extraction used"}

    async def _read_docs(request: Request, caller: Any, who: str,
                         doc_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch + extract several documents CONCURRENTLY (bounded), in input order.
        Each row: doc_id, blob, ctype, text, reason (skip reason or None), info."""
        import asyncio as _asyncio

        gate = _asyncio.Semaphore(4)

        async def one(doc_id: str) -> dict[str, Any]:
            async with gate:
                blob, ctype, err = await _doc_fetch(request, caller, who, doc_id)
                if err is not None:
                    return {"doc_id": doc_id, "blob": None, "ctype": "", "text": "",
                            "reason": err, "info": {}}
                text, reason, info = await _extract(request, doc_id, blob or b"", ctype)
                info["figures"] = record_document(
                    doc_id=doc_id, fmt=doc_format(ctype, blob or b""),
                    via=info.get("via", "basic"), ok=bool(text.strip()),
                    telemetry=info.get("telemetry"), reason=reason or "")
                return {"doc_id": doc_id, "blob": blob, "ctype": ctype, "text": text,
                        "reason": reason, "info": info}

        return list(await _asyncio.gather(*(one(d) for d in doc_ids)))

    def _budgeted(text: str, used: int) -> tuple[str, str | None]:
        """Apply the per-document and per-request limits; (text, note)."""
        note = None
        if len(text) > max_doc_chars:
            text, note = text[:max_doc_chars], f"truncated to {max_doc_chars} characters"
        room = total_budget - used
        if len(text) > room:
            text = text[:max(room, 0)]
            note = ("over this request's total document budget — "
                    + ("truncated" if text else "left out") + f" ({total_budget} characters)")
        return text, note

    def _scan_reason(info: dict[str, Any], what: str = "scanned PDF") -> str:
        if info.get("via") == "docrag":
            detail = "; ".join(w for w in info.get("warnings") or [] if "Sarvam" in w)
            return (f"{what} — OCR produced no text"
                    + (f" ({detail})" if detail else
                       " (set SARVAM_API_KEY on DocRAG to read scans)"))
        return ("scanned PDF — " + (
            "over the attachment limits for one draft" if engine.supports_documents else
            "this engine cannot read scans; configure DocRAG with Sarvam OCR "
            "(WORKFLOWS_DOCRAG_URL + SARVAM_API_KEY)"))

    def _why(reason: str | None, info: dict[str, Any]) -> str | None:
        """A skip reason the analyst can act on."""
        if reason == _SCANNED_PDF:
            return _scan_reason(info)
        if reason == _SCANNED_IMAGE:
            return _scan_reason(info, "image")
        return reason

    async def _doc_text(request: Request, caller: Any, who: str,
                        doc_id: str) -> tuple[str, str | None]:
        """(text, skip_reason). Text-like content comes back whole (bounded);
        formats with no extractor are skipped WITH the reason."""
        [row] = await _read_docs(request, caller, who, [doc_id])
        text, reason = row["text"], row["reason"]
        if reason is not None:
            return "", _why(reason, row["info"])
        if len(text) > max_doc_chars:
            return text[:max_doc_chars], f"truncated to {max_doc_chars} characters"
        return text, None

    def _pdf_block(blob: bytes) -> dict[str, Any]:
        import base64

        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(blob).decode("ascii")}}

    async def _scan_blocks(request: Request, caller: Any, who: str,
                           doc_ids: list[str]) -> list[dict[str, Any]]:
        """Document blocks for the SCANNED PDFs among doc_ids — the files an engine
        with visual PDF support reads itself. Bounded; failures drop silently here
        because the caller already reported each document's fate at generate time."""
        blocks: list[dict[str, Any]] = []
        for doc_id in doc_ids:
            if len(blocks) >= _PDF_ATTACH_MAX_DOCS:
                break
            blob, ctype, err = await _doc_fetch(request, caller, who, doc_id)
            if err is not None or not blob or not is_pdf(ctype, blob):
                continue
            if len(blob) > _PDF_ATTACH_MAX_BYTES:
                continue
            _text, reason = extract_text(ctype, blob)
            if reason == _SCANNED_PDF:
                blocks.append(_pdf_block(blob))
        return blocks

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

    @app.get("/v1/cam/doc-text", tags=["CAM"],
             summary="What the engine will actually read from one document")
    async def cam_doc_text(doc_id: str, request: Request) -> Any:
        """The extracted text of a Data Register document — the workbench shows it so
        the analyst can SEE what goes to the engine (and copy it if they want to work
        outside). A document with no extractable text answers with the reason instead;
        a scanned PDF says it will be attached visually."""
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)
        blob, ctype, fetch_err = await _doc_fetch(request, caller, who, doc_id)
        if fetch_err is not None:
            return problem(404, "Not found", f"Document {doc_id!r}: {fetch_err}")
        text, reason, info = await _extract(request, doc_id, blob or b"", ctype)
        truncated = len(text) > max_doc_chars
        out: dict[str, Any] = {"doc_id": doc_id, "content_type": ctype,
                               "text": text[:max_doc_chars], "truncated": truncated,
                               "via": info.get("via", "basic")}
        if info.get("warnings"):
            out["warnings"] = info["warnings"]
        if info.get("note"):
            out["note"] = info["note"]
        if reason is not None:
            attachable = reason == _SCANNED_PDF and engine.supports_documents
            out["reason"] = (reason if attachable or reason != _SCANNED_PDF
                             else _scan_reason(info))
            out["attachable"] = attachable
        return out

    # A filed letter is IMMUTABLE, so its extraction is too: the first read (the
    # sanction-terms dialog fires one the moment the letter uploads) computes and
    # CACHES per document; every later read — the CP checklist's "Read CP
    # conditions", another user, another session — answers in milliseconds from
    # here. Concurrent reads of the same document share ONE engine call. In-memory
    # by design: a service restart merely recomputes on the next click.
    _extract_cache: dict[str, tuple[float, dict]] = {}
    _extract_inflight: dict[str, Any] = {}
    _EXTRACT_TTL_S = 24 * 3600

    @app.post("/v1/cam/extract-terms", tags=["CAM"],
              summary="Read CP / CS / covenants OUT of a sanction letter (engine-parsed)")
    async def cam_extract_terms(payload: ExtractTermsIn, request: Request) -> Any:
        """The signed sanction letter already lists the Conditions Precedent, the
        Conditions Subsequent (with timelines) and the Reporting Covenants — nobody
        should re-type them into the terms form. The engine reads the letter and hands
        back the three lists as data; the analyst reviews and saves. Everything stays
        checkable: the response names the engine, and the analyst edits before seeding."""
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)

        import asyncio as _asyncio
        import time as _time
        cache_key = str(payload.doc_id)
        hit = _extract_cache.get(cache_key)
        if hit and (_time.monotonic() - hit[0]) < _EXTRACT_TTL_S:
            return {**hit[1], "cached": True}
        pending = _extract_inflight.get(cache_key)
        if pending is not None:
            # someone is already reading this letter — share their answer
            try:
                result = await _asyncio.shield(pending)
                return {**result, "cached": True}
            except Exception:  # noqa: BLE001 — their failure; run our own read below
                pass
        fut: Any = _asyncio.get_event_loop().create_future()
        _extract_inflight[cache_key] = fut

        blob, ctype, fetch_err = await _doc_fetch(request, caller, who, payload.doc_id)
        if fetch_err is not None:
            _extract_inflight.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(RuntimeError(str(fetch_err)))
            return problem(404, "Not found", f"Document {payload.doc_id!r}: {fetch_err}")
        text, reason, _info = await _extract(request, payload.doc_id, blob or b"", ctype)
        content: Any
        instruction = (
            "Read the sanction letter above (and the credit note, when given) and "
            "extract the sanction exactly as stated. Respond with ONLY a JSON object, "
            "no prose, shaped:\n"
            '{"cp_items": ["<each Condition Precedent, one short line>"],\n'
            ' "cs_items": [{"label": "<Condition Subsequent>", "timeline": "<as stated>"}],\n'
            ' "covenants": [{"name": "<reporting covenant>", '
            '"frequency": "Monthly|Quarterly|SemiAnnual|Annual", '
            '"timeline": "<as stated>"}],\n'
            ' "terms": {"amount_cr": <number, in ₹ crore>, '
            '"rate_kind": "Fixed|Floating", "rate_pct": <number>, '
            '"spread_pct": <number or null>, "tenor_months": <integer>, '
            '"emi_amount": <number or null>, "repayment_start": "YYYY-MM-DD or null", '
            '"day_count": "365|360", "penal_rate_pct": <number or null>, '
            '"moratorium_months": <integer or null>, '
            '"schedule_kind": "EMI|Bullet|Custom"}}\n'
            "Frequency: infer from the stated timeline (monthly→Monthly, quarter→"
            "Quarterly, half-year→SemiAnnual, annual→Annual); default Monthly. "
            "CLASSIFICATION — letters name these sections differently, so classify by "
            "NATURE, not by heading: cp_items are ONE-TIME conditions to satisfy "
            "BEFORE disbursement (whether headed 'Conditions Precedent', "
            "'Pre-disbursement conditions', 'Terms and Conditions', or just listed); "
            "cs_items are one-time conditions allowed AFTER disbursement with a "
            "stated timeline; covenants are ONLY the ONGOING, RECURRING obligations "
            "(periodic reporting, ratio maintenance like DSCR/FACR, insurance "
            "renewal). A one-time act is NEVER a covenant — when in doubt between a "
            "CP and a covenant, a condition without a recurring frequency is a CP. "
            "A typical sanction letter yields several cp_items; an empty cp_items "
            "list is almost always a misclassification — re-check before answering. "
            "GROUNDING: every item must correspond to a clause actually written in "
            "the letter — one listed clause yields ONE item (split only when a "
            "single clause plainly names distinct obligations), and the total item "
            "count across the three lists should track the letter's own "
            "enumeration, never exceed it by padding or paraphrase variants. "
            "In terms, use null for anything the documents do not state. Do not invent "
            "conditions or figures that are not in the documents.")
        extra = (f"\n\n===== CREDIT NOTE (committee approval) =====\n"
                 f"{payload.credit_note[:20_000]}" if payload.credit_note else "")
        if text.strip():
            content = (f"===== SANCTION LETTER =====\n{text[:max_doc_chars]}"
                       f"{extra}\n\n{instruction}")
        elif (reason == _SCANNED_PDF and engine.supports_documents and blob
              and len(blob) <= _PDF_ATTACH_MAX_BYTES):
            content = [_pdf_block(blob), {"type": "text", "text": extra + "\n\n" + instruction
                                          if extra else instruction}]
        else:
            _extract_inflight.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(RuntimeError("unreadable letter"))
            why = _why(reason, _info)
            return problem(422, "Validation failed",
                           f"The letter could not be read{f' ({why})' if why else ''} "
                           "— enter the conditions by hand.")
        _SYSTEM = ("You extract structured data from credit documents. You answer with "
                   "JSON only — no commentary, no code fences.")
        try:
            reply = await engine.generate(
                request.app.state.http, _SYSTEM,
                [{"role": "user", "content": content}])
        except Exception as exc:  # noqa: BLE001 — vendor errors surface as text
            _extract_inflight.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(RuntimeError(str(exc)))
            return problem(502, "Extraction failed", str(exc))
        import json as _json
        import re as _re

        def _parse(raw: str) -> dict:
            mm = _re.search(r"\{.*\}", raw, _re.DOTALL)
            try:
                return _json.loads(mm.group(0)) if mm else {}
            except ValueError:
                return {}

        data = _parse(reply)
        # A sanction letter with ZERO conditions precedent is almost always a
        # misclassification (everything swept into covenants) — the field hit it
        # three letters running. One corrective re-ask, only in that case: the
        # model is shown its own answer and told to re-split by nature.
        if (not (data.get("cp_items") or [])
                and (data.get("covenants") or data.get("cs_items"))):
            try:
                reply2 = await engine.generate(
                    request.app.state.http, _SYSTEM,
                    [{"role": "user", "content": content},
                     {"role": "assistant", "content": reply},
                     {"role": "user", "content":
                      "Your answer has an empty cp_items list, which is almost "
                      "certainly wrong for a sanction letter. Re-read the letter: "
                      "every ONE-TIME act required before disbursement is a CP even "
                      "when it sits under a generic 'Terms and Conditions' heading "
                      "or reads like documentation/security/fee work. Keep in "
                      "covenants ONLY genuinely recurring periodic obligations. "
                      "Answer again with the SAME JSON shape, complete."}])
                data2 = _parse(reply2)
                if data2.get("cp_items"):
                    data = data2
            except Exception:  # noqa: BLE001 — the first answer stands
                pass
        # CLAMPED to the register's own schema limits (covenant name 200, labels
        # 1000) — a letter whose clause runs long must never wedge the save with a
        # 422 the user cannot fix from the dialog.
        cp = [str(x).strip()[:1000] for x in (data.get("cp_items") or []) if str(x).strip()]
        cs = [({"label": str(x.get("label") or "").strip()[:1000],
                **({"timeline": str(x.get("timeline")).strip()[:200]} if x.get("timeline") else {})}
               if isinstance(x, dict) else {"label": str(x).strip()[:1000]})
              for x in (data.get("cs_items") or [])]
        cs = [x for x in cs if x["label"]]
        freq_ok = {"Monthly", "Quarterly", "SemiAnnual", "Annual"}
        cov = []
        for x in (data.get("covenants") or []):
            if not isinstance(x, dict) or not str(x.get("name") or "").strip():
                continue
            f = str(x.get("frequency") or "").strip()
            cov.append({"name": str(x["name"]).strip()[:200],
                        "frequency": f if f in freq_ok else "Monthly",
                        **({"timeline": str(x.get("timeline")).strip()[:200]}
                           if x.get("timeline") else {})})
        # The NUMERIC terms — validated field by field, null for anything unstated.
        raw_terms = data.get("terms") if isinstance(data.get("terms"), dict) else {}
        terms: dict[str, Any] = {}

        def _num(key: str, lo: float, hi: float) -> None:
            v = raw_terms.get(key)
            if isinstance(v, (int, float)) and lo <= float(v) <= hi:
                terms[key] = float(v)

        _num("amount_cr", 0.0001, 1_000_000)
        _num("rate_pct", 0, 100)
        _num("spread_pct", 0, 100)
        _num("emi_amount", 0.0000001, 10**12)
        _num("penal_rate_pct", 0, 100)
        for key, hi in (("tenor_months", 600), ("moratorium_months", 120)):
            v = raw_terms.get(key)
            if isinstance(v, (int, float)) and 0 <= int(v) <= hi:
                terms[key] = int(v)
        for key, allowed in (("rate_kind", {"Fixed", "Floating"}),
                             ("day_count", {"365", "360"}),
                             ("schedule_kind", {"EMI", "Bullet", "Custom"})):
            v = str(raw_terms.get(key) or "").strip()
            if v in allowed:
                terms[key] = v
        rs = str(raw_terms.get("repayment_start") or "").strip()
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", rs):
            terms["repayment_start"] = rs

        if not (cp or cs or cov or terms):
            _extract_inflight.pop(cache_key, None)
            if not fut.done():
                fut.set_exception(RuntimeError("extraction yielded nothing"))
            return problem(422, "Extraction failed",
                           "The engine did not return usable lists from this letter — "
                           "enter the conditions by hand (the offline stub engine "
                           "cannot parse documents).")
        result = {"engine": engine.name, "cp_items": cp, "cs_items": cs,
                  "covenants": cov, "terms": terms}
        _extract_cache[cache_key] = (_time.monotonic(), result)
        _extract_inflight.pop(cache_key, None)
        if not fut.done():
            fut.set_result(result)
        return result

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

        # The brief comes from the credit team — a prompt DOCUMENT or TYPED text; the
        # workbench never invents one. Refuse without either.
        if payload.prompt_text and payload.prompt_text.strip():
            prompt_text = payload.prompt_text
        elif payload.prompt_doc_id:
            prompt_text, skip = await _doc_text(request, caller, who, payload.prompt_doc_id)
            if not prompt_text.strip():
                return problem(422, "Validation failed",
                               f"The prompt document could not be read"
                               f"{f' ({skip})' if skip else ''} — the workbench drafts only "
                               "from the credit team's own prompts.")
        else:
            return problem(422, "Validation failed",
                           "Pick a prompt document or type the drafting brief — the "
                           "workbench drafts only from the credit team's own prompts.")

        included: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        parts: list[str] = []
        attachments: list[dict[str, Any]] = []
        used = 0
        for row in await _read_docs(request, caller, who, payload.source_doc_ids):
            doc_id, blob, reason, info = row["doc_id"], row["blob"], row["reason"], row["info"]
            if blob is None:
                skipped.append({"doc_id": doc_id, "reason": reason})
                continue
            text = row["text"]
            if text.strip():
                text, note = _budgeted(text, used)
                if not text:
                    skipped.append({"doc_id": doc_id, "reason": note})
                    continue
                used += len(text)
                notes = [n for n in (note, info.get("note")) if n]
                included.append({"doc_id": doc_id, "via": info.get("via", "basic"),
                                 **({"note": "; ".join(notes)} if notes else {}),
                                 **({"warnings": info["warnings"]}
                                    if info.get("warnings") else {})})
                parts.append(f"\n\n===== DOCUMENT {doc_id} =====\n{text}")
                continue
            # A SCAN has no text layer — but an engine with visual PDF support reads
            # the file itself, so hand it over instead of refusing.
            if (reason == _SCANNED_PDF and engine.supports_documents and blob
                    and len(blob) <= _PDF_ATTACH_MAX_BYTES
                    and len(attachments) < _PDF_ATTACH_MAX_DOCS):
                attachments.append(_pdf_block(blob))
                included.append({"doc_id": doc_id,
                                 "note": "scanned PDF — attached for the engine to read"})
                continue
            skipped.append({"doc_id": doc_id, "reason": _why(reason, info) or "empty"})
        if not included:
            return problem(422, "Validation failed",
                           "None of the selected documents could be read — "
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
        # Scanned PDFs ride as document blocks beside the text — the engine reads them
        # visually. (The durable transcript records WHICH documents, not the bytes.)
        content: Any = ([*attachments, {"type": "text", "text": first_ask}]
                        if attachments else first_ask)
        try:
            brief = (f"prompt doc {payload.prompt_doc_id}" if payload.prompt_doc_id
                     else "typed brief")
            await _record_turn(request, caller, who, report["id"], "user",
                               f"[generate] {brief}; "
                               f"documents: {', '.join(d['doc_id'] for d in included)}"
                               + (f"; {len(attachments)} scanned PDF(s) attached"
                                  if attachments else ""))
            with metered() as meter:
                draft = await engine.generate(request.app.state.http, _SYSTEM,
                                              [{"role": "user", "content": content}])
            await _record_turn(request, caller, who, report["id"], "assistant",
                               draft, draft=draft)
        except RuntimeError as exc:
            return problem(502, "Drafting failed", f"{exc} The CAM version stays open — "
                           "retry the generation; nothing was filed.")
        return {"report_id": report["id"], "report_version": report["report_version"],
                "engine": engine.name, "draft_md": draft,
                "included": included, "skipped": skipped, "usage": meter.summary()["llm"]}

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
        # First ask on this line? The version OPENS ITSELF — the workbench is a
        # conversation from the first question, not a generate-then-talk two-step.
        if report is None:
            open_path = "/v1/internal/cam-reports"
            opened = await request.app.state.http.post(
                f"{base}{open_path}",
                json={"lending_id": lending_id, "engine": engine.name},
                headers=_reg_headers(request, caller, who, "POST", open_path))
            if opened.status_code >= 300:
                return problem(opened.status_code if opened.status_code in (403, 404, 409)
                               else 502, "CAM not opened", opened.text[:500])
            report = opened.json()
        # Rebuild the conversation from the durable transcript (engines are stateless).
        path = f"/v1/internal/cam-reports/{report['id']}"
        full = await request.app.state.http.get(
            f"{base}{path}", headers=_reg_headers(request, caller, who, "GET", path))
        turns = [{"role": t["role"], "content": t["content"]}
                 for t in (full.json().get("turns") or [])] if full.status_code < 300 else []
        # Documents sent WITH this turn: text extracts inline, scans as attachments,
        # anything unreadable named in the response — same honesty as generate.
        extra_parts: list[str] = []
        turn_attachments: list[dict[str, Any]] = []
        doc_notes: list[dict[str, Any]] = []
        used = 0
        for row in await _read_docs(request, caller, who, payload.source_doc_ids):
            doc_id, blob, reason, info = row["doc_id"], row["blob"], row["reason"], row["info"]
            if blob is None:
                doc_notes.append({"doc_id": doc_id, "reason": reason})
                continue
            text = row["text"]
            if text.strip():
                text, note = _budgeted(text, used)
                if not text:
                    doc_notes.append({"doc_id": doc_id, "reason": note})
                    continue
                used += len(text)
                extra_parts.append(f"\n\n===== DOCUMENT {doc_id} =====\n{text}")
                notes = [n for n in (note, info.get("note")) if n]
                doc_notes.append({"doc_id": doc_id, "included": "text",
                                  "via": info.get("via", "basic"),
                                  **({"note": "; ".join(notes)} if notes else {}),
                                  **({"warnings": info["warnings"]}
                                     if info.get("warnings") else {})})
            elif (reason == _SCANNED_PDF and engine.supports_documents and blob
                    and len(blob) <= _PDF_ATTACH_MAX_BYTES
                    and len(turn_attachments) < _PDF_ATTACH_MAX_DOCS):
                turn_attachments.append(_pdf_block(blob))
                doc_notes.append({"doc_id": doc_id, "included": "attached"})
            else:
                doc_notes.append({"doc_id": doc_id, "reason": (
                    _why(reason, info) or "empty")})
        ask_text = payload.instruction + "".join(extra_parts)
        # The transcript stores text only, so the ORIGINAL scanned sources are
        # RE-ATTACHED on each turn — the engine keeps seeing the same pages.
        content: Any = ask_text
        blocks: list[dict[str, Any]] = list(turn_attachments)
        if engine.supports_documents and report.get("source_doc_ids"):
            blocks = [*await _scan_blocks(request, caller, who,
                                          list(report["source_doc_ids"])), *blocks]
        if blocks:
            content = [*blocks, {"type": "text", "text": ask_text}]
        turns.append({"role": "user", "content": content})
        try:
            sent = [d["doc_id"] for d in doc_notes if d.get("included")]
            await _record_turn(request, caller, who, report["id"], "user",
                               payload.instruction
                               + (f"\n[documents sent: {', '.join(sent)}]" if sent else ""))
            with metered() as meter, stage("ask" if not payload.update_draft else "update"):
                reply = await engine.generate(
                    request.app.state.http,
                    _SYSTEM if payload.update_draft else _ASK_SYSTEM, turns)
            # An ASK records the exchange (the transcript stays the audit answer to
            # "where did this figure come from?") but leaves draft_md alone.
            await _record_turn(request, caller, who, report["id"], "assistant",
                               reply, draft=reply if payload.update_draft else None)
        except RuntimeError as exc:
            return problem(502, "Drafting failed", str(exc))
        return {"report_id": report["id"], "report_version": report["report_version"],
                "engine": engine.name, "draft_md": reply,
                "updated_draft": payload.update_draft, "usage": meter.summary()["llm"],
                **({"documents": doc_notes} if payload.source_doc_ids else {})}

    @app.post("/v1/cam/{lending_id}/export-docx", tags=["CAM"],
              summary="Render workbench Markdown into a Word (.docx) file to review and upload")
    async def cam_export_docx(lending_id: str, payload: ExportDocxIn,
                              request: Request) -> Any:
        """The box content, as a Word document. Pure rendering — nothing is filed,
        no engine is called; the analyst reviews the file in Word and uploads it
        through the normal 'completed CAM' lane. With ``template_doc_id`` the draft is
        written into the CAM TEMPLATE's own package (its styles and letterhead); an
        unreadable template falls back to the plain document rather than failing."""
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        tmpl_blob = None
        if payload.template_doc_id:
            caller, _ = caller_context(request, who)
            tmpl_blob, _t, _e = await _doc_fetch(request, caller, who, payload.template_doc_id)
        if tmpl_blob:
            blob = markdown_into_template(
                payload.markdown, tmpl_blob, payload.title or None, cam=True,
                notice=("DRAFT — generated by PRISM from the CAM prompt and the deal's "
                        "documents. Verify every figure against the source documents "
                        "before filing, and delete this notice from the final CAM."))
        else:
            blob = markdown_to_docx(payload.markdown, payload.title or None, cam=True)
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", payload.title or "CAM").strip() or "CAM"
        return Response(
            content=blob, media_type=_DOCX_TYPE,
            headers={"Content-Disposition": f'attachment; filename="{safe}.docx"'})

    @app.post("/v1/cam/{lending_id}/draft-letter", tags=["CAM"],
              summary="Draft the sanction letter as Word: template structure, CAM figures")
    async def cam_draft_letter(lending_id: str, payload: DraftLetterIn,
                               request: Request) -> Any:
        """The engine FILLS the sanction-letter template — its structure and wording,
        the CAM's + credit note's + typed terms' figures — and the result comes back
        as a .docx to review and hand-edit in Word. Nothing is filed: the analyst
        uploads the finished, signed letter through the normal lane."""
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)
        # One fetch, two uses: the TEXT goes to the engine; the BYTES stay the
        # package the answer renders back into (styles, theme, logo, letterhead).
        tmpl_blob, _ctype, fetch_err = await _doc_fetch(
            request, caller, who, payload.template_doc_id)
        if fetch_err is not None:
            return problem(422, "Template unreadable",
                           f"The sanction letter template could not be read: {fetch_err}.")
        tmpl_text, tmpl_skip = extract_text(_ctype, tmpl_blob or b"")
        if not tmpl_text.strip():
            return problem(422, "Template unreadable",
                           f"The sanction letter template could not be read: "
                           f"{tmpl_skip or 'no text'}.")
        parts = [f"===== SANCTION LETTER TEMPLATE =====\n{tmpl_text}"]
        if payload.cam_doc_id:
            cam_text, cam_skip = await _doc_text(request, caller, who, payload.cam_doc_id)
            if cam_text.strip():
                parts.append(f"===== APPROVED CAM =====\n{cam_text}")
            else:
                parts.append(f"(The CAM document could not be read: {cam_skip or 'no text'})")
        if payload.credit_note:
            parts.append(f"===== COMMITTEE CREDIT NOTE =====\n{payload.credit_note}")
        typed = {k: v for k, v in (payload.terms or {}).items()
                 if v not in (None, "", [])}
        if typed:
            parts.append("===== TERMS ALREADY TYPED BY THE ANALYST (these override "
                         "the documents) =====\n"
                         + "\n".join(f"{k}: {v}" for k, v in sorted(typed.items())))
        parts.append(
            "Draft the sanction letter now: follow the TEMPLATE's structure, sections "
            "and wording; fill every figure and particular from the CAM, the credit "
            "note and the typed terms (typed terms win on conflict). Where a value is "
            "genuinely not on record, leave a [____] placeholder for the analyst. "
            "Begin EXACTLY where the template's text begins and mirror its opening "
            "block once — never add a document title, letterhead line or heading the "
            "template itself does not have. "
            "Formatting: every section title (CREDIT FACILITY DETAILS, REPAYMENT, "
            "FEE, INTEREST & CHARGES, …) is a '## ' heading; tabular particulars are "
            "Markdown tables with a header row, label in the first column; wrap the "
            "key commercial terms the borrower must not miss — sanctioned amount, "
            "rate, tenor, crucial conditions — in ==double equals== (yellow marker); "
            "wrap deal-variable figures the analyst must verify — notice periods, "
            "day counts, dates, anything that changes per deal — in ::double "
            "colons:: (blue marker), exactly where the template marks them. "
            "Output the LETTER ONLY, in clean Markdown — no commentary.")
        system = (
            "You draft SANCTION LETTERS for a climate-finance lender. You fill the "
            "credit team's own template faithfully — structure, clauses and tone — "
            "with figures taken ONLY from the supplied documents and terms; never "
            "invent a number. Missing values become [____] placeholders.")
        try:
            reply = await engine.generate(
                request.app.state.http, system,
                [{"role": "user", "content": "\n\n".join(parts)}])
        except RuntimeError as exc:
            return problem(502, "Letter drafting failed", str(exc))
        # Rendered INTO the template package — its fonts, colors and letterhead
        # are the credit team's, so the draft looks like the letter, not a memo.
        # letterhead=True adds the letter's visual language: navy section bars,
        # navy table headers with white text, navy bold label columns, and
        # ==highlights== as yellow marker.
        blob = markdown_into_template(
            reply, tmpl_blob or b"", letterhead=True,
            notice=("DRAFT — machine-generated by PRISM from the template, the CAM "
                    "and the credit note. Verify every figure, condition and [____] "
                    "placeholder against the committee's approval before signing, "
                    "and delete this notice from the final letter."))
        return Response(
            content=blob, media_type=_DOCX_TYPE,
            headers={"Content-Disposition":
                     'attachment; filename="Sanction letter - draft.docx"'})

    # ------------------------------------------------------------------ staged drafting
    # Generate CAM runs as a JOB: tens of engine calls take minutes, far past what a
    # proxied request should hold open. The job lives in this process (like the terms
    # cache) — a restart loses it and the analyst simply generates again.
    import asyncio as _aio
    import re as _re
    import time as _t
    import uuid as _uuid
    from types import SimpleNamespace

    _jobs: dict[str, dict[str, Any]] = {}
    _job_tasks: set[Any] = set()
    _digests = DigestCache()
    _SENT = _re.compile(r"\[documents sent: ([^\]]+)\]")

    def _owner(request: Request, who: str) -> str:
        """Whose job this is: the verified identity, or — in the dev posture, where
        there is none — the e-mail the gateway forwards."""
        return (who or request.headers.get("X-User-Email") or "").strip().lower()

    def _job_view(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def _conversation_notes(turns: list[dict[str, Any]], limit: int = 40_000) -> str:
        """The analyst's questions and the answers they got — context for the writers.
        Long assistant turns (earlier drafts) are clipped; the most recent part wins."""
        lines = []
        for t in turns:
            text = _SENT.sub("", str(t.get("content") or "")).strip()
            if not text or text.startswith("[generate"):
                continue
            who_ = "Analyst" if t.get("role") == "user" else "Assistant"
            lines.append(f"{who_}: {text if who_ == 'Analyst' else text[:4000]}")
        joined = "\n\n".join(lines)
        return joined[-limit:]

    async def _run_staged(job: dict[str, Any], ctx: Any, caller: Any, who: str,
                          report: dict[str, Any], prompt_text: str, doc_ids: list[str],
                          titles: dict[str, str], turns: list[dict[str, Any]],
                          instruction: str, template_id: str | None = None) -> None:
        meter = UsageMeter()

        def progress(stage: str, done: int, total: int) -> None:
            job.update(stage=stage, done=done, total=total, updated=_t.time(),
                       usage=meter.summary())

        with metered(meter):
            await _staged_body(job, ctx, caller, who, report, prompt_text, doc_ids, titles,
                               turns, instruction, template_id, progress)
        job["usage"] = meter.summary()
        u = job["usage"]
        _tlog.info(
            "cam_draft_usage job=%s status=%s engine=%s llm_calls=%d failed=%d "
            "input_tokens=%d output_tokens=%d documents_read=%d skipped=%d "
            "sarvam_jobs=%d sarvam_pages=%d ocr_pages=%d seconds=%.0f",
            job["job_id"], job.get("status"), job.get("engine") or engine.name,
            u["llm"]["calls"], u["llm"]["failed"], u["llm"]["input_tokens"],
            u["llm"]["output_tokens"], u["documents"]["read"], u["documents"]["skipped"],
            u["documents"]["sarvam_jobs"], u["documents"]["sarvam_pages_submitted"],
            u["documents"]["ocr_pages"], (job.get("finished") or _t.time()) - job["started"],
            extra={"event": "cam_draft_usage", "job_id": job["job_id"],
                   "lending_id": job.get("lending_id"), "status": job.get("status"),
                   "usage": u})

    async def _staged_body(job: dict[str, Any], ctx: Any, caller: Any, who: str,
                           report: dict[str, Any], prompt_text: str, doc_ids: list[str],
                           titles: dict[str, str], turns: list[dict[str, Any]],
                           instruction: str, template_id: str | None, progress: Any) -> None:
        try:
            progress("Reading documents", 0, len(doc_ids))
            rows = await _read_docs(ctx, caller, who, doc_ids)
            docs: list[SourceDoc] = []
            notes: list[dict[str, Any]] = []
            for row in rows:
                name = titles.get(row["doc_id"]) or row["doc_id"]
                info = row["info"]
                blob = row["blob"]
                if row["text"].strip():
                    docs.append(SourceDoc(row["doc_id"], name, row["text"],
                                          info.get("doc_type") or "", info.get("pages")))
                    notes.append({"doc_id": row["doc_id"], "document": name,
                                  "via": info.get("via", "basic"),
                                  **info.get("figures", {}),
                                  **({"note": info["note"]} if info.get("note") else {}),
                                  **({"warnings": info["warnings"]}
                                     if info.get("warnings") else {})})
                elif (row["reason"] == _SCANNED_PDF and engine.supports_documents and blob
                      and len(blob) <= _PDF_ATTACH_MAX_BYTES):
                    # An engine that reads PDFs itself (Anthropic) gets the scan, as the
                    # single-call workbench always did.
                    docs.append(SourceDoc(row["doc_id"], name, "", "", None,
                                          blocks=[_pdf_block(blob)]))
                    notes.append({"doc_id": row["doc_id"], "document": name,
                                  "via": "attached scan"})
                else:
                    reason = row["reason"]
                    notes.append({"doc_id": row["doc_id"], "document": name,
                                  "reason": _why(reason, info) or "empty",
                                  **{k: v for k, v in info.get("figures", {}).items()
                                     if k != "via"}})
            if not docs:
                raise RuntimeError("None of the documents could be read — "
                                   + "; ".join(f"{n['document']}: {n.get('reason')}"
                                               for n in notes))
            analyst = _conversation_notes(turns)
            if instruction:
                analyst = f"{analyst}\n\nAnalyst's instruction for this draft: {instruction}"
            master = parse_master_prompt(prompt_text)
            http = ctx.app.state.http
            template_text = ""
            if master is None and template_id:
                template_text, _skip = await _doc_text(ctx, caller, who, template_id)
            if master is None:
                # Not a sectioned master prompt: one call, as the workbench always did.
                job.update(mode="single")
                progress("Drafting", 0, 1)
                used, parts = 0, []
                for d in docs:
                    text, _note = _budgeted(d.text, used)
                    used += len(text)
                    if text:
                        parts.append(f"\n\n===== DOCUMENT: {d.name} =====\n{text}")
                if template_text:
                    parts.insert(0, "\n\n===== CAM TEMPLATE (reproduce its structure: the "
                                    "same sections, order and headings) =====\n"
                                    + template_text[:max_doc_chars])
                ask = (f"{prompt_text.strip()}\n\n{analyst}".strip() + "".join(parts)
                       + "\n\nPrepare the complete CAM report now, following the prompt "
                       "document's instructions exactly.")
                scans = [b for d in docs for b in (d.blocks or [])]
                content: Any = ([*scans, {"type": "text", "text": ask}] if scans else ask)
                with stage("single_draft"):
                    draft = await engine.generate(http, _SYSTEM,
                                                  [{"role": "user", "content": content}])
                workpaper, calls = "", 1
            else:
                job.update(mode="staged", sections=len(master.sections))
                result = await draft_staged(
                    generate=lambda sys, t: engine.generate(http, sys, t),
                    system=_SYSTEM, prompt=master, docs=docs, analyst_notes=analyst,
                    cache=_digests, model_id=engine.name, progress=progress,
                    concurrency=int(getattr(settings, "cam_pipeline_concurrency", 4)),
                    call_budget=int(getattr(settings, "cam_call_budget_chars", 360_000)),
                    digest_part_chars=int(getattr(settings, "cam_digest_part_chars", 90_000)))
                draft, workpaper, calls = result.draft_md, result.workpaper_md, result.calls
                notes.extend(result.notes)
            await _record_turn(ctx, caller, who, report["id"], "user",
                               f"[generate — {job.get('mode')}] documents: "
                               + ", ".join(d.name for d in docs))
            await _record_turn(ctx, caller, who, report["id"], "assistant", draft, draft=draft)
            job.update(status="done", stage="Done", draft_md=draft, workpaper_md=workpaper,
                       documents=notes, calls=calls, engine=engine.name,
                       finished=_t.time())
        except Exception as exc:  # noqa: BLE001 — the job reports it; nothing half-filed
            job.update(status="failed", error=str(exc), finished=_t.time())

    @app.post("/v1/cam/{lending_id}/draft", status_code=202, tags=["CAM"],
              summary="Generate the CAM in stages (digest → dataset → sections) as a job")
    async def cam_draft(lending_id: str, payload: StagedDraftIn, request: Request) -> Any:
        """Starts the staged drafter and answers at once with a job id; poll
        ``GET /v1/cam/jobs/{job_id}`` for progress and the draft. With the credit
        team's sectioned master prompt the CAM is written section by section from
        per-document fact sheets and a locked financial dataset; any other prompt is
        drafted in one call."""
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        caller, _ = caller_context(request, who)
        if payload.prompt_text and payload.prompt_text.strip():
            prompt_text = payload.prompt_text
        elif payload.prompt_doc_id:
            prompt_text, skip = await _doc_text(request, caller, who, payload.prompt_doc_id)
            if not prompt_text.strip():
                return problem(422, "Validation failed",
                               f"The prompt document could not be read"
                               f"{f' ({skip})' if skip else ''}.")
        else:
            return problem(422, "Validation failed",
                           "Pick a prompt document or type the drafting brief.")
        report = await _open_report(request, caller, who, lending_id)
        if report is None:
            open_path = "/v1/internal/cam-reports"
            opened = await request.app.state.http.post(
                f"{base}{open_path}", json={"lending_id": lending_id, "engine": engine.name},
                headers=_reg_headers(request, caller, who, "POST", open_path))
            if opened.status_code >= 300:
                return problem(opened.status_code if opened.status_code in (403, 404, 409)
                               else 502, "CAM not opened", opened.text[:500])
            report = opened.json()
        path = f"/v1/internal/cam-reports/{report['id']}"
        full = await request.app.state.http.get(
            f"{base}{path}", headers=_reg_headers(request, caller, who, "GET", path))
        turns = (full.json().get("turns") or []) if full.status_code < 300 else []
        # Every document the analyst has put in front of the engine on this CAM.
        ids: list[str] = []
        for doc_id in [*payload.source_doc_ids, *(report.get("source_doc_ids") or []),
                       *(i.strip() for t in turns for m in _SENT.finditer(str(t.get("content")))
                         for i in m.group(1).split(","))]:
            if doc_id and doc_id not in ids and doc_id not in (payload.prompt_doc_id,
                                                               payload.template_doc_id):
                ids.append(doc_id)
        if not ids:
            return problem(422, "Validation failed",
                           "Tick the deal documents the CAM should be drafted from.")
        job_id = _uuid.uuid4().hex
        job = {"job_id": job_id, "lending_id": lending_id, "report_id": report["id"],
               "report_version": report.get("report_version"), "status": "running",
               "stage": "Starting", "done": 0, "total": 0, "documents_requested": len(ids),
               "started": _t.time(), "_who": _owner(request, who)}
        # Keep the finished ones for a while (the UI polls); cap the table.
        for old in sorted(_jobs.values(), key=lambda j: j["started"])[:-100]:
            _jobs.pop(old["job_id"], None)
        _jobs[job_id] = job
        ctx = SimpleNamespace(headers=dict(request.headers), app=request.app)
        task = _aio.create_task(_run_staged(job, ctx, caller, who, report, prompt_text, ids,
                                            dict(payload.doc_titles), turns,
                                            (payload.instruction or "").strip(),
                                            payload.template_doc_id))
        _job_tasks.add(task)
        task.add_done_callback(_job_tasks.discard)
        return _job_view(job)

    @app.get("/v1/cam/jobs/{job_id}", tags=["CAM"],
             summary="Progress and result of a staged CAM draft")
    async def cam_job(job_id: str, request: Request) -> Any:
        if (resp := denied(request.headers.get("X-API-Key"))) is not None:
            return resp
        who, err = await verified_email(request, "")
        if err is not None:
            return err
        job = _jobs.get(job_id)
        if job is None or job.get("_who") != _owner(request, who):
            return problem(404, "Not found", "No such drafting job (it may have expired "
                           "after a restart — generate again).")
        return _job_view(job)

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

        # The uploaded-document lane: the analyst filled the CAM in Word and uploaded
        # it. Filing is WORKBENCH work — the document is attached to the version and the
        # version stays Draft. The committee request is a separate, deliberate step
        # ("Send to credit committee"), which reads this filed document. No open draft
        # is fine (they may have worked entirely outside): a version row is opened to
        # carry the file.
        if payload.document_id:
            if report is None:
                open_path = "/v1/internal/cam-reports"
                opened = await request.app.state.http.post(
                    f"{base}{open_path}",
                    json={"lending_id": lending_id, "engine": "analyst:document"},
                    headers=_reg_headers(request, caller, who, "POST", open_path))
                if opened.status_code >= 300:
                    return problem(502, "Filing failed",
                                   f"The register would not open a CAM version (HTTP "
                                   f"{opened.status_code}): {opened.text[:300]}")
                report = opened.json()
            turn_path = f"/v1/internal/cam-reports/{report['id']}/turns"
            filed = await request.app.state.http.post(
                f"{base}{turn_path}",
                json={"role": "user",
                      "content": f"[uploaded CAM] The analyst filed the completed CAM "
                                 f"document (document {payload.document_id}) — the "
                                 f"committee copy once this goes to committee.",
                      "document_id": payload.document_id},
                headers=_reg_headers(request, caller, who, "POST", turn_path))
            if filed.status_code >= 300:
                return problem(502, "Filing failed", filed.text[:500])
            return {"report_id": report["id"],
                    "report_version": report["report_version"],
                    "document_id": payload.document_id, "status": "Draft"}

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

