"""Client for Sarvam's Doc AI digitise API -- the OCR path for pages whose
local extraction looks weak.

Sarvam has a purpose-built async document API for this. The earlier approach
here (page image -> /v1/chat/completions) was wrong twice over: v1 rejects
array-shaped `content` outright ("Input should be a valid string"), and image
input exists only on /v2/chat/completions with model `gemma4`, which is
beta-gated per account. Doc AI is neither, handles multi-page PDFs in one
job, and returns per-page Markdown.

Lifecycle: POST the file -> poll /status until terminal -> GET /results.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from evam_backend_core.logging import get_logger

from app.config import get_settings

log = get_logger("docrag.sarvam")

TERMINAL_STATUSES = {"completed", "partially_completed", "failed", "rejected"}


class SarvamError(RuntimeError):
    pass


@dataclass
class SarvamPage:
    page_number: int
    markdown: str


@dataclass
class SarvamDigitiseResult:
    pages: list[SarvamPage]
    status: str
    job_id: str
    # Sarvam's own count for the job: pages_total / processed / succeeded / failed.
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


def _log_job(file_path: str, job_id: str, status: str, usage: dict[str, int],
             seconds: float, error: str = "") -> None:
    """One line per Doc AI job — the unit Sarvam bills — whatever its outcome."""
    log.info(
        "sarvam_digitise job=%s status=%s pages_total=%s pages_succeeded=%s "
        "pages_failed=%s seconds=%.1f%s", job_id or "-", status,
        usage.get("pages_total", "?"), usage.get("pages_succeeded", "?"),
        usage.get("pages_failed", "?"), seconds, f" error={error[:200]}" if error else "",
        extra={"event": "sarvam_digitise", "job_id": job_id, "status": status,
               "usage": usage, "seconds": round(seconds, 2), "error": error[:500],
               "file_suffix": Path(file_path).suffix.lower()})


def _headers() -> dict[str, str]:
    return {"api-subscription-key": get_settings().sarvam_api_key}


def _explain_http_error(resp: requests.Response) -> str:
    """Turn Sarvam's error codes into something actionable rather than a
    bare status line -- these three are the ones actually worth
    distinguishing when a run fails."""
    body = resp.text[:500]
    if resp.status_code == 402:
        return f"Sarvam account has no credit balance (HTTP 402). Request shape was valid. Body: {body}"
    if resp.status_code == 403:
        return f"Sarvam rejected the API key (HTTP 403 — note Sarvam uses 403, not 401). Body: {body}"
    return f"Sarvam Doc AI returned HTTP {resp.status_code}: {body}"


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise SarvamError(f"Sarvam Doc AI returned non-JSON (HTTP {resp.status_code}): "
                          f"{resp.text[:300]}") from exc
    if not isinstance(data, dict):
        raise SarvamError(f"Sarvam Doc AI returned unexpected JSON: {str(data)[:300]}")
    return data


def digitise_document(
    file_path: str,
    output_format: str = "md",
    poll_interval: float = 4.0,
    max_wait_seconds: float | None = None,
) -> SarvamDigitiseResult:
    """Send a whole document through Doc AI digitise, return per-page Markdown.

    Raises SarvamError on missing key, network failure, non-2xx response, or
    timeout -- callers are expected to catch it and fall back to local
    extraction with a warning, never to let a failure produce silent empty
    output.
    """
    settings = get_settings()
    if not settings.sarvam_configured():
        raise SarvamError(
            "DOCRAG_SARVAM_API_KEY is not set. Set a real key before routing pages to Sarvam."
        )

    base = settings.sarvam_base_url.rstrip("/")
    max_wait = max_wait_seconds or settings.sarvam_docai_max_wait_seconds
    started = time.monotonic()
    try:
        result = _digitise(file_path, output_format, poll_interval, max_wait, base)
    except SarvamError as exc:
        _log_job(file_path, getattr(exc, "job_id", ""), "error", getattr(exc, "usage", {}),
                 time.monotonic() - started, str(exc))
        raise
    result.seconds = time.monotonic() - started
    _log_job(file_path, result.job_id, result.status, result.usage, result.seconds)
    return result


def _digitise(file_path: str, output_format: str, poll_interval: float,
              max_wait: float, base: str) -> SarvamDigitiseResult:
    settings = get_settings()

    try:
        with open(file_path, "rb") as fh:
            resp = requests.post(
                f"{base}{settings.sarvam_docai_digitise_path}",
                headers=_headers(),
                files={"file": fh},
                data={"output_format": output_format},
                timeout=settings.sarvam_timeout_seconds,
            )
    except requests.RequestException as exc:
        raise SarvamError(f"Sarvam Doc AI submit failed: {exc}") from exc

    if resp.status_code >= 400:
        raise SarvamError(_explain_http_error(resp))

    job_id = _json(resp).get("job_id")
    if not job_id:
        raise SarvamError(f"Sarvam Doc AI returned no job_id: {resp.text[:300]}")

    status, usage = _poll_until_terminal(base, job_id, poll_interval, max_wait)
    if status in ("failed", "rejected"):
        err = SarvamError(f"Sarvam Doc AI job {job_id} ended as '{status}'")
        err.job_id, err.usage = job_id, usage  # type: ignore[attr-defined]
        raise err

    pages, result_usage = _fetch_results(base, job_id)
    return SarvamDigitiseResult(pages=pages, status=status, job_id=job_id,
                                usage=_usage(result_usage or usage))


def _usage(raw: dict | None) -> dict[str, int]:
    keys = ("pages_total", "pages_processed", "pages_succeeded", "pages_failed")
    return {k: int((raw or {}).get(k) or 0) for k in keys}


def _poll_until_terminal(base: str, job_id: str, poll_interval: float,
                         max_wait_seconds: float) -> tuple[str, dict]:
    deadline = time.monotonic() + max_wait_seconds
    while True:
        try:
            resp = requests.get(
                f"{base}{get_settings().sarvam_docai_job_path}/{job_id}/status",
                headers=_headers(),
                timeout=60,
            )
        except requests.RequestException as exc:
            raise SarvamError(f"Sarvam Doc AI status poll failed: {exc}") from exc

        if resp.status_code >= 400:
            raise SarvamError(_explain_http_error(resp))

        body = _json(resp)
        status = body.get("status", "")
        if status in TERMINAL_STATUSES:
            return status, body.get("usage") or {}

        if time.monotonic() >= deadline:
            raise SarvamError(
                f"Sarvam Doc AI job {job_id} did not reach a terminal status "
                f"within {max_wait_seconds:.0f}s (last status: '{status}')"
            )
        time.sleep(poll_interval)


def _fetch_results(base: str, job_id: str) -> tuple[list[SarvamPage], dict]:
    try:
        resp = requests.get(
            f"{base}{get_settings().sarvam_docai_job_path}/{job_id}/results",
            headers=_headers(),
            timeout=120,
        )
    except requests.RequestException as exc:
        raise SarvamError(f"Sarvam Doc AI results fetch failed: {exc}") from exc

    if resp.status_code >= 400:
        raise SarvamError(_explain_http_error(resp))

    data = _json(resp)
    pages: list[SarvamPage] = []
    for document in data.get("documents", []):
        for page in document.get("pages", []):
            number = page.get("page_number", page.get("page_num", len(pages) + 1))
            content = page.get("content")
            if not content and page.get("blocks"):
                content = _blocks_markdown(page["blocks"])
            pages.append(SarvamPage(page_number=int(number), markdown=(content or "").strip()))
    pages.sort(key=lambda p: p.page_number)
    return pages, data.get("usage") or {}


# Layout tags Sarvam marks as headings; everything else is body text.
_HEADING_TAGS = {"headline", "title", "section_header", "section-header", "heading"}


def _blocks_markdown(blocks: list[dict]) -> str:
    """Current Doc AI results carry a page as layout blocks (text, layout_tag,
    reading_order) instead of a single Markdown string. Rebuild the Markdown in
    reading order, keeping headings as headings so section structure survives."""
    ordered = sorted(blocks, key=lambda b: b.get("reading_order") or 0)
    parts: list[str] = []
    for block in ordered:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        tag = str(block.get("layout_tag") or "").lower()
        parts.append(f"## {text}" if tag in _HEADING_TAGS else text)
    return "\n\n".join(parts)
