"""Documents catalog + the ATLAS "Data Register" rollup.

Covers: subject-aware registration (denormalised entity roll-up), the checklist template,
inline small-file storage + download, the metadata-only record, the section/progress
rollup, ad-hoc documents, and the inline size ceiling.
"""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _entity(client: AsyncClient, code: str) -> str:
    r = await client.post("/v1/entities", json={"code": code, "legal_name": code})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_checklist(client: AsyncClient) -> None:
    """Seed a minimal 2-section checklist (2 required, 1 optional) via the API."""
    items = [
        ("KYC & Constitutional", 1, "coi", "Certificate of Incorporation", True, 0),
        ("KYC & Constitutional", 1, "company_pan", "Company PAN", True, 1),
        ("Financials", 2, "audited_financials", "Audited financials", False, 2),
    ]
    for section, so, slot, label, req, order in items:
        r = await client.post("/v1/document-checklist", json={
            "section": section, "section_order": so, "slot_key": slot, "label": label,
            "is_required": req, "sort_order": order})
        assert r.status_code == 201, r.text


async def test_checklist_template_grouped(client: AsyncClient):
    await _seed_checklist(client)
    r = await client.get("/v1/document-checklist/template", params={"applies_to": "Lead"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["required_total"] == 2
    sections = body["sections"]
    assert [s["section"] for s in sections] == ["KYC & Constitutional", "Financials"]
    assert sections[0]["required_total"] == 2
    assert {i["slot_key"] for i in sections[0]["items"]} == {"coi", "company_pan"}


async def test_register_inline_document_and_download(client: AsyncClient):
    lead_id = (await client.post("/v1/leads", json={"company": "EcoSoch"})).json()["id"]
    blob = b"%PDF-1.4 fake certificate of incorporation"
    r = await client.post(f"/v1/leads/{lead_id}/documents", json={
        "slot_key": "coi", "section": "KYC & Constitutional",
        "title": "Certificate of Incorporation",
        "original_filename": "coi.pdf", "content_type": "application/pdf",
        "content_base64": base64.b64encode(blob).decode(),
    })
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["subject_type"] == "Lead" and doc["subject_id"] == lead_id
    assert doc["storage_backend"] == "inline"
    assert doc["size_bytes"] == len(blob)
    assert doc["uploaded_by"] == "pytest" and doc["uploaded_at"]
    assert doc["checksum"] and "inline_content" not in doc  # bytes never returned in metadata

    # Download streams the exact bytes back.
    dl = await client.get(f"/v1/documents/{doc['id']}/content")
    assert dl.status_code == 200
    assert dl.content == blob
    assert dl.headers["content-type"].startswith("application/pdf")


async def test_register_by_storage_uri_and_metadata_only(client: AsyncClient):
    eid = await _entity(client, "ACME")
    # Object-storage-backed reference.
    r = await client.post(f"/v1/entities/{eid}/documents", json={
        "slot_key": "audited_financials", "title": "Audited FY24",
        "storage_uri": "s3://prism-docs/acme/audited-fy24.pdf", "size_bytes": 2_500_000})
    assert r.status_code == 201, r.text
    assert r.json()["storage_backend"] == "s3"
    assert r.json()["entity_id"] == eid  # denormalised from the Entity subject

    # Metadata-only record (large file recorded, bytes not stored) → download 404s.
    r2 = await client.post(f"/v1/entities/{eid}/documents", json={
        "title": "Big DPR", "slot_key": "project_report_dpr", "size_bytes": 50_000_000})
    doc2 = r2.json()
    assert r2.status_code == 201 and doc2["storage_backend"] is None
    assert (await client.get(f"/v1/documents/{doc2['id']}/content")).status_code == 404


async def test_data_register_rollup_progress(client: AsyncClient):
    await _seed_checklist(client)
    lead_id = (await client.post("/v1/leads", json={"company": "Navgrun"})).json()["id"]

    # Nothing on file yet.
    r = await client.get(f"/v1/leads/{lead_id}/data-register")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["required_total"] == 2 and body["required_on_file"] == 0
    assert body["percent_complete"] == 0

    # File one of the two required slots.
    await client.post(f"/v1/leads/{lead_id}/documents", json={
        "slot_key": "coi", "title": "COI", "storage_uri": "https://x/coi.pdf"})
    body = (await client.get(f"/v1/leads/{lead_id}/data-register")).json()
    assert body["required_on_file"] == 1 and body["percent_complete"] == 50
    kyc = next(s for s in body["sections"] if s["section"] == "KYC & Constitutional")
    coi = next(i for i in kyc["items"] if i["slot_key"] == "coi")
    assert coi["on_file"] is True and coi["count"] == 1
    assert kyc["required_on_file"] == 1

    # An ad-hoc document (no slot_key) surfaces separately, not against the checklist.
    await client.post(f"/v1/leads/{lead_id}/documents", json={
        "title": "Board note", "storage_uri": "https://x/note.pdf"})
    body = (await client.get(f"/v1/leads/{lead_id}/data-register")).json()
    assert len(body["ad_hoc"]) == 1 and body["ad_hoc"][0]["title"] == "Board note"
    assert body["document_count"] == 2
    assert body["required_on_file"] == 1  # ad-hoc doesn't move required progress


async def test_inline_over_limit_rejected(client: AsyncClient):
    lead_id = (await client.post("/v1/leads", json={"company": "Big"})).json()["id"]
    # settings.documents_inline_max_bytes defaults to 400 KB; send 500 KB.
    big = base64.b64encode(b"x" * (500 * 1024)).decode()
    r = await client.post(f"/v1/leads/{lead_id}/documents", json={
        "title": "huge.bin", "content_base64": big})
    assert r.status_code == 422  # ValidationAppError
    assert "object storage" in r.text.lower()


async def test_upload_inline_fallback(client: AsyncClient):
    """With no object store configured (the dev default), a small file upload is kept
    inline and is downloadable through the API."""
    lead_id = (await client.post("/v1/leads", json={"company": "Inline"})).json()["id"]
    blob = b"small inline pdf"
    r = await client.post(f"/v1/leads/{lead_id}/documents/upload",
                          files={"file": ("note.pdf", blob, "application/pdf")},
                          data={"slot_key": "coi", "title": "COI"})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["storage_backend"] == "inline"
    assert doc["size_bytes"] == len(blob) and doc["checksum"]
    dl = await client.get(f"/v1/documents/{doc['id']}/content")
    assert dl.status_code == 200 and dl.content == blob


async def test_documents_filter_by_subject(client: AsyncClient):
    eid = await _entity(client, "FILT")
    await client.post(f"/v1/entities/{eid}/documents", json={
        "slot_key": "coi", "title": "COI", "storage_uri": "https://x/1"})
    r = await client.get("/v1/documents", params={
        "subject_type": "Entity", "subject_id": eid, "with_total": True})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["slot_key"] == "coi"
