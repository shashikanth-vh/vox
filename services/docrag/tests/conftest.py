"""DocRAG fixtures — stub embedder, in-process Qdrant, PyMuPDF-only extraction, a fresh
data dir per test, and documents generated on the fly (no binary fixtures in the repo)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openpyxl
import pymupdf as fitz
import pytest
import pytest_asyncio

from app.config import get_settings

TABLE_ROWS = [("Lender", "Evam Finance Private Limited"),
              ("Facility", "Term Loan"),
              ("Tenor", "36 months"),
              ("Interest", "11.5% per annum")]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCRAG_EMBEDDER", "stub")
    monkeypatch.setenv("DOCRAG_QDRANT_URL", ":memory:")
    monkeypatch.setenv("DOCRAG_USE_ODL", "false")
    monkeypatch.setenv("DOCRAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "")
    monkeypatch.setenv("DOCRAG_LOG_LEVEL", "WARNING")
    monkeypatch.delenv("DOCRAG_API_KEYS", raising=False)
    monkeypatch.delenv("DOCRAG_DEV_UI", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def term_sheet_pdf() -> bytes:
    """A text-layer PDF: prose with a PAN and an INR amount, then a ruled 2-column table."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Indicative Term Sheet", fontsize=18)
    page.insert_text((72, 110), "Borrower PAN ABCDE1234F. Sanctioned amount Rs. 5,00,00,000 for the",
                     fontsize=10)
    page.insert_text((72, 124), "solar rooftop portfolio; disbursement subject to conditions precedent.",
                     fontsize=10)
    x0, x1, x2, y = 72, 220, 520, 160
    for label, value in TABLE_ROWS:
        page.draw_rect(fitz.Rect(x0, y, x1, y + 24))
        page.draw_rect(fitz.Rect(x1, y, x2, y + 24))
        page.insert_text((x0 + 4, y + 16), label, fontsize=10)
        page.insert_text((x1 + 4, y + 16), value, fontsize=10)
        y += 24
    out = doc.tobytes()
    doc.close()
    return out


def scanned_pdf() -> bytes:
    """A page with no text layer at all — what a scan looks like to the extractor."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(72, 72, 300, 200), fill=(0.8, 0.8, 0.8))
    out = doc.tobytes()
    doc.close()
    return out


def covenants_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Covenants"
    ws.append(["Covenant", "Threshold", "Frequency"])
    ws.append(["DSCR", "1.20x", "Quarterly"])
    ws.append(["Debt to Equity", "3.0x", "Annual"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@asynccontextmanager
async def new_client() -> AsyncIterator[httpx.AsyncClient]:
    """A fresh app (with its lifespan) built from the CURRENT environment."""
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://docrag",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with new_client() as c:
        yield c


async def upload(c: httpx.AsyncClient, name: str, data: bytes, **headers: str) -> httpx.Response:
    return await c.post("/v1/documents", files={"file": (name, data)}, headers=headers)


async def wait_ready(c: httpx.AsyncClient, doc_id: str, timeout: float = 30.0, **headers: str) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        body = (await c.get(f"/v1/documents/{doc_id}", headers=headers)).json()
        if body["status"] in ("ready", "failed"):
            return body
        if loop.time() > deadline:
            raise AssertionError(f"document {doc_id} stuck in {body['status']}")
        await asyncio.sleep(0.05)
