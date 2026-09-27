"""The knowledge-object schema: the central, reusable representation that
every extraction backend (local PyMuPDF parsing or Sarvam Vision) is
flattened into, and that every downstream consumer (chunker, RAG index,
citation UI) reads from.

The point of this file is that OpenDataLoader-shaped output and
Sarvam-Vision-shaped output both become the *same* shape below, so nothing
downstream needs to know or care which engine produced a given page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ElementType = Literal["heading", "paragraph", "table", "list", "caption"]
ExtractionEngine = Literal["opendataloader", "local_text_layer", "local_ocr", "sarvam_docai"]


@dataclass
class TableObject:
    """A table kept as structured rows/columns, not flattened prose.

    Retained verbatim alongside the semantic (prose) representation used
    for embedding, so numeric questions can be answered from exact cell
    values rather than a lossy text rendering. See README for the
    rationale ("don't make the vector DB your source of truth").
    """

    columns: list[str]
    rows: list[list[str]]
    caption: str | None = None

    def to_markdown(self) -> str:
        if not self.columns:
            return "\n".join(" | ".join(row) for row in self.rows)
        header = "| " + " | ".join(self.columns) + " |"
        sep = "| " + " | ".join(["---"] * len(self.columns)) + " |"
        body = "\n".join("| " + " | ".join(row) + " |" for row in self.rows)
        return "\n".join([header, sep, body])

    def _all_rows(self) -> list[list[str]]:
        """Every row including `columns`, which for a header-less table is
        just the first data row rather than column names."""
        return ([self.columns] if self.columns else []) + self.rows

    @staticmethod
    def _compact(row: list[str]) -> list[str]:
        """Drop empty padding cells that layout-derived tables carry."""
        return [c.strip().replace("\n", " ") for c in row if c and c.strip()]

    def is_label_value(self) -> bool:
        """True for a field table whose rows are (label, value) pairs rather
        than records under shared column headers.

        Covers both the plain two-column form ("Lender | Evam Finance...")
        and the numbered-field form common in certificates ("1. | Legal
        Name | | PINNACLE LITHIUM POWER"), including the empty padding
        columns those carry. Such tables have no header row, so treating
        row 0 as column names pairs unrelated fields together and yields
        nonsense like "Legal Name: Trade Name".
        """
        rows = [self._compact(r) for r in self._all_rows()]
        rows = [r for r in rows if len(r) >= 2]
        if len(rows) < 2:
            return False

        # A real header row disqualifies the whole shape: a transaction
        # table ("SNO | TRAN DATE | DESCRIPTION | DEBITS ...") also has a
        # sequential first column, but its header names every column, and
        # rendering it as label/value would garble the rows and drop the
        # index. Label/value tables have no such row.
        header = self._compact(self.columns)
        if len(header) >= 3 and all(
            c and len(c) <= 24 and not re.fullmatch(r"[\d.,%/-]+", c) for c in header
        ):
            return False

        # A numbered field table ("1. | Legal Name | value") is the giveaway
        # form: a leading index column counting up. Row widths vary in these
        # (a date row may carry From/To sub-cells), so the index, not a
        # fixed width, is what identifies them.
        numbered = sum(1 for r in rows if re.fullmatch(r"\d+[.)]?", r[0]))
        indexed = numbered / len(rows) >= 0.8

        labels = [(r[1] if indexed and len(r) > 2 else r[0]) for r in rows]
        if not labels:
            return False
        # Field labels are short; data cells generally are not.
        short_ratio = sum(len(label) <= 45 for label in labels) / len(labels)
        if indexed:
            return short_ratio >= 0.8
        # Without an index column, require the narrow two-column shape so a
        # real data table with short first-column values isn't misread.
        return short_ratio >= 0.8 and {len(r) for r in rows} <= {2, 3}

    def to_prose(self) -> str:
        """A sentence-shaped rendering for embedding — table structure is
        preserved verbatim above; this is only for retrieval matching."""
        if not self.rows and not self.columns:
            return ""

        if self.is_label_value():
            # Render every row, including the one parsed as "columns", as
            # its own "label: value" pair.
            pairs = []
            for row in self._all_rows():
                cells = self._compact(row)
                if not cells:
                    continue
                if len(cells) > 2 and re.fullmatch(r"\d+[.)]?", cells[0]):
                    cells = cells[1:]  # drop a leading row number
                label, value = cells[0], " ".join(cells[1:])
                if label or value:
                    pairs.append(f"{label}: {value}" if value else label)
            return "; ".join(pairs)

        lines: list[str] = []
        for row in self.rows:
            if self.columns and len(self.columns) == len(row):
                rendered = ", ".join(f"{c}: {v}" for c, v in zip(self.columns, row, strict=True) if v)
                if rendered:
                    lines.append(rendered)
            else:
                lines.append(", ".join(v for v in row if v))
        return "; ".join(lines)


@dataclass
class SourceRef:
    """Provenance: exactly where in the source document this came from."""

    page: int
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in PDF points, if known
    extraction_engine: ExtractionEngine = "local_text_layer"


@dataclass
class ContentElement:
    """One atomic unit of document content -- a heading, paragraph, or table."""

    type: ElementType
    text: str
    source: SourceRef
    table: TableObject | None = None  # populated only when type == "table"


@dataclass
class Section:
    """A heading and the elements that fall under it, before the next
    heading of equal-or-higher level. Sections may nest via `subsections`."""

    title: str
    level: int
    elements: list[ContentElement] = field(default_factory=list)
    subsections: list[Section] = field(default_factory=list)


@dataclass
class ImageRegion:
    """An image present in the source but not text-extracted.

    Surfaced rather than dropped so a consumer can tell the difference
    between "this document has no such content" and "this document has a
    stamp/seal/signature whose text we never read".
    """

    page: int
    bbox: list[float] | None = None
    source: str | None = None
    has_alt_text: bool = False


@dataclass
class DocumentMetadata:
    title: str
    doc_type: str  # e.g. "bank_statement", "sanction_letter", "id_card", "financial_report", "unknown"
    source_filename: str
    page_count: int
    extraction_engines_used: list[ExtractionEngine] = field(default_factory=list)
    # From the PDF's own metadata, when the extractor reports it.
    pdf_title: str | None = None
    author: str | None = None
    creation_date: str | None = None
    # Images found in the source and NOT text-extracted.
    images: list[ImageRegion] = field(default_factory=list)
    element_counts: dict[str, int] = field(default_factory=dict)

    @property
    def image_count(self) -> int:
        return len(self.images)


@dataclass
class KnowledgeDocument:
    """The full reconstructed document: metadata + a section tree.

    This -- not the vector DB -- is the canonical, citable representation
    of the source file. Chunking and embedding are a derived, disposable
    view built from this for retrieval.
    """

    metadata: DocumentMetadata
    sections: list[Section] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def all_elements(self) -> list[tuple[Section, ContentElement]]:
        out: list[tuple[Section, ContentElement]] = []

        def walk(section: Section):
            for el in section.elements:
                out.append((section, el))
            for sub in section.subsections:
                walk(sub)

        for s in self.sections:
            walk(s)
        return out
