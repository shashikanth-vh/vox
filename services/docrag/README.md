# PRISM DocRAG — document → knowledge → cited answers

DocRAG turns uploaded documents (PDF, XLSX) into a clean, provenance-preserving knowledge
representation, and answers questions over it with citations. It treats "PDF to RAG" as
two problems, not one:

```
file ──▶ knowledge pipeline (extract → reconstruct → chunk) ──▶ RAG (index → retrieve → answer)
```

No dependency on the Register, Access or the gateway — its state is its volume (documents,
the source of truth) and its own Qdrant (vectors, derived and rebuildable). That makes it deployable two ways (see **Deployment**): inside PRISM
behind the gateway at `/docrag/...`, or on its own as a separate service.

## API

| Endpoint | What it does |
| --- | --- |
| `POST /v1/documents` | Multipart `file` (`.pdf` / `.xlsx`), optional `use_sarvam` form field. Answers **202** with the document; processing runs in the background. An identical file already held by the tenant returns **200** with `duplicate: true`. |
| `GET /v1/documents` | The tenant's documents, newest first, with `status` (`queued` → `processing` → `ready` / `failed`), `warnings`, `doc_type`, `chunk_count`. |
| `GET /v1/documents/{id}` | One document. `?include=knowledge` adds the reconstructed section tree, entities and metadata. |
| `GET /v1/documents/{id}/chunks` | The RAG-ready chunks, each with section path, pages, bbox, entities, extraction engine and (for tables) verbatim rows. |
| `DELETE /v1/documents/{id}` | Remove a document and its chunks from the index. |
| `POST /v1/query` | `{"query", "mode": "extractive"\|"generative", "top_k", "doc_ids"}` → answer + citations + ranked evidence (fused score, vector rank, BM25 rank). |
| `GET /v1/status` | What this deployment can do (OpenDataLoader, Sarvam) and the tenant's index size. |
| `GET /v1/dev-ui` | Browser test console (only with `DOCRAG_DEV_UI=true`). |
| `/healthz`, `/readyz` | Liveness; readiness = the embedding models are loaded AND Qdrant answers (503 with the reason otherwise). |

Every `/v1` route requires the front-door key (`X-API-Key` or `Bearer`, from
`DOCRAG_API_KEYS`; empty = open for dev) and reads `X-Tenant`. Through the gateway, the key
is injected and the caller needs the `upload_remove_documents` operation.

```bash
# through the edge, dev posture
curl -k -F file=@term-sheet.pdf https://localhost:8443/docrag/v1/documents
curl -k https://localhost:8443/docrag/v1/documents/<id>        # until status=ready
curl -k -H 'Content-Type: application/json' \
     -d '{"query":"What is the tenor?"}' https://localhost:8443/docrag/v1/query
```

Or open `https://localhost:8443/docrag/v1/dev-ui` for the upload / explore / query console.

## Calling the API

- **Postman / newman**: `postman/PRISM_DocRAG.postman_collection.json` — the full flow with
  assertions, against either deployment (`{{docragUrl}}`). See `docs/POSTMAN.md` §1c.
- **Contract**: `docs/openapi/docrag.openapi.json` (regenerate with `scripts/export_openapi.sh`).
- **Every PRISM API**: the DocRAG folder in `postman/PRISM_All_APIs.postman_collection.json`.

## Deployment

| | Inside PRISM | On its own |
| --- | --- | --- |
| Compose | `deploy/compose/docker-compose.yml` (services `docrag` + `docrag-qdrant`, no host ports) | `deploy/compose/docker-compose.docrag.yml` (the same pair; host port 8010; both keys mandatory) |
| Helm | umbrella `deploy/helm/prism` (`docrag.enabled`, on by default) | the subchart alone: `deploy/helm/prism/charts/docrag` with `ingress.enabled=true` |
| Vector store | its own Qdrant (bundled; or `qdrant.enabled=false` + `qdrant.url` for a managed one) | same |
| Front door | gateway `/docrag/*`; the gateway injects DocRAG's key | callers send `X-API-Key` directly |
| Authorization | per user: the `upload_remove_documents` operation, from the live Access matrix | the API key only — every key holder can use every tenant it names |
| TLS / CORS | the edge / gateway | your proxy or ingress; `DOCRAG_CORS_ORIGINS` for a UI on another origin |

