"""xlsx extraction: each sheet becomes one PageResult-shaped table block,
reusing the same downstream reconstruction path as PDFs so xlsx and PDF
inputs converge on the same KnowledgeDocument schema."""

from __future__ import annotations

import openpyxl

from app.knowledge.extractors.pdf_extractor import ExtractedTable, PageResult


def extract_xlsx(path: str) -> list[PageResult]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    results: list[PageResult] = []

    for i, sheet_name in enumerate(wb.sheetnames):
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            if all(c is None for c in row):
                continue
            rows.append(["" if c is None else str(c) for c in row])

        if not rows:
            results.append(
                PageResult(page_number=i + 1, markdown=f"# {sheet_name}\n\n[empty sheet]", tables=[],
                           engine="local_text_layer")
            )
            continue

        markdown = f"# {sheet_name}\n"
        # Spreadsheet cells have no PDF-point geometry, so no bbox.
        results.append(
            PageResult(
                page_number=i + 1,
                markdown=markdown,
                tables=[ExtractedTable(rows=rows)],
                engine="local_text_layer",
            )
        )

    wb.close()
    return results
