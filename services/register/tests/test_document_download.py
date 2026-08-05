"""The approver dialog's download path: the analyst uploads the completed CAM on the
lending line; everyone with company read — the preparer, Credit Head, Management —
downloads it through /v1/documents/{id}/content. The filename carries an em-dash on
purpose: HTTP headers are latin-1, and an unescaped Content-Disposition made exactly
these hand-named documents 500 on download (RFC 6266 encoding is the fix)."""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

ANALYST = {"X-User-Email": "bhavana@evamfinance.com", "X-User-Roles": "Deal Analyst",
           "X-User-Id": "8c5a2c1e-0000-4000-8000-000000000001"}
HEAD = {"X-User-Email": "credithead@evamfinance.com", "X-User-Roles": "Credit Head",
        "X-User-Id": "8c5a2c1e-0000-4000-8000-000000000002"}
MGMT = {"X-User-Email": "divya.rao@evamfinance.com", "X-User-Roles": "Management",
        "X-User-Id": "8c5a2c1e-0000-4000-8000-000000000003"}


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>CAM"
                   "</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


async def test_committee_can_download_the_uploaded_cam(client: AsyncClient):
    ent = (await client.post("/v1/entities", json={
        "code": f"DL-{uuid.uuid4().hex[:6]}", "legal_name": "Download Co"})).json()
    lend = (await client.post("/v1/lending", json={
        "entity_id": ent["id"], "stage": "Diligence",
        "analyst": "Bhavana"})).json()
    up = await client.post(
        f"/v1/lending/{lend['id']}/documents/upload",
        files={"file": ("CAM example — Pinnacle Lithium Power (format reference) (2).docx", _docx(),
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document")},
        data={"section": "Sanction", "title": "CAM example — Pinnacle (2)", "doc_type": "CAM",
              "status": "On File"},
        headers=ANALYST)
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]

    for who in (ANALYST, HEAD, MGMT):
        r = await client.get(f"/v1/documents/{doc_id}/content", headers=who)
        assert r.status_code == 200, (who["X-User-Roles"], r.status_code, r.text[:300])
        assert r.content[:2] == b"PK", who["X-User-Roles"]
        cd = r.headers["content-disposition"]
        assert "filename*=UTF-8''" in cd and "%E2%80%94" in cd, cd