```bash
# On its own, Compose (one VM, no other PRISM service needed)
DOCRAG_API_KEYS=$(openssl rand -hex 24) DOCRAG_QDRANT_API_KEY=$(openssl rand -hex 24) \
SARVAM_API_KEY=... docker compose -f deploy/compose/docker-compose.docrag.yml up -d --build

# On its own, Kubernetes
helm upgrade --install docrag deploy/helm/prism/charts/docrag -n docrag --create-namespace \
  --set fullnameOverride=docrag \
  --set config.apiKeys=<key> \
  --set qdrant.apiKey.value=<another-key> \
  --set sarvam.apiKey.existingSecret=<secret-with-sarvam-api-key> \
  --set ingress.enabled=true --set 'ingress.hosts[0].host=docrag.example.com' \
  --set 'ingress.hosts[0].paths[0].path=/' --set 'ingress.hosts[0].paths[0].pathType=Prefix'
```

Things to know when running it separately:

- **Issue one key per consuming system** (comma-separated in `DOCRAG_API_KEYS`) so one can be
  rotated without the others. The key is the whole authorization boundary in this mode.
- **Tenancy is the caller's claim** (`X-Tenant`). Behind the gateway the caller is resolved
  as a user OF that tenant (unknown → 403); standalone, a key holder can name any tenant.
- **Back up the DocRAG volume** (`docragdata` / the `-data` PVC): the originals, knowledge
  and chunks. Inside the PRISM Compose stack the `backup` profile already covers it (nightly
  tar + 60 s S3 mirror); standalone or on Kubernetes, snapshot the volume yourself (EBS
  snapshots / Velero). The Qdrant volume need not be backed up — it is rebuilt.
- **One replica.** The document registry and ingestion queue live in the process; scale
  up (CPU/memory), not out. Qdrant is not the constraint.
- To move from standalone to inside PRISM, point the gateway at it (`GATEWAY_DOCRAG_URL`,
  `GATEWAY_DOCRAG_API_KEY` = one of its keys); the API and data are unchanged.

## Pipeline

```
PDF ─▶ OpenDataLoader (typed headings/paragraphs/tables, bboxes; Java)
       + PyMuPDF find_tables() recovers borderless label/value grids ODL flattens
       └─ ODL missing/failed → PyMuPDF-only fallback, with a warning
   ─▶ per-page difficulty score (too few chars / garbled)
       └─ difficult pages → Sarvam Doc AI (one async job for the file), replacing only
          those pages; no key / failure → local text kept, with a warning
XLSX ─▶ one table per sheet
   ─▶ reconstruct: section tree by heading level, tables kept structured,
      regex entities (PAN, GSTIN, INR amounts, dates)
   ─▶ doc-type classification (keyword rules)
   ─▶ chunks: full section path, pages, bbox, engine; a table is never merged with prose
      and is split into header-repeating row groups that fit the embedder's window
   ─▶ coverage check: source words that reached no chunk become a warning
```

The **KnowledgeDocument is canonical**; chunks and vectors are a derived, disposable view.

## Retrieval (Qdrant)

- **Dense**: FastEmbed `BAAI/bge-small-en-v1.5` (384-d, cosine), separate passage/query
  encoding.
- **Sparse**: FastEmbed `Qdrant/bm25` with Qdrant's IDF weighting — catches exact strings
  embeddings miss: PANs, account numbers, rupee figures.
- **Reciprocal rank fusion** of the two ranked lists, plus a small boost when a chunk's
  tagged entities appear in the query. Fusion runs in DocRAG so every result keeps its rank
  in both lists (`vector_rank`, `bm25_rank` in the API).
