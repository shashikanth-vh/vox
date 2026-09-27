"""OpenDataLoader-based PDF extraction -- the primary structural path.

OpenDataLoader returns a typed document model (heading / paragraph / table /
list / caption / image, each with a page number and bounding box) rather
than a flat string. That is the difference that matters: with real element
types, reconstruction *reads* the document's structure instead of guessing
it from casing and line length, which is what shredded label/value tables
into one-word fragments under the PyMuPDF-only path.

Shells out to the `opendataloader-pdf` CLI (Java), parses its JSON, and
flattens it into the same StructuredElement list every other extractor
produces, so nothing downstream knows which engine ran.

Always the LOCAL Java pipeline: `--hybrid off` is passed explicitly rather than relied on
as a default. Hybrid mode would send pages to a separate docling server (the `[hybrid]`
extra, which the image does not install); scanned pages go to Sarvam Doc AI instead.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings


class OdlError(RuntimeError):
    pass


@dataclass
class StructuredElement:
    """One typed element of a document, with its provenance."""

    type: str  # heading | paragraph | table | list | caption
    text: str
    page: int
    bbox: list[float] | None = None
    level: int | None = None  # heading depth, when known
    table_rows: list[list[str]] | None = None
    table_columns: list[str] | None = None
    # Which engine produced this element; None = the document's primary engine.
    engine: str | None = None


@dataclass
class ImageRef:
    """An image OpenDataLoader found but did not text-extract.

    Recorded rather than dropped: a stamp, seal or signature block may
    carry text that never reaches any chunk, and a citation should be able
    to say "there is a figure here" instead of silently omitting it.
    """

    page: int
    bbox: list[float] | None = None
    source: str | None = None      # relative path to the extracted image file
    has_alt_text: bool = False


@dataclass
class OdlResult:
    elements: list[StructuredElement] = field(default_factory=list)
    page_count: int = 0
    images: list[ImageRef] = field(default_factory=list)
    doc_title: str | None = None
    author: str | None = None
    creation_date: str | None = None

    @property
    def image_count(self) -> int:
        return len(self.images)


# OpenDataLoader reports heading depth as a style name, not a number.
_LEVEL_NAMES = {
    "title": 1,
    "subtitle": 2,
    "heading 1": 1,
    "heading 2": 2,
    "heading 3": 3,
    "heading 4": 4,
    "heading 5": 5,
    "heading 6": 6,
}


def _level_to_int(level) -> int:
    if isinstance(level, int):
        return max(1, min(level, 6))
    if isinstance(level, str):
        key = level.strip().lower()
        if key in _LEVEL_NAMES:
            return _LEVEL_NAMES[key]
        digits = "".join(c for c in key if c.isdigit())
        if digits:
            return max(1, min(int(digits), 6))
    return 1


def _node_text(node: dict) -> str:
    """Concatenate a node's own content plus all descendants', in order."""
    parts: list[str] = []
    if node.get("content"):
        parts.append(str(node["content"]))
    for kid in node.get("kids", []):
        child = _node_text(kid)
        if child:
            parts.append(child)
    return " ".join(parts).strip()


def _bbox(node: dict) -> list[float] | None:
    raw = node.get("bounding box")
    if isinstance(raw, list) and len(raw) >= 4:
        return [float(v) for v in raw[:4]]
    return None


def _table_to_grid(table: dict) -> list[list[str]]:
    """Flatten ODL's row/cell tree into a rectangular grid of strings.

    Cells carry explicit `column number`, and a row may skip columns
    entirely (GST certificates do), so cells are placed by their
    stated column rather than by their order in the list.
    """
    grid: list[list[str]] = []
    for row in table.get("rows", []):
        cells = row.get("cells", [])
        if not cells:
            continue
        placed: dict[int, str] = {}
        for cell in cells:
            col = cell.get("column number")
            col = int(col) if isinstance(col, int | float) else len(placed) + 1
            placed[col] = _node_text(cell)
        width = max(placed) if placed else 0
        grid.append([placed.get(i + 1, "") for i in range(width)])

    if not grid:
        return []
    width = max(len(r) for r in grid)
    return [r + [""] * (width - len(r)) for r in grid]


def _walk(node: dict, out: list[StructuredElement], counters: dict) -> None:
    node_type = (node.get("type") or "").lower()

    if node_type == "image":
        counters.setdefault("images", []).append(
            ImageRef(
                page=int(node.get("page number") or 1),
                bbox=_bbox(node),
                source=node.get("source"),
                has_alt_text=str(node.get("alt_source", "missing")).lower() != "missing",
            )
        )
        return

    if node_type == "table":
        grid = _table_to_grid(node)
        if grid:
            out.append(
                StructuredElement(
                    type="table",
                    text="",  # rendered later by TableObject.to_prose()
                    page=int(node.get("page number") or 1),
                    bbox=_bbox(node),
                    table_columns=grid[0],
                    table_rows=grid[1:],
                )
            )
        return  # do not descend: cell text belongs to the table, not loose prose

    if node_type in ("heading", "paragraph", "list", "caption"):
        text = _node_text(node)
        if text:
            out.append(
                StructuredElement(
                    type=node_type,
                    text=text,
                    page=int(node.get("page number") or 1),
                    bbox=_bbox(node),
                    level=_level_to_int(node.get("level")) if node_type == "heading" else None,
                )
            )
        return  # content already collected from descendants

    # container node (document root, "text block", etc.): recurse in order
    for kid in node.get("kids", []):
        _walk(kid, out, counters)


def parse_odl_json(data: dict) -> OdlResult:
    elements: list[StructuredElement] = []
    counters: dict = {}
    _walk(data, elements, counters)
    return OdlResult(
        elements=elements,
        page_count=int(data.get("number of pages") or 0),
        images=counters.get("images", []),
        doc_title=data.get("title") or None,
        author=data.get("author") or None,
        creation_date=data.get("creation date") or None,
    )


def extract_with_odl(pdf_path: str, timeout: float | None = None) -> OdlResult:
    """Run the opendataloader-pdf CLI over one PDF and parse its JSON.

    Raises OdlError if the binary is missing, fails, or writes no JSON --
    callers fall back to the PyMuPDF path rather than losing the document.
    """
    settings = get_settings()
    binary = settings.odl_binary()
    if not binary:
        raise OdlError(f"opendataloader-pdf not found ({settings.odl_bin or 'not on PATH'})")

    with tempfile.TemporaryDirectory() as tmp:
        cmd = [binary, pdf_path, "-o", tmp, "--format", "json", "--hybrid", "off"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout or settings.odl_timeout_seconds,
                env=settings.java_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise OdlError(f"opendataloader-pdf timed out on {pdf_path}") from exc
        except OSError as exc:
            raise OdlError(f"could not run opendataloader-pdf: {exc}") from exc

        produced = sorted(Path(tmp).glob("*.json"))
        if not produced:
            stderr = proc.stderr.decode("utf-8", errors="replace")[-400:]
            raise OdlError(
                f"opendataloader-pdf produced no JSON (exit {proc.returncode}): {stderr}"
            )

        data = json.loads(produced[0].read_text(encoding="utf-8", errors="replace"))

    return parse_odl_json(data)
