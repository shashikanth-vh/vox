"""The knowledge pipeline on its own: extraction → reconstruction → chunks, per input
kind, plus the Sarvam fallback and the OpenDataLoader JSON parser."""

from __future__ import annotations

from pathlib import Path

from conftest import covenants_xlsx, scanned_pdf, term_sheet_pdf

from app.knowledge import pipeline as pipeline_mod
from app.knowledge.extractors.odl_extractor import parse_odl_json
from app.knowledge.pipeline import run_pipeline
from app.knowledge.sarvam_client import SarvamDigitiseResult, SarvamPage


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_pdf_reconstructs_table_and_entities(tmp_path):
    result = run_pipeline(_write(tmp_path, "source.pdf", term_sheet_pdf()),
                          display_name="IP Term Sheet.pdf")
    doc = result.document

    assert doc.metadata.source_filename == "IP Term Sheet.pdf"
    assert doc.metadata.extraction_engines_used == ["local_text_layer"]
    assert "PAN:ABCDE1234F" in doc.entities

    tables = [c for c in result.chunks if c.element_types == ["table"]]
    assert tables, "the ruled table must become its own chunk"
    # A label/value table renders as label: value pairs, and keeps its verbatim rows.
    assert "Tenor: 36 months" in tables[0].text
    rows = [tables[0].table_columns, *tables[0].table_rows]
    assert ["Tenor", "36 months"] in rows
    assert all(c.doc == "IP Term Sheet.pdf" and c.pages == [1] for c in result.chunks)
    # Every source word reached a chunk — no coverage warning.
    assert not any("did not reach any chunk" in w for w in doc.warnings)


def test_scanned_page_warns_instead_of_silently_empty(tmp_path):
    result = run_pipeline(_write(tmp_path, "source.pdf", scanned_pdf()))
    assert any("page 1" in w and "DOCRAG_SARVAM_API_KEY is not set" in w
               for w in result.document.warnings)


def test_sarvam_recovers_difficult_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "real-looking-key")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(pipeline_mod, "digitise_document", lambda path: SarvamDigitiseResult(
        pages=[SarvamPage(page_number=1, markdown="CIBIL consent signed by Aravind Kumar")],
        status="completed", job_id="job-1"))

    result = run_pipeline(_write(tmp_path, "source.pdf", scanned_pdf()))

    [chunk] = result.chunks
    assert "CIBIL consent" in chunk.text
    assert chunk.extraction_engines == ["sarvam_docai"]
    assert result.document.metadata.extraction_engines_used == ["sarvam_docai"]


def test_xlsx_sheet_becomes_table_chunk(tmp_path):
    result = run_pipeline(_write(tmp_path, "source.xlsx", covenants_xlsx()))
    tables = [c for c in result.chunks if c.element_types == ["table"]]
    assert tables and tables[0].table_columns == ["Covenant", "Threshold", "Frequency"]
    assert "Covenant: DSCR, Threshold: 1.20x, Frequency: Quarterly" in tables[0].text


def _cma_xls() -> bytes:
    """A small real Excel 97–2003 workbook, like the CMA files desks upload."""
    import io

    import xlwt

    book = xlwt.Workbook()
    sheet = book.add_sheet("Operating Statement")
    for r, row in enumerate([["Particulars", "FY2024", "FY2025"],
                             ["Revenue", 4200, 5310.5], ["EBITDA", 610, 742]]):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    book.add_sheet("Blank")
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def test_xls_sheet_becomes_table_chunk(tmp_path):
    result = run_pipeline(_write(tmp_path, "source.xls", _cma_xls()))
    tables = [c for c in result.chunks if c.element_types == ["table"]]
    assert tables and tables[0].table_columns == ["Particulars", "FY2024", "FY2025"]
    # Whole numbers read as integers, not 4200.0.
    assert "Particulars: Revenue, FY2024: 4200, FY2025: 5310.5" in tables[0].text


def test_parse_odl_json_typed_elements():
    data = {
        "number of pages": 1, "title": "Sanction", "kids": [
            {"type": "heading", "level": "Heading 2", "page number": 1, "content": "Facility"},
            {"type": "paragraph", "page number": 1, "content": "Term loan of Rs. 10,00,000"},
            {"type": "table", "page number": 1, "rows": [
                {"cells": [{"column number": 1, "content": "Tenor"},
                           {"column number": 2, "content": "36 months"}]},
                {"cells": [{"column number": 2, "content": "orphan"}]}]},
            {"type": "image", "page number": 1, "bounding box": [0, 0, 10, 10]},
        ]}
    odl = parse_odl_json(data)
    assert [e.type for e in odl.elements] == ["heading", "paragraph", "table"]
    assert odl.elements[0].level == 2
    # Cells are placed by their stated column, so a skipped column stays empty.
    assert odl.elements[2].table_rows == [["", "orphan"]]
    assert odl.image_count == 1 and odl.doc_title == "Sanction"


def test_opendataloader_runs_local_never_hybrid(tmp_path, monkeypatch):
    """The ODL call must pin the local Java pipeline: `--hybrid off`, no hybrid URL."""
    import json
    import subprocess

    from app.config import get_settings
    from app.knowledge.extractors import odl_extractor

    fake_bin = tmp_path / "opendataloader-pdf"
    fake_bin.write_text("")
    monkeypatch.setenv("DOCRAG_ODL_BIN", str(fake_bin))
    get_settings.cache_clear()
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        out = cmd[cmd.index("-o") + 1]
        (Path(out) / "doc.json").write_text(json.dumps({"number of pages": 1, "kids": []}))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(odl_extractor.subprocess, "run", fake_run)
    odl_extractor.extract_with_odl(str(tmp_path / "x.pdf"))

    [cmd] = seen
    assert cmd[cmd.index("--hybrid") + 1] == "off"
    assert not any(arg.startswith("--hybrid-") for arg in cmd)


def test_model_download_is_retried_then_succeeds(monkeypatch, tmp_path):
    from app.rag import bake_models

    monkeypatch.setattr(bake_models.time, "sleep", lambda s: None)
    calls = []

    def flaky(source, **kw):
        calls.append(kw["max_workers"])
        if len(calls) < 3:
            raise RuntimeError("499 unknown")
        return "/opt/models/sparse"

    assert bake_models.download(flaky, "Qdrant/bm25", "22b8d2af", str(tmp_path)) == "/opt/models/sparse"
    assert calls == [2, 2, 2]
