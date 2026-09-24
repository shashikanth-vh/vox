# PRISM Chitti — conversational analytics

Chitti answers questions about the PRISM ledger through an OpenAI-compatible chat API.
It reads caller-visible records from Register, applies validated operations, and returns
answers with supporting evidence. It covers leads, commercial deals, lending,
syndication, lender submissions, and asset monetisation.

For the system design, request flow, security boundaries, and operating limits, see
the [Chitti technical architecture](../../docs/chitti/ARCHITECTURE.md).

## Why it is built this way

- **Register owns the facts and access rules.** Chitti has no ledger database and cannot
  create, update, approve, or delete records. Every data read carries a caller identity.
- **Business definitions are versioned.** Qdrant stores embeddings of the checked-in
  ontology, not ledger records. Dense and sparse retrieval plus reranking select the
  definitions used to ground a question.
- **Models interpret; Python calculates.** Typed stages resolve the conversation,
  interpret and ground the question, check answerability, plan reads, execute bounded
  operations, and construct evidence before generating an answer.
- **Missing evidence stays visible.** Answers carry completeness caveats. Qualitative
  analysis uses cited lending remarks or lender conversation snapshots. Ambiguous
  requests are intended to ask for a business clarification.

## API

| Endpoint | What it does |
| --- | --- |
| `GET /v1/models` | Lists the public chat model. |
| `POST /v1/chat/completions` | Accepts chat messages; returns JSON or an SSE stream. |
| `GET /healthz` | Process liveness. |
| `GET /readyz` | Pipeline readiness, model configuration, and ontology index identity. |

Chat endpoints accept `Authorization: Bearer <chitti-key>` or `X-API-Key`.
Delegated requests also carry a signed `X-Internal-Context`. Chitti verifies the
HTTP method, path, and tenant, then preserves the caller's permissions and policy
metadata when binding each Register `GET`. The `svc_chitti` credential has no
independent Register read or write grants.

