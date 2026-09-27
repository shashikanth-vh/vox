"""Produce one typed element list per PDF, combining both local engines.

Neither extractor wins outright on this corpus, which is why this layer
exists rather than a straight swap:

- OpenDataLoader gives real element types, heading levels and per-element
  bounding boxes -- structure that PyMuPDF makes you guess at. But it only
  reports a `table` when the PDF has genuine table markup, so a borderless
  label/value grid (a typical term sheet) comes back as ~55 loose paragraphs.
- PyMuPDF's `find_tables()` detects those borderless grids by geometry,
  recovering the label/value pairs ODL flattened, but gives no heading
  information at all.

So: take ODL's typed elements as the backbone, and where PyMuPDF found a
table that ODL did not, substitute the table for the loose paragraphs
sitting inside its bounds. Each engine covers the other's blind spot.
"""

from __future__ import annotations

import pymupdf as fitz

from app.config import get_settings
from app.knowledge import difficulty
from app.knowledge.extractors.odl_extractor import (
    OdlError,
    OdlResult,
    StructuredElement,
    extract_with_odl,
)
from app.knowledge.extractors.pdf_extractor import _extract_page_local


def _pymupdf_elements(path: str) -> tuple[list[StructuredElement], int, dict[int, str]]:
    """Fallback path: PyMuPDF text blocks + geometric tables, untyped."""
    doc = fitz.open(path)
    elements: list[StructuredElement] = []
    page_text: dict[int, str] = {}

    for i, page in enumerate(doc):
        page_number = i + 1
        text, tables = _extract_page_local(page)
        page_text[page_number] = text

        for table in tables:
            if not table.rows:
                continue
            elements.append(
                StructuredElement(
                    type="table",
                    text="",
                    page=page_number,
                    bbox=table.bbox,
                    table_columns=table.rows[0],
                    table_rows=table.rows[1:],
                    engine="local_text_layer",
                )
            )
        for block in text.split("\n\n"):
            block = block.strip()
            if block:
                elements.append(
                    StructuredElement(type="paragraph", text=block, page=page_number,
                                      engine="local_text_layer")
                )

    page_count = doc.page_count
    doc.close()
    return elements, page_count, page_text


def _pymupdf_tables_by_page(path: str) -> dict[int, list[StructuredElement]]:
    doc = fitz.open(path)
    found: dict[int, list[StructuredElement]] = {}
    for i, page in enumerate(doc):
        page_number = i + 1
        _, tables = _extract_page_local(page)
        for table in tables:
            if not table.rows or not table.bbox:
                continue
            found.setdefault(page_number, []).append(
                StructuredElement(
                    type="table",
                    text="",
                    page=page_number,
                    bbox=table.bbox,
                    table_columns=table.rows[0],
                    table_rows=table.rows[1:],
                    engine="local_text_layer",
                )
            )
    doc.close()
    return found


def _inside(bbox: list[float] | None, container: list[float]) -> bool:
    if not bbox:
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return container[0] <= cx <= container[2] and container[1] <= cy <= container[3]


def _merge_tables(
    elements: list[StructuredElement], tables_by_page: dict[int, list[StructuredElement]]
) -> tuple[list[StructuredElement], int]:
    """Replace loose ODL paragraphs with a PyMuPDF table covering them.

    Only applies where ODL found no table on that page -- if both engines
    saw a table, ODL's typed version wins, since it carries real cell
    structure rather than geometry-inferred columns.
    """
    pages_with_odl_tables = {e.page for e in elements if e.type == "table"}
    merged: list[StructuredElement] = []
    inserted_pages: set[tuple[int, tuple[float, ...]]] = set()
    substitutions = 0

    for element in elements:
        candidates = [
            t
            for t in tables_by_page.get(element.page, [])
            if element.page not in pages_with_odl_tables
        ]
        covering = next((t for t in candidates if t.bbox and _inside(element.bbox, t.bbox)), None)

        if covering is not None:
            # Drop this paragraph; emit the table once, at the position of
            # the first element it swallows, preserving reading order.
            key = (covering.page, tuple(covering.bbox or ()))
            if key not in inserted_pages:
                merged.append(covering)
                inserted_pages.add(key)
                substitutions += 1
            continue

        merged.append(element)

    return merged, substitutions


def extract_structured(path: str) -> tuple[OdlResult, list[str]]:
    """Return (result, warnings) for one PDF, from whichever engine ran."""
    warnings: list[str] = []

    if not get_settings().use_odl:
        elements, page_count, _ = _pymupdf_elements(path)
        return (
            OdlResult(elements=elements, page_count=page_count),
            ["OpenDataLoader disabled (DOCRAG_USE_ODL=false); used PyMuPDF only"],
        )

    try:
        odl = extract_with_odl(path)
    except OdlError as exc:
        elements, page_count, _ = _pymupdf_elements(path)
        warnings.append(f"OpenDataLoader unavailable ({exc}); fell back to PyMuPDF extraction")
        return OdlResult(elements=elements, page_count=page_count), warnings

    try:
        odl.elements, substitutions = _merge_tables(odl.elements, _pymupdf_tables_by_page(path))
        if substitutions:
            warnings.append(
                f"recovered {substitutions} borderless table(s) via PyMuPDF that "
                "OpenDataLoader reported as loose paragraphs"
            )
    except Exception as exc:
        warnings.append(f"table-merge pass skipped ({exc}); used OpenDataLoader elements as-is")

    return odl, warnings


def score_pages(
    elements: list[StructuredElement], page_count: int
) -> dict[int, difficulty.PageDifficulty]:
    """Difficulty per page, computed over everything extracted for it."""
    per_page: dict[int, list[str]] = {}
    for element in elements:
        text = element.text
        if element.type == "table":
            rows = ([element.table_columns] if element.table_columns else []) + (
                element.table_rows or []
            )
            text = " ".join(" ".join(r) for r in rows)
        per_page.setdefault(element.page, []).append(text)

    return {
        page: difficulty.score_page(" ".join(per_page.get(page, [])), page)
        for page in range(1, max(page_count, 1) + 1)
    }
