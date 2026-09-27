#!/usr/bin/env python3
"""Generate the DocRAG Postman collection + a standalone environment.

    postman/PRISM_DocRAG.postman_collection.json
    postman/PRISM_DocRAG_Standalone.postman_environment.json
    postman/fixtures/docrag-sample-term-sheet.pdf

The collection walks the whole flow with assertions: status → upload → wait until
ready → list / knowledge / chunks → query (extractive, scoped, generative) → delete.
Every request targets {{docragUrl}}, so ONE collection serves both deployments:

  * inside PRISM (through the edge):  docragUrl = {{baseUrl}}/docrag  — works with the
    PRISM Full / All-APIs environments (the gateway injects DocRAG's key);
  * DocRAG deployed on its own:       docragUrl = http://localhost:8010 and
    docragApiKey = the service's DOCRAG_API_KEYS — the standalone environment.

    newman run postman/PRISM_DocRAG.postman_collection.json \\
        -e postman/PRISM_DocRAG_Standalone.postman_environment.json --working-dir postman
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "postman"
FIXTURE = "fixtures/docrag-sample-term-sheet.pdf"

H = [{"key": "X-Tenant", "value": "{{tenant}}"},
     {"key": "X-API-Key", "value": "{{docragApiKey}}"},
     {"key": "X-User-Email", "value": "{{userEmail}}"},
     {"key": "Authorization", "value": "Bearer {{adminToken}}"}]


def req(name, method, path, *, body=None, form=None, desc=None, tests=None):
    url = "{{docragUrl}}" + path
    r: dict = {"method": method, "header": [dict(h) for h in H],
               "url": {"raw": url, "host": ["{{docragUrl}}"],
                       "path": [s for s in path.split("?")[0].split("/") if s]}}
    if "?" in path:
        r["url"]["query"] = [{"key": k, "value": v} for k, v in
                             (kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))]
    if body is not None:
        r["header"].append({"key": "Content-Type", "value": "application/json"})
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                     "options": {"raw": {"language": "json"}}}
    if form is not None:
        r["body"] = {"mode": "formdata", "formdata": form}
    if desc:
        r["description"] = desc
    item: dict = {"name": name, "request": r}
    if tests:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return item


def fixture_pdf() -> bytes:
    """A small synthetic term sheet (no real party data): prose + a ruled table."""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Indicative Term Sheet", fontsize=18)
    page.insert_text((72, 110), "Borrower: Sample Solar Private Limited, PAN ABCDE1234F.", fontsize=10)
    page.insert_text((72, 124), "Sanctioned amount Rs. 5,00,00,000 for a rooftop solar portfolio.",
                     fontsize=10)
    rows = [("Lender", "Evam Finance Private Limited"), ("Facility", "Term Loan"),
            ("Tenor", "36 months"), ("Interest", "11.5% per annum"),
            ("Security", "Hypothecation of project assets")]
    y = 160
    for label, value in rows:
        page.draw_rect(fitz.Rect(72, y, 220, y + 24))
        page.draw_rect(fitz.Rect(220, y, 520, y + 24))
        page.insert_text((76, y + 16), label, fontsize=10)
        page.insert_text((224, y + 16), value, fontsize=10)
        y += 24
    doc.set_metadata({"title": "Sample term sheet", "creationDate": "D:20260101000000"})
    data = doc.tobytes(garbage=3, deflate=True, no_new_id=True)
    doc.close()
    return data


def main() -> None:
    wait_tests = [
        "pm.test('document found', () => pm.response.to.have.status(200));",
        "const d = pm.response.json();",
        "const n = Number(pm.variables.get('pollCount') || 0);",
        "if (['queued', 'processing'].includes(d.status) && n < 60) {",
        "    pm.variables.set('pollCount', n + 1);",
        "    setTimeout(() => {}, 1000);",
        "    postman.setNextRequest(pm.info.requestName);",
        "} else {",
        "    pm.variables.set('pollCount', 0);",
        "    pm.test('ingestion finished as ready', () => pm.expect(d.status).to.eql('ready'));",
        "    pm.test('produced chunks', () => pm.expect(d.chunk_count).to.be.above(0));",
        "    if (d.warnings.length) console.log('warnings:', d.warnings);",
        "}",
    ]
    flow = [
        req("00 Status — what this deployment can do", "GET", "/v1/status",
            desc="Engines available (OpenDataLoader, Sarvam) and the tenant's index size.",
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "pm.test('has tenant', () => pm.expect(pm.response.json()).to.have.property('tenant'));"]),
        req("01 Upload a document (PDF / XLSX)", "POST", "/v1/documents",
            form=[{"key": "file", "type": "file", "src": FIXTURE},
                  {"key": "use_sarvam", "value": "true", "type": "text"}],
            desc="Multipart upload. 202 = queued for background processing; 200 with "
                 "duplicate=true = this exact file is already indexed for the tenant. In "
                 "the Postman app, re-select the file if the fixture path does not resolve "
                 "(newman: --working-dir postman).",
            tests=["pm.test('accepted', () => pm.expect([200, 202]).to.include(pm.response.code));",
                   "const d = pm.response.json();",
                   "pm.collectionVariables.set('docId', d.id);",
                   "pm.variables.set('pollCount', 0);"]),
        req("02 Wait until ready (polls itself)", "GET", "/v1/documents/{{docId}}",
            desc="Re-runs itself every second while status is queued/processing (up to 60 "
                 "polls) — in the collection runner / newman. Sent by hand, just re-send it.",
            tests=wait_tests),
        req("03 List documents", "GET", "/v1/documents",
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "pm.test('contains the upload', () => pm.expect(pm.response.json().items"
                   ".map(d => d.id)).to.include(pm.collectionVariables.get('docId')));"]),
        req("04 Document + reconstructed knowledge", "GET",
            "/v1/documents/{{docId}}?include=knowledge",
            desc="Section tree, tables (verbatim rows), entities, metadata and warnings.",
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "pm.test('has sections', () => pm.expect(pm.response.json().knowledge.sections)"
                   ".to.be.an('array').that.is.not.empty);"]),
        req("05 Knowledge chunks", "GET", "/v1/documents/{{docId}}/chunks",
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "pm.test('chunks carry provenance', () => {",
                   "    const c = pm.response.json().items[0];",
                   "    pm.expect(c).to.include.keys('section_path', 'pages', 'extraction_engines', 'doc_id');",
                   "});"]),
        req("06 Query — extractive (cited passages)", "POST", "/v1/query",
            body={"query": "What is the tenor of the facility?", "mode": "extractive", "top_k": 5},
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "const r = pm.response.json();",
                   "pm.test('ranked results with citations', () => {",
                   "    pm.expect(r.results).to.be.an('array').that.is.not.empty;",
                   "    pm.expect(r.citations.length).to.eql(r.results.length);",
                   "});"]),
        req("07 Query — restricted to this document", "POST", "/v1/query",
            body={"query": "PAN ABCDE1234F", "top_k": 3, "doc_ids": ["{{docId}}"]},
            tests=["pm.test('200', () => pm.response.to.have.status(200));",
                   "pm.test('only this document', () => pm.response.json().results.forEach("
                   "x => pm.expect(x.chunk.doc_id).to.eql(pm.collectionVariables.get('docId'))));"]),
        req("08 Query — generative (Sarvam)", "POST", "/v1/query",
            body={"query": "Summarise the key commercial terms.", "mode": "generative", "top_k": 5},
            desc="Needs DOCRAG_SARVAM_API_KEY on the service; without it the answer is 409 "
                 "(never a fabricated answer). 502 = Sarvam itself failed.",
            tests=["pm.test('answered, or refused for lack of a key', () => "
                   "pm.expect([200, 409]).to.include(pm.response.code));"]),
        req("09 Query — validation error (empty query)", "POST", "/v1/query",
            body={"query": ""},
            tests=["pm.test('422', () => pm.response.to.have.status(422));"]),
        req("10 Delete the document", "DELETE", "/v1/documents/{{docId}}",
            tests=["pm.test('204', () => pm.response.to.have.status(204));"]),
        req("11 Deleted document is gone", "GET", "/v1/documents/{{docId}}",
            tests=["pm.test('404', () => pm.response.to.have.status(404));"]),
    ]
    col = {
        "info": {
            "name": "PRISM · DocRAG — documents → cited answers",
            "description":
                "Upload a PDF/XLSX, wait for background processing, explore the reconstructed "
                "knowledge, query it (extractive or generative), delete it. All requests use "
                "{{docragUrl}}: set it to {{baseUrl}}/docrag inside PRISM (the gateway "
                "injects the service key; the caller needs `upload_remove_documents`), or to "
                "the service's own URL when DocRAG is deployed on its own (then "
                "{{docragApiKey}} must be one of its DOCRAG_API_KEYS).",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        # An empty token resolves to a bare "Bearer " — drop it (dev / standalone posture).
        "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
            "const a = pm.request.headers.find(h => h.key.toLowerCase() === 'authorization' && !h.disabled);",
            "if (a && /^\\s*(Bearer|Basic)?\\s*$/i.test(pm.variables.replaceIn(a.value))) {",
            "    pm.request.headers.remove(a.key);",
            "}",
            "if (!pm.variables.get('docragUrl')) {",
            "    pm.variables.set('docragUrl', pm.variables.replaceIn('{{baseUrl}}') + '/docrag');",
            "}",
        ]}}],
        "variable": [{"key": "docId", "value": ""}],
        "item": flow,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "PRISM_DocRAG.postman_collection.json").write_text(json.dumps(col, indent=2) + "\n")
    env = {"name": "PRISM — DocRAG standalone", "values": [
        {"key": "docragUrl", "value": "http://localhost:8010", "enabled": True},
        {"key": "docragApiKey", "value": "", "enabled": True},
        {"key": "tenant", "value": "EVAM", "enabled": True},
        {"key": "userEmail", "value": "", "enabled": True},
        {"key": "adminToken", "value": "", "enabled": True},
    ]}
    (OUT / "PRISM_DocRAG_Standalone.postman_environment.json").write_text(json.dumps(env, indent=2) + "\n")
    (OUT / FIXTURE).parent.mkdir(exist_ok=True)
    (OUT / FIXTURE).write_bytes(fixture_pdf())
    print(f"PRISM_DocRAG: {len(flow)} requests (+ standalone environment, sample PDF)")


if __name__ == "__main__":
    main()