For access through Gateway, configure its [Chitti upstream](../gateway/README.md#chitti).
The browser/API client sends its bearer token to `/chitti/v1/models` or
`/chitti/v1/chat/completions`; Gateway resolves the user through Access and injects
the Chitti key and signed context. Gateway and Chitti use matching signing material.

## Configuration

Settings are defined in [app/config.py](app/config.py). The Chitti Compose project
requires signed delegation and uses `CHITTI_GATEWAY_KEY` for its inbound key.

| Variable | Compose value / purpose |
| --- | --- |
| `CHITTI_API_KEYS` | Supplied through `CHITTI_GATEWAY_KEY` |
| `CHITTI_PUBLIC_MODEL_ID` | `prism-chitti` |
| `CHITTI_REGISTER_BASE_URL` | `http://<services-instance-private-ip>:8000` |
| `CHITTI_REGISTER_API_KEY` | Register credential for `svc_chitti`, supplied through `SVC_CHITTI_KEY` |
| `CHITTI_REGISTER_TENANT` | `EVAM` |
| `CHITTI_INTERNAL_SIGNING_SECRET` | Must match Register; supplied through `INTERNAL_SIGNING_SECRET` |
| `CHITTI_LLM_API_KEY` | Required credential for the configured model provider |
| `CHITTI_LLM_BASE_URL` | Bedrock's OpenAI-compatible endpoint in `ap-south-1` |
| `CHITTI_*_MODEL` | Per-stage model selection; Compose defaults to `zai.glm-5` |
| `CHITTI_QDRANT_URL` | `http://qdrant:6333` |
| `CHITTI_MODEL_CACHE_DIR` | `/models`; pinned embedding and reranking artifacts |
| `CHITTI_MAX_PAGES_PER_RESOURCE` | `10`; bounds traversal |
| `CHITTI_MAX_RECORDS_PER_REQUEST` | `2000`; bounds returned records |
| `CHITTI_REQUEST_TIMEOUT_SECONDS` | `240` |

See [docker-compose.chitti.yml](../../deploy/compose/docker-compose.chitti.yml) for
model revisions, retrieval limits, and runtime configuration.

## Run it

Chitti and the services stack run as separate Compose projects on one or two
machines. Configure protected `deploy/compose/.env.chitti` from
[.env.chitti.example](../../deploy/compose/.env.chitti.example), then follow the
[production guide](../../docs/PRODUCTION_DEPLOYMENT.md#chitti) to configure the
services project and matching credentials.

Start Chitti from the repository root:

```bash
docker compose -p chitti --env-file deploy/compose/.env.chitti \
  -f deploy/compose/docker-compose.chitti.yml up -d --build
```

Compose prepares the pinned model cache and ontology index before starting Chitti.
The Chitti host needs access to model downloads, Bedrock, and Register's private
endpoint. For both projects on one machine, use its private/LAN address in both
environment files. Open the PRISM website, sign in, and select **Chitti**. The browser
sends its bearer token through Gateway; Chitti delegates the caller's Register reads.

Chat displays business-facing prose, evidence references, and a support request ID.
Public SSE status events carry the pipeline's progress descriptions unchanged.
Gateway selects the public response format: execution metadata, database identifiers,
and the internal result table are excluded. Schema-style names, code blocks, or
tables in generated prose cause that answer to be withheld with a retry message.
Validated record results also supply `chitti.tables`: allowlisted business column
labels, display values, row counts, coverage, and deal group codes for navigation.
These tables preserve the result rows rather than the prose model's five-row sample.
The response budget bounds table data; the UI labels any omitted rows explicitly.
The browser keeps one conversation in this tab's sessionStorage for the current
login. Navigation and refresh restore it; New conversation, sign-out, and
user/tenant changes clear it.

### Refresh the ontology

For a prepared model cache and running Qdrant, run on the Chitti host:

```bash
docker compose -p chitti --env-file deploy/compose/.env.chitti \
  -f deploy/compose/docker-compose.chitti.yml stop chitti
docker compose -p chitti --env-file deploy/compose/.env.chitti \
  -f deploy/compose/docker-compose.chitti.yml build chitti
docker compose -p chitti --env-file deploy/compose/.env.chitti \
  -f deploy/compose/docker-compose.chitti.yml run --rm --no-deps chitti-semantic-index
docker compose -p chitti --env-file deploy/compose/.env.chitti \
  -f deploy/compose/docker-compose.chitti.yml up -d --no-deps --wait chitti
```

The index job replaces Chitti's ontology collection. Keep the Qdrant and model-cache
volumes; ledger imports do not require re-embedding. Changed model revisions require
rerunning `chitti-model-preload` before indexing. Do not use `down --volumes` for a
refresh: it removes the prepared model cache and ontology index.

`python -m app.model_preload --reuse-existing` verifies an existing cache offline.
`python -m app.semantic_index --reuse-existing` verifies an existing collection without
replacing it. A mismatched cache or index fails verification. The Helm deployment
uses these options with a separate cache and collection prefix for each artifact
identifier; retained identifiers support rollback.

## Storage

| Volume | Contents |
| --- | --- |
| `chitti_model_cache` | Pinned embedding and reranking artifacts, with a verification manifest. |
| `chitti_qdrant_data` | The versioned ontology index. |

The preload job downloads model artifacts; serving reads the prepared cache offline.
The external model provider receives questions and selected ledger/evidence content
for interpretation and answer generation. Register remains the source of ledger data.

## Tests

Use a repository-root Python 3.12 virtual environment:

```bash
.venv/bin/pip install -e packages/evam-backend-core -e packages/evam-register-client \
  -e './services/chitti[dev]'
.venv/bin/pytest services/chitti/tests
.venv/bin/ruff check services/chitti/app services/chitti/tests services/chitti/tools
.venv/bin/mypy services/chitti/app
```

The service suite uses controlled model and Register fixtures. Run each service suite
separately because the services share the Python package name `app`.

## Extending it

- **Business definitions** → [ontology passages](app/ontology/passages.json), including
  their source claims; rebuild the index after changes.
- **Register resources and filters** → [register_access.py](app/register_access.py).
  Keep the contract aligned with Register and preserve bounded-read completeness.
- **Model contracts and planning** → [stage_models.py](app/stage_models.py) and
  [model_stages.py](app/model_stages.py).
- **Deterministic calculations** → [operations.py](app/operations.py) and
  [executor.py](app/executor.py).
- **Qualitative evidence** → [qualitative.py](app/qualitative.py). New source fields
  need explicit ontology support, citation validation, and missing-evidence coverage.

Stage failures include a request ID. Use it to trace `pipeline_stage_failed` and
`pipeline_model_output_invalid` in the Chitti container logs.