- **One collection, tenant-partitioned**: a keyword payload index on `tenant` with
  `is_tenant` (Qdrant's multi-tenancy layout) plus one on `doc_id`; every search, count
  and delete is filtered by tenant. Point ids derive from (tenant, document, chunk), so
  re-indexing overwrites instead of duplicating.
- **Rebuildable**: at startup every ready document is reconciled — if its points are
  missing (new or lost Qdrant volume) it is re-embedded from `chunks.json`.
- **Model changes are explicit**: both models are pinned by commit and baked at exactly
  that commit (an upstream update can neither change nor break the build); the collection
  name carries the model identity, so a pin bump lands in a new collection rebuilt from
  the chunks — never mixed vectors.
- **Extractive** answers (default) show the cited passages; **generative** answers
  (`DOCRAG_SARVAM_API_KEY` set) ask Sarvam chat to answer only from those passages and
  cite them. With no key, generative returns 409 rather than a fabricated answer.

## Why it is built this way

- **No silent failures.** Every fallback (no Sarvam key, Sarvam error, ODL missing, table
  finder throwing, unextracted images) ships a warning on the document. A scanned page
  with no key is reported, never an empty success.
- **Asynchronous ingestion.** OCR through Doc AI polls for minutes; a synchronous upload
  would outlive every proxy timeout on the path. Upload returns at once; poll the status.
- **Documents on a volume, vectors in Qdrant.** `{tenant}/{doc_id}/` holds the original,
  the knowledge document and the chunks, written atomically; work interrupted by a restart
  is requeued. "Ready" is set only after the chunks are in Qdrant — ready means searchable.
- **Its own Qdrant.** Not shared with other modules: DocRAG's documents (KYC, credit) never
  sit in another service's store, and each can be sized, secured and upgraded alone.
- **Models baked into the image** (~65 MB ONNX, no torch), served with `HF_HUB_OFFLINE=1` —
  no runtime dependency on huggingface.co (the same rule as STT).

## Configuration (`DOCRAG_*`)

| Variable | Default | Notes |
| --- | --- | --- |
| `API_KEYS` | `""` | Front-door keys (comma-separated). Set in every non-dev deployment. |
| `DEFAULT_TENANT` | `EVAM` | Used when no `X-Tenant` is sent. |
| `CORS_ORIGINS` | `""` | Browser origins allowed cross-origin (standalone only; behind the gateway it owns CORS). |
| `DATA_DIR` | `/data/docrag` | The volume (documents — the source of truth). |
| `QDRANT_URL` | `http://docrag-qdrant:6333` | `:memory:` = in-process Qdrant (tests, quick local runs). |
| `QDRANT_API_KEY` | `""` | Must equal the Qdrant server's `QDRANT__SERVICE__API_KEY`. |
| `QDRANT_COLLECTION_PREFIX` | `docrag` | Collection = `<prefix>_chunks_<model-identity-hash>`. |
| `MAX_UPLOAD_BYTES` | 50 MB | |
| `INGEST_WORKERS` | `2` | Background ingestion threads. |
| `SARVAM_API_KEY` | `""` | Enables Doc AI OCR and generative answers. |
| `SARVAM_BASE_URL`, `SARVAM_CHAT_MODEL`, `SARVAM_CHAT_PATH`, `SARVAM_DOCAI_*` | see `app/config.py` | Sarvam's surface has shifted; every value is overridable. |
| `USE_ODL` | `true` | `false` forces the PyMuPDF-only path. |
| `ODL_BIN`, `ODL_JAVA_HOME` | `""` | Default: `opendataloader-pdf` and `java` on PATH (as in the image). |
| `EMBEDDER` | `fastembed` | `stub` = hashed vectors, for tests. |
| `DENSE_MODEL`, `DENSE_MODEL_REVISION`, `SPARSE_MODEL`, `SPARSE_MODEL_REVISION` | bge-small / bm25, pinned | Baked at build; changing them needs a rebuild (and re-indexes). |
| `MODEL_DIR`, `MODEL_OFFLINE` | `/opt/models`, `true` in the image | Serve only the baked models. |
| `DEV_UI` | `false` | Browser console at `/v1/dev-ui`. |

Chunking and retrieval knobs (`MAX_CHUNK_CHARS`, `MAX_TABLE_CHUNK_CHARS`, `TOP_K_*`,
`MIN_CHARS_PER_PAGE`, `MAX_GARBLED_RATIO`) are also settings.

## Develop

```bash
pip install -e packages/evam-backend-core -e "services/docrag[dev]"
cd services/docrag && python -m pytest -q   # stub embedder, in-process Qdrant: no models, no Java
# with the real engines (FastEmbed downloads the pinned models on first use):
pip install -e "services/docrag[odl]"          # + a JDK on PATH for OpenDataLoader
DOCRAG_DATA_DIR=/tmp/docrag DOCRAG_QDRANT_URL=:memory: DOCRAG_DEV_UI=true \
  uvicorn app.main:app --port 8010             # or point DOCRAG_QDRANT_URL at a Qdrant
```

## Known limits / next steps

- **One API replica**: the document registry and ingestion queue are in-process. Moving
  them to a shared store (e.g. the platform Postgres) would allow horizontal scaling.
- **Record scope**: the gateway checks the `upload_remove_documents` grant, but documents
  are not yet tied to Register records or to the uploader's book — a SCOPED user sees the
  tenant's whole DocRAG library. Linking documents to entities/deals is the natural next step.
- **Entities are regex-based** (PAN, GSTIN, INR, dates), not NER.
- **Doc-type classification can tie** between related types (term sheet vs sanction letter).
- **No cross-encoder reranker**; RRF is the fusion step. FastEmbed ships one
  (`ms-marco-MiniLM-L-6-v2`) if precision on ambiguous queries needs it.
