"""Answer synthesis with citations.

Two modes:
- extractive (default, no API key needed): show the retrieved chunks
  themselves, each with its citation, and let the reader do the final
  synthesis. Honest when no generation LLM key is configured.
- generative (only if DOCRAG_SARVAM_API_KEY is set): ask Sarvam's chat
  model to answer using ONLY the retrieved chunks as context, and to cite
  [doc, page] for each claim. Never invoked silently -- the caller decides.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from app.config import get_settings
from app.rag.retrieval import RetrievedChunk


class GenerativeUnavailableError(RuntimeError):
    """Generative mode was requested but no Sarvam key is configured."""


class GenerativeFailedError(RuntimeError):
    """The Sarvam chat call failed (network, HTTP error, or unexpected shape)."""


@dataclass
class Citation:
    doc_id: str
    doc: str
    section_path: str
    pages: list[int]
    engines: list[str]


@dataclass
class Answer:
    text: str
    citations: list[Citation]
    mode: str


def _format_context(retrieved: list[RetrievedChunk]) -> str:
    blocks = []
    for i, r in enumerate(retrieved):
        c = r.chunk
        blocks.append(
            f"[{i+1}] Document: {c['doc']} | Section: {c['section_path']} | Pages: {c['pages']}\n{c['text']}"
        )
    return "\n\n".join(blocks)


def build_citations(retrieved: list[RetrievedChunk]) -> list[Citation]:
    return [
        Citation(
            doc_id=r.chunk["doc_id"],
            doc=r.chunk["doc"],
            section_path=r.chunk["section_path"],
            pages=r.chunk["pages"],
            engines=r.chunk.get("extraction_engines", []),
        )
        for r in retrieved
    ]


def extractive_answer(query: str, retrieved: list[RetrievedChunk]) -> Answer:
    if not retrieved:
        return Answer(text="No relevant chunks found for this query.", citations=[], mode="extractive")

    lines = [f"Top matching passages for: \"{query}\"\n"]
    for i, r in enumerate(retrieved):
        c = r.chunk
        lines.append(f"**[{i+1}] {c['doc']} — {c['section_path']}** "
                     f"(p. {c['pages']}, score {r.fused_score:.3f})")
        lines.append(c["text"][:800] + ("…" if len(c["text"]) > 800 else ""))
        lines.append("")

    return Answer(text="\n".join(lines), citations=build_citations(retrieved), mode="extractive")


def generative_answer(query: str, retrieved: list[RetrievedChunk]) -> Answer:
    settings = get_settings()
    if not settings.sarvam_configured():
        raise GenerativeUnavailableError("DOCRAG_SARVAM_API_KEY is not set — generative mode is unavailable; "
                                    "use mode=extractive.")
    if not retrieved:
        return Answer(text="No relevant chunks found for this query.", citations=[], mode="generative")

    context = _format_context(retrieved)
    prompt = (
        "Answer the question using ONLY the context passages below. "
        "Cite the passage number(s) you used in square brackets, e.g. [1]. "
        "If the answer isn't in the context, say so plainly.\n\n"
        f"Context:\n{context}\n\nQuestion: {query}\nAnswer:"
    )

    url = settings.sarvam_base_url.rstrip("/") + settings.sarvam_chat_path
    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Authorization": f"Bearer {settings.sarvam_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.sarvam_chat_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=settings.sarvam_timeout_seconds)
    except requests.RequestException as exc:
        raise GenerativeFailedError(f"Sarvam chat request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GenerativeFailedError(f"Sarvam chat returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GenerativeFailedError(f"Sarvam chat returned an unexpected body: {resp.text[:300]}") from exc

    return Answer(text=text, citations=build_citations(retrieved), mode="generative")
