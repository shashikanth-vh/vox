"""Synchronous extraction for callers that need a document's CONTENT, not a search index —
the CAM workbench above all: it hands whole documents to an LLM.

``extract()`` runs the same knowledge pipeline as ingestion (OpenDataLoader + PyMuPDF
tables, Sarvam Doc AI for pages with no usable text) and renders the reconstructed
document as Markdown an LLM reads well: real heading levels, tables as Markdown tables,
and ``[page N]`` markers so an answer can cite where a figure came from.

Results are cached on the volume per (tenant, file hash, Sarvam on/off, pipeline
version): a filed document never changes, and the workbench re-sends the same documents
turn after turn — OpenDataLoader and Sarvam run once per file. A result degraded by a
transient Sarvam failure is NOT cached, so the next call retries the OCR.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evam_backend_core.logging import get_logger

from app.config import Settings
from app.knowledge.pipeline import no_sarvam_usage, run_pipeline
from app.knowledge.schema import KnowledgeDocument, TableObject

log = get_logger("docrag.extract")

# Bump when the rendering or pipeline output changes, so cached results are recomputed.
PIPELINE_VERSION = "1"
_TRANSIENT = ("Sarvam Doc AI failed", "Sarvam returned no content")


def _cell(value: str) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")


def table_markdown(table: TableObject) -> str:
    """A Markdown table; a header-less (label/value) table gets a neutral header row so
    every row survives as data instead of the first one turning into column names."""
    rows = [list(r) for r in table.rows]
    if table.columns and not table.is_label_value():
        header = [_cell(c) for c in table.columns]
    else:
        rows = ([list(table.columns)] if table.columns else []) + rows
        width = max((len(r) for r in rows), default=0)
        header = ["Field", "Value"] if width == 2 else [f"Column {i + 1}" for i in range(width)]
    width = max([len(header), *(len(r) for r in rows)]) if rows or header else 0
    header = header + [""] * (width - len(header))
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in rows:
        cells = [_cell(c) for c in row] + [""] * (width - len(row))
        if any(cells):
            out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def document_markdown(doc: KnowledgeDocument) -> str:
    """The reconstructed document, in reading order, as Markdown."""
    parts: list[str] = []
    page = None
    for section, el in doc.all_elements():
        if el.source.page != page:
            page = el.source.page
            parts.append(f"[page {page}]")
        if el.type == "heading":
            level = min(max(section.level, 1), 6)
            parts.append(f"{'#' * level} {el.text.strip()}")
        elif el.type == "table" and el.table is not None:
            parts.append(table_markdown(el.table))
        elif el.type == "caption":
            parts.append(f"*{el.text.strip()}*")
        elif el.text.strip():
            parts.append(el.text.strip())
    return "\n\n".join(parts).strip() + "\n"


@dataclass
class Extraction:
    sha256: str
    filename: str
    doc_type: str
    page_count: int
    extraction_engines: list[str]
    warnings: list[str]
    markdown: str
    ocr_pages: int = 0
    # Sarvam's spend when the file was first extracted (cached with the result).
    sarvam: dict[str, Any] = field(default_factory=no_sarvam_usage)

    def as_dict(self, cached: bool, seconds: float) -> dict[str, Any]:
        return {"sha256": self.sha256, "filename": self.filename, "doc_type": self.doc_type,
                "page_count": self.page_count, "extraction_engines": self.extraction_engines,
                "warnings": self.warnings, "markdown": self.markdown,
                "chars": len(self.markdown), "cached": cached,
                # What THIS call spent: a cache hit made no Sarvam calls.
                "telemetry": {"cached": cached, "seconds": round(seconds, 2),
                              "pages": self.page_count, "ocr_pages": self.ocr_pages,
                              "sarvam": no_sarvam_usage() if cached else self.sarvam}}


class Extractor:
    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.data_dir) / ".extract-cache"
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def extract(self, tenant: str, filename: str, data: bytes,
                use_sarvam: bool) -> dict[str, Any]:
        started = time.monotonic()
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest}-{'s' if use_sarvam else 'n'}-v{PIPELINE_VERSION}"
        path = self.root / tenant / f"{key}.json"
        # One extraction per file at a time: concurrent requests for the same document
        # (two analysts, a retried turn) wait for the first instead of paying twice.
        with self._lock_for(f"{tenant}/{key}"):
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                cached["filename"] = filename
                hit = Extraction(**cached)
                log.info("docrag_extract_cached sha=%s pages=%d ocr_pages=%d",
                         digest[:12], hit.page_count, hit.ocr_pages,
                         extra={"event": "docrag_extract_cached", "tenant": tenant,
                                "sha256": digest[:12], "pages": hit.page_count,
                                "ocr_pages": hit.ocr_pages})
                return hit.as_dict(cached=True, seconds=time.monotonic() - started)

            suffix = Path(filename).suffix.lower()
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / f"source{suffix}"
                src.write_bytes(data)
                result = run_pipeline(src, use_sarvam_for_difficult=use_sarvam,
                                      display_name=filename)
            doc = result.document
            extraction = Extraction(
                sha256=digest, filename=filename, doc_type=doc.metadata.doc_type,
                page_count=doc.metadata.page_count,
                extraction_engines=list[str](doc.metadata.extraction_engines_used),
                warnings=list(doc.warnings), markdown=document_markdown(doc),
                ocr_pages=int(result.telemetry.get("ocr_pages") or 0),
                sarvam=result.telemetry.get("sarvam") or no_sarvam_usage())
            if not any(t in w for w in extraction.warnings for t in _TRANSIENT):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_name(path.name + ".tmp")
                tmp_path.write_text(json.dumps(extraction.__dict__, ensure_ascii=False),
                                    encoding="utf-8")
                os.replace(tmp_path, path)
            seconds = time.monotonic() - started
            sv = extraction.sarvam
            log.info("docrag_extracted sha=%s suffix=%s pages=%d ocr_pages=%d sarvam_jobs=%d "
                     "sarvam_pages=%d engines=%s warnings=%d seconds=%.1f", digest[:12], suffix,
                     extraction.page_count, extraction.ocr_pages, sv.get("jobs", 0),
                     sv.get("pages_submitted", 0), ",".join(extraction.extraction_engines),
                     len(extraction.warnings), seconds,
                     extra={"event": "docrag_extracted",
                            "tenant": tenant, "sha256": digest[:12], "suffix": suffix,
                            "pages": extraction.page_count, "ocr_pages": extraction.ocr_pages,
                            "sarvam": sv, "engines": extraction.extraction_engines,
                            "chars": len(extraction.markdown),
                            "warnings": len(extraction.warnings),
                            "seconds": round(seconds, 2)})
            return extraction.as_dict(cached=False, seconds=seconds)
