"""Object storage (S3 / MinIO) via boto3, exercised against an in-process moto mock.

Two levels:
* the ``S3Storage`` backend directly (put / get / presign / delete, auto-create bucket);
* the full upload → catalog → presigned-download path through the API.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from moto import mock_aws

from app import storage as storage_mod
from app.storage.s3 import S3Storage, StorageError

pytestmark = pytest.mark.asyncio


def _store() -> S3Storage:
    return S3Storage(
        bucket="prism-documents", region="us-east-1", endpoint_url=None,
        public_endpoint_url=None, access_key_id="testing", secret_access_key="testing",
        use_ssl=True, path_style=True, presign_expiry_seconds=3600, auto_create_bucket=True,
    )


async def test_s3storage_put_get_presign_delete():
    with mock_aws():
        st = _store()
        obj = await st.put("tenant/Lead/x/coi.pdf", b"hello-bytes", "application/pdf")
        assert obj.backend == "s3"
        assert obj.uri == "s3://prism-documents/tenant/Lead/x/coi.pdf"
        assert obj.bucket == "prism-documents" and obj.key == "tenant/Lead/x/coi.pdf"

        assert await st.get("tenant/Lead/x/coi.pdf") == b"hello-bytes"

        url = await st.presigned_get_url("tenant/Lead/x/coi.pdf", filename="coi.pdf")
        assert "X-Amz-Signature" in url and "coi.pdf" in url

        await st.delete("tenant/Lead/x/coi.pdf")
        with pytest.raises(StorageError):
            await st.get("tenant/Lead/x/coi.pdf")


async def test_s3storage_auto_creates_bucket():
    with mock_aws():
        # A fresh moto has no bucket; put() must create it on first use.
        st = _store()
        obj = await st.put("a/b/c.txt", b"x", "text/plain")
        assert obj.bucket == "prism-documents"


async def test_upload_through_api_to_s3_and_presigned_download(
    client: AsyncClient, monkeypatch
):
    with mock_aws():
        store = _store()
        monkeypatch.setattr(storage_mod, "get_storage", lambda: store)

        lead_id = (await client.post("/v1/leads", json={"company": "EcoSoch"})).json()["id"]
        blob = b"%PDF-1.4 uploaded certificate"
        r = await client.post("/v1/documents/upload",
                              files={"file": ("coi.pdf", blob, "application/pdf")},
                              data={"subject_type": "Lead", "subject_id": lead_id,
                                    "slot_key": "coi", "section": "KYC & Constitutional",
                                    "title": "Certificate of Incorporation",
                                    "is_required": "true"})
        assert r.status_code == 201, r.text
        doc = r.json()
        assert doc["storage_backend"] == "s3"
        assert doc["storage_uri"].startswith("s3://prism-documents/")
        assert doc["size_bytes"] == len(blob) and doc["checksum"]
        assert doc["entity_id"] is None  # lead with no entity

        # The bytes really landed in the store.
        _bucket, key = storage_mod.parse_s3_uri(doc["storage_uri"])
        assert await store.get(key) == blob

        # Download redirects to a presigned URL (no bytes proxied through the API).
        dl = await client.get(f"/v1/documents/{doc['id']}/content", follow_redirects=False)
        assert dl.status_code in (302, 307)
        assert "X-Amz-Signature" in dl.headers["location"]


async def test_nested_upload_rolls_up_entity(client: AsyncClient, monkeypatch):
    with mock_aws():
        monkeypatch.setattr(storage_mod, "get_storage", lambda: _store())
        eid = (await client.post("/v1/entities",
                                 json={"code": "ACME", "legal_name": "Acme"})).json()["id"]
        r = await client.post(f"/v1/entities/{eid}/documents/upload",
                              files={"file": ("pan.pdf", b"PANDATA", "application/pdf")},
                              data={"slot_key": "company_pan", "title": "Company PAN"})
        assert r.status_code == 201, r.text
        assert r.json()["entity_id"] == eid
        assert r.json()["storage_backend"] == "s3"


async def test_upload_bad_subject_404(client: AsyncClient, monkeypatch):
    with mock_aws():
        monkeypatch.setattr(storage_mod, "get_storage", lambda: _store())
        r = await client.post("/v1/documents/upload",
                              files={"file": ("x.pdf", b"x", "application/pdf")},
                              data={"subject_type": "Lead",
                                    "subject_id": "00000000-0000-0000-0000-000000000000",
                                    "title": "x"})
        assert r.status_code == 404
