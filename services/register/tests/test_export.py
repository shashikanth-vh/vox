"""DB → Excel / JSON export."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook

pytestmark = pytest.mark.asyncio


async def _seed(client: AsyncClient) -> None:
    eid = (await client.post("/v1/entities", json={"code": "EXP1", "legal_name": "Export Co"})).json()["id"]
    await client.post("/v1/leads", json={"company": "Lead Co"})
    await client.post(f"/v1/entities/{eid}/interactions",
                      json={"interaction_type": "Phone Call", "notes": "hi"})


async def test_export_counts(client: AsyncClient):
    await _seed(client)
    r = await client.get("/v1/export/counts")
    assert r.status_code == 200
    c = r.json()
    assert c["entities"] == 1 and c["leads"] == 1 and c["interactions"] == 1


async def test_export_excel(client: AsyncClient):
    await _seed(client)
    r = await client.get("/v1/export/excel")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.headers["content-disposition"].endswith('.xlsx"')

    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    # One sheet per table.
    assert {"entities", "leads", "interactions", "deals"} <= set(wb.sheetnames)
    ws = wb["entities"]
    rows = list(ws.iter_rows(values_only=True))
    header, data = rows[0], rows[1:]
    assert "legal_name" in header and "id" in header and "version" in header
    assert len(data) == 1
    assert data[0][header.index("legal_name")] == "Export Co"


async def test_export_json_and_subset(client: AsyncClient):
    await _seed(client)
    r = await client.get("/v1/export/json", params={"tables": "entities,leads"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["tables"].keys()) == {"entities", "leads"}
    assert body["tables"]["entities"][0]["legal_name"] == "Export Co"
    assert "exported_at" in body and body["tenant"] == "EVAM"


async def test_export_excel_subset(client: AsyncClient):
    await _seed(client)
    r = await client.get("/v1/export/excel", params={"tables": "leads"})
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    assert wb.sheetnames == ["leads"]
