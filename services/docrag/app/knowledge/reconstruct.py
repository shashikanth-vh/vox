"""Semantic Document Reconstruction: typed elements -> KnowledgeDocument.

This stage used to guess at structure -- inferring headings from casing and
line length because PyMuPDF only hands back a flat string. That guessing is
what shredded label/value tables into one-word chunks ("Facility",
"Revolving", "Interest"). With OpenDataLoader supplying real element types
and heading levels, this module now *reads* the document's structure rather
than inferring it, and the heuristic is gone.
"""

from __future__ import annotations

import re

from app.knowledge.extractors.odl_extractor import StructuredElement
from app.knowledge.schema import (
    ContentElement,
    DocumentMetadata,
    ImageRegion,
    KnowledgeDocument,
    Section,
    SourceRef,
    TableObject,
)

# Small, precise patterns for this corpus's domain (Indian financial and
# legal documents). A general system would use an NER model; regexes are
# cheap and exact for well-formatted identifiers.
_ENTITY_PATTERNS = {
    "PAN": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    "GSTIN": re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z]\w\b"),
    "amount_inr": re.compile(r"(?:₹|Rs\.?|INR)\s?[\d,]+(?:\.\d+)?"),
    "date": re.compile(r"\b\d{1,2}[-/](?:\d{1,2}|[A-Za-z]{3,9})[-/]\d{2,4}\b"),
}


def _extract_entities(text: str) -> set[str]:
    found = set()
    for label, pattern in _ENTITY_PATTERNS.items():
        for match in pattern.finditer(text):
            found.add(f"{label}:{match.group(0)}")
    return found


def reconstruct_document(
    elements: list[StructuredElement],
    source_filename: str,
    doc_type: str = "unknown",
    page_count: int = 0,
    engine: str = "opendataloader",
    warnings: list[str] | None = None,
    images: list[ImageRegion] | None = None,
    pdf_title: str | None = None,
    author: str | None = None,
    creation_date: str | None = None,
) -> KnowledgeDocument:
    root = Section(title=source_filename, level=0)
    stack: list[Section] = [root]
    entity_text: list[str] = []

    for element in elements:
        source = SourceRef(page=element.page, bbox=element.bbox,
                           extraction_engine=element.engine or engine)  # type: ignore[arg-type]

        if element.type == "heading":
            level = element.level or 1
            # Pop to the nearest ancestor shallower than this heading so an
            # H3 nests under its H2 instead of flattening onto the root.
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            section = Section(title=element.text, level=level)
            # Keep the heading as a retrievable element too, not just a
            # label -- otherwise a line like "Form GST REG-06" exists only
            # as a section title and never appears in any chunk's text.
            section.elements.append(
                ContentElement(type="heading", text=element.text, source=source)
            )
            stack[-1].subsections.append(section)
            stack.append(section)
            entity_text.append(element.text)
            continue

        if element.type == "table":
            table = TableObject(
                columns=element.table_columns or [],
                rows=element.table_rows or [],
            )
            prose = table.to_prose()
            stack[-1].elements.append(
                ContentElement(type="table", text=prose, source=source, table=table)
            )
            entity_text.append(prose)
            continue

        # paragraph / list / caption
        element_type = element.type if element.type in ("paragraph", "list", "caption") else "paragraph"
        stack[-1].elements.append(
            ContentElement(type=element_type, text=element.text, source=source)  # type: ignore[arg-type]
        )
        entity_text.append(element.text)

    element_counts: dict[str, int] = {}
    for element in elements:
        element_counts[element.type] = element_counts.get(element.type, 0) + 1

    metadata = DocumentMetadata(
        title=source_filename,
        doc_type=doc_type,
        source_filename=source_filename,
        page_count=page_count or len({e.page for e in elements}),
        extraction_engines_used=sorted({e.engine or engine for e in elements}) or [engine],  # type: ignore[misc,list-item]
        pdf_title=pdf_title,
        author=author,
        creation_date=creation_date,
        images=list(images or []),
        element_counts=element_counts,
    )

    # Content before the first heading lives on root itself; returning only
    # root's subsections would silently drop it.
    sections = list(root.subsections)
    if root.elements:
        sections.insert(0, Section(title=root.title, level=1, elements=root.elements))
    if not sections:
        sections = [root]

    return KnowledgeDocument(
        metadata=metadata,
        sections=sections,
        entities=sorted(_extract_entities("\n".join(entity_text))),
        warnings=list(warnings or []),
    )
