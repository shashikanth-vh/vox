"""The DocRAG HTTP contract: upload → background ingest → cited query, plus the front
door, tenant isolation, validation, persistence and the generative (Sarvam) path."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import covenants_xlsx, new_client, scanned_pdf, term_sheet_pdf, upload, wait_ready

from app.rag import answer as answer_mod

pytestmark = pytest.mark.asyncio


async def _ingest(c: httpx.AsyncClient, name: str, data: bytes, **headers: str) -> dict:
    r = await upload(c, name, data, **headers)
    assert r.status_code == 202, r.text
    doc = await wait_ready(c, r.json()["id"], **headers)
    assert doc["status"] == "ready", doc
    return doc


async def test_upload_ingest_and_extractive_query(client):
    doc = await _ingest(client, "IP Term Sheet.pdf", term_sheet_pdf())
    assert doc["chunk_count"] > 0 and doc["page_count"] == 1
    assert doc["extraction_engines"] == ["local_text_layer"]

    r = await client.post("/v1/query", json={"query": "What is the tenor of the facility?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "extractive"
    top = body["results"][0]["chunk"]
    assert "36 months" in top["text"]
    assert top["doc"] == "IP Term Sheet.pdf" and top["doc_id"] == doc["id"]
    assert body["citations"][0] == {"doc_id": doc["id"], "doc": "IP Term Sheet.pdf",
                                    "section_path": top["section_path"], "pages": [1],
                                    "engines": ["local_text_layer"]}


async def test_exact_identifier_is_found(client):
    await _ingest(client, "ts.pdf", term_sheet_pdf())
    await _ingest(client, "covenants.xlsx", covenants_xlsx())
    body = (await client.post("/v1/query", json={"query": "ABCDE1234F"})).json()
    assert "ABCDE1234F" in body["results"][0]["chunk"]["text"]


async def test_knowledge_and_chunks_endpoints(client):
    doc = await _ingest(client, "covenants.xlsx", covenants_xlsx())
    full = (await client.get(f"/v1/documents/{doc['id']}?include=knowledge")).json()
    assert full["knowledge"]["metadata"]["source_filename"] == "covenants.xlsx"
    assert full["knowledge"]["sections"][0]["title"] == "Covenants"
    chunks = (await client.get(f"/v1/documents/{doc['id']}/chunks")).json()
    assert chunks["total"] == doc["chunk_count"]
    [table] = [c for c in chunks["items"] if c["element_types"] == ["table"]]
    assert table["table_columns"] == ["Covenant", "Threshold", "Frequency"]
    assert table["table_rows"][0] == ["DSCR", "1.20x", "Quarterly"]
    plain = (await client.get(f"/v1/documents/{doc['id']}")).json()
    assert "knowledge" not in plain


async def test_duplicate_upload_returns_existing(client):
    data = term_sheet_pdf()
    first = await _ingest(client, "ts.pdf", data)
    again = await upload(client, "renamed.pdf", data)
    assert again.status_code == 200
    assert again.json()["id"] == first["id"] and again.json()["duplicate"] is True
    assert (await client.get("/v1/documents")).json()["total"] == 1


async def test_scanned_page_warning_surfaces_on_document(client):
    r = await upload(client, "consent.pdf", scanned_pdf())
    doc = await wait_ready(client, r.json()["id"])
    assert doc["status"] == "ready"
    assert any("DOCRAG_SARVAM_API_KEY is not set" in w for w in doc["warnings"])


@pytest.mark.parametrize(("name", "data", "status"), [
    ("notes.docx", b"PK\x03\x04...", 415),          # unsupported extension
    ("fake.pdf", b"not a pdf at all", 415),         # extension/content mismatch
    ("empty.pdf", b"", 422),
])
async def test_upload_validation(client, name, data, status):
    r = await upload(client, name, data)
    assert r.status_code == status, r.text
    assert r.json()["error"]["status"] == status


async def test_upload_size_cap(monkeypatch):
    monkeypatch.setenv("DOCRAG_MAX_UPLOAD_BYTES", "100")
    async with new_client() as c:
        r = await upload(c, "big.pdf", b"%PDF" + b"x" * 200)
    assert r.status_code == 413


async def test_path_traversal_filename_is_neutralised(client):
    doc = await _ingest(client, "../../etc/ts.pdf", term_sheet_pdf())
    assert doc["filename"] == "ts.pdf"


async def test_tenant_isolation(client):
    doc = await _ingest(client, "ts.pdf", term_sheet_pdf(), **{"X-Tenant": "ALPHA"})
    other = {"X-Tenant": "BETA"}
    assert (await client.get("/v1/documents", headers=other)).json()["total"] == 0
    assert (await client.get(f"/v1/documents/{doc['id']}", headers=other)).status_code == 404
    q = await client.post("/v1/query", json={"query": "tenor"}, headers=other)
    assert q.json()["results"] == []
    bad = await client.get("/v1/documents", headers={"X-Tenant": "../evil"})
    assert bad.status_code == 422


async def test_doc_ids_filter_restricts_retrieval(client):
    ts = await _ingest(client, "ts.pdf", term_sheet_pdf())
    cov = await _ingest(client, "covenants.xlsx", covenants_xlsx())
    body = (await client.post("/v1/query", json={"query": "tenor DSCR", "doc_ids": [cov["id"]]})).json()
    assert body["results"] and {r["chunk"]["doc_id"] for r in body["results"]} == {cov["id"]}
    assert ts["id"] not in {c["doc_id"] for c in body["citations"]}


async def test_delete_removes_from_index(client):
    doc = await _ingest(client, "ts.pdf", term_sheet_pdf())
    assert (await client.delete(f"/v1/documents/{doc['id']}")).status_code == 204
    assert (await client.get(f"/v1/documents/{doc['id']}")).status_code == 404
    assert (await client.delete(f"/v1/documents/{doc['id']}")).status_code == 404
    assert (await client.post("/v1/query", json={"query": "tenor"})).json()["results"] == []


async def test_lost_index_is_rebuilt_from_saved_chunks(client):
    """A restart against an EMPTY Qdrant (new volume, restored host, model change) must
    not lose documents: ready documents are re-embedded from their saved chunks."""
    doc = await _ingest(client, "ts.pdf", term_sheet_pdf())
    async with new_client() as c:  # same data dir, brand-new (empty) in-memory Qdrant
        listed = (await c.get("/v1/documents")).json()
        for _ in range(200):
            if (await c.get("/v1/status")).json()["chunks_indexed"] == doc["chunk_count"]:
                break
            await asyncio.sleep(0.02)
        body = (await c.post("/v1/query", json={"query": "tenor"})).json()
    assert [d["id"] for d in listed["items"]] == [doc["id"]]
    assert "36 months" in body["results"][0]["chunk"]["text"]


async def test_front_door_key(monkeypatch):
    monkeypatch.setenv("DOCRAG_API_KEYS", "k1,k2")
    async with new_client() as c:
        assert (await c.get("/v1/documents")).status_code == 401
        assert (await c.get("/v1/documents", headers={"X-API-Key": "nope"})).status_code == 401
        assert (await c.get("/v1/documents", headers={"X-API-Key": "k2"})).status_code == 200
        assert (await c.get("/v1/documents", headers={"Authorization": "Bearer k1"})).status_code == 200
        # Behind the gateway: the user's OIDC token rides in Authorization, the service key
        # in X-API-Key. The request must pass on the key.
        via_gateway = {"Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.user.token", "X-API-Key": "k1"}
        assert (await c.get("/v1/documents", headers=via_gateway)).status_code == 200
        spec = (await c.get("/openapi.json")).json()
        assert {"APIKeyHeader", "HTTPBearer"} <= set(spec["components"]["securitySchemes"])
        assert (await c.get("/healthz")).status_code == 200


async def test_cors_only_when_configured(client, monkeypatch):
    pre = {"Origin": "https://ui.example.com", "Access-Control-Request-Method": "POST"}
    assert "access-control-allow-origin" not in (await client.options("/v1/query", headers=pre)).headers
    monkeypatch.setenv("DOCRAG_CORS_ORIGINS", "https://ui.example.com")
    async with new_client() as c:
        r = await c.options("/v1/query", headers=pre)
    assert r.headers["access-control-allow-origin"] == "https://ui.example.com"


async def test_generative_without_key_is_409(client):
    await _ingest(client, "ts.pdf", term_sheet_pdf())
    r = await client.post("/v1/query", json={"query": "tenor", "mode": "generative"})
    assert r.status_code == 409
    assert "DOCRAG_SARVAM_API_KEY" in r.json()["error"]["detail"]


class _Resp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code, self._body, self.text = status, body, text

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no json")
        return self._body


async def test_generative_answer_grounded_and_cited(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")
    sent: dict = {}

    def fake_post(url, headers, json, timeout):  # noqa: A002 - mirrors requests.post
        sent.update(url=url, prompt=json["messages"][0]["content"], key=headers["api-subscription-key"])
        return _Resp(200, {"choices": [{"message": {"content": "The tenor is 36 months [1]."}}]})

    monkeypatch.setattr(answer_mod.requests, "post", fake_post)
    async with new_client() as c:
        await _ingest(c, "ts.pdf", term_sheet_pdf())
        r = await c.post("/v1/query", json={"query": "What is the tenor?", "mode": "generative"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "The tenor is 36 months [1]."
    assert sent["url"].endswith("/v1/chat/completions") and sent["key"] == "sk-test"
    assert "36 months" in sent["prompt"] and "ONLY the context" in sent["prompt"]


async def test_generative_upstream_failure_is_502(monkeypatch):
    monkeypatch.setenv("DOCRAG_SARVAM_API_KEY", "sk-test")
    monkeypatch.setattr(answer_mod.requests, "post",
                        lambda *a, **k: _Resp(402, text="no credits"))
    async with new_client() as c:
        await _ingest(c, "ts.pdf", term_sheet_pdf())
        r = await c.post("/v1/query", json={"query": "tenor", "mode": "generative"})
    assert r.status_code == 502
    assert "402" in r.json()["error"]["detail"]


async def test_status_and_health(client):
    await _ingest(client, "ts.pdf", term_sheet_pdf())
    s = (await client.get("/v1/status")).json()
    assert s["documents_ready"] == 1 and s["chunks_indexed"] > 0
    assert s["vector_index"]["engine"] == "qdrant"
    assert s["vector_index"]["collection"].startswith("docrag_chunks_")
    assert s["sarvam_configured"] is False and s["opendataloader"] is False
    assert (await client.get("/readyz")).json()["status"] == "ok"


async def test_readyz_503_when_model_missing(client):
    class Broken:
        loaded, load_error, dim = False, "model could not be loaded from '/opt/models'", 384

    client._transport.app.state.embedder = Broken()  # type: ignore[attr-defined]
    r = await client.get("/readyz")
    assert r.status_code == 503 and "/opt/models" in r.json()["detail"]


async def test_dev_ui_only_when_enabled(client, monkeypatch):
    assert (await client.get("/v1/dev-ui")).status_code == 404
    monkeypatch.setenv("DOCRAG_DEV_UI", "true")
    async with new_client() as c:
        r = await c.get("/v1/dev-ui")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
