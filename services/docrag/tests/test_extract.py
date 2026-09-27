"""POST /v1/extract — a document's content as Markdown for LLM callers (the CAM workbench)."""

from __future__ import annotations

import pytest
from conftest import covenants_xlsx, new_client, scanned_pdf, term_sheet_pdf

from app.knowledge import pipeline as pipeline_mod
from app.knowledge.sarvam_client import SarvamDigitiseResult, SarvamError, SarvamPage

pytestmark = pytest.mark.asyncio


async def _extract(c, name: str, data: bytes, **form: str):
    return await c.post("/v1/extract", files={"file": (name, data)}, data=form)


async def test_pdf_becomes_structured_markdown(client):
    r = await _extract(client, "Term Sheet.pdf", term_sheet_pdf())
    assert r.status_code == 200, r.text
    body = r.json()
    md = body["markdown"]
    assert body["filename"] == "Term Sheet.pdf" and body["page_count"] == 1
    assert md.startswith("[page 1]")
    assert "ABCDE1234F" in md
    # The label/value grid survives as a real table: every field is a row, none is
    # promoted to a column header.
    assert "| Field | Value |" in md
    assert "| Tenor | 36 months |" in md and "| Lender | Evam Finance Private Limited |" in md
    assert body["extraction_engines"] == ["local_text_layer"]
    assert body["cached"] is False and body["chars"] == len(md)


async def test_same_file_is_served_from_cache(client):
    data = term_sheet_pdf()
    first = (await _extract(client, "a.pdf", data)).json()
    again = (await _extract(client, "renamed.pdf", data)).json()
    assert again["cached"] is True
    assert again["markdown"] == first["markdown"] and again["filename"] == "renamed.pdf"
    # Sarvam on/off is part of the key: a different request is a different result.
    off = (await _extract(client, "a.pdf", data, use_sarvam="false")).json()
    assert off["cached"] is False


async def test_cache_is_per_tenant(client):
    data = term_sheet_pdf()
    await client.post("/v1/extract", files={"file": ("a.pdf", data)}, headers={"X-Tenant": "ALPHA"})
    other = await client.post("/v1/extract", files={"file": ("a.pdf", data)},
                              headers={"X-Tenant": "BETA"})
    assert other.json()["cached"] is False


async def test_scan_without_sarvam_is_named_not_silent(client):
    body = (await _extract(client, "consent.pdf", scanned_pdf())).json()
    assert body["markdown"].strip() == ""
    assert any("DOCRAG_SARVAM_API_KEY is not set" in w for w in body["warnings"])


