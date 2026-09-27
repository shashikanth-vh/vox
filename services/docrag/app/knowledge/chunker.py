"""Turn a KnowledgeDocument's section tree into semantic chunks -- the
"Knowledge Objects" from the architecture: each one carries its section
context, entities, and source provenance, not just raw text.

Rules:
- A table is always its own chunk (never merged with prose, never split).
  Both its prose rendering (for embedding) and its structured
  columns/rows (for exact-value lookups) are kept -- see schema.TableObject.
- Paragraphs accumulate under the same section up to max_chars, then flush:
  splitting a paragraph loses less meaning than splitting a table.
- Every chunk's `section` is the full heading path, not just the immediate
  parent, so a chunk retrieved alone still states its place in the document.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.config import get_settings
from app.knowledge.schema import KnowledgeDocument, Section, TableObject


def _entities_in(entities: list[str], text: str) -> list[str]:
    """Match an entity to text on word boundaries, not bare substring.

    Plain `in` matching tags a chunk containing "₹1,000,000" with the
    entity "₹1,000", which then inflates that chunk's entity boost during
    retrieval for a query that never mentioned it.
    """
    found = []
    for entity in entities:
        value = entity.split(":", 1)[-1]
        if not value:
            continue
        # Trailing guard also rejects a continuing number: "₹1,000" must not
        # match inside "₹1,000,000" or "₹1,000.50", where the next char is a
        # separator rather than a word character.
        if re.search(rf"(?<!\w){re.escape(value)}(?!\w)(?![.,]\d)", text):
            found.append(entity)
    return found


@dataclass
class KnowledgeChunk:
    doc: str
    doc_type: str
    chunk_id: int
    section_path: str
    element_types: list[str]
    text: str
    entities: list[str] = field(default_factory=list)
    table_columns: list[str] | None = None
    table_rows: list[list[str]] | None = None
    pages: list[int] = field(default_factory=list)
    extraction_engines: list[str] = field(default_factory=list)
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in PDF points, when known
    char_count: int = 0
    # True when a page this chunk came from also holds an image that was
    # never text-extracted -- i.e. the chunk may be missing context that is
    # visually present in the source (a stamp, seal or signature block).
    page_has_unextracted_image: bool = False


def _split_table(
    table: TableObject, max_chars: int | None = None
) -> list[TableObject]:
    """Split a table into row-groups small enough to embed, repeating the
    header columns in each group.

    A table is never split *within* a row -- the row, with its column
    headers, stays the atomic unit, so a retrieved group never shows a
    figure without saying what it measures. Small tables come back as a
    single group, unchanged.
    """
    if not table.rows:
        return [table]
    limit = max_chars or get_settings().max_table_chunk_chars

    # In a label/value table the "columns" row is real data (the first
    # field), not headers. Repeating it across groups would duplicate that
    # field into every chunk and pair it against unrelated values, so each
    # group instead carries its own rows only.
    label_value = table.is_label_value()

    def make(rows: list[list[str]], first: bool) -> TableObject:
        if label_value:
            if first:
                return TableObject(columns=table.columns, rows=rows, caption=table.caption)
            # Later groups: promote this group's own first row to `columns`
            # so to_prose() renders it as another label/value pair.
            return TableObject(columns=rows[0], rows=rows[1:], caption=table.caption)
        return TableObject(columns=table.columns, rows=rows, caption=table.caption)

    groups: list[TableObject] = []
    current: list[list[str]] = []

    for row in table.rows:
        # Measure the rendered prose, not the raw cells: to_prose() repeats
        # every column name per row, so the rendered form runs several times
        # longer than the row text and is what actually gets embedded.
        candidate = make(current + [row], not groups)
        if current and len(candidate.to_prose()) > limit:
            groups.append(make(current, not groups))
            current = []
        current.append(row)

    if current:
        groups.append(make(current, not groups))
    return groups


def _flatten_sections(sections: list[Section], parent_path: str = "") -> list[tuple[str, Section]]:
    out = []
    for s in sections:
        path = f"{parent_path} > {s.title}" if parent_path else s.title
        out.append((path, s))
        out.extend(_flatten_sections(s.subsections, path))
    return out


def build_chunks(
    doc: KnowledgeDocument, max_chars: int | None = None
) -> list[KnowledgeChunk]:
    max_chars = max_chars or get_settings().max_chunk_chars
    chunks: list[KnowledgeChunk] = []
    doc_name = doc.metadata.source_filename
    image_pages = {img.page for img in doc.metadata.images}

    buf_texts: list[str] = []
    buf_types: list[str] = []
    buf_pages: set[int] = set()
    buf_engines: set[str] = set()
    current_section_path = doc_name

    def flush():
        nonlocal buf_texts, buf_types, buf_pages, buf_engines
        if not buf_texts:
            return
        text = "\n\n".join(buf_texts).strip()
        chunks.append(
            KnowledgeChunk(
                doc=doc_name,
                doc_type=doc.metadata.doc_type,
                chunk_id=len(chunks),
                section_path=current_section_path,
                element_types=list(dict.fromkeys(buf_types)),
                text=text,
                entities=_entities_in(doc.entities, text),
                pages=sorted(buf_pages),
                extraction_engines=sorted(buf_engines),
                char_count=len(text),
                page_has_unextracted_image=bool(buf_pages & image_pages),
            )
        )
        buf_texts, buf_types, buf_pages, buf_engines = [], [], set(), set()

    for section_path, section in _flatten_sections(doc.sections):
        current_section_path = section_path
        for el in section.elements:
            if el.type == "table" and el.table is not None:
                flush()
                for group in _split_table(el.table):
                    prose = group.to_prose()
                    chunks.append(
                        KnowledgeChunk(
                            doc=doc_name,
                            doc_type=doc.metadata.doc_type,
                            chunk_id=len(chunks),
                            section_path=section_path,
                            element_types=["table"],
                            text=f"Table in section '{section_path}': {prose}",
                            entities=_entities_in(doc.entities, prose),
                            table_columns=group.columns,
                            table_rows=group.rows,
                            pages=[el.source.page],
                            extraction_engines=[el.source.extraction_engine],
                            bbox=el.source.bbox,
                            char_count=len(prose),
                            page_has_unextracted_image=el.source.page in image_pages,
                        )
                    )
                continue

            candidate_len = sum(len(t) for t in buf_texts) + len(el.text)
            if candidate_len > max_chars and buf_texts:
                flush()
            buf_texts.append(el.text)
            buf_types.append(el.type)
            buf_pages.add(el.source.page)
            buf_engines.add(el.source.extraction_engine)

        # Don't flush a section that contributed only its own heading --
        # carry it forward so the heading joins the body that follows it
        # instead of becoming a standalone one-line chunk.
        if any(t != "heading" for t in buf_types):
            flush()

    flush()
    return chunks


def chunks_to_dicts(chunks: list[KnowledgeChunk]) -> list[dict]:
    return [asdict(c) for c in chunks]
