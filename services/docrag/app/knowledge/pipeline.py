"""Orchestrates the full Document -> Knowledge pipeline:

    file -> extract (OpenDataLoader + PyMuPDF, Sarvam Doc AI for scans)
         -> reconstruct -> classify -> chunk -> KnowledgeDocument + chunks

This is the one entry point the ingestion worker calls -- it doesn't know or
care whether the source was a PDF or xlsx.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.knowledge.chunker import KnowledgeChunk, build_chunks
from app.knowledge.doc_type import classify_doc_type
from app.knowledge.extractors.odl_extractor import StructuredElement
from app.knowledge.extractors.structured import extract_structured, score_pages
from app.knowledge.extractors.xlsx_extractor import extract_xls, extract_xlsx
from app.knowledge.reconstruct import reconstruct_document
from app.knowledge.sarvam_client import SarvamError, digitise_document
from app.knowledge.schema import ImageRegion, KnowledgeDocument

# Images are read by OCR: wrapped into a PDF (one page per frame) and sent to Sarvam.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def no_sarvam_usage() -> dict[str, Any]:
    return {"jobs": 0, "failed_jobs": 0, "pages_submitted": 0, "pages_succeeded": 0,
            "pages_failed": 0, "seconds": 0.0, "job_ids": []}


@dataclass
class PipelineResult:
    document: KnowledgeDocument
    chunks: list[KnowledgeChunk]
    elements: list[StructuredElement]
    # What the extraction spent: Sarvam jobs/pages and how many pages came from OCR.
    telemetry: dict[str, Any] = field(default_factory=lambda: {
        "ocr_pages": 0, "sarvam": no_sarvam_usage()})


def _xlsx_elements(path: Path) -> tuple[list[StructuredElement], int]:
    elements: list[StructuredElement] = []
    pages = (extract_xls if path.suffix.lower() == ".xls" else extract_xlsx)(str(path))
    for page in pages:
        sheet_name = page.markdown.lstrip("# ").strip()
        if sheet_name:
            elements.append(
                StructuredElement(type="heading", text=sheet_name, page=page.page_number, level=1)
            )
        for table in page.tables:
            if not table.rows:
                continue
            elements.append(
                StructuredElement(
                    type="table",
                    text="",
                    page=page.page_number,
                    table_columns=table.rows[0],
                    table_rows=table.rows[1:],
                )
            )
    return elements, len(pages)


def _apply_sarvam_to_difficult_pages(
    path: Path,
    elements: list[StructuredElement],
    page_count: int,
    use_sarvam: bool,
    stats: dict[str, Any],
) -> tuple[list[StructuredElement], list[str]]:
    """Send the document to Sarvam Doc AI when pages came back near-empty.

    A page with no extractable text is a scan; Doc AI digitises the whole
    file in one job and its per-page Markdown replaces those pages only.
    Any failure degrades to the local result with a warning attached --
    never a silent empty page.
    """
    warnings: list[str] = []
    scores = score_pages(elements, page_count)
    difficult = sorted(p for p, d in scores.items() if d.is_difficult)
    if not difficult:
        return elements, warnings

    reasons = {p: scores[p].reason for p in difficult}
    if not use_sarvam:
        for page in difficult:
            warnings.append(f"page {page}: {reasons[page]}; Sarvam routing disabled")
        return elements, warnings
    if not get_settings().sarvam_configured():
        for page in difficult:
            warnings.append(f"page {page}: {reasons[page]}; DOCRAG_SARVAM_API_KEY is not set")
        return elements, warnings

    stats["jobs"] += 1
    try:
        result = digitise_document(str(path))
    except SarvamError as exc:
        stats["failed_jobs"] += 1
        stats["pages_submitted"] += page_count
        for page in difficult:
            warnings.append(f"page {page}: {reasons[page]}; Sarvam Doc AI failed ({exc})")
        return elements, warnings

    stats["job_ids"].append(result.job_id)
    stats["seconds"] = round(stats["seconds"] + result.seconds, 1)
    stats["pages_submitted"] += result.usage.get("pages_total") or page_count
    stats["pages_succeeded"] += result.usage.get("pages_succeeded", 0)
    stats["pages_failed"] += result.usage.get("pages_failed", 0)
    recovered = {p.page_number: p.markdown for p in result.pages if p.markdown}
    kept = [e for e in elements if e.page not in difficult or e.page not in recovered]
    for page in difficult:
        if page in recovered:
            kept.append(
                StructuredElement(type="paragraph", text=recovered[page], page=page,
                                  engine="sarvam_docai")
            )
        else:
            warnings.append(f"page {page}: {reasons[page]}; Sarvam returned no content")

    stats["ocr_pages"] = stats.get("ocr_pages", 0) + sum(1 for p in difficult if p in recovered)
    kept.sort(key=lambda e: e.page)
    return kept, warnings


def _image_as_pdf(path: Path, out_dir: str) -> tuple[Path, int]:
    """An image (every frame of a multi-page TIFF) as a PDF Sarvam can digitise."""
    import fitz

    with fitz.open(path) as img:
        pdf_bytes = img.convert_to_pdf()
    out = Path(out_dir) / "image.pdf"
    out.write_bytes(pdf_bytes)
    with fitz.open(out) as pdf:
        return out, pdf.page_count


def _check_coverage(elements: list[StructuredElement], chunks: list[KnowledgeChunk]) -> list[str]:
    """Report source words that never made it into any chunk.

    The goal is to extract everything in the document, so anything the
    reconstruction stage silently discards is a bug -- this makes that
    visible instead of letting it pass unnoticed.
    """
    source: set[str] = set()
    for element in elements:
        source |= set(re.findall(r"\w+", element.text.lower()))
        rows = ([element.table_columns] if element.table_columns else []) + (element.table_rows or [])
        for row in rows:
            for cell in row:
                source |= set(re.findall(r"\w+", str(cell).lower()))

    chunked: set[str] = set()
    for chunk in chunks:
        chunked |= set(re.findall(r"\w+", chunk.text.lower()))

    missing = source - chunked
    if not missing:
        return []
    sample = ", ".join(sorted(missing)[:12])
    return [
        f"{len(missing)} of {len(source)} source words did not reach any chunk (e.g. {sample})"
    ]


def run_pipeline(
    path: str | Path, use_sarvam_for_difficult: bool = True, display_name: str | None = None
) -> PipelineResult:
    path = Path(path)
    suffix = path.suffix.lower()
    warnings: list[str] = []

    images: list[ImageRegion] = []
    pdf_title = author = creation_date = None
    stats = no_sarvam_usage()

    if suffix in (".xlsx", ".xls"):
        elements, page_count = _xlsx_elements(path)
        engine = "local_text_layer"
    elif suffix == ".pdf":
        odl, warnings = extract_structured(str(path))
        elements, page_count = odl.elements, odl.page_count
        pdf_title, author, creation_date = odl.doc_title, odl.author, odl.creation_date
        images = [
            ImageRegion(page=i.page, bbox=i.bbox, source=i.source, has_alt_text=i.has_alt_text)
            for i in odl.images
        ]
        elements, sarvam_warnings = _apply_sarvam_to_difficult_pages(
            path, elements, page_count, use_sarvam_for_difficult, stats
        )
        warnings.extend(sarvam_warnings)
        # Elements from a fallback engine carry their own tag; untagged ones are ODL's.
        engine = "opendataloader"
    elif suffix in IMAGE_SUFFIXES:
        # No text layer to read locally: every page is an OCR page.
        with tempfile.TemporaryDirectory() as tmp:
            pdf, page_count = _image_as_pdf(path, tmp)
            elements, sarvam_warnings = _apply_sarvam_to_difficult_pages(
                pdf, [], page_count, use_sarvam_for_difficult, stats
            )
        warnings.extend(sarvam_warnings)
        engine = "image"
    else:
        supported = ", ".join(sorted({".pdf", ".xlsx", ".xls", *IMAGE_SUFFIXES}))
        raise ValueError(f"Unsupported file type: {suffix} (supported: {supported})")

    if images:
        pages = sorted({i.page for i in images})
        warnings.append(
            f"{len(images)} embedded image(s) on page(s) {pages} were not "
            "text-extracted; any text inside them is missing unless the page "
            "also went through Sarvam Doc AI"
        )

    sample = "\n".join(e.text for e in elements[:40])
    doc_type = classify_doc_type(sample)

    document = reconstruct_document(
        elements,
        source_filename=display_name or path.name,
        doc_type=doc_type,
        page_count=page_count,
        engine=engine,
        warnings=warnings,
        images=images,
        pdf_title=pdf_title,
        author=author,
        creation_date=creation_date,
    )
    chunks = build_chunks(document)
    document.warnings.extend(_check_coverage(elements, chunks))

    ocr_pages = stats.pop("ocr_pages", 0)
    return PipelineResult(document=document, chunks=chunks, elements=elements,
                          telemetry={"ocr_pages": ocr_pages, "sarvam": stats})


def knowledge_document_to_dict(doc: KnowledgeDocument) -> dict:
    return asdict(doc)