async def test_scan_with_sarvam_is_ocr_text(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")
    monkeypatch.setattr(pipeline_mod, "digitise_document", lambda path: SarvamDigitiseResult(
        pages=[SarvamPage(page_number=1, markdown="CIBIL consent signed by the borrower")],
        status="completed", job_id="j1"))
    async with new_client() as c:
        body = (await _extract(c, "consent.pdf", scanned_pdf())).json()
    assert "CIBIL consent signed" in body["markdown"]
    assert body["extraction_engines"] == ["sarvam_docai"]


async def test_transient_sarvam_failure_is_not_cached(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")

    def boom(path):
        raise SarvamError("HTTP 503")

    monkeypatch.setattr(pipeline_mod, "digitise_document", boom)
    async with new_client() as c:
        data = scanned_pdf()
        first = (await _extract(c, "s.pdf", data)).json()
        again = (await _extract(c, "s.pdf", data)).json()
    assert any("Sarvam Doc AI failed" in w for w in first["warnings"])
    assert again["cached"] is False          # retried, not frozen in the cache


async def test_xlsx_sheet_is_a_table(client):
    body = (await _extract(client, "covenants.xlsx", covenants_xlsx())).json()
    assert "# Covenants" in body["markdown"]
    assert "| Covenant | Threshold | Frequency |" in body["markdown"]
    assert "| DSCR | 1.20x | Quarterly |" in body["markdown"]


async def test_extract_validates_and_needs_the_key(monkeypatch):
    monkeypatch.setenv("DOCRAG_API_KEYS", "k1")
    async with new_client() as c:
        assert (await _extract(c, "a.pdf", term_sheet_pdf())).status_code == 401
        bad = await c.post("/v1/extract", files={"file": ("x.docx", b"PK\x03\x04")},
                           headers={"X-API-Key": "k1"})
        assert bad.status_code == 415


async def test_extract_only_mode_needs_no_index(monkeypatch):
    monkeypatch.setenv("DOCRAG_INDEX_ENABLED", "false")
    monkeypatch.setenv("DOCRAG_QDRANT_URL", "http://127.0.0.1:1")   # nothing listens here
    async with new_client() as c:
        ready = (await c.get("/readyz")).json()
        ok = await _extract(c, "a.pdf", term_sheet_pdf())
        docs = await c.get("/v1/documents")
        status = (await c.get("/v1/status")).json()
    assert ready == {"status": "ok", "mode": "extract-only"}
    assert ok.status_code == 200 and "36 months" in ok.json()["markdown"]
    assert docs.status_code == 503 and "DOCRAG_INDEX_ENABLED" in docs.json()["error"]["detail"]
    assert status["mode"] == "extract-only" and status["vector_index"] is None


def _photo(fmt: str = "png") -> bytes:
    """A photographed page: pixels only, no text layer."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=300, height=150)
    page.insert_text((20, 80), "Site visit photo", fontsize=18)
    return page.get_pixmap(dpi=72).tobytes(fmt)


async def test_images_are_read_by_ocr_and_the_spend_is_reported(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")
    sent: list[str] = []

    def fake(path):
        sent.append(str(path))
        return SarvamDigitiseResult(
            pages=[SarvamPage(page_number=1, markdown="Site visit photo: 42 MW plant")],
            status="completed", job_id="j-img", seconds=2.5,
            usage={"pages_total": 1, "pages_processed": 1, "pages_succeeded": 1,
                   "pages_failed": 0})

    monkeypatch.setattr(pipeline_mod, "digitise_document", fake)
    async with new_client() as c:
        first = (await _extract(c, "site.jpg", _photo("jpg"))).json()
        again = (await _extract(c, "site-copy.jpg", _photo("jpg"))).json()
        png = await _extract(c, "site.png", _photo("png"))
    assert "42 MW plant" in first["markdown"]
    assert first["extraction_engines"] == ["sarvam_docai"]
    assert sent[0].endswith(".pdf")                      # the image went to Sarvam as a PDF
    t = first["telemetry"]
    assert (t["cached"], t["pages"], t["ocr_pages"]) == (False, 1, 1)
    assert t["sarvam"]["jobs"] == 1 and t["sarvam"]["pages_submitted"] == 1
    assert t["sarvam"]["job_ids"] == ["j-img"]
    # The same image again is a cache hit: no new Sarvam spend, the OCR text is kept.
    assert again["telemetry"]["cached"] is True and again["telemetry"]["sarvam"]["jobs"] == 0
    assert again["telemetry"]["ocr_pages"] == 1 and "42 MW plant" in again["markdown"]
    assert png.status_code == 200 and len(sent) == 2


async def test_a_pdf_reports_no_sarvam_spend(client):
    t = (await _extract(client, "Term Sheet.pdf", term_sheet_pdf())).json()["telemetry"]
    assert (t["ocr_pages"], t["sarvam"]["jobs"], t["sarvam"]["pages_submitted"]) == (0, 0, 0)


async def test_a_failed_sarvam_job_still_counts(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")

    def boom(path):
        raise SarvamError("HTTP 400: OUTPUT_FORMAT_INVALID")

    monkeypatch.setattr(pipeline_mod, "digitise_document", boom)
    async with new_client() as c:
        t = (await _extract(c, "s.pdf", scanned_pdf())).json()["telemetry"]
    assert t["sarvam"]["jobs"] == 1 and t["sarvam"]["failed_jobs"] == 1
    assert t["ocr_pages"] == 0


async def test_a_non_image_with_an_image_name_is_refused(client):
    r = await _extract(client, "photo.jpg", b"%PDF-1.7 not a jpeg")
    assert r.status_code == 415
