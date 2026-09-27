"""Local PDF page extraction via PyMuPDF: geometric tables first, then the prose
that lies outside them. The shared building block for the PyMuPDF fallback path and
the borderless-table recovery pass in ``structured.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz  # PyMuPDF; `pymupdf` is the non-deprecated import name


@dataclass
class ExtractedTable:
    rows: list[list[str]]
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in PDF points


@dataclass
class PageResult:
    page_number: int
    markdown: str
    tables: list[ExtractedTable]
    engine: str
    warning: str | None = None


def _extract_page_local(page: fitz.Page) -> tuple[str, list[ExtractedTable]]:
    """Extract a page's tables, then its remaining prose.

    Order matters. A label/value table ("Lender | Evam Finance...") read as
    flat text is a run of short title-case lines, which any heading
    heuristic shreds into one-word fragments. The table finder already
    recovers the real structure, so text inside a detected table's bounds
    is excluded from the prose pass rather than parsed twice -- the table
    keeps its rows, and prose keeps its paragraphs.
    """
    tables: list[ExtractedTable] = []
    table_rects: list[fitz.Rect] = []
    try:
        finder = page.find_tables()
        for tbl in finder.tables:
            rows = tbl.extract()
            rows = [["" if c is None else str(c).strip() for c in row] for row in rows]
            bbox = [float(v) for v in tbl.bbox] if getattr(tbl, "bbox", None) else None
            tables.append(ExtractedTable(rows=rows, bbox=bbox))
            if bbox:
                table_rects.append(fitz.Rect(bbox))
    except Exception:  # noqa: S110 - table-finding is best-effort; the text pass still runs
        pass

    if not table_rects:
        return page.get_text("text"), tables

    kept: list[str] = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, block_text = block[0], block[1], block[2], block[3], block[4]
        # Drop a block whose centre falls inside a table -- the table has it.
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        if any(r.contains(centre) for r in table_rects):
            continue
        if block_text.strip():
            kept.append(block_text.strip())

    return "\n\n".join(kept), tables
